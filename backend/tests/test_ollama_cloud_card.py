"""Ollama Cloud balance card (2026-10-19).

Since 2026-10-17 Ollama's usage API answers request counts only ({range, scope, totals, buckets}); the
weekly/session limit fractions are gone. The card used to turn that into the error "Ollama usage
response missing limits.weekly.usage". It now shows the request count, and still shows the weekly
percentage should the limits come back.
"""
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.schemas import ProviderCredentials  # noqa: E402
from providers.ollama_cloud import OllamaCloudProvider  # noqa: E402

COUNTS = {"range": "week", "scope": "account",
          "totals": {"request_count": 1674},
          "buckets": [{"start": "2026-10-13T00:00:00Z", "request_count": 200}]}
LIMITS = {"limits": {"weekly": {"usage": 0.227}, "session": {"usage": 0.002}},
          "activity": {"cost": "0.00000"}}


def fetch(handler):
    p = OllamaCloudProvider(ProviderCredentials(api_key="k"))
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)
    return asyncio.run(p.fetch_balance())


def test_counts_only_answer_shows_requests_not_an_error():
    s = fetch(lambda req: httpx.Response(200, json=COUNTS))
    assert s.status == "ok", s.error_message
    assert s.used_credits == 1674
    assert s.currency == "requests"
    assert s.total_credits is None and s.remaining_credits is None  # no limit published, no percentage
    assert s.raw_data["request_count"] == 1674 and s.raw_data["range"] == "week"
    assert "counts only" in s.raw_data["note"]


def test_limits_shape_still_shows_weekly_percentage():
    s = fetch(lambda req: httpx.Response(200, json=LIMITS))
    assert s.status == "ok"
    assert s.currency == "% weekly quota"
    assert s.used_credits == 22.7 and s.remaining_credits == 77.3
    assert s.raw_data["session_used_pct"] == 0.2


def test_rate_limited_read_is_a_short_error():
    s = fetch(lambda req: httpx.Response(429, text="slow down " + "x" * 500))
    assert s.status == "error"
    assert s.error_message.startswith("HTTP 429")
    assert len(s.error_message) < 120


def test_answer_with_neither_shape_is_an_error():
    s = fetch(lambda req: httpx.Response(200, json={"something": "else"}))
    assert s.status == "error"


def test_quota_reader_refreshes_well_inside_the_proxy_staleness_limit():
    # The proxies hold an Ollama reading without a reset time for 15 minutes after Reckoner read it;
    # a 15-minute read interval left the gate open part of every cycle should limits come back.
    from quota.readers import SERVER_READERS
    assert SERVER_READERS["ollama_cloud"][2] <= 300


def test_non_finite_or_negative_count_is_an_error_not_an_exception():
    for bad in ('NaN', 'Infinity', '-5'):
        body = '{"range": "week", "totals": {"request_count": %s}}' % bad
        s = fetch(lambda req, b=body: httpx.Response(200, content=b.encode(), headers={"content-type": "application/json"}))
        assert s.status == "error", bad


def test_range_is_only_kept_when_known():
    s = fetch(lambda req: httpx.Response(200, json=dict(COUNTS, range="<script>" + "x" * 5000)))
    assert s.status == "ok" and s.raw_data["range"] is None
    s = fetch(lambda req: httpx.Response(200, json=dict(COUNTS, range="day")))
    assert s.raw_data["range"] == "day"
    s = fetch(lambda req: httpx.Response(200, json=dict(COUNTS, range="7d")))  # what Ollama sends (2026-10-19)
    assert s.raw_data["range"] == "7d"


def test_bad_limit_fraction_is_an_error_not_an_exception():
    for bad in ('"lots"', 'NaN', '{"x": 1}'):
        body = '{"limits": {"weekly": {"usage": %s}}}' % bad
        s = fetch(lambda req, b=body: httpx.Response(200, content=b.encode(), headers={"content-type": "application/json"}))
        assert s.status == "error", bad
