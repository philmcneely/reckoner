#!/usr/bin/env python3
"""Client usage collector — ask each client that knows its own plan usage, push it to Reckoner.

Where a vendor has no usage API, Reckoner asks the agent/client that knows. This runs on the
workstation where those clients are signed in, every 5 minutes (LaunchAgent template
com.reckoner.usage-collector.plist), and POSTs to Reckoner's /api/quota/ingest. Settings
(RECKONER_INGEST_URL, RECKONER_INGEST_TOKEN, optional RECKONER_COLLECTOR_HOST) come from the
environment or ~/.config/reckoner/collector.env (mode 600).

Sources (each verified 2026-10-18):
  claude_max, cursor, copilot
      herdr's own usage reader: the local herdr-web bridge answers GET /api/usage (port + token
      from ~/.config/herdr-web-ui/bridges/<sha256>.json). For Claude it reads Claude Code's own
      login on this Mac and asks Anthropic's plan-usage endpoint — our plan, our login; nothing
      is proxied, no token leaves this machine. (opencode/antigravity entries there are ignored:
      OpenCode Go is read from its own API by Reckoner, Antigravity from its own CLI below.)
  antigravity
      `antigravity-usage quota --json` (Google Cloud Code quota); grouped Claude / Gemini Pro /
      Gemini Flash, each group's fullest model, like agy's own status bar.
  cline
      `statusline-cline --quota --json` — the Cline account API (/api/v1/users/me/plan + usage
      ledger; ClinePass caps 5h $20 / 7d $50 / 30d $100). Its own cache time is reported as the read
      time, so a stuck refresh shows as stale instead of current.
  muse
      the Muse CLI's own `usage/read` (MSP over `muse serve`). Muse only knows its plan usage after
      a model call, so this sends ONE tiny turn at most once an hour (cached in between).
  opencode_cli
      `opencode stats --days 7 --models --cost --json` — what opencode itself recorded per provider
      (opencode-go, zai, nanogpt, …): steps, tokens, cost.

No account names, emails or tokens are sent to Reckoner. Each reader is isolated: one failing
never stops the others. stdlib only (runs under /usr/local/bin/python3 from launchd).
"""

import glob
import json
import os
import re
import select
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
ENV_FILE = os.path.join(HOME, ".config/reckoner/collector.env")
CACHE_DIR = os.path.join(HOME, ".cache/reckoner-collector")
MUSE_EVERY_S = int(os.environ.get("RECKONER_MUSE_EVERY_S", "3600"))
ANTIGRAVITY = os.environ.get("ANTIGRAVITY_USAGE_BIN", "/usr/local/bin/antigravity-usage")
CLINE = os.environ.get("STATUSLINE_CLINE_BIN", os.path.join(HOME, ".local/bin/statusline-cline"))
MUSE = os.environ.get("MUSE_BIN", os.path.join(HOME, ".local/bin/muse"))
OPENCODE = os.environ.get("OPENCODE_BIN", "/usr/local/bin/opencode")



