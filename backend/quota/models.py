"""Schemas for plan-usage readings.

A reading is one vendor's current usage against its plan: a list of windows (5-hour, weekly,
monthly, ...) each with a used percentage and a reset time, plus where the number came from.

source_kind:
  api       — the vendor's own usage/quota API, read by Reckoner itself
  client    — the vendor's own client on a workstation (herdr, a CLI), pushed to /api/quota/ingest
  our_logs  — the vendor publishes no limits or usage; this is our proxies' own record of calls
"""

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

ID_PATTERN = r"^[a-z0-9_]{1,32}$"
MAX_WINDOWS = 24


class QuotaWindow(BaseModel):
    name: str = Field(min_length=1, max_length=48)
    used_pct: Optional[float] = Field(default=None, ge=0, le=100000)
    resets_at: Optional[datetime] = None
    starts_at: Optional[datetime] = None
    used: Optional[float] = Field(default=None, ge=0)
    limit: Optional[float] = Field(default=None, ge=0)
    unit: Optional[str] = Field(default=None, max_length=24)

    model_config = {"extra": "forbid"}


class QuotaReading(BaseModel):
    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=64)
    source: str = Field(min_length=1, max_length=96)
    source_kind: Literal["api", "client", "our_logs"]
    plan: Optional[str] = Field(default=None, max_length=64)
    ok: bool = True
    error: Optional[str] = Field(default=None, max_length=300)
    fetched_at: Optional[datetime] = None
    windows: list[QuotaWindow] = Field(default_factory=list, max_length=MAX_WINDOWS)
    money: dict[str, float] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = Field(default=None, max_length=400)
    stale_after_s: int = Field(default=1800, ge=60, le=7 * 86400)

    model_config = {"extra": "forbid"}

    @field_validator("money")
    @classmethod
    def _money_small(cls, v):
        if len(v) > 12:
            raise ValueError("too many money fields")
        return v

    @field_validator("extra")
    @classmethod
    def _extra_small(cls, v):
        if len(v) > 40 or len(str(v)) > 8000:
            raise ValueError("extra too large")
        return v


class IngestPayload(BaseModel):
    source: str = Field(min_length=1, max_length=64)
    readings: list[QuotaReading] = Field(max_length=32)

    model_config = {"extra": "forbid"}
