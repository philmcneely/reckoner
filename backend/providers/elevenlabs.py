"""
ElevenLabs provider — https://elevenlabs.io

TTS character credits. The subscription endpoint reports the character
allowance for the current billing period and how much has been used.

Endpoint:
  GET https://api.elevenlabs.io/v1/user/subscription   (header: xi-api-key)
    → character_count, character_limit, tier, status,
      next_character_count_reset_unix
"""

import logging
from datetime import datetime, timezone

import httpx

from models.schemas import BalanceSnapshot
from providers.base import BaseProvider

logger = logging.getLogger(__name__)

SUBSCRIPTION_ENDPOINT = "https://api.elevenlabs.io/v1/user/subscription"


class ElevenLabsProvider(BaseProvider):
    """ElevenLabs TTS character-credit provider."""

    provider_id = "elevenlabs"
    provider_name = "ElevenLabs"
    auth_type = "api_key"

    def is_configured(self) -> bool:
        return bool(self.credentials.api_key)

    async def fetch_balance(self) -> BalanceSnapshot:
        if not self.is_configured():
            return self._unconfigured_snapshot()

        client = await self.get_client()
        headers = {"xi-api-key": self.credentials.api_key}

        try:
            r = await client.get(SUBSCRIPTION_ENDPOINT, headers=headers)
            r.raise_for_status()
            d = r.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                return self._error_snapshot("Invalid API key")
            return self._error_snapshot(
                f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            )
        except httpx.RequestError as e:
            return self._error_snapshot(f"Network error: {str(e)}")
        except Exception as e:
            return self._error_snapshot(f"Unexpected error: {str(e)}")

        used = d.get("character_count")
        limit = d.get("character_limit")
        remaining = (
            (limit - used) if (limit is not None and used is not None) else None
        )
        reset = d.get("next_character_count_reset_unix")

        raw = {
            "tier": d.get("tier"),
            "status": d.get("status"),
            "character_count": used,
            "character_limit": limit,
            "next_reset": (
                datetime.fromtimestamp(reset, tz=timezone.utc).isoformat()
                if reset
                else None
            ),
        }

        return self._make_snapshot(
            total_credits=limit,
            used_credits=used,
            remaining_credits=remaining,
            currency="chars",
            raw_data=raw,
        )
