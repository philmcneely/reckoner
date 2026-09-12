"""
z.ai (Zhipu / GLM Coding Plan) provider.

The GLM Coding Plan is a flat-rate subscription (billed against a coding
endpoint quota, not metered dollar credits) and — unlike the general
bigmodel.cn/z.ai pay-as-you-go platform — it does NOT expose a
balance/usage/quota REST endpoint. Confirmed by direct probing on
2026-09-11 against both api bases:

  https://api.z.ai/api/coding/paas/v4/{usage,user/info,subscription,quota}
  https://api.z.ai/api/paas/v4/{usage,user/info,subscription,quota}

...all return 404. z.ai's own docs (docs.z.ai) likewise document no
usage/balance API — usage stats are only surfaced on the web dashboard at
bigmodel.cn/coding-plan/personal/usage. See docs.z.ai/api-reference and
docs.z.ai/devpack/* for the current documented endpoint set.

What IS available and confirmed live: GET .../models — lists available
GLM models and cheaply validates the API key (no completion tokens spent).
This mirrors the key-validation pattern used by providers/gemini.py for
services with no balance API.

Endpoint:
  GET https://api.z.ai/api/coding/paas/v4/models
    Auth: Authorization: Bearer <API key>
    → {"object": "list", "data": [{"id": "glm-4.6", ...}, ...]}

Auth: ZAI_API_KEY (from z.ai → API Keys; the GLM Coding Plan key, used with
base URL https://api.z.ai/api/coding/paas/v4).
"""

import logging
import httpx

from models.schemas import BalanceSnapshot
from providers.base import BaseProvider

logger = logging.getLogger(__name__)

MODELS_ENDPOINT = "https://api.z.ai/api/coding/paas/v4/models"


class ZaiProvider(BaseProvider):
    """z.ai (GLM Coding Plan) provider — key validation, no balance API."""

    provider_id   = "zai"
    provider_name = "z.ai"
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

            if resp.status_code == 401:
                return self._error_snapshot("Invalid API key")

            resp.raise_for_status()
            data = resp.json()
            model_count = len(data.get("data", []))

            return self._make_snapshot(
                status="ok",
                raw_data={
                    "note": (
                        "No balance/usage endpoint — the GLM Coding Plan does not "
                        "expose quota via API (only via the bigmodel.cn dashboard). "
                        "Key validated via model list."
                    ),
                    "key_valid": True,
                    "available_models": model_count,
                },
            )
        except httpx.HTTPStatusError as e:
            return self._error_snapshot(f"HTTP {e.response.status_code}: {e.response.text[:200]}")
        except httpx.RequestError as e:
            return self._error_snapshot(f"Network error: {str(e)}")
        except Exception as e:
            return self._error_snapshot(f"Unexpected error: {str(e)}")
