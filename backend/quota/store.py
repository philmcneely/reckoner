"""Storage for plan-usage readings: the latest reading per vendor plus 30 days of history."""

import json
from datetime import datetime, timedelta, timezone

from models.database import get_db
from quota.models import QuotaReading

HISTORY_DAYS = 30


async def init_quota_tables() -> None:
    async with get_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS quota_latest (
                vendor TEXT PRIMARY KEY,
                reading TEXT NOT NULL,
                fetched_at TEXT,
                stored_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS quota_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vendor TEXT NOT NULL,
                reading TEXT NOT NULL,
                stored_at TEXT NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_quota_history_vendor ON quota_history(vendor, stored_at)")
        await db.commit()


async def save(reading: QuotaReading) -> None:
    """Store a reading. A failed read never replaces a good one's windows: the last good windows are
    kept and the error is recorded beside them, so the dashboard shows the last known numbers as
    'last good' rather than going blank."""
    await init_quota_tables()
    now = datetime.now(timezone.utc).isoformat()
    doc = json.loads(reading.model_dump_json())
    async with get_db() as db:
        if not reading.ok:
            async with db.execute("SELECT reading FROM quota_latest WHERE vendor = ?", (reading.id,)) as cur:
                row = await cur.fetchone()
            if row:
                prev = json.loads(row[0])
                if prev.get("ok") or prev.get("last_good"):
                    good = prev if prev.get("ok") else prev.get("last_good")
                    doc["last_good"] = {k: good.get(k) for k in ("windows", "fetched_at", "plan", "money", "extra")}
        await db.execute(
            "INSERT INTO quota_latest (vendor, reading, fetched_at, stored_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(vendor) DO UPDATE SET reading = excluded.reading, fetched_at = excluded.fetched_at, "
            "stored_at = excluded.stored_at",
            (reading.id, json.dumps(doc), doc.get("fetched_at"), now))
        if reading.ok:
            await db.execute("INSERT INTO quota_history (vendor, reading, stored_at) VALUES (?, ?, ?)",
                             (reading.id, json.dumps(doc), now))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).isoformat()
        await db.execute("DELETE FROM quota_history WHERE stored_at < ?", (cutoff,))
        await db.commit()


def _stale(doc, now):
    fetched = doc.get("fetched_at")
    if not fetched:
        return True
    try:
        ts = datetime.fromisoformat(str(fetched).replace("Z", "+00:00"))
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() > int(doc.get("stale_after_s") or 1800)


async def latest_all() -> list[dict]:
    await init_quota_tables()
    now = datetime.now(timezone.utc)
    async with get_db() as db:
        async with db.execute("SELECT reading, stored_at FROM quota_latest ORDER BY vendor") as cur:
            rows = await cur.fetchall()
    out = []
    for reading, stored_at in rows:
        doc = json.loads(reading)
        doc["stale"] = _stale(doc, now)
        doc["stored_at"] = stored_at
        out.append(doc)
    return out


async def history(vendor: str, hours: int = 168) -> list[dict]:
    await init_quota_tables()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    async with get_db() as db:
        async with db.execute("SELECT reading FROM quota_history WHERE vendor = ? AND stored_at >= ? ORDER BY id",
                              (vendor, cutoff)) as cur:
            rows = await cur.fetchall()
    return [json.loads(r[0]) for r in rows]
