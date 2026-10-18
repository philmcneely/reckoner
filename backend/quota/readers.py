"""Server-side plan-usage readers (run by Reckoner's scheduler).

Each reader asks the vendor's own usage/quota API once and returns a QuotaReading. They never
raise: any failure becomes ok=False with a short error that never contains the key.

Verified 2026-10-18 against the live APIs:
  Z.ai        GET https://api.z.ai/api/monitor/usage/quota/limit          (Bearer key)
              data.limits[]: TOKENS_LIMIT unit 3 / number 5 = 5-hour, unit 6 / number 1 = weekly,
              TIME_LIMIT = MCP tools allowance; percentage, nextResetTime (ms); data.level = plan.
  OpenCode Go GET https://opencode.ai/zen/go/v1/usage                     (Bearer key; a non-default
              User-Agent is required — Cloudflare answers 1010 to python's)
              usage.{rolling,weekly,monthly}.{percent, resetsAt}.
  NanoGPT     GET https://nano-gpt.com/api/subscription/v1/usage          (x-api-key) — the same
              endpoint the opencode-quota plugin shows in opencode's sidebar.
              limits.weeklyInputTokens, weeklyInputTokens.{used,remaining,resetAt}, dailyImages.
  pgsgrove    GET https://api.pgsgrove.com/v1/usage                       (Bearer key, curl UA)
              weekly_used_percent, daily_used_percent, weekly_resets_at. Banked full-speed time:
              at the cap it slows down, it does not stop.
  Ollama      GET https://ollama.com/api/usage                            (Bearer usage key) — since
              2026-10-17 it answers request counts only ({range, totals.request_count, buckets});
              the plan limits are no longer published.
For the two vendors with no usage or limits API (Mistral's education key reports per-minute rate
limits only; Google publishes no per-key usage for an AI Studio key), the reading is our own
proxies' calls from Switchboard GET /api/backend-usage, labelled source_kind="our_logs".
"""

import os
from datetime import datetime, timezone
from typing import Optional

import httpx

from quota.models import QuotaReading, QuotaWindow

UA = "reckoner/1.0 (+https://github.com/philmcneely/reckoner)"
MAX_BYTES = 256 * 1024


def _now():
    return datetime.now(timezone.utc)


def _ts(v) -> Optional[datetime]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        if isinstance(v, (int, float)):
            secs = float(v) / 1000.0 if v > 1e11 else float(v)
            if not (0 <= secs <= 4102444800):
                return None
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        if isinstance(v, str) and v:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _fail(vid, name, source, kind, error, key=None, notes=None) -> QuotaReading:
    msg = str(error)
    if key:
        msg = msg.replace(key, "***")
    return QuotaReading(id=vid, name=name, source=source, source_kind=kind, ok=False, error=msg[:300],
                        fetched_at=_now(), notes=notes)


async def _get_json(client: httpx.AsyncClient, url, headers):
    r = await client.get(url, headers={"Accept": "application/json", "User-Agent": UA, **headers})
    if r.status_code == 429:
        raise RuntimeError("HTTP 429 (vendor rate-limited this read)")
    if r.status_code in (401, 403):
        raise RuntimeError(f"HTTP {r.status_code} (key rejected)")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    if len(r.content) > MAX_BYTES:
        raise RuntimeError("answer over size cap")
    return r.json()


async def read_zai(client, key) -> QuotaReading:
    vid, name, src = "zai", "Z.ai (GLM Coding)", "z.ai quota API"
    try:
        d = await _get_json(client, "https://api.z.ai/api/monitor/usage/quota/limit", {"Authorization": f"Bearer {key}"})
        if not isinstance(d, dict) or d.get("success") is False or d.get("code") not in (None, 0, 200):
            raise RuntimeError(f"error payload code={d.get('code') if isinstance(d, dict) else '?'}")
        data = d.get("data") or {}
        windows = []
        for lim in data.get("limits") or []:
            if not isinstance(lim, dict) or _num(lim.get("percentage")) is None:
                continue
            unit, number = lim.get("unit"), lim.get("number")
            if lim.get("type") == "TIME_LIMIT":
                wname = "mcp-tools"
            elif unit == 3:
                wname = f"{number}h"
            elif unit == 6:
                wname = "weekly" if number == 1 else f"{number}w"
            else:
                wname = f"unit{unit}x{number}"
            windows.append(QuotaWindow(name=wname, used_pct=_num(lim.get("percentage")), resets_at=_ts(lim.get("nextResetTime"))))
        if not windows:
            raise RuntimeError("no limits in answer")
        return QuotaReading(id=vid, name=name, source=src, source_kind="api", plan=data.get("level"),
                            fetched_at=_now(), windows=windows, stale_after_s=900)
    except Exception as e:  # noqa: BLE001
        return _fail(vid, name, src, "api", e, key)