def _setting(name, default=""):
    """Environment first, then ~/.config/reckoner/collector.env (KEY=value lines)."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    try:
        for line in open(ENV_FILE):
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return default


HOST = _setting("RECKONER_COLLECTOR_HOST") or socket.gethostname().split(".")[0].lower()
HERDR_IDS = {"claude": ("claude_max", "Claude Max"), "cursor": ("cursor", "Cursor"), "copilot": ("copilot", "GitHub Copilot")}


def _iso(ms_or_s):
    if ms_or_s is None or isinstance(ms_or_s, bool):
        return None
    try:
        v = float(ms_or_s)
        v = v / 1000.0 if v > 1e11 else v
        return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _now():
    return datetime.now(timezone.utc).isoformat()


_AT_RE = re.compile(r"\S*@\S*")
_TOKENISH_RE = re.compile(r"[A-Za-z0-9_\-.]{20,}")


def _scrub(text, limit=300):
    """Error text from a CLI we don't control, made safe to store: anything with an "@" (an email or
    account) and any token-like run of 20+ [A-Za-z0-9_-.] is masked, control characters dropped,
    length bounded. None for empty."""
    if not text:
        return None
    out = _TOKENISH_RE.sub("[redacted]", _AT_RE.sub("[redacted]", str(text)))
    out = "".join(c for c in out if c.isprintable())
    return out[:limit] or None


def _run_json(argv, timeout=60):
    out = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    if out.returncode != 0 and not out.stdout:
        raise RuntimeError(f"{os.path.basename(argv[0])} exited {out.returncode}")
    return json.loads(out.stdout)


# ── herdr (Claude Max, Cursor, Copilot) ──────────────────────────────────────

def from_herdr_usage(doc):
    readings = []
    for p in (doc or {}).get("providers") or []:
        mapped = HERDR_IDS.get(p.get("id"))
        if not mapped:
            continue
        vid, name = mapped
        windows = []
        for w in p.get("windows") or []:
            label = w.get("kind") or "window"
            if w.get("scope"):
                label = f"{label} ({w['scope']})"
            windows.append({"name": label[:48], "used_pct": w.get("used_percent"),
                            "resets_at": w.get("resets_at"), "starts_at": w.get("starts_at")})
        r = {"id": vid, "name": name, "source": f"herdr on {HOST}", "source_kind": "client", "plan": p.get("plan"),
             "ok": not p.get("problem"), "error": _scrub(p.get("problem")),
             "fetched_at": p.get("checked_at") or _now(), "windows": windows, "stale_after_s": 1800}
        readings.append(r)
    return readings


def read_herdr():
    paths = sorted(glob.glob(os.path.join(HOME, ".config/herdr-web-ui/bridges/*.json")), key=os.path.getmtime)
    if not paths:
        raise RuntimeError("no herdr-web bridge on this Mac")
    d = json.load(open(paths[-1]))
    req = urllib.request.Request(f"http://127.0.0.1:{int(d['port'])}/api/usage",
                                 headers={"Authorization": "Bearer " + d["token"], "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # loopback only
        return from_herdr_usage(json.loads(resp.read(512 * 1024)))


# ── Antigravity ──────────────────────────────────────────────────────────────

def _antigravity_group(label):
    lab = (label or "").lower()
    if lab.startswith("claude") or "gpt" in lab:
        return "Claude models" if lab.startswith("claude") else "GPT models"
    if "flash" in lab:
        return "Gemini Flash"
    if "pro" in lab:
        return "Gemini Pro"
    return "Other models"


def from_antigravity(doc):
    groups = {}
    for m in (doc or {}).get("models") or []:
        if m.get("isAutocompleteOnly"):
            continue
        rem = m.get("remainingPercentage")
        if isinstance(rem, bool) or not isinstance(rem, (int, float)):
            continue
        used = round((1.0 - float(rem)) * 100.0, 1)
        g = _antigravity_group(m.get("label"))
        cur = groups.get(g)
        if cur is None or used > cur["used_pct"]:
            groups[g] = {"name": g, "used_pct": used, "resets_at": m.get("resetTime")}
    return {"id": "antigravity", "name": "Google Antigravity", "source": f"Antigravity CLI on {HOST}", "source_kind": "client",
            "plan": None, "ok": bool(groups), "error": None if groups else "no models in answer",
            "fetched_at": (doc or {}).get("timestamp") or _now(),
            "windows": [groups[k] for k in sorted(groups)], "stale_after_s": 1800}


def read_antigravity():
    if not os.path.exists(ANTIGRAVITY):
        return None
    return from_antigravity(_run_json([ANTIGRAVITY, "quota", "--json"], timeout=90))


# ── Cline ────────────────────────────────────────────────────────────────────

def from_cline(doc):
    q = (doc or {}).get("quota") or {}
    windows = [{"name": w.get("window"), "used_pct": w.get("usedPct"), "used": w.get("usedUsd"),
                "limit": w.get("capUsd"), "unit": "USD", "resets_at": w.get("nextRolloff")}
               for w in q.get("windows") or [] if w.get("window")]
    err = doc.get("error") if isinstance(doc, dict) else None
    return {"id": "cline", "name": "Cline (ClinePass)", "source": f"Cline account API via statusline-cline on {HOST}",
            "source_kind": "client", "plan": q.get("planName"), "ok": bool(windows),
            "error": (_scrub(f"refresh failing: {err}") if err else None),
            # the time the numbers were actually read from Cline, not now: a stuck cache shows stale
            "fetched_at": q.get("fetchedAt") or _now(), "windows": windows, "stale_after_s": 1800,
            "extra": {"period_end": q.get("periodEnd")}}


def read_cline():
    if not os.path.exists(CLINE):
        return None
    return from_cline(_run_json([CLINE, "--quota", "--json"], timeout=90))


# ── Muse ─────────────────────────────────────────────────────────────────────

def from_muse(result):
    u = (result or {}).get("usage")
    if not isinstance(u, dict):
        return {"id": "muse", "name": "Muse Code", "source": f"Muse CLI on {HOST} (usage/read)", "source_kind": "client",
                "ok": False, "error": "Muse has observed no plan usage yet", "fetched_at": _now(), "windows": [],
                "stale_after_s": 3 * 3600}
    win, wk = u.get("window") or {}, u.get("weekly") or {}
    mins = win.get("windowDurationMins")
    windows = [{"name": f"{round(mins / 60)}h" if mins else "window", "used_pct": win.get("usedPercent"),
                "resets_at": _iso(win.get("resetsAtMs"))},
               {"name": "weekly", "used_pct": wk.get("usedPercent"), "resets_at": _iso(wk.get("resetsAtMs"))}]
    return {"id": "muse", "name": "Muse Code", "source": f"Muse CLI on {HOST} (usage/read)", "source_kind": "client",
            "plan": "Muse Code subscription", "ok": True, "fetched_at": _iso(u.get("observedAtMs")) or _now(),
            "windows": windows, "stale_after_s": 3 * 3600,
            "notes": "Muse only knows its plan usage after a model call: one tiny turn per hour."}


def _uuid7():
    ms = int(time.time() * 1000)
    rand = uuid.uuid4().int
    val = (ms << 80) | (0x7 << 76) | (((rand >> 64) & 0xFFF) << 64) | (0b10 << 62) | (rand & ((1 << 62) - 1))
    return str(uuid.UUID(int=val))


def _muse_probe(timeout=120):
    """Start `muse serve`, open one session, run one tiny turn, then read usage/read."""
    p = subprocess.Popen([MUSE, "serve", "--no-session-log"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    buf = b""

    def send(obj):
        p.stdin.write(json.dumps(obj).encode() + b"\n")
        p.stdin.flush()

    def until(pred, secs):
        nonlocal buf
        end = time.time() + secs
        while time.time() < end:
            r, _, _ = select.select([p.stdout], [], [], 0.3)
            if not r:
                continue
            chunk = os.read(p.stdout.fileno(), 65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if pred(o):
                    return o
        return None

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "reckoner_collector", "version": "1"}}})
        if not until(lambda o: o.get("id") == 1, 15):
            raise RuntimeError("muse serve did not initialize")
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "session/start", "params": {"commandId": _uuid7()}})
        started = until(lambda o: o.get("id") == 2, 20)
        sid = ((started or {}).get("result") or {}).get("session", {}).get("sessionId")
        if not sid:
            raise RuntimeError("muse session did not start")
        send({"jsonrpc": "2.0", "id": 3, "method": "turn/start", "params": {
            "commandId": _uuid7(), "sessionId": sid, "input": [{"type": "text", "text": "Reply with the single word: ok"}]}})
        until(lambda o: (o.get("method") or "").startswith("turn/completed") or o.get("method") == "usage/changed", timeout)
        send({"jsonrpc": "2.0", "id": 4, "method": "usage/read"})
        res = until(lambda o: o.get("id") == 4, 15)
        return (res or {}).get("result") or {}
    finally:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()


def read_muse():
    if not os.path.exists(MUSE):
        return None
    os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
    cache = os.path.join(CACHE_DIR, "muse.json")
    try:
        c = json.load(open(cache))
        if time.time() - c.get("at", 0) < MUSE_EVERY_S:
            return from_muse(c.get("result"))
    except (OSError, ValueError):
        pass
    result = _muse_probe()
    tmp = cache + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"at": time.time(), "result": result}, f)
    os.replace(tmp, cache)
    return from_muse(result)


# ── opencode ─────────────────────────────────────────────────────────────────

def from_opencode_stats(doc):
    by_provider = {}
    for m in (doc or {}).get("models") or []:
        model = m.get("model") or {}
        prov = model.get("providerID") or "unknown"
        cur = by_provider.setdefault(prov, {"steps": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "models": []})
        tok = m.get("tokens") or {}
        cur["steps"] += int(m.get("steps") or 0)
        cur["input_tokens"] += int(tok.get("input") or 0)
        cur["output_tokens"] += int(tok.get("output") or 0)
        cur["cost_usd"] = round(cur["cost_usd"] + float(m.get("cost") or 0), 4)
        if model.get("id") and len(cur["models"]) < 8:
            cur["models"].append(model["id"])
    t = (doc or {}).get("tokens") or {}
    return {"id": "opencode_cli", "name": "opencode (all providers)", "source": f"opencode stats on {HOST}",
            "source_kind": "client", "ok": True, "fetched_at": _now(), "windows": [],
            "money": {"cost_7d_usd": round(float((doc or {}).get("cost") or 0), 4)},
            "extra": {"sessions_7d": doc.get("sessions"), "prompts_7d": doc.get("prompts"),
                      "tokens_7d": {"input": t.get("input"), "output": t.get("output"),
                                    "cache_read": (t.get("cache") or {}).get("read")},
                      "by_provider": by_provider},
            "notes": "What opencode itself recorded over the last 7 days, by provider. Plan percentages for "
                     "OpenCode Go come from its own API (OpenCode Go card).",
            "stale_after_s": 1800}


def read_opencode_stats():
    if not os.path.exists(OPENCODE):
        return None
    return from_opencode_stats(_run_json([OPENCODE, "stats", "--days", "7", "--models", "--cost", "--json"], timeout=120))


# ── push ─────────────────────────────────────────────────────────────────────

def _post(url, body, token, timeout):
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {token}", "User-Agent": "reckoner-usage-collector/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(4096)
    except urllib.error.HTTPError as e:
        return e.code, e.read(4096)


def push(readings, url=None):
    url = url or _setting("RECKONER_INGEST_URL")
    token = _setting("RECKONER_INGEST_TOKEN")
    if not url or not token:
        raise RuntimeError(f"RECKONER_INGEST_URL and RECKONER_INGEST_TOKEN must be set (env or {ENV_FILE})")
    body = json.dumps({"source": f"collector@{HOST}"[:64], "readings": readings}).encode()
    code, text = _post(url, body, token, 30)
    if code != 200:
        raise RuntimeError(f"Reckoner answered HTTP {code}: {text[:200]!r}")
    return code


def collect():
    readings, errors = [], []
    for name, fn in (("herdr", read_herdr), ("antigravity", read_antigravity), ("cline", read_cline),
                     ("muse", read_muse), ("opencode", read_opencode_stats)):
        try:
            got = fn()
        except Exception as e:  # noqa: BLE001 — one client failing never stops the others
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:200]}")
            continue
        if got is None:
            continue
        readings.extend(got if isinstance(got, list) else [got])
    return readings, errors


def main():
    readings, errors = collect()
    for e in errors:
        print(f"[{_now()}] reader failed: {e}", file=sys.stderr)
    if readings:
        push(readings)
    print(f"[{_now()}] pushed {len(readings)} readings: {', '.join(r['id'] for r in readings)}")


if __name__ == "__main__":
    main()
