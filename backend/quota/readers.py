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
  Synthetic   GET https://api.synthetic.new/v2/quotas                     (Bearer key; /quotas reads do not
              count against the limits). Read live 2026-10-19: rollingFiveHourLimit.{max,remaining,
              nextTickAt,limited} = the 5-hour request allowance; weeklyTokenLimit.{percentRemaining,
              maxCredits,remainingCredits ("$60.00" strings),nextRegenAt} = weekly credits, which regenerate
              continuously (nextRegenAt is the next tick, not a reset); subscription / search.hourly /
              freeToolCalls.{limit,requests,renewsAt} = request counters (limit 0 = not on this plan).
  Command Code GET https://api.commandcode.ai/alpha/billing/credits         (Bearer account key; the three GETs
              /alpha/billing/subscriptions and /alpha/usage/summary?since=<period start> are what the CLI's own
              /usage runs). Read live 2026-10-20: credits.{monthlyCredits (left), purchasedCredits, freeCredits},
              windowLimits.{fiveHour,weekly}.{used,cap (USD),exceeded,resetAt (ms; 0 = window not started)},
              subscription planId + currentPeriodEnd, summary.totalMonthlyCredits (spent this billing period).
  Ollama      GET https://ollama.com/api/usage?range=24h|7d (Bearer usage key) — since 2026-10-17 request
              counts only ({range, granularity hour|day, totals.request_count, buckets[]}); POST /api/me gives
              the plan. Caps are not published: session/weekly windows are counts against configured caps, and
              Switchboard's /api/backend-limit-events (our proxies' 429s) pins the session at 100% (see read_ollama).
Google publishes no per-key usage for an AI Studio key, so the Gemini API reading is our own proxies'
calls from Switchboard GET /api/backend-usage, labelled source_kind="our_logs".
Mistral's education key has a $15/month allowance (pay-as-you-go off: a hard stop) and no usage API. Its
reading is one monthly dollar window: the larger of the price-book cost of our proxies' calls this month
(backend-usage `cost_usd`, since the 1st) and the latest admin-console figure for the same month, which a
human records as vendor `mistral_console` through /api/quota/ingest (see README).
"""

import math
import os
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from quota import store
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


def _usd(v) -> Optional[float]:
    """"$56.70" -> 56.7; None for anything that is not a plain non-negative dollar amount."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        out = float(v)
    elif isinstance(v, str):
        try:
            out = float(v.strip().lstrip("$").replace(",", ""))
        except ValueError:
            return None
    else:
        return None
    return out if 0 <= out < 1e12 else None


def _count_window(name, d) -> Optional[QuotaWindow]:
    """A {limit, requests, renewsAt} counter; None when the plan has no such allowance (limit 0)."""
    if not isinstance(d, dict):
        return None
    limit, used = _num(d.get("limit")), _num(d.get("requests"))
    if not limit or limit < 0 or used is None or used < 0:
        return None
    return QuotaWindow(name=name, used_pct=round(used / limit * 100.0, 3), used=used, limit=limit, unit="requests",
                       resets_at=_ts(d.get("renewsAt")))


