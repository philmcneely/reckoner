"""
NanoGPT provider - fetches wallet balance via the official check-balance endpoint.

Endpoint:
  POST https://nano-gpt.com/api/check-balance
    Header: x-api-key: <api-key>
    → usd_balance            (USD wallet balance, string)
    → usd_pending_usage      (USD usage not yet settled, nullable)
    → usd_spendable_balance  (USD immediately spendable, nullable — falls
                               back to usd_balance when null)
    → nano_balance           (Nano cryptocurrency balance, string)
    → nanoDepositAddress     (Nano deposit address for topping up)

NanoGPT is a prepaid, pay-per-use provider (top up a wallet, usage is
deducted from it) rather than a subscription with a fixed credit pool, so
there is no separate "total credits" figure — usd_balance/usd_spendable_balance
both represent current spendable funds.
"""

import logging
import httpx

from models.schemas import BalanceSnapshot
from providers.base import BaseProvider

logger = logging.getLogger(__name__)

CHECK_BALANCE_ENDPOINT = "https://nano-gpt.com/api/check-balance"


class NanoGPTProvider(BaseProvider):
    """NanoGPT wallet balance provider."""

    provider_id   = "nanogpt"
    provider_name = "NanoGPT"
    auth_type     = "api_key"

    def is_configured(self) -> bool:
        return bool(self.credentials.api_key)

    async def fetch_balance(self) -> BalanceSnapshot:
        """Fetch wallet balance from NanoGPT's check-balance endpoint."""
        if not self.is_configured():
            return self._unconfigured_snapshot()

        client  = await self.get_client()
        headers = {"x-api-key": self.credentials.api_key}

        try:
            resp = await client.post(CHECK_BALANCE_ENDPOINT, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                return self._error_snapshot("Invalid API key")
            return self._error_snapshot(f"HTTP {e.response.status_code}: {e.response.text[:200]}")
        except httpx.RequestError as e:
            return self._error_snapshot(f"Network error: {str(e)}")
        except Exception as e:
            return self._error_snapshot(f"Unexpected error: {str(e)}")

        def _to_float(value):
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        usd_balance           = _to_float(data.get("usd_balance"))
        usd_spendable_balance = _to_float(data.get("usd_spendable_balance"))
        nano_balance          = _to_float(data.get("nano_balance"))

        # Prefer the explicit spendable figure; fall back to the raw wallet
        # balance when NanoGPT doesn't report one separately.
        remaining = usd_spendable_balance if usd_spendable_balance is not None else usd_balance

        return self._make_snapshot(
            balance_usd       = usd_balance,
            total_credits      = None,
            used_credits       = None,
            remaining_credits  = remaining,
            currency           = "USD",
            raw_data           = {
                "usd_balance":            data.get("usd_balance"),
                "usd_pending_usage":      data.get("usd_pending_usage"),
                "usd_pending_usage_status": data.get("usd_pending_usage_status"),
                "usd_spendable_balance":  data.get("usd_spendable_balance"),
                "nano_balance":           nano_balance,
                "nanoDepositAddress":     data.get("nanoDepositAddress"),
            },
        )
