"""
Ollama Cloud provider — fetches weekly/session quota usage.

Ollama Cloud (ollama.com) does not sell prepaid USD credits for its hosted
:cloud models — it meters usage against rolling session and weekly quotas
instead. There is no dollar balance to show, so this provider surfaces
quota consumption as a percentage instead (see currency="% weekly quota").

Endpoint:
  GET https://ollama.com/api/usage
    Auth: Authorization: Bearer <Ollama Cloud usage API key>
      (a dedicated usage-reporting key — NOT the same as a per-request
      Ollama API key used for chat/completions)
    → {
        "activity": {
          "cost": "0.00000",
          "period": {"type": "last_4_weeks", "starting_at": "...", "ending_at": "..."},
          "models": [...]
        },
        "limits": {
          "session": {"usage": 0.002, "models": [{"name": "...", "request_count": N}, ...]},
          "weekly":  {"usage": 0.227, "models": [{"name": "...", "request_count": N}, ...]}
        }
      }

  `limits.weekly.usage` / `limits.session.usage` are fractions (0-1) of the
  respective quota already consumed — not USD amounts. This mirrors the
  fields the fleet's llm-proxy already polls (see
  _refresh_ollama_usage_cache() / _get_ollama_weekly_usage() in
  llm-proxy.py) to gate routing once a quota nears exhaustion.

This provider surfaces the WEEKLY quota as the headline number (the
longer-lived, more meaningful budget) and includes session-level detail
in raw_data.

Auth: OLLAMA_CLOUD_API_KEY (Ollama Cloud usage API key, from
ollama.com/settings → API keys — must be a usage-reporting key, not a
regular inference key).
"""

import logging
import httpx

from models.schemas import BalanceSnapshot
from providers.base import BaseProvider

logger = logging.getLogger(__name__)

USAGE_ENDPOINT = "https://ollama.com/api/usage"


class OllamaCloudProvider(BaseProvider):
    """Ollama Cloud quota usage provider."""

    provider_id   = "ollama_cloud"
    provider_name = "Ollama Cloud"
    auth_type     = "api_key"

    def is_configured(self) -> bool:
        return bool(self.credentials.api_key)

    async def fetch_balance(self) -> BalanceSnapshot:
        if not self.is_configured():
            return self._unconfigured_snapshot()

        client  = await self.get_client()
        headers = {"Authorization": f"Bearer {self.credentials.api_key}"}

        try:
            resp = await client.get(USAGE_ENDPOINT, headers=headers)
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

        limits  = data.get("limits") or {}
        weekly  = limits.get("weekly") or {}
        session = limits.get("session") or {}
        activity = data.get("activity") or {}

        weekly_usage_frac = weekly.get("usage")
        if weekly_usage_frac is None:
            return self._error_snapshot("Ollama usage response missing limits.weekly.usage")

        weekly_used_pct      = round(float(weekly_usage_frac) * 100, 2)
        weekly_remaining_pct = round(100.0 - weekly_used_pct, 2)

        session_usage_frac = session.get("usage")
        session_used_pct = (
            round(float(session_usage_frac) * 100, 2) if session_usage_frac is not None else None
        )

        raw: dict = {
            "weekly_used_pct":      weekly_used_pct,
            "weekly_remaining_pct": weekly_remaining_pct,
            "weekly_models":        weekly.get("models"),
            "session_used_pct":     session_used_pct,
            "session_models":       session.get("models"),
            "activity_cost_usd":    activity.get("cost"),
            "activity_period":      activity.get("period"),
        }

        return self._make_snapshot(
            total_credits     = 100.0,
            used_credits      = weekly_used_pct,
            remaining_credits = weekly_remaining_pct,
            currency          = "% weekly quota",
            raw_data          = raw,
        )