async def read_synthetic(client, key) -> QuotaReading:
    vid, name, src = "synthetic", "Synthetic", "synthetic.new quotas API"
    try:
        d = await _get_json(client, "https://api.synthetic.new/v2/quotas", {"Authorization": f"Bearer {key}"})
        if not isinstance(d, dict):
            raise RuntimeError("answer is not an object")
        windows, extra = [], {}
        five = d.get("rollingFiveHourLimit")
        if isinstance(five, dict):
            cap, left = _num(five.get("max")), _num(five.get("remaining"))
            if cap and cap > 0 and left is not None:
                used = min(max(cap - left, 0.0), cap)
                pct = 100.0 if five.get("limited") is True else round(used / cap * 100.0, 3)
                windows.append(QuotaWindow(name="5h", used_pct=pct, used=used, limit=cap, unit="requests",
                                           resets_at=_ts(five.get("nextTickAt"))))
                extra["limited"] = five.get("limited") is True
                extra["tick_percent"] = _num(five.get("tickPercent"))
        week = d.get("weeklyTokenLimit")
        if isinstance(week, dict):
            left_pct = _num(week.get("percentRemaining"))
            cap, left = _usd(week.get("maxCredits")), _usd(week.get("remainingCredits"))
            pct = round(min(max(100.0 - left_pct, 0.0), 100.0), 3) if left_pct is not None else (
                round(min(max((cap - left) / cap * 100.0, 0.0), 100.0), 3) if cap and left is not None else None)
            if pct is not None:
                have = cap is not None and left is not None
                windows.append(QuotaWindow(name="weekly", used_pct=pct, used=round(max(cap - left, 0.0), 4) if have else None,
                                           limit=cap if have else None, unit="USD" if have else None,
                                           resets_at=_ts(week.get("nextRegenAt"))))
                extra["weekly_regen_credits"] = _usd(week.get("nextRegenCredits"))
        for wname, node in (("subscription", d.get("subscription")),
                            ("search-hourly", (d.get("search") or {}).get("hourly") if isinstance(d.get("search"), dict) else None)):
            w = _count_window(wname, node)
            if w:
                windows.append(w)
        if not windows:
            raise RuntimeError("no quota windows in answer")
        return QuotaReading(id=vid, name=name, source=src, source_kind="api", fetched_at=_now(), windows=windows,
                            extra={k: v for k, v in extra.items() if v is not None}, stale_after_s=900,
                            notes="Weekly credits regenerate continuously; the weekly time is the next regeneration "
                                  "tick, not a reset. Reading /v2/quotas does not count against the limits.")
    except Exception as e:  # noqa: BLE001
        return _fail(vid, name, src, "api", e, key)


