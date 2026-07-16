from __future__ import annotations

import asyncio
import ipaddress
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from module.duel_data.live import duel_live_broker, publish_live_event
from module.duel_data.models import (
    DuelMatch,
    DuelMatchList,
    DuelMatchPatch,
    DuelSummary,
)
from module.duel_data.repository import DuelRepository
from module.server.api_logger import ApiLoggingRoute


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    return bool(
        isinstance(address, ipaddress.IPv6Address)
        and address.ipv4_mapped is not None
        and address.ipv4_mapped.is_loopback
    )


def _require_local_duel_access(request: Request) -> None:
    """Keep private duel history on the local machine.

    OAS currently has no global API authentication.  The client-address check
    blocks LAN callers, while the Origin check also blocks an arbitrary web
    page from using permissive global CORS to call a loopback OAS instance.
    Native OASX requests do not send an Origin header.
    """
    client_host = request.client.host if request.client is not None else None
    if not _is_loopback_host(client_host):
        raise HTTPException(status_code=403, detail="斗技数据接口仅允许本机访问")
    origin = request.headers.get("origin")
    if origin and not _is_loopback_host(urlsplit(origin).hostname):
        raise HTTPException(status_code=403, detail="斗技数据接口拒绝非本机网页来源")


duel_data_app = APIRouter(
    prefix="/duel-data",
    tags=["duel-data"],
    route_class=ApiLoggingRoute,
    dependencies=[Depends(_require_local_duel_access)],
)


@lru_cache(maxsize=1)
def get_duel_repository() -> DuelRepository:
    return DuelRepository()


@duel_data_app.get("/matches", response_model=DuelMatchList)
async def duel_matches(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    account_id: int | None = Query(default=None, ge=1),
    result: Literal["win", "loss", "unknown"] | None = None,
    practice_mode: bool | None = None,
    valid: bool | None = None,
):
    repository = get_duel_repository()
    return await asyncio.to_thread(
        repository.list_matches,
        page=page,
        page_size=page_size,
        account_id=account_id,
        result=result,
        practice_mode=practice_mode,
        valid=valid,
    )


@duel_data_app.get("/matches/{match_id}", response_model=DuelMatch)
async def duel_match_detail(match_id: int):
    match = await asyncio.to_thread(get_duel_repository().get_match, match_id)
    if match is None:
        raise HTTPException(status_code=404, detail="斗技战报不存在")
    return match


@duel_data_app.patch("/matches/{match_id}", response_model=DuelMatch)
async def duel_match_patch(match_id: int, patch: DuelMatchPatch):
    match = await asyncio.to_thread(get_duel_repository().patch_match, match_id, patch)
    if match is None:
        raise HTTPException(status_code=404, detail="斗技战报不存在")
    publish_live_event("match", match.model_dump())
    return match


@duel_data_app.get("/summary", response_model=DuelSummary)
async def duel_summary():
    return await asyncio.to_thread(get_duel_repository().summary)


@duel_data_app.get("/live")
async def duel_live(config_name: str | None = Query(default=None, max_length=128)):
    response = StreamingResponse(
        duel_live_broker.stream(config_name=config_name),
        media_type="text/event-stream",
    )
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Connection"] = "keep-alive"
    response.headers["X-Accel-Buffering"] = "no"
    return response