async def read_opencode_go(client, key) -> QuotaReading:
    vid, name, src = "opencode_go", "OpenCode Go", "opencode.ai usage API"
    try:
        d = await _get_json(client, "https://opencode.ai/zen/go/v1/usage", {"Authorization": f"Bearer {key}"})
        usage = d.get("usage") if isinstance(d, dict) else None
        if not isinstance(usage, dict):
            raise RuntimeError("no usage object")
        windows = []
        for k, wname in (("rolling", "rolling-5h"), ("weekly", "weekly"), ("monthly", "monthly")):
            w = usage.get(k)
            if isinstance(w, dict) and _num(w.get("percent")) is not None:
                windows.append(QuotaWindow(name=wname, used_pct=_num(w.get("percent")), resets_at=_ts(w.get("resetsAt"))))
        if not windows:
            raise RuntimeError("no usage windows")
        return QuotaReading(id=vid, name=name, source=src, source_kind="api", plan="Go", fetched_at=_now(),
                            windows=windows, stale_after_s=900)
    except Exception as e:  # noqa: BLE001
        return _fail(vid, name, src, "api", e, key)


async def read_nanogpt(client, key) -> QuotaReading:
    vid, name, src = "nanogpt", "NanoGPT", "nano-gpt.com subscription usage API"
    try:
        d = await _get_json(client, "https://nano-gpt.com/api/subscription/v1/usage", {"x-api-key": key})
        if not isinstance(d, dict) or d.get("unauthenticated"):
            raise RuntimeError("key rejected (unauthenticated)")
        limits = d.get("limits") or {}
        windows = []
        for field, wname, unit in (("weeklyInputTokens", "weekly-input-tokens", "tokens"),
                                   ("dailyInputTokens", "daily-input-tokens", "tokens"),
                                   ("dailyImages", "daily-images", "images")):
            w = d.get(field)
            if not isinstance(w, dict):
                continue
            used, limit = _num(w.get("used")), _num(limits.get(field))
            if limit is None and used is not None and _num(w.get("remaining")) is not None:
                limit = used + float(w["remaining"])
            pct = round(used / limit * 100.0, 3) if used is not None and limit else None
            windows.append(QuotaWindow(name=wname, used_pct=pct, used=used, limit=limit, unit=unit,
                                       resets_at=_ts(w.get("resetAt"))))
        plan = "subscription (active)" if d.get("active") else "subscription (inactive)"
        return QuotaReading(id=vid, name=name, source=src, source_kind="api", plan=plan, fetched_at=_now(),
                            windows=windows, stale_after_s=1800,
                            extra={"period_end": (d.get("period") or {}).get("currentPeriodEnd"),
                                   "overage_allowed": bool(d.get("allowOverage"))})
    except Exception as e:  # noqa: BLE001
        return _fail(vid, name, src, "api", e, key)


async def read_pgsgrove(client, key) -> QuotaReading:
    vid, name, src = "pgsgrove", "PGSGrove", "pgsgrove usage API"
    try:
        r = await client.get("https://api.pgsgrove.com/v1/usage",
                             headers={"Authorization": f"Bearer {key}", "User-Agent": "curl/8.4.0", "Accept": "application/json"})
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        d = r.json()
        windows = []
        if _num(d.get("daily_used_percent")) is not None:
            windows.append(QuotaWindow(name="daily", used_pct=_num(d.get("daily_used_percent"))))
        if _num(d.get("weekly_used_percent")) is not None:
            windows.append(QuotaWindow(name="weekly", used_pct=_num(d.get("weekly_used_percent")),
                                       resets_at=_ts(d.get("weekly_resets_at"))))
        return QuotaReading(id=vid, name=name, source=src, source_kind="api", plan="flat monthly (banked time)",
                            fetched_at=_now(), windows=windows, stale_after_s=1800,
                            extra={"api_share_of_weekly_percent": d.get("api_share_of_weekly_percent")},
                            notes="Banked full-speed time: at the cap it slows down, it does not stop.")
    except Exception as e:  # noqa: BLE001
        return _fail(vid, name, src, "api", e, key)