def _ms_ts(v) -> Optional[datetime]:
    """Epoch-ms reset time; 0 / missing means the window has not started."""
    return _ts(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


async def read_commandcode(client, key) -> QuotaReading:
    """Command Code (the agent CLI, one account key on every box): 5-hour and weekly dollar caps and a
    monthly credit pool, from the same billing endpoints its CLI's /usage uses. The credits call is required;
    the subscription (plan, period) and summary (spent this period) calls only add the plan and the monthly
    window, so a failure of either leaves the other windows."""
    vid, name, src = "commandcode", "Command Code", "commandcode.ai billing API"
    base, hdr = "https://api.commandcode.ai", {"Authorization": f"Bearer {key}"}
    try:
        d = await _get_json(client, f"{base}/alpha/billing/credits", hdr)
        if not isinstance(d, dict):
            raise RuntimeError("credits answer is not an object")
        credits = d.get("credits") if isinstance(d.get("credits"), dict) else {}
        left = _num(credits.get("monthlyCredits"))
        windows, extra = [], {}
        caps = d.get("windowLimits") if isinstance(d.get("windowLimits"), dict) else {}
        if caps.get("limited") is not False:
            for field, wname in (("fiveHour", "5h"), ("weekly", "weekly")):
                w = caps.get(field)
                if not isinstance(w, dict):
                    continue
                cap, used = _num(w.get("cap")), _num(w.get("used"))
                if not cap or cap <= 0 or used is None or used < 0:
                    continue
                pct = 100.0 if w.get("exceeded") is True else round(used / cap * 100.0, 3)
                windows.append(QuotaWindow(name=wname, used_pct=pct, used=used, limit=cap, unit="USD",
                                           resets_at=_ms_ts(w.get("resetAt"))))
        plan, period_end, spent = None, None, None
        try:
            sub = await _get_json(client, f"{base}/alpha/billing/subscriptions", hdr)
            data = sub.get("data") if isinstance(sub, dict) and isinstance(sub.get("data"), dict) else {}
            plan = data.get("planId") if isinstance(data.get("planId"), str) else None
            period_end, since = _ts(data.get("currentPeriodEnd")), data.get("currentPeriodStart")
            q = urllib.parse.urlencode({"since": since}) if isinstance(since, str) and since else ""
            summary = await _get_json(client, f"{base}/alpha/usage/summary" + (f"?{q}" if q else ""), hdr)
            spent = _num(summary.get("totalMonthlyCredits")) if isinstance(summary, dict) else None
        except Exception as e:  # noqa: BLE001 — the plan and the monthly window are extras
            extra["monthly_unavailable"] = str(e).replace(key, "***")[:120]
        if spent is not None and left is not None and spent >= 0 and left >= 0 and spent + left > 0:
            pool = spent + left
            windows.append(QuotaWindow(name="monthly", used_pct=round(spent / pool * 100.0, 3), used=round(spent, 6),
                                       limit=round(pool, 6), unit="USD", resets_at=period_end))
        elif "monthly_unavailable" not in extra:
            extra["monthly_unavailable"] = "no monthly credit pool on this plan"
        money = {k: v for k, v in (("monthly_credits_left", left), ("purchased_credits", _num(credits.get("purchasedCredits"))),
                                   ("free_credits", _num(credits.get("freeCredits")))) if v is not None}
        return QuotaReading(id=vid, name=name, source=src, source_kind="api", plan=plan, fetched_at=_now(),
                            windows=windows, money=money, extra=extra, stale_after_s=900)
    except Exception as e:  # noqa: BLE001
        return _fail(vid, name, src, "api", e, key)


# Ollama Cloud (read live 2026-10-21). GET /api/usage answers request counts only: ?range=24h gives hourly
# buckets, ?range=7d daily ones (anything else is a 400 "range must be 24h, 7d, or 30d"). Only completed
# requests are counted: the 429s of a capped window are not. POST /api/me names the plan ("Plan": "pro").
# No endpoint publishes the caps (api/account, limits, plan, billing, subscription, quota: all 404), and
# the cap is not a pure request count: the Pro 5-hour limit hit after ~590 completed requests on one
# window and after ~850 on another (heavier calls cost more). So the percentage is an estimate against
# a configured request cap, and the exact signal is the proxies' own 429s ("You reached your Pro 5-hour
# limit"), read from Switchboard's limit-events feed, which pins the session window at 100%.
OLLAMA_SESSION_HOURS = 5
OLLAMA_SESSION_CAP_DEFAULTS = {"pro": 600.0}   # lowest completed-request count seen before a Pro 5-hour 429 (2026-10-20)
OLLAMA_LIMITED_WITHIN_S = 600                  # a 429 this recent, with no success after it, means "limited now"
OLLAMA_REPROBE_S = 600                         # a limited window's reset time: when to look again, not a promise
_OLLAMA_CALL_URL = "https://ollama.com"


def _env_cap(name) -> Optional[float]:
    try:
        v = float(os.environ.get(name, "").strip())
    except ValueError:
        return None
    return v if math.isfinite(v) and v > 0 else None


def _count(v) -> Optional[int]:
    n = _num(v)
    return int(n) if n is not None and math.isfinite(n) and n >= 0 else None


def _buckets(doc):
    """[(from, count)] of the well-formed buckets of an Ollama usage answer."""
    out = []
    for b in (doc.get("buckets") if isinstance(doc, dict) else None) or []:
        if not isinstance(b, dict):
            continue
        start, n = _ts(b.get("from")), _count(b.get("request_count"))
        if start is not None and n is not None:
            out.append((start, n))
    return out


def _naive(v) -> Optional[datetime]:
    """A Switchboard row timestamp: naive, in the writing proxy's clock. Only ever compared with another one."""
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v).replace(tzinfo=None)   # an offset must not make one side aware
    except ValueError:
        return None


