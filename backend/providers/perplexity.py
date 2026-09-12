"""
Perplexity (Sonar API) provider.

Perplexity's API (api.perplexity.ai) does not expose a credits/balance
endpoint. The only documented endpoints are the chat-completions endpoints
(/chat/completions, /router/v1/chat/completions) and the OpenAI-compatible
/v1/models listing — none of which return account balance, wallet, or
usage-remaining data. Credit balance is only visible in the web console at
console.perplexity.ai (Settings → API).

This provider therefore validates the API key against the lightweight,
no-token-cost GET /v1/models endpoint and reports key validity, mirroring
the gemini.py key-validity pattern.

Auth: PERPLEXITY_API_KEY (from console.perplexity.ai/settings/api)
"""

import logging
import httpx

from models.schemas import BalanceSnapshot
from providers.base import BaseProvider

logger = logging.getLogger(__name__)

MODELS_ENDPOINT = "https://api.perplexity.ai/v1/models"


class PerplexityProvider(BaseProvider):
    """Perplexity (Sonar API) provider — key validation only, no balance API."""

    provider_id   = "perplexity"
    provider_name = "Perplexity"
    auth_type     = "api_key"

    def is_configured(self) -> bool:
        return bool(self.credentials.api_key)

    async def fetch_balance(self) -> BalanceSnapshot:
        if not self.is_configured():
            return self._unconfigured_snapshot()

        client  = await self.get_client()
        headers = {"Authorization": f"Bearer {self.credentials.api_key}"}

        try:
            resp = await client.get(MODELS_ENDPOINT, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                return self._error_snapshot("Invalid API key")
            return self._error_snapshot(f"HTTP {e.response.status_code}: {e.response.text[:200]}")
        except httpx.RequestError as e:
            return self._error_snapshot(f"Network error: {str(e)}")
        except Exception as e:
            return self._error_snapshot(f"Unexpected error: {str(e)}")

        return self._make_snapshot(
            status   = "ok",
            raw_data = {
                "note": "No balance endpoint — key valid",
                "key_valid": True,
            },
        )