async def read_ollama(client, key) -> QuotaReading:
    vid, name, src = "ollama_cloud", "Ollama Cloud", "ollama.com usage API"
    note = ("Since 2026-10-17 Ollama's usage API reports request counts only; plan limits are not "
            "published. The free models (gpt-oss, gemma4, minimax-m3, nemotron) are unmetered.")
    try:
        d = await _get_json(client, "https://ollama.com/api/usage", {"Authorization": f"Bearer {key}"})
        totals = d.get("totals") if isinstance(d, dict) else None
        extra = {"range": d.get("range") if isinstance(d, dict) else None,
                 "request_count_week": (totals or {}).get("request_count")}
        limits = d.get("limits") if isinstance(d, dict) else None
        windows = []
        if isinstance(limits, dict):  # the pre-2026-10-17 shape, should it come back
            for k in ("session", "weekly"):
                frac = _num((limits.get(k) or {}).get("usage"))
                if frac is not None:
                    windows.append(QuotaWindow(name=k, used_pct=round(frac * 100.0, 2)))
        return QuotaReading(id=vid, name=name, source=src, source_kind="api", fetched_at=_now(), windows=windows,
                            extra=extra, notes=note, stale_after_s=3600)
    except Exception as e:  # noqa: BLE001
        return _fail(vid, name, src, "api", e, key, notes=note)


async def _our_logs(client, vid, name, backend, notes) -> QuotaReading:
    src = "our proxies' logs (Switchboard)"
    base = os.environ.get("SWITCHBOARD_URL", "").strip().rstrip("/")
    try:
        if not base:
            raise RuntimeError("SWITCHBOARD_URL is not set")
        r = await client.get(f"{base}/api/backend-usage", params={"backend": backend, "hours": 168},
                             headers={"User-Agent": UA, "Accept": "application/json"})
        if r.status_code != 200:
            raise RuntimeError(f"Switchboard HTTP {r.status_code}")
        d = r.json()
        return QuotaReading(id=vid, name=name, source=src, source_kind="our_logs", fetched_at=_now(), windows=[],
                            extra={"calls_7d": d.get("calls"), "ok_7d": d.get("ok"),
                                   "input_tokens_7d": d.get("input_tokens"), "output_tokens_7d": d.get("output_tokens"),
                                   "last_call": d.get("last_ts"), "by_machine": d.get("by_machine")},
                            notes=notes, stale_after_s=3600)
    except Exception as e:  # noqa: BLE001
        return _fail(vid, name, src, "our_logs", e, notes=notes)


async def read_mistral(client) -> QuotaReading:
    return await _our_logs(client, "mistral", "Mistral (education key)", "mistral",
                           "Mistral publishes no usage or plan-limit API for this key; its responses carry "
                           "per-minute rate limits only (1,000 requests and 500K tokens a minute, checked "
                           "2026-10-18). Counts are our own proxies' calls.")


async def read_gemini_api(client) -> QuotaReading:
    return await _our_logs(client, "gemini_api", "Gemini API (AI Studio key)", "ai_studio",
                           "Google has no usage API for an AI Studio key. Counts are our own proxies' calls. "
                           "The Antigravity plan quota is read separately, from its own CLI.")


SERVER_READERS = {
    # id: (reader, env var holding the key or None, interval seconds)
    "zai": (read_zai, "ZAI_API_KEY", 180),
    "opencode_go": (read_opencode_go, "OPENCODE_GO_API_KEY", 180),
    "nanogpt": (read_nanogpt, "NANOGPT_API_KEY", 300),
    "pgsgrove": (read_pgsgrove, "PGSGROVE_API_KEY", 300),
    "ollama_cloud": (read_ollama, "OLLAMA_CLOUD_API_KEY", 900),
    "mistral": (read_mistral, None, 600),
    "gemini_api": (read_gemini_api, None, 600),
}