async def _ollama_limit_events(client) -> Optional[dict]:
    """Are our proxies being refused right now? Switchboard's feed of recent HTTP 429s on ollama :cloud
    models. None when it cannot be asked (not configured, older Switchboard, down): then nothing is said."""
    base = os.environ.get("SWITCHBOARD_URL", "").strip().rstrip("/")
    if not base:
        return None
    try:
        r = await client.get(f"{base}/api/backend-limit-events", params={"backend": "ollama", "cloud": "1"},
                             headers={"User-Agent": UA, "Accept": "application/json"})
        if r.status_code != 200 or len(r.content) > MAX_BYTES:
            return None
        ev = r.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(ev, dict):
        return None
    ref, last429, last_ok = _naive(ev.get("ref_ts")), _naive(ev.get("last_429_ts")), _naive(ev.get("last_ok_ts"))
    limited = bool(ref and last429 and 0 <= (ref - last429).total_seconds() <= OLLAMA_LIMITED_WITHIN_S
                   and (last_ok is None or last_ok < last429))
    return {"limited": limited, "last_429_age_s": int((ref - last429).total_seconds()) if ref and last429 else None,
            "n_429": _count(ev.get("n_429"))}


async def _ollama_plan(client, key) -> Optional[str]:
    try:
        r = await client.post(f"{_OLLAMA_CALL_URL}/api/me", headers={"Authorization": f"Bearer {key}", "User-Agent": UA,
                                                                  "Accept": "application/json"})
        if r.status_code != 200 or len(r.content) > MAX_BYTES:
            return None
        plan = r.json().get("Plan")
    except Exception:  # noqa: BLE001
        return None
    return plan.strip().lower()[:32] if isinstance(plan, str) and plan.strip() else None


async def read_ollama(client, key, now=None) -> QuotaReading:
    """Ollama Cloud: the 5-hour session and weekly windows, from request counts plus our proxies' 429s."""
    vid, name, src = "ollama_cloud", "Ollama Cloud", "ollama.com usage API"
    now = now or _now()
    note = ("Ollama publishes request counts only and no plan caps. Session = completed requests in the last "
            "5 hourly buckets against a configured cap (an estimate: heavier calls count more); a recent 429 "
            "from our proxies pins it at 100%. The free models (gpt-oss, gemma4, minimax-m3, nemotron) are "
            "unmetered.")
    hdr = {"Authorization": f"Bearer {key}"}
    usage_error = None
    hourly = daily = None
    try:
        hourly = await _get_json(client, "https://ollama.com/api/usage?range=24h", hdr)
        try:
            daily = await _get_json(client, "https://ollama.com/api/usage?range=7d", hdr)
        except Exception as e:  # noqa: BLE001 — the session window does not need the weekly answer
            usage_error = str(e).replace(key, "***")[:120]
    except Exception as e:  # noqa: BLE001
        usage_error = str(e).replace(key, "***")[:120]
    events = await _ollama_limit_events(client)
    limited = None if events is None else events["limited"]

    if hourly is None:
        if not limited:   # a read failure with no proxy signal is just a failure
            return _fail(vid, name, src, "api", usage_error or "read failed", key, notes=note)
        win = QuotaWindow(name="session", used_pct=100.0, resets_at=now + timedelta(seconds=OLLAMA_REPROBE_S))
        return QuotaReading(id=vid, name=name, source=src, source_kind="api", fetched_at=now, windows=[win],
                            extra={"limited": True, "limited_source": "429 in Switchboard usage_log",
                                   "limited_reset_estimated": True, "window_basis": "limit events only",
                                   "usage_error": usage_error, "n_429": events["n_429"]},
                            notes=note, stale_after_s=900)

    plan = await _ollama_plan(client, key)
    windows, extra = [], {"range": "24h+7d", "plan_endpoint": "POST /api/me", "limited": limited}

    since = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=OLLAMA_SESSION_HOURS - 1)
    sess = [(t, n) for t, n in _buckets(hourly) if t >= since]
    used = sum(n for _, n in sess)
    busy = [t for t, n in sess if n > 0]
    reset = (min(busy) + timedelta(hours=OLLAMA_SESSION_HOURS)) if busy else now + timedelta(hours=OLLAMA_SESSION_HOURS)
    cap = _env_cap("OLLAMA_SESSION_CAP_REQUESTS")
    cap_src = "config OLLAMA_SESSION_CAP_REQUESTS"
    if cap is None and plan in OLLAMA_SESSION_CAP_DEFAULTS:
        cap, cap_src = OLLAMA_SESSION_CAP_DEFAULTS[plan], f"config default for the {plan} plan (observed, not published)"
    pct = round(used / cap * 100.0, 2) if cap else None
    if limited:
        pct, reset = 100.0, now + timedelta(seconds=OLLAMA_REPROBE_S)
        extra.update(limited_source="429 in Switchboard usage_log", limited_reset_estimated=True,
                     n_429=events["n_429"])
    windows.append(QuotaWindow(name="session", used_pct=pct, used=float(used), limit=cap, unit="requests", resets_at=reset))
    if cap:
        extra["session_cap_source"] = cap_src
    extra["request_count_session"] = used

    if daily is not None:
        wk = _buckets(daily)
        totals = daily.get("totals") if isinstance(daily, dict) else None
        wused = _count((totals or {}).get("request_count")) if isinstance(totals, dict) else None
        if wused is None:
            wused = sum(n for _, n in wk)
        wbusy = [t for t, n in wk if n > 0 and t + timedelta(days=7) > now]  # a bucket already past 7 d frees nothing
        wcap = _env_cap("OLLAMA_WEEKLY_CAP_REQUESTS")
        wreset = (min(wbusy) + timedelta(days=7)) if wbusy else now + timedelta(days=7)
        windows.append(QuotaWindow(name="weekly", used_pct=round(wused / wcap * 100.0, 2) if wcap else None,
                                   used=float(wused), limit=wcap, unit="requests", resets_at=wreset))
        extra["request_count_week"] = wused
        if wcap:
            extra["weekly_cap_source"] = "config OLLAMA_WEEKLY_CAP_REQUESTS"
    else:
        extra["usage_error"] = usage_error
    return QuotaReading(id=vid, name=name, source=src, source_kind="api", plan=plan, fetched_at=now, windows=windows,
                        extra=extra, notes=note, stale_after_s=900)


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


