"""Plan-usage API.

GET  /api/quota                 every vendor's latest reading. Auth: the read token
                                (RECKONER_QUOTA_TOKEN, for Switchboard) or a dashboard login.
GET  /api/quota/history/{id}    one vendor's readings over `hours`. Same auth.
POST /api/quota/refresh         read every server-side vendor now. Dashboard login (the read
                                token when no dashboard password is set).
POST /api/quota/ingest          client-side readers (collectors/) push readings. Auth: the
                                ingest token (RECKONER_INGEST_TOKEN). Only the client-sourced ids
                                (RECKONER_INGEST_IDS) are accepted, so a client can never overwrite a
                                vendor Reckoner reads from the vendor's own API.

Whenever a read token or a dashboard password is configured, the reads and refresh need one of
them, whatever RECKONER_PROTECT_DASHBOARD says. Only with neither configured (local dev) are they
open.
"""

import os
import secrets

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import ValidationError

from auth import auth_enabled, verify_token
from quota import store
from quota.models import ID_PATTERN, IngestPayload

router = APIRouter()

_INGEST_TOKEN = os.environ.get("RECKONER_INGEST_TOKEN", "").strip()
_READ_TOKEN = os.environ.get("RECKONER_QUOTA_TOKEN", "").strip()
INGEST_IDS = frozenset(
    x.strip() for x in os.environ.get(
        "RECKONER_INGEST_IDS", "claude_max,cursor,copilot,antigravity,cline,muse,opencode_cli").split(",")
    if x.strip())
MAX_INGEST_BYTES = 256 * 1024


def _bearer(request: Request) -> str:
    h = request.headers.get("Authorization", "")
    return h[7:] if h.startswith("Bearer ") else ""


def _token_eq(given: str, expected: str) -> bool:
    """Constant-time compare on bytes: a non-ASCII header is a mismatch, never a TypeError (500)."""
    return bool(given and expected) and secrets.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _unauthorized() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required", headers={"WWW-Authenticate": "Bearer"})


def _require_read(request: Request) -> None:
    if not _READ_TOKEN and not auth_enabled:
        return
    tok = _bearer(request)
    if _token_eq(tok, _READ_TOKEN):
        return
    if auth_enabled and tok and verify_token(tok):
        return
    raise _unauthorized()


def _require_login(request: Request) -> None:
    if auth_enabled:
        tok = _bearer(request)
        if not tok or not verify_token(tok):
            raise _unauthorized()
        return
    _require_read(request)


@router.get("")
async def get_quota(request: Request):
    _require_read(request)
    vendors = await store.latest_all()
    return {"vendors": vendors, "ingest_ids": sorted(INGEST_IDS)}


@router.get("/history/{vendor}")
async def get_history(request: Request, vendor: str, hours: int = Query(168, ge=1, le=24 * 30)):
    _require_read(request)
    import re
    if not re.match(ID_PATTERN, vendor):
        raise HTTPException(422, "bad vendor id")
    return {"vendor": vendor, "hours": hours, "readings": await store.history(vendor, hours)}


@router.post("/refresh")
async def refresh_now(request: Request):
    _require_login(request)
    from quota.jobs import run_server_readers
    done = await run_server_readers(force=True)
    return {"refreshed": done}


@router.post("/ingest")
async def ingest(request: Request):
    if not _INGEST_TOKEN:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ingest disabled (no RECKONER_INGEST_TOKEN)")
    tok = _bearer(request)
    if not _token_eq(tok, _INGEST_TOKEN):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad ingest token", headers={"WWW-Authenticate": "Bearer"})
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_INGEST_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "payload too large")
    raw = await request.body()
    if len(raw) > MAX_INGEST_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "payload too large")
    try:
        payload = IngestPayload.model_validate_json(raw)
    except ValidationError as e:
        raise HTTPException(422, f"invalid payload: {e.error_count()} error(s)")
    bad = sorted({r.id for r in payload.readings if r.id not in INGEST_IDS})
    if bad:
        raise HTTPException(422, f"ids not accepted from clients: {', '.join(bad)}")
    for r in payload.readings:
        if r.source_kind != "client":
            r.source_kind = "client"
        await store.save(r)
    return {"stored": [r.id for r in payload.readings]}
