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


def test_our_logs_readers_are_labelled(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_URL", "http://switchboard.invalid:8800")

    def h(req):
        assert "backend=mistral" in str(req.url) or "backend=ai_studio" in str(req.url)
        return httpx.Response(200, json={"calls": 7, "ok": 6, "input_tokens": 1000, "output_tokens": 50,
                                         "cache_read_tokens": 0, "last_ts": "2026-10-18T20:00:00", "by_machine": {"box-a": 7}})
    m = run(readers.read_mistral(client_for(h)))
    assert m.source_kind == "our_logs" and m.ok and m.windows == []
    assert "per-minute" in m.notes and m.extra["calls_7d"] == 7
    g = run(readers.read_gemini_api(client_for(h)))
    assert g.source_kind == "our_logs" and "no usage api" in g.notes.lower()


def test_our_logs_without_switchboard_url_says_so(monkeypatch):
    monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
    m = run(readers.read_mistral(client_for(lambda req: httpx.Response(500))))
    assert not m.ok and "SWITCHBOARD_URL" in m.error and m.source_kind == "our_logs"


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