MISTRAL_DEFAULT_MONTHLY_USD = 15.0
MISTRAL_CONSOLE_ID = "mistral_console"
_UNSET = object()


def _mistral_monthly_usd() -> float:
    try:
        v = float(os.environ.get("MISTRAL_MONTHLY_USD", "").strip())
    except ValueError:
        return MISTRAL_DEFAULT_MONTHLY_USD
    return v if math.isfinite(v) and 0 < v <= 100000 else MISTRAL_DEFAULT_MONTHLY_USD


def _month_bounds(now: datetime):
    """(first instant of this calendar month, first instant of the next), UTC."""
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    nxt = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=timezone.utc)
    return start, nxt


def _console_month_usd(doc, start: datetime, reset: datetime):
    """(used USD, read time) from a mistral_console reading, only when it was read in this calendar month.
    The read time is fetched_at, or, for a reading posted without one, when it was stored. The window is the
    one named like "month" (month, monthly, month-to-date), else the only USD window; the amount may be a
    number or a "$1.18" string."""
    if not isinstance(doc, dict) or doc.get("ok") is False:
        return None, None
    at = _ts(doc.get("fetched_at")) or _ts(doc.get("stored_at"))
    if at is None or not (start <= at < reset):
        return None, None
    wins = [w for w in (doc.get("windows") or []) if isinstance(w, dict)]
    pick = next((w for w in wins if "month" in str(w.get("name", "")).lower()), None)
    if pick is None:
        usd = [w for w in wins if str(w.get("unit", "")).upper() == "USD"]
        pick = usd[0] if len(usd) == 1 else None
    if pick is None:
        return None, None
    used = _usd(pick.get("used"))
    limit = _usd(pick.get("limit"))
    pct = _num(pick.get("used_pct"))
    if used is None and pct is not None and limit:
        used = pct / 100.0 * limit
    return (used, at) if used is not None and 0 <= used < 1e9 else (None, None)


