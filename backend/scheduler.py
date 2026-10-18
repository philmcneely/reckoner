"""
Background scheduler for auto-refreshing provider balances.
Uses APScheduler to periodically fetch balances from all configured providers.
"""

import asyncio
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config_manager import get_all_provider_configs, save_balance_snapshot
from providers import PROVIDER_REGISTRY

logger = logging.getLogger(__name__)

_scheduler = AsyncIOScheduler()
_last_refresh: dict[str, datetime] = {}


async def refresh_provider(provider_id: str) -> None:
    """Fetch and cache balance for a single provider."""
    try:
        from config_manager import get_provider_config
        config = await get_provider_config(provider_id)
        if not config or not config.enabled:
            return

        meta = PROVIDER_REGISTRY.get(provider_id)
        if not meta:
            return

        provider_class = meta["class"]
        provider = provider_class(config.credentials)
        # Count the attempt even if the fetch raises, so a failing provider waits its interval
        # instead of being retried on every 30 s tick.
        _last_refresh[provider_id] = datetime.now(timezone.utc)

        logger.info(f"Refreshing balance for: {config.name}")
        snapshot = await provider.fetch_balance()
        await save_balance_snapshot(snapshot)

        _last_refresh[provider_id] = datetime.now(timezone.utc)
        logger.info(f"Balance refreshed for {config.name}: status={snapshot.status}")

    except Exception as e:
        logger.error(f"Error refreshing provider {provider_id}: {e}")


async def refresh_all_providers() -> None:
    """Refresh balances for all enabled providers (manual "refresh all")."""
    configs = await get_all_provider_configs()
    tasks = [
        refresh_provider(config.id)
        for config in configs
        if config.enabled
    ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def due_providers(intervals: dict[str, int], last: dict[str, datetime], now: datetime) -> list[str]:
    """Providers whose own refresh_interval has passed since their last refresh (never-refreshed
    ones are due). The tick used to refresh EVERY provider every 30 s regardless of its interval:
    ~2,900 snapshots a day per provider and vendor rate limits (Ollama answered 429)."""
    out = []
    for pid, interval in intervals.items():
        prev = last.get(pid)
        if prev is None or (now - prev).total_seconds() >= max(60, int(interval or 300)):
            out.append(pid)
    return sorted(out)


async def refresh_due_providers() -> None:
    configs = await get_all_provider_configs()
    intervals = {c.id: c.refresh_interval for c in configs if c.enabled}
    due = due_providers(intervals, _last_refresh, datetime.now(timezone.utc))
    if due:
        await asyncio.gather(*(refresh_provider(pid) for pid in due), return_exceptions=True)


async def refresh_quota() -> None:
    from quota.jobs import run_server_readers
    try:
        await run_server_readers()
    except Exception as e:  # noqa: BLE001
        logger.error(f"quota readers failed: {type(e).__name__}")


def start_scheduler() -> None:
    """Start the background refresh scheduler."""
    # Run an initial refresh shortly after startup
    _scheduler.add_job(
        refresh_due_providers,
        trigger=IntervalTrigger(seconds=30),
        id="balance_refresh",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        refresh_quota,
        trigger=IntervalTrigger(seconds=30),
        id="quota_refresh",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("Background scheduler started")


def stop_scheduler() -> None:
    """Stop the background scheduler."""
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Background scheduler stopped")


def get_last_refresh_times() -> dict[str, str]:
    """Get the last refresh time for each provider."""
    return {
        pid: ts.isoformat()
        for pid, ts in _last_refresh.items()
    }
