"""Scheduler hook for the server-side plan-usage readers: each vendor at its own interval, with
back-off after failures (a 429 from a vendor is answered by reading it less often, not more)."""

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

from quota import store
from quota.readers import SERVER_READERS

logger = logging.getLogger(__name__)

_last_run: dict[str, datetime] = {}
_fails: dict[str, int] = {}


def due(vendor: str, interval_s: int, now: datetime) -> bool:
    last = _last_run.get(vendor)
    if last is None:
        return True
    wait = interval_s * (2 ** min(_fails.get(vendor, 0), 4))  # up to 16x the interval after failures
    return (now - last) >= timedelta(seconds=wait)


async def run_server_readers(force: bool = False) -> list[str]:
    now = datetime.now(timezone.utc)
    done = []
    async with httpx.AsyncClient(timeout=15) as client:
        for vendor, (reader, key_env, interval) in SERVER_READERS.items():
            if not force and not due(vendor, interval, now):
                continue
            key = os.environ.get(key_env, "").strip() if key_env else None
            if key_env and not key:
                continue
            try:
                reading = await (reader(client, key) if key_env else reader(client))
            except Exception as e:  # noqa: BLE001 — readers don't raise, but never let one stop the rest
                logger.error("quota reader %s crashed: %s", vendor, type(e).__name__)
                continue
            _last_run[vendor] = now
            _fails[vendor] = 0 if reading.ok else _fails.get(vendor, 0) + 1
            await store.save(reading)
            done.append(vendor)
            if not reading.ok:
                logger.warning("quota %s: %s", vendor, reading.error)
    return done