async def read_mistral(client, console=_UNSET, now: Optional[datetime] = None) -> QuotaReading:
    """Mistral education key: $15 of API usage a month, resets on the 1st, pay-as-you-go off (a hard stop).
    Mistral publishes no usage API for it, so the month's spend is the larger of the price-book cost of our
    proxies' calls (Switchboard) and the latest admin-console reading for this month (mistral_console, entered
    by hand), which also covers traffic that bypasses our proxies. `console`/`now` are injectable for tests."""
    vid, name = "mistral", "Mistral (education key)"
    src = "our proxies' logs (Switchboard) + admin console"
    plan = "Education: $15/month API allowance"
    now = now or _now()
    start, reset = _month_bounds(now)
    limit = _mistral_monthly_usd()
    notes = ("Included API usage $15/month, resets on the 1st; pay-as-you-go is off, so it is a hard stop. Spend is the "
             "larger of our proxies' calls at list price and the last admin-console reading. Rate limits (console, "
             "2026-10-19): mistral-large-2512 800,000 tokens/min and 0.5 req/s (30/min); mistral-large-4 10M tokens/min, 16.67 req/s.")
    base = os.environ.get("SWITCHBOARD_URL", "").strip().rstrip("/")
    computed, calls, unpriced, sb_error = None, None, 0, None
    try:
        if not base:
            raise RuntimeError("SWITCHBOARD_URL is not set")
        r = await client.get(f"{base}/api/backend-usage", params={"backend": "mistral", "since": start.strftime("%Y-%m-%dT%H:%M:%S")},
                             headers={"User-Agent": UA, "Accept": "application/json"})
        if r.status_code != 200:
            raise RuntimeError(f"Switchboard HTTP {r.status_code}")
        d = r.json()
        cost = _num(d.get("cost_usd")) if isinstance(d, dict) else None
        if cost is None or cost < 0:
            raise RuntimeError("Switchboard answer has no cost_usd (older build)")
        computed, calls = cost, d.get("calls")
        unpriced = int(_num(d.get("unpriced_calls")) or 0)
    except Exception as e:  # noqa: BLE001
        sb_error = f"Switchboard usage unavailable: {e}"[:200]
    if console is _UNSET:
        try:
            console = await store.latest_one(MISTRAL_CONSOLE_ID)
        except Exception:  # noqa: BLE001 — the store being unreadable must not hide the computed number
            console = None
    console_usd, console_at = _console_month_usd(console, start, reset)
    have = [x for x in (computed, console_usd) if x is not None]
    if not have:
        return _fail(vid, name, src, "our_logs", sb_error or "no usage figure", notes=notes)
    used = max(have)
    if unpriced:
        notes = notes[:300] + f" {unpriced} call(s) are unpriced, so the logged cost is a floor."
    if computed is None:
        notes = notes[:325] + " Switchboard cost unavailable: this is the admin-console figure only."
    extra = {"computed_usd": computed, "console_usd": console_usd,
             "console_at": console_at.isoformat() if console_at else None, "calls_month": calls,
             "unpriced_calls": unpriced, "basis": "max(computed from our logs, latest console reading this month)"}
    if sb_error:
        extra["switchboard_error"] = sb_error
    window = QuotaWindow(name="month", used_pct=round(used / limit * 100.0, 3), used=round(used, 8), limit=limit,
                         unit="USD", resets_at=reset)
    return QuotaReading(id=vid, name=name, source=src, source_kind="our_logs", plan=plan, fetched_at=now,
                        windows=[window], extra=extra, notes=notes[:400], stale_after_s=1800)


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
    "synthetic": (read_synthetic, "SYNTHETIC_API_KEY", 300),
    "commandcode": (read_commandcode, "COMMANDCODE_API_KEY", 300),
    # 300 s: the proxies drop a reading without a reset time 15 min after this read (2026-10-19:
    # they stopped polling Ollama themselves and take this reading through Switchboard).
    "ollama_cloud": (read_ollama, "OLLAMA_CLOUD_API_KEY", 300),
    "mistral": (read_mistral, None, 300),
    "gemini_api": (read_gemini_api, None, 600),
}
