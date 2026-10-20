"""Plan-usage / quota readings (2026-10-18).

Reckoner reads every metered vendor ONCE for the fleet (quota is checked in one place):
  * server-side readers here: Z.ai, OpenCode Go, NanoGPT, pgsgrove, Ollama Cloud, and — for the two
    vendors that publish no limits — Mistral and the Gemini API key from our own proxy rows
    (Switchboard GET /api/backend-usage), labelled as our own logs;
  * client-side readers on a workstation (herdr's Claude / Cursor / Copilot reader, the Antigravity CLI,
    Cline's account API via statusline-cline, the Muse CLI, opencode stats) POST to
    /api/quota/ingest with a token, restricted to their own ids;
  * Switchboard reads GET /api/quota (read token) and hands each proxy only what it uses.
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

TMP = tempfile.mkdtemp()
os.environ["RECKONER_DB_PATH"] = str(Path(TMP) / "tracker.db")
os.environ["RECKONER_INGEST_TOKEN"] = "ingest-token-0123456789abcdef0123456789"
os.environ["RECKONER_QUOTA_TOKEN"] = "quota-read-token-0123456789abcdef012345"
os.environ.setdefault("RECKONER_PASSWORD", "dashboard-pw")  # dashboard protected, like the deploy
os.environ.setdefault("RECKONER_PROTECT_DASHBOARD", "true")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from models import database  # noqa: E402
from quota import readers, store  # noqa: E402
from quota.models import QuotaReading  # noqa: E402

NOW = datetime(2026, 10, 18, 23, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)


# ── database path ────────────────────────────────────────────────────────────

def test_db_path_comes_from_env():
    # The compose volume is mounted at /app/data; the old default (/app/backend/data) lived in the
    # container's writable layer and was lost on every rebuild.
    assert str(database.DB_PATH) == os.environ["RECKONER_DB_PATH"]


# ── server-side readers ──────────────────────────────────────────────────────

ZAI = {"code": 200, "msg": "Operation successful", "success": True, "data": {"level": "lite", "limits": [
    {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 21, "nextResetTime": 1792382686000},
    {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 40, "nextResetTime": 1792665914944},
    {"type": "TIME_LIMIT", "unit": 5, "number": 1, "percentage": 0, "nextResetTime": 1793184314998}]}}


def test_zai_reader():
    seen = {}

    def h(req):
        seen["auth"] = req.headers.get("authorization")
        seen["url"] = str(req.url)
        return httpx.Response(200, json=ZAI)
    r = run(readers.read_zai(client_for(h), "zk"))
    assert seen["url"] == "https://api.z.ai/api/monitor/usage/quota/limit"
    assert seen["auth"] == "Bearer zk"
    assert r.ok and r.id == "zai" and r.plan == "lite" and r.source_kind == "api"
    assert [(w.name, w.used_pct) for w in r.windows] == [("5h", 21.0), ("weekly", 40.0), ("mcp-tools", 0.0)]
    assert r.windows[0].resets_at is not None


def test_zai_error_payload_is_not_ok():
    r = run(readers.read_zai(client_for(lambda req: httpx.Response(200, json={"code": 401, "success": False})), "k"))
    assert not r.ok and r.error


def test_opencode_go_reader_sends_user_agent():
    seen = {}

    def h(req):
        seen["ua"] = req.headers.get("user-agent")
        return httpx.Response(200, json={"usage": {
            "rolling": {"percent": 3, "resetsAt": "2026-10-19T04:06:53.467Z"},
            "weekly": {"percent": 15, "resetsAt": "2026-10-19T00:00:00.000Z"},
            "monthly": {"percent": 7, "resetsAt": "2026-11-10T02:41:18.000Z"}}})
    r = run(readers.read_opencode_go(client_for(h), "ok"))
    assert seen["ua"] and "python" not in seen["ua"].lower()  # Cloudflare 1010 blocks default UAs
    assert [(w.name, w.used_pct) for w in r.windows] == [("rolling-5h", 3.0), ("weekly", 15.0), ("monthly", 7.0)]


def test_nanogpt_reader_uses_subscription_usage():
    seen = {}

    def h(req):
        seen["url"] = str(req.url)
        seen["key"] = req.headers.get("x-api-key")
        return httpx.Response(200, json={
            "active": True, "limits": {"weeklyInputTokens": 60000000, "dailyImages": 100},
            "period": {"currentPeriodEnd": "2026-11-18T14:46:46.000Z"},
            "weeklyInputTokens": {"used": 585339, "remaining": 59414661, "percentUsed": 0.00975565, "resetAt": 1792700000000},
            "dailyImages": {"used": 0, "remaining": 100, "percentUsed": 0, "resetAt": 1792368000000}})
    r = run(readers.read_nanogpt(client_for(h), "nk"))
    assert seen["url"] == "https://nano-gpt.com/api/subscription/v1/usage" and seen["key"] == "nk"
    w = {x.name: x for x in r.windows}
    assert abs(w["weekly-input-tokens"].used_pct - 0.976) < 0.01  # used/limit, not the API's fraction
    assert w["weekly-input-tokens"].used == 585339 and w["weekly-input-tokens"].limit == 60000000
    assert "daily-images" in w and r.plan == "subscription (active)"


# Shape of GET https://api.synthetic.new/v2/quotas (Bearer key), read live 2026-10-19; values here are
# invented. Credit amounts are dollar strings; limits and counts are numbers; times are ISO strings.
SYNTHETIC = {
    "subscription": {"limit": 500, "requests": 125, "renewsAt": "2026-10-19T23:18:37.976Z"},
    "search": {"hourly": {"limit": 250, "requests": 50, "renewsAt": "2026-10-19T21:18:37.977Z"}},
    "freeToolCalls": {"limit": 0, "requests": 0, "renewsAt": "2026-10-20T23:18:37.985Z"},
    "weeklyTokenLimit": {"nextRegenAt": "2026-10-19T22:54:06.000Z", "percentRemaining": 94.5,
                         "maxCredits": "$60.00", "remainingCredits": "$56.70", "nextRegenCredits": "$1.20"},
    "rollingFiveHourLimit": {"nextTickAt": "2026-10-19T23:30:16.000Z", "tickPercent": 0.05,
                             "remaining": 400, "max": 500, "limited": False},
}


def test_synthetic_reader_maps_every_window():
    seen = {}

    def h(req):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json=SYNTHETIC)
    r = run(readers.read_synthetic(client_for(h), "sk"))
    assert seen["url"] == "https://api.synthetic.new/v2/quotas" and seen["auth"] == "Bearer sk"
    assert r.ok and r.id == "synthetic" and r.source_kind == "api"
    w = {x.name: x for x in r.windows}
    assert set(w) == {"5h", "weekly", "subscription", "search-hourly"}  # freeToolCalls has limit 0: no window
    assert w["5h"].used == 100 and w["5h"].limit == 500 and w["5h"].unit == "requests" and w["5h"].used_pct == 20.0
    assert w["5h"].resets_at is not None
    assert abs(w["weekly"].used_pct - 5.5) < 0.01 and w["weekly"].unit == "USD"  # 100 - percentRemaining
    assert abs(w["weekly"].used - 3.3) < 0.001 and w["weekly"].limit == 60.0
    assert w["weekly"].resets_at is not None
    assert w["subscription"].used_pct == 25.0 and w["subscription"].limit == 500
    assert w["search-hourly"].used_pct == 20.0 and w["search-hourly"].unit == "requests"


def test_synthetic_limited_means_full():
    doc = {"rollingFiveHourLimit": {"nextTickAt": "2026-10-19T23:30:16.000Z", "tickPercent": 0.05,
                                    "remaining": 3, "max": 500, "limited": True}}
    r = run(readers.read_synthetic(client_for(lambda req: httpx.Response(200, json=doc)), "sk"))
    assert r.windows[0].name == "5h" and r.windows[0].used_pct == 100.0 and r.extra["limited"] is True


def test_synthetic_survives_missing_and_odd_fields():
    doc = {"weeklyTokenLimit": {"percentRemaining": "n/a", "maxCredits": "free", "remainingCredits": None},
           "rollingFiveHourLimit": {"remaining": 5, "max": 0}, "subscription": "oops",
           "search": {"hourly": {"limit": 10, "requests": True}}}
    r = run(readers.read_synthetic(client_for(lambda req: httpx.Response(200, json=doc)), "sk"))
    assert not r.ok and r.error  # nothing usable: say so rather than show an empty healthy card
    r2 = run(readers.read_synthetic(client_for(lambda req: httpx.Response(200, json={})), "sk"))
    assert not r2.ok


def test_synthetic_rejected_key_and_no_key_leak():
    r = run(readers.read_synthetic(client_for(lambda req: httpx.Response(401, json={"error": "nope"})), "sk-live-1"))
    assert not r.ok and "401" in r.error

    def h(req):
        raise httpx.ConnectError("boom sk-live-1 boom")
    r = run(readers.read_synthetic(client_for(h), "sk-live-1"))
    assert not r.ok and "sk-live-1" not in r.error


def test_synthetic_is_a_scheduled_server_reader():
    reader, env, interval = readers.SERVER_READERS["synthetic"]
    assert reader is readers.read_synthetic and env == "SYNTHETIC_API_KEY" and interval == 300


# Shapes of Command Code's own billing API (the three GETs behind its CLI's /usage), read live 2026-10-20 with the
# account key as a Bearer token on https://api.commandcode.ai. Numbers here are invented.
CC_CREDITS = {"credits": {"belowThreshold": False, "creditThreshold": 0, "monthlyCredits": 55.0, "purchasedCredits": 4.5,
                          "freeCredits": 1.25},
              "windowLimits": {"limited": True, "exceeded": None,
                               "fiveHour": {"used": 3.5, "cap": 14, "exceeded": False, "resetAt": 1792382686000},
                               "weekly": {"used": 14.0, "cap": 35, "exceeded": False, "resetAt": 1792665914944}},
              "sandboxAccess": True, "sandboxMinutes": {"usedMinutes": 0, "limitMinutes": 0, "overageUsd": 0, "resetAt": 1793491200000}}
CC_SUB = {"success": True, "data": {"status": "active", "planId": "individual-goat", "orgId": None,
                                     "currentPeriodStart": "2026-10-10T02:00:00.000Z", "currentPeriodEnd": "2026-11-10T02:00:00.000Z"}}
CC_SUMMARY = {"totalCount": 40, "totalCost": 15.0, "totalCredits": 15.0, "totalMonthlyCredits": 15.0, "totalPurchasedCredits": 0,
              "periodBasis": "billing-period", "periodStart": "2026-10-10T02:00:00.000Z", "daily": []}


def _cc_handler(seen, credits=CC_CREDITS, sub=CC_SUB, summary=CC_SUMMARY):
    def h(req):
        seen.setdefault("calls", []).append((req.url.path, dict(req.url.params)))
        seen["auth"] = req.headers.get("authorization")
        out = {"/alpha/billing/credits": credits, "/alpha/billing/subscriptions": sub, "/alpha/usage/summary": summary}[req.url.path]
        return out if isinstance(out, httpx.Response) else httpx.Response(200, json=out)
    return h


def test_commandcode_reader_maps_the_windows():
    seen = {}
    r = run(readers.read_commandcode(client_for(_cc_handler(seen)), "cc-key"))
    assert seen["auth"] == "Bearer cc-key"
    assert [c[0] for c in seen["calls"]] == ["/alpha/billing/credits", "/alpha/billing/subscriptions", "/alpha/usage/summary"]
    assert seen["calls"][2][1] == {"since": "2026-10-10T02:00:00.000Z"}
    assert r.ok and r.id == "commandcode" and r.source_kind == "api" and r.plan == "individual-goat"
    w = {x.name: x for x in r.windows}
    assert set(w) == {"5h", "weekly", "monthly"}
    assert w["5h"].used == 3.5 and w["5h"].limit == 14 and w["5h"].unit == "USD" and w["5h"].used_pct == 25.0
    assert w["5h"].resets_at == datetime.fromtimestamp(1792382686, tz=timezone.utc)
    assert w["weekly"].used_pct == 40.0 and w["weekly"].resets_at is not None
    # monthly: spent 15.00 of the period, 55.00 left -> a 70.00 pool
    assert w["monthly"].used == 15.0 and w["monthly"].limit == 70.0 and abs(w["monthly"].used_pct - 21.429) < 0.001
    assert w["monthly"].resets_at == datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)
    assert r.money == {"monthly_credits_left": 55.0, "purchased_credits": 4.5, "free_credits": 1.25}


def test_commandcode_window_not_started_has_no_reset_and_exceeded_reads_full():
    credits = json.loads(json.dumps(CC_CREDITS))
    credits["windowLimits"]["fiveHour"].update(used=0, resetAt=0)
    credits["windowLimits"]["weekly"].update(used=35, exceeded=True)
    r = run(readers.read_commandcode(client_for(_cc_handler({}, credits=credits)), "k"))
    w = {x.name: x for x in r.windows}
    assert w["5h"].used_pct == 0 and w["5h"].resets_at is None          # resetAt 0 = the window has not started
    assert w["weekly"].used_pct == 100.0


def test_commandcode_plan_without_caps_or_monthly_pool():
    credits = {"credits": {"monthlyCredits": 0, "purchasedCredits": 3, "freeCredits": 0}, "windowLimits": {"limited": False}}
    r = run(readers.read_commandcode(client_for(_cc_handler({}, credits=credits, summary={"totalMonthlyCredits": 0})), "k"))
    assert r.ok and r.windows == [] and r.money["purchased_credits"] == 3.0


def test_commandcode_credits_alone_is_enough_when_the_others_fail():
    sub_down = httpx.Response(500)
    r = run(readers.read_commandcode(client_for(_cc_handler({}, sub=sub_down, summary=httpx.Response(500))), "k"))
    assert r.ok and {x.name for x in r.windows} == {"5h", "weekly"} and r.plan is None
    assert r.extra["monthly_unavailable"]


def test_commandcode_errors_never_leak_the_key():
    r = run(readers.read_commandcode(client_for(lambda req: httpx.Response(401, json={"error": "no"})), "cc-key-9"))
    assert not r.ok and "401" in r.error and "cc-key-9" not in r.error

    def h(req):
        raise httpx.ConnectError("boom cc-key-9 boom")
    r = run(readers.read_commandcode(client_for(h), "cc-key-9"))
    assert not r.ok and "cc-key-9" not in r.error
    r = run(readers.read_commandcode(client_for(lambda req: httpx.Response(200, json=["x"])), "k"))
    assert not r.ok


def test_commandcode_is_a_scheduled_server_reader():
    reader, env, interval = readers.SERVER_READERS["commandcode"]
    assert reader is readers.read_commandcode and env == "COMMANDCODE_API_KEY" and interval == 300


def test_pgsgrove_reader():
    r = run(readers.read_pgsgrove(client_for(lambda req: httpx.Response(200, json={
        "weekly_used_percent": 12.5, "daily_used_percent": 0.1, "weekly_resets_at": "2026-10-20T00:00:00Z",
        "api_share_of_weekly_percent": 3})), "pk"))
    assert [(w.name, w.used_pct) for w in r.windows] == [("daily", 0.1), ("weekly", 12.5)]
    assert r.notes and "slows" in r.notes.lower()  # banked time: it slows, it doesn't stop


def test_ollama_new_format_and_429():
    ok = run(readers.read_ollama(client_for(lambda req: httpx.Response(200, json={
        "range": "week", "totals": {"request_count": 42}, "buckets": []})), "ok"))
    assert ok.ok and ok.windows == [] and ok.extra["request_count_week"] == 42
    limited = run(readers.read_ollama(client_for(lambda req: httpx.Response(429, json={"error": "too many requests"})), "k"))
    assert not limited.ok and "429" in limited.error


def test_gemini_our_logs_reader_is_labelled(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_URL", "http://switchboard.invalid:8800")

    def h(req):
        assert "backend=ai_studio" in str(req.url)
        return httpx.Response(200, json={"calls": 7, "ok": 6, "input_tokens": 1000, "output_tokens": 50,
                                         "cache_read_tokens": 0, "last_ts": "2026-10-18T20:00:00", "by_machine": {"box-a": 7}})
    g = run(readers.read_gemini_api(client_for(h)))
    assert g.source_kind == "our_logs" and g.ok and g.windows == []
    assert "no usage api" in g.notes.lower() and g.extra["calls_7d"] == 7


def test_our_logs_without_switchboard_url_says_so(monkeypatch):
    monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
    m = run(readers.read_gemini_api(client_for(lambda req: httpx.Response(500))))
    assert not m.ok and "SWITCHBOARD_URL" in m.error and m.source_kind == "our_logs"


# ── Mistral: a monthly dollar window ─────────────────────────────────────────
# The education key has $15 of API usage a month, resetting on the 1st; pay-as-you-go is off, so $15 is a
# hard stop. Mistral publishes no usage API for it. The reading is the larger of (a) the price-book cost of
# our proxies' calls this month (Switchboard GET /api/backend-usage) and (b) the latest admin-console reading
# for the same month, ingested as vendor `mistral_console`, so traffic outside our proxies is not lost.

MNOW = datetime(2026, 10, 19, 22, 44, tzinfo=timezone.utc)
MONTH_RESET = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _usage_handler(seen, **body):
    base = {"backend": "mistral", "calls": 13, "ok": 13, "input_tokens": 118610, "output_tokens": 159,
            "cost_usd": 0.0595435, "real_cost_usd": 0.0, "unpriced_calls": 0, "last_ts": "2026-10-18T18:15:12.200640"}
    base.update(body)

    def h(req):
        seen["url"] = str(req.url)
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json=base)
    return h


def _console(used=1.18, fetched=datetime(2026, 10, 19, 22, 44, tzinfo=timezone.utc), **over):
    doc = {"id": "mistral_console", "ok": True, "fetched_at": fetched.isoformat(),
           "windows": [{"name": "month", "used": used, "limit": 15, "unit": "USD", "resets_at": MONTH_RESET.isoformat()}]}
    doc.update(over)
    return doc


def _mistral(handler, console=None, now=MNOW, monkeypatch=None):
    return run(readers.read_mistral(client_for(handler), console=console, now=now))


@pytest.fixture
def sb_env(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_URL", "http://switchboard.invalid:8800")
    monkeypatch.delenv("MISTRAL_MONTHLY_USD", raising=False)


def test_mistral_month_to_date_from_our_logs(sb_env):
    seen = {}
    r = _mistral(_usage_handler(seen))
    assert seen["params"] == {"backend": "mistral", "since": "2026-10-01T00:00:00"}
    assert r.ok and r.id == "mistral" and r.source_kind == "our_logs"
    (w,) = r.windows
    assert w.name == "month" and w.unit == "USD" and w.limit == 15.0 and abs(w.used - 0.0595435) < 1e-9
    assert abs(w.used_pct - 0.397) < 0.001
    assert w.resets_at == MONTH_RESET
    assert r.extra["computed_usd"] == pytest.approx(0.0595435) and r.extra["console_usd"] is None
    assert r.extra["calls_month"] == 13


def test_mistral_console_reading_wins_when_higher(sb_env):
    r = _mistral(_usage_handler({}), console=_console(1.18))
    w = r.windows[0]
    assert w.used == pytest.approx(1.18) and w.used_pct == pytest.approx(7.867, abs=0.001)
    assert r.extra["computed_usd"] == pytest.approx(0.0595435) and r.extra["console_usd"] == 1.18
    assert r.extra["console_at"].startswith("2026-10-19T22:44")


def test_mistral_computed_wins_when_higher(sb_env):
    r = _mistral(_usage_handler({}, cost_usd=2.5), console=_console(1.18))
    assert r.windows[0].used == pytest.approx(2.5) and r.windows[0].used_pct == pytest.approx(16.667, abs=0.001)


def test_mistral_console_from_another_month_is_ignored(sb_env):
    last_month = _console(14.0, fetched=datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc))
    r = _mistral(_usage_handler({}), console=last_month)
    assert r.windows[0].used == pytest.approx(0.0595435) and r.extra["console_usd"] is None


def test_mistral_limit_from_env_and_bad_values_fall_back(sb_env, monkeypatch):
    monkeypatch.setenv("MISTRAL_MONTHLY_USD", "30")
    assert _mistral(_usage_handler({}), console=_console(3.0)).windows[0].limit == 30.0
    for bad in ("", "abc", "0", "-5", "nan", "inf"):
        monkeypatch.setenv("MISTRAL_MONTHLY_USD", bad)
        assert _mistral(_usage_handler({})).windows[0].limit == 15.0, bad


def test_mistral_at_the_hard_stop_reads_100_and_over(sb_env):
    r = _mistral(_usage_handler({}, cost_usd=15.0))
    assert r.windows[0].used_pct == pytest.approx(100.0)
    r = _mistral(_usage_handler({}), console=_console(16.5))
    assert r.windows[0].used_pct == pytest.approx(110.0)


def test_mistral_month_boundaries(sb_env):
    seen = {}
    dec = datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc)
    r = _mistral(_usage_handler(seen), now=dec)
    assert seen["params"]["since"] == "2026-12-01T00:00:00"
    assert r.windows[0].resets_at == datetime(2027, 1, 1, tzinfo=timezone.utc)
    first = datetime(2026, 11, 1, 0, 0, 5, tzinfo=timezone.utc)
    assert _mistral(_usage_handler(seen), console=_console(14.9), now=first).windows[0].used == pytest.approx(0.0595435)


def test_mistral_switchboard_down_falls_back_to_the_console(sb_env):
    def down(req):
        raise httpx.ConnectError("refused")
    r = _mistral(down, console=_console(1.18))
    assert r.ok and r.windows[0].used == pytest.approx(1.18)
    assert r.extra["computed_usd"] is None and "switchboard" in r.extra["switchboard_error"].lower()
    both = _mistral(down, console=None)
    assert not both.ok and "Switchboard" in both.error


def test_mistral_old_switchboard_without_cost_field(sb_env):
    def old(req):
        return httpx.Response(200, json={"backend": "mistral", "calls": 13, "input_tokens": 1})
    r = _mistral(old, console=_console(1.18))
    assert r.ok and r.windows[0].used == pytest.approx(1.18) and r.extra["computed_usd"] is None
    assert not _mistral(old, console=None).ok


def test_mistral_unpriced_calls_make_the_cost_a_floor(sb_env):
    r = _mistral(_usage_handler({}, unpriced_calls=4))
    assert r.extra["unpriced_calls"] == 4 and "floor" in r.notes.lower()


def test_mistral_notes_carry_the_checked_rate_limits_not_the_old_ones(sb_env):
    r = _mistral(_usage_handler({}))
    assert len(r.notes) <= 400
    assert "800,000" in r.notes and "30/min" in r.notes and "16.67" in r.notes
    assert "500K" not in r.notes and "1,000 requests" not in r.notes
    assert "hard stop" in r.notes.lower() and r.plan and "15" in r.plan


def test_mistral_without_switchboard_url_says_so(monkeypatch):
    monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
    m = _mistral(lambda req: httpx.Response(500))
    assert not m.ok and "SWITCHBOARD_URL" in m.error and m.source_kind == "our_logs"


def test_mistral_is_scheduled_every_five_minutes():
    reader, env, interval = readers.SERVER_READERS["mistral"]
    assert reader is readers.read_mistral and env is None and interval == 300


def test_reader_never_leaks_key_in_error():
    def h(req):
        raise httpx.ConnectError("boom zk-secret-123 boom")
    r = run(readers.read_zai(client_for(h), "zk-secret-123"))
    assert not r.ok and "zk-secret-123" not in (r.error or "")


# ── API ──────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def api():
    import main
    with TestClient(main.app) as c:
        yield c


def _ingest_body(**over):
    reading = {"id": "claude_max", "name": "Claude Max", "source": "herdr on ws1", "source_kind": "client",
               "plan": "max", "ok": True, "fetched_at": NOW.isoformat(),
               "windows": [{"name": "session", "used_pct": 7, "resets_at": (NOW + timedelta(hours=4)).isoformat()},
                           {"name": "week", "used_pct": 98, "resets_at": (NOW + timedelta(hours=16)).isoformat()}]}
    reading.update(over)
    return {"source": "collector@ws1", "readings": [reading]}


def test_ingest_requires_token(api):
    assert api.post("/api/quota/ingest", json=_ingest_body()).status_code == 401
    assert api.post("/api/quota/ingest", json=_ingest_body(),
                    headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_ingest_and_read_back(api):
    r = api.post("/api/quota/ingest", json=_ingest_body(),
                 headers={"Authorization": "Bearer " + os.environ["RECKONER_INGEST_TOKEN"]})
    assert r.status_code == 200, r.text
    q = api.get("/api/quota", headers={"Authorization": "Bearer " + os.environ["RECKONER_QUOTA_TOKEN"]})
    assert q.status_code == 200
    v = {x["id"]: x for x in q.json()["vendors"]}
    assert v["claude_max"]["windows"][1]["used_pct"] == 98
    assert v["claude_max"]["source"] == "herdr on ws1"


def test_console_reading_can_be_recorded_and_feeds_the_mistral_reading(api, monkeypatch):
    # How a human records what the Mistral admin console shows: the ingest token, id mistral_console.
    hdr = {"Authorization": "Bearer " + os.environ["RECKONER_INGEST_TOKEN"]}
    now = datetime.now(timezone.utc)
    body = {"source": "console", "readings": [{
        "id": "mistral_console", "name": "Mistral admin console", "source": "Mistral admin console (entered by hand)",
        "source_kind": "client", "fetched_at": now.isoformat(),
        "windows": [{"name": "month", "used": 7.77, "limit": 15, "unit": "USD"}]}]}
    r = api.post("/api/quota/ingest", json=body, headers=hdr)
    assert r.status_code == 200, r.text
    monkeypatch.setenv("SWITCHBOARD_URL", "http://switchboard.invalid:8800")
    monkeypatch.delenv("MISTRAL_MONTHLY_USD", raising=False)
    seen = {}
    reading = run(readers.read_mistral(client_for(_usage_handler(seen))))  # console loaded from the store
    assert reading.ok and reading.windows[0].used == pytest.approx(7.77)
    assert reading.extra["console_usd"] == 7.77
    # the mistral card itself is server-read: a client still cannot overwrite it
    over = dict(body, readings=[dict(body["readings"][0], id="mistral")])
    assert api.post("/api/quota/ingest", json=over, headers=hdr).status_code == 422


def test_ingest_cannot_overwrite_a_server_read_vendor(api):
    r = api.post("/api/quota/ingest", json=_ingest_body(id="zai", name="Z.ai"),
                 headers={"Authorization": "Bearer " + os.environ["RECKONER_INGEST_TOKEN"]})
    assert r.status_code == 422


def test_ingest_validates_shape(api):
    hdr = {"Authorization": "Bearer " + os.environ["RECKONER_INGEST_TOKEN"]}
    assert api.post("/api/quota/ingest", json=_ingest_body(windows=[{"name": "x", "used_pct": "lots"}]),
                    headers=hdr).status_code == 422
    assert api.post("/api/quota/ingest", json=_ingest_body(id="BAD ID!"), headers=hdr).status_code == 422
    big = {"source": "x", "readings": [_ingest_body()["readings"][0]] * 200}
    assert api.post("/api/quota/ingest", json=big, headers=hdr).status_code in (413, 422)


def test_quota_read_needs_token_or_login(api):
    assert api.get("/api/quota").status_code == 401
    assert api.get("/api/quota", headers={"Authorization": "Bearer nope"}).status_code == 401


# A credential is required on the quota reads and on refresh whenever a read token or a dashboard
# password is configured, whatever RECKONER_PROTECT_DASHBOARD says.

def test_quota_reads_need_a_credential_with_dashboard_protection_off(api, monkeypatch):
    import auth
    from quota import router as qr
    monkeypatch.setattr(auth, "protect_dashboard", False)
    monkeypatch.setattr(qr, "protect_dashboard", False, raising=False)
    assert api.get("/api/quota").status_code == 401
    assert api.get("/api/quota/history/zai").status_code == 401
    ok = api.get("/api/quota", headers={"Authorization": "Bearer " + os.environ["RECKONER_QUOTA_TOKEN"]})
    assert ok.status_code == 200


def test_quota_reads_need_the_read_token_with_auth_off(api, monkeypatch):
    from quota import router as qr
    monkeypatch.setattr(qr, "auth_enabled", False)
    assert api.get("/api/quota").status_code == 401
    assert api.get("/api/quota/history/zai").status_code == 401
    assert api.post("/api/quota/refresh").status_code == 401
    hdr = {"Authorization": "Bearer " + os.environ["RECKONER_QUOTA_TOKEN"]}
    assert api.get("/api/quota", headers=hdr).status_code == 200
    assert api.get("/api/quota/history/zai", headers=hdr).status_code == 200


def test_quota_open_only_when_nothing_is_configured(api, monkeypatch):
    from quota import router as qr
    monkeypatch.setattr(qr, "auth_enabled", False)
    monkeypatch.setattr(qr, "_READ_TOKEN", "")
    assert api.get("/api/quota").status_code == 200


def test_refresh_needs_login_when_auth_is_on(api):
    assert api.post("/api/quota/refresh").status_code == 401
    assert api.post("/api/quota/refresh", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_non_ascii_authorization_is_401_not_500(api):
    hdr = {"Authorization": "Bearer t\u00f6ken-\u00fc".encode("utf-8")}
    assert api.get("/api/quota", headers=hdr).status_code == 401
    assert api.get("/api/quota/history/zai", headers=hdr).status_code == 401
    assert api.post("/api/quota/refresh", headers=hdr).status_code == 401
    assert api.post("/api/quota/ingest", json=_ingest_body(), headers=hdr).status_code == 401


def test_non_ascii_login_password_is_401_not_500(api):
    r = api.post("/api/auth/login", json={"password": "p\u00e4sswort"})
    assert r.status_code == 401


def test_stale_reading_is_flagged(api):
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    api.post("/api/quota/ingest", json=_ingest_body(id="cursor", name="Cursor", fetched_at=old, stale_after_s=900),
             headers={"Authorization": "Bearer " + os.environ["RECKONER_INGEST_TOKEN"]})
    v = {x["id"]: x for x in api.get("/api/quota", headers={
        "Authorization": "Bearer " + os.environ["RECKONER_QUOTA_TOKEN"]}).json()["vendors"]}
    assert v["cursor"]["stale"] is True


def test_history_is_kept(api):
    rows = run(store.history("claude_max", hours=24))
    assert rows and rows[-1]["windows"][1]["used_pct"] == 98


# ── scheduler ────────────────────────────────────────────────────────────────

def test_balance_scheduler_respects_each_provider_interval():
    import scheduler
    last = {"openrouter": NOW - timedelta(seconds=30), "ollama_cloud": NOW - timedelta(seconds=30)}
    due = scheduler.due_providers({"openrouter": 300, "ollama_cloud": 600, "zai": 300}, last, NOW)
    assert due == ["zai"]  # never-refreshed only; the others aren't due (was: everything every 30 s)
    due_later = scheduler.due_providers({"openrouter": 300, "ollama_cloud": 600}, last, NOW + timedelta(seconds=280))
    assert due_later == ["openrouter"]


def test_dashboard_login_token_reads_quota(api):
    # The Plan usage panel sends the dashboard login token (frontend api.ts request()).
    tok = api.post("/api/auth/login", json={"password": os.environ["RECKONER_PASSWORD"]}).json()["token"]
    hdr = {"Authorization": "Bearer " + tok}
    assert api.get("/api/quota", headers=hdr).status_code == 200
    assert api.get("/api/quota/history/zai", headers=hdr).status_code == 200
