"""Catalog API route: paginated video listing."""

from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse

from web.shared import limiter
from web.cache import build_catalog, build_shorts_catalog, build_requests_row

router = APIRouter()


@router.get("/api/catalog")
@limiter.limit("30/minute")
async def api_catalog(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=100),
    channel: str = Query("", max_length=200),
    category: str = Query("", max_length=10),
    shorts: bool = Query(False),
    requests: bool = Query(False),
):
    """Paginated catalog of all watchable videos for the current profile."""
    state = request.app.state
    profile_id = request.session.get("child_id", "default")
    if requests:
        full = build_requests_row(state, limit=200, profile_id=profile_id)
    elif shorts:
        full = build_shorts_catalog(state, profile_id=profile_id)
    else:
        full = build_catalog(state, channel_filter=channel, profile_id=profile_id)
    if category:
        full = [v for v in full if v.get("category", "fun") == category]
    page = full[offset:offset + limit]
    return JSONResponse({
        "videos": page,
        "has_more": offset + limit < len(full),
        "total": len(full),
    })
