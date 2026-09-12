"""
PGSGrove (Phoenix Grove) provider — https://api.pgsgrove.com

Flat-monthly "banked full-speed time" plan: there is no USD balance. The
account usage endpoint reports how much of the weekly/daily allowance has
been consumed (as percentages) plus when the weekly window resets.

Endpoint:
  GET https://api.pgsgrove.com/v1/usage
    → weekly_used_percent, daily_used_percent,
      api_share_of_weekly_percent, weekly_resets_at

NOTE: PGSGrove's Cloudflare WAF rejects clients that don't look like a real
HTTP client (e.g. python-urllib's default UA), so we send a curl User-Agent.
"""

import logging

import httpx

from models.schemas import BalanceSnapshot
from providers.base import BaseProvider

logger = logging.getLogger(__name__)

USAGE_ENDPOINT = "https://api.pgsgrove.com/v1/usage"


class PGSGroveProvider(BaseProvider):
    """PGSGrove flat-plan usage provider (percentage of weekly allowance)."""

    provider_id = "pgsgrove"
    provider_name = "PGSGrove"
    auth_type = "api_key"

    def is_configured(self) -> bool:
        return bool(self.credentials.api_key)

    async def fetch_balance(self) -> BalanceSnapshot:
        if not self.is_configured():
            return self._unconfigured_snapshot()

        client = await self.get_client()
        headers = {
            "Authorization": f"Bearer {self.credentials.api_key}",
            "User-Agent": "curl/8.4.0",  # PGSGrove WAF blocks default UAs
        }

        try:
            r = await client.get(USAGE_ENDPOINT, headers=headers)
            r.raise_for_status()
            d = r.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                return self._error_snapshot("Invalid API key")
            return self._error_snapshot(
                f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            )
        except httpx.RequestError as e:
            return self._error_snapshot(f"Network error: {str(e)}")
        except Exception as e:
            return self._error_snapshot(f"Unexpected error: {str(e)}")

        weekly_used = d.get("weekly_used_percent")
        remaining = round(100.0 - weekly_used, 1) if weekly_used is not None else None

        raw = {
            "plan": "flat monthly — banked full-speed time (never hard-stops)",
            "weekly_used_percent": weekly_used,
            "daily_used_percent": d.get("daily_used_percent"),
            "api_share_of_weekly_percent": d.get("api_share_of_weekly_percent"),
            "weekly_resets_at": d.get("weekly_resets_at"),
        }

        return self._make_snapshot(
            total_credits=100.0 if weekly_used is not None else None,
            used_credits=weekly_used,
            remaining_credits=remaining,
            currency="% wk",
            raw_data=raw,
        )
