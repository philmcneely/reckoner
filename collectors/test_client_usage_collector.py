"""Client usage collector: asks each client that knows its own plan usage, pushes to Reckoner.

Run: python3 -m pytest -q collectors/test_client_usage_collector.py   (no network, no real clients)
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import client_usage_collector as C  # noqa: E402

HERDR_USAGE = {"providers": [
    {"id": "claude", "key": "claude:x", "account": "someone@example.com", "plan": "max", "problem": None,
     "checked_at": "2026-10-18T23:00:12.377Z", "windows": [
         {"kind": "session", "scope": None, "used_percent": 7, "resets_at": "2026-10-19T02:50:00.055Z",
          "starts_at": "2026-10-18T21:50:00.055Z"},
         {"kind": "week", "scope": None, "used_percent": 98, "resets_at": "2026-10-19T15:00:00.055Z"},
         {"kind": "week", "scope": "Fable", "used_percent": 0, "resets_at": "2026-10-19T15:00:00.000Z"}]},
    {"id": "copilot", "plan": "individual_pro", "problem": None, "checked_at": "2026-10-18T23:00:12.377Z",
     "windows": [{"kind": "month", "scope": "Premium", "used_percent": 52.6, "resets_at": "2026-11-01T00:00:00.000Z"}]},
    {"id": "cursor", "plan": "free", "problem": "token expired", "checked_at": "2026-10-18T23:00:12.377Z", "windows": []},
    {"id": "opencode", "plan": "Go", "windows": [{"kind": "week", "used_percent": 15}]},  # read server-side instead
]}


def test_herdr_mapping():
    readings = C.from_herdr_usage(HERDR_USAGE)
    by = {r["id"]: r for r in readings}
    assert sorted(by) == ["claude_max", "copilot", "cursor"]  # opencode is read from its own API by Reckoner
    claude = by["claude_max"]
    assert claude["source"] == f"herdr on {C.HOST}" and claude["source_kind"] == "client" and claude["plan"] == "max"
    assert [(w["name"], w["used_pct"]) for w in claude["windows"]] == [("session", 7), ("week", 98), ("week (Fable)", 0)]
    assert "account" not in json.dumps(claude) or "someone@example.com" not in json.dumps(claude)  # no account names
    assert by["cursor"]["ok"] is False and "token expired" in by["cursor"]["error"]


ANTIGRAVITY = {"timestamp": "2026-10-18T23:08:38.093Z", "method": "google", "email": "x@example.com", "models": [
    {"label": "Claude Opus 5.5 (High)", "remainingPercentage": 0.4, "resetTime": "2026-10-19T04:08:38Z", "isAutocompleteOnly": False},
    {"label": "Claude Sonnet 5.5 (Low)", "remainingPercentage": 0.9, "resetTime": "2026-10-19T04:08:38Z", "isAutocompleteOnly": False},
    {"label": "Gemini 3.1 Pro (High)", "remainingPercentage": 1, "resetTime": "2026-10-19T04:08:38Z", "isAutocompleteOnly": False},
    {"label": "Gemini 3 Flash", "remainingPercentage": 0.75, "resetTime": "2026-10-19T03:00:00Z", "isAutocompleteOnly": False},
    {"label": "Gemini 2.5 Pro", "remainingPercentage": 0.0, "resetTime": "2026-10-19T04:08:38Z", "isAutocompleteOnly": True}]}


def test_antigravity_groups():
    r = C.from_antigravity(ANTIGRAVITY)
    w = {x["name"]: x for x in r["windows"]}
    assert w["Claude models"]["used_pct"] == 60.0  # the fullest model in the group
    assert w["Gemini Pro"]["used_pct"] == 0.0  # autocomplete-only models ignored
    assert w["Gemini Flash"]["used_pct"] == 25.0
    assert r["source"] == f"Antigravity CLI on {C.HOST}" and "x@example.com" not in json.dumps(r)


CLINE = {"quota": {"planName": "Cline Pass (Monthly)", "isActive": True, "fetchedAt": "2026-10-16T04:51:04.631Z",
                   "periodEnd": "2026-11-05T13:41:21Z", "windows": [
                       {"window": "5h", "hours": 5, "usedUsd": 0.28, "capUsd": 20, "usedPct": 1.4, "nextRolloff": "2026-10-16T08:59:26.153Z"},
                       {"window": "7d", "hours": 168, "usedUsd": 0.83, "capUsd": 50, "usedPct": 1.67},
                       {"window": "30d", "hours": 720, "usedUsd": 0.83, "capUsd": 100, "usedPct": 0.83}]},
         "cached": True, "stale": True, "ageSeconds": 240740, "error": "TOKEN_PREFIX is not defined"}


def test_cline_uses_the_real_read_time():
    r = C.from_cline(CLINE)
    assert r["fetched_at"] == "2026-10-16T04:51:04.631Z"  # cached numbers keep their own age -> shown stale
    assert [(w["name"], w["used_pct"], w["used"], w["limit"], w["unit"]) for w in r["windows"]][0] == ("5h", 1.4, 0.28, 20, "USD")
    assert r["error"] and "TOKEN_PREFIX" in r["error"] and r["ok"] is True  # numbers are real, refresh failing


def test_muse_usage_read():
    r = C.from_muse({"usage": {"window": {"usedPercent": 3, "windowDurationMins": 300, "resetsAtMs": 1792382686000},
                               "weekly": {"usedPercent": 12, "resetsAtMs": 1792368000000},
                               "tier": "27681527378179523", "observedAtMs": 1792364688097}})
    assert [(w["name"], w["used_pct"]) for w in r["windows"]] == [("5h", 3), ("weekly", 12)]
    assert r["windows"][0]["resets_at"].startswith("2026-")
    assert r["stale_after_s"] >= 7200  # probed hourly


def test_muse_nothing_observed_is_not_ok():
    r = C.from_muse({})
    assert r["ok"] is False


OPENCODE = {"range": {"from": 1791781200000, "to": 1792364898252}, "sessions": 14, "prompts": 46,
            "tokens": {"input": 498484, "output": 40608, "reasoning": 33386, "cache": {"read": 7450817, "write": 150525}},
            "cost": 0.531478445, "models": [
                {"model": {"providerID": "nanogpt", "id": "deepseek-v4"}, "steps": 49,
                 "tokens": {"input": 10, "output": 5}, "cost": 0.0659},
                {"model": {"providerID": "opencode-go", "id": "qwen3.8-flash"}, "steps": 66,
                 "tokens": {"input": 20, "output": 6}, "cost": 0.0856}]}


def test_opencode_stats():
    r = C.from_opencode_stats(OPENCODE)
    assert r["id"] == "opencode_cli" and r["money"]["cost_7d_usd"] == 0.5315
    assert r["extra"]["by_provider"]["nanogpt"]["steps"] == 49
    assert r["extra"]["tokens_7d"]["input"] == 498484


def test_push_payload_and_auth(monkeypatch):
    sent = {}

    def fake_post(url, body, token, timeout):
        sent.update(url=url, body=json.loads(body), token=token)
        return 200, b'{"stored": []}'
    monkeypatch.setattr(C, "_post", fake_post)
    monkeypatch.setenv("RECKONER_INGEST_TOKEN", "tok-1234567890")
    code = C.push([{"id": "claude_max", "name": "Claude Max", "source": "herdr on ws1", "source_kind": "client",
                    "ok": True, "windows": []}], url="http://r.invalid/api/quota/ingest")
    assert code == 200 and sent["token"] == "tok-1234567890"
    assert sent["body"]["source"] == f"collector@{C.HOST}" and sent["body"]["readings"][0]["id"] == "claude_max"


def test_push_refuses_without_url_or_token(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("RECKONER_INGEST_URL", raising=False)
    monkeypatch.setenv("RECKONER_INGEST_TOKEN", "tok-1234567890")
    try:
        C.push([])
    except RuntimeError as e:
        assert "RECKONER_INGEST_URL" in str(e)
    else:
        raise AssertionError("push without a URL must refuse")


def test_settings_come_from_the_env_file(monkeypatch, tmp_path):
    f = tmp_path / "collector.env"
    f.write_text("RECKONER_INGEST_URL=http://r.invalid/api/quota/ingest\nRECKONER_INGEST_TOKEN='tok-abc'\n")
    monkeypatch.setattr(C, "ENV_FILE", str(f))
    monkeypatch.delenv("RECKONER_INGEST_URL", raising=False)
    monkeypatch.delenv("RECKONER_INGEST_TOKEN", raising=False)
    assert C._setting("RECKONER_INGEST_URL") == "http://r.invalid/api/quota/ingest"
    assert C._setting("RECKONER_INGEST_TOKEN") == "tok-abc"


def test_a_failing_reader_does_not_stop_the_others(monkeypatch):
    monkeypatch.setattr(C, "read_herdr", lambda: (_ for _ in ()).throw(RuntimeError("bridge down")))
    monkeypatch.setattr(C, "read_antigravity", lambda: C.from_antigravity(ANTIGRAVITY))
    monkeypatch.setattr(C, "read_cline", lambda: None)
    monkeypatch.setattr(C, "read_muse", lambda: None)
    monkeypatch.setattr(C, "read_opencode_stats", lambda: None)
    readings, errors = C.collect()
    assert [r["id"] for r in readings] == ["antigravity"] and "herdr" in errors[0]


# ── error text from CLIs we don't control is scrubbed before it is sent ─────
# herdr "problem" and cline "error" can carry an account name, an email or a token.

LEAKY = ("auth failed for someone@example.com (acct:me@corp): bearer "
         "sk-ant-api03_AbCdEf0123456789xyz.QwErTy rejected")


def test_scrub_masks_emails_and_token_like_strings():
    out = C._scrub(LEAKY)
    assert "@" not in out
    assert "someone" not in out and "corp" not in out
    assert "sk-ant-api03_AbCdEf0123456789xyz.QwErTy" not in out and "AbCdEf0123456789" not in out
    assert out.startswith("auth failed for ") and "rejected" in out


def test_scrub_keeps_short_plain_text_and_bounds_length():
    assert C._scrub("token expired") == "token expired"
    assert C._scrub("HTTP 401: unauthorized.") == "HTTP 401: unauthorized."
    assert len(C._scrub("x " * 1000)) <= 300
    assert C._scrub(None) is None and C._scrub("") is None
    assert "\x1b" not in C._scrub("bad\x1b[31m colour")


def test_herdr_problem_is_scrubbed():
    doc = {"providers": [{"id": "cursor", "plan": "free", "problem": LEAKY, "windows": []}]}
    err = C.from_herdr_usage(doc)[0]["error"]
    assert "@" not in err and "AbCdEf0123456789" not in err and err.startswith("auth failed")


def test_cline_error_is_scrubbed():
    err = C.from_cline({"error": LEAKY, "quota": {}})["error"]
    assert err.startswith("refresh failing: auth failed")
    assert "@" not in err and "AbCdEf0123456789" not in err


def test_scrub_is_fast_on_huge_input():
    t0 = time.time()
    out = C._scrub("a" * 500_000 + " x@y")
    assert time.time() - t0 < 0.5 and len(out) <= 300
