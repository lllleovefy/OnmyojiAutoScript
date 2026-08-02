from __future__ import annotations

import asyncio
import ipaddress
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from module.duel_data.live import duel_live_broker, publish_live_event
from module.duel_data.models import (
    DuelMatch,
    DuelMatchList,
    DuelMatchPatch,
    DuelPortraitLabel,
    DuelPortraitStatus,
    DuelPortraitTemplate,
    DuelPortraitTemplateList,
    DuelShishenAsset,
    DuelSummary,
)
from module.duel_data.repository import DuelRepository, PROJECT_ROOT
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


_ALIAS_SEPARATOR = re.compile(r"[,，、;；|/]+")


def _normalize_aliases(value: Any, canonical_name: str) -> list[str]:
    raw_aliases = value if isinstance(value, list) else [value]
    aliases: list[str] = []
    seen = {canonical_name}
    for raw_alias in raw_aliases:
        if raw_alias is None:
            continue
        for part in _ALIAS_SEPARATOR.split(str(raw_alias)):
            alias = part.strip()
            if alias and alias not in seen:
                aliases.append(alias)
                seen.add(alias)
    return aliases


def _load_shishen_assets(repository: DuelRepository) -> list[DuelShishenAsset]:
    """Return a deterministic, validated view of the imported asset mapping."""

    payload = repository.latest_snapshot("shishen_assets")
    if isinstance(payload, dict):
        for key in ("items", "data", "list"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                payload = candidate
                break
    if not isinstance(payload, list):
        return []

    assets: dict[int, DuelShishenAsset] = {}
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        try:
            asset_id = int(raw.get("id"))
        except (TypeError, ValueError):
            continue
        name = str(raw.get("name") or "").strip()
        if asset_id < 0 or not name:
            continue
        aliases = _normalize_aliases(raw.get("alias", raw.get("aliases")), name)
        existing = assets.get(asset_id)
        if existing is None:
            assets[asset_id] = DuelShishenAsset(
                id=asset_id,
                name=name,
                alias=aliases,
            )
            continue
        if existing.name == name:
            existing.alias = _normalize_aliases(
                [*existing.alias, *aliases],
                existing.name,
            )
    return [assets[asset_id] for asset_id in sorted(assets)]


def _canonical_portrait_label(
    repository: DuelRepository,
    label: DuelPortraitLabel,
) -> DuelPortraitLabel:
    asset = next(
        (
            candidate
            for candidate in _load_shishen_assets(repository)
            if candidate.id == label.shishen_id
        ),
        None,
    )
    if asset is None:
        raise HTTPException(
            status_code=422,
            detail="式神 ID 不存在于本地式神映射",
        )
    if label.name.strip() != asset.name:
        raise HTTPException(
            status_code=422,
            detail=f"式神 ID {asset.id} 的规范名称应为 {asset.name}",
        )
    return label.model_copy(update={"name": asset.name})


def _label_and_promote_portrait(
    repository: DuelRepository,
    template_id: int,
    label: DuelPortraitLabel,
) -> DuelPortraitTemplate | None:
    """Label an unresolved crop and make it visible to the runtime matcher."""

    existing = repository.get_portrait_template(template_id)
    if existing is None:
        return None
    promoted_path = None
    library_root = (
        PROJECT_ROOT / "config" / "duel" / "portrait_library"
    ).resolve()
    try:
        source = (library_root / existing.path).resolve()
        source.relative_to(library_root)
        if source.is_file():
            from tasks.Duel.portrait_library import (
                PortraitLibrary,
                load_image,
            )

            library = PortraitLibrary(library_root)
            result = library.add_template(
                load_image(source),
                shikigami_id=label.shishen_id,
                name=label.name,
                view=existing.view,
                source=label.source,
                confidence=label.confidence,
            )
            promoted_path = result.record.relative_path
            library.write_coverage_report(
                [
                    asset.model_dump()
                    for asset in _load_shishen_assets(repository)
                ]
            )
    except (OSError, TypeError, ValueError):
        # Historical rows may reference an external diagnostic path or an old
        # view name. The correction remains useful in the database even when
        # no local image can be promoted.
        promoted_path = None
    return repository.label_portrait_template(
        template_id,
        **label.model_dump(),
        path=promoted_path,
    )


def _resolve_portrait_image(
    repository: DuelRepository,
    template_id: int,
) -> Path:
    """Resolve a recorded PNG without allowing access outside the library."""

    template = repository.get_portrait_template(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="斗技头像样本不存在")
    library_root = (
        PROJECT_ROOT / "config" / "duel" / "portrait_library"
    ).resolve()
    raw_path = Path(template.path)
    if raw_path.is_absolute():
        candidate = raw_path.resolve()
    else:
        project_candidate = (PROJECT_ROOT / raw_path).resolve()
        try:
            project_candidate.relative_to(library_root)
        except ValueError:
            candidate = (library_root / raw_path).resolve()
        else:
            candidate = project_candidate
    try:
        candidate.relative_to(library_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=403,
            detail="斗技头像路径不在素材库内",
        ) from exc
    if candidate.suffix.lower() != ".png" or not candidate.is_file():
        raise HTTPException(status_code=404, detail="斗技头像图片不存在")
    return candidate


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


@duel_data_app.get("/shishen-assets", response_model=list[DuelShishenAsset])
async def duel_shishen_assets():
    return await asyncio.to_thread(
        _load_shishen_assets,
        get_duel_repository(),
    )


@duel_data_app.get("/portraits/status", response_model=DuelPortraitStatus)
async def duel_portrait_status():
    return await asyncio.to_thread(get_duel_repository().portrait_status)


@duel_data_app.get(
    "/portraits/unresolved",
    response_model=DuelPortraitTemplateList,
)
async def duel_portrait_unresolved(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
    view: str | None = Query(default=None, min_length=1, max_length=64),
):
    return await asyncio.to_thread(
        get_duel_repository().list_unresolved_portrait_templates,
        page=page,
        page_size=page_size,
        view=view,
    )


@duel_data_app.get("/portraits/{template_id}/image")
async def duel_portrait_image(template_id: int):
    image_path = await asyncio.to_thread(
        _resolve_portrait_image,
        get_duel_repository(),
        template_id,
    )
    return FileResponse(
        image_path,
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@duel_data_app.post(
    "/portraits/{template_id}/label",
    response_model=DuelPortraitTemplate,
)
async def duel_portrait_label(template_id: int, label: DuelPortraitLabel):
    repository = get_duel_repository()
    existing = await asyncio.to_thread(
        repository.get_portrait_template,
        template_id,
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="斗技头像样本不存在")
    canonical_label = await asyncio.to_thread(
        _canonical_portrait_label,
        repository,
        label,
    )
    template = await asyncio.to_thread(
        _label_and_promote_portrait,
        repository,
        template_id,
        canonical_label,
    )
    if template is None:
        raise HTTPException(status_code=404, detail="斗技头像样本不存在")
    return template


@duel_data_app.get("/live/snapshot")
async def duel_live_snapshot(
    config_name: str | None = Query(default=None, max_length=128),
):
    return duel_live_broker.snapshot(config_name=config_name)


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
