"""Page routes: homepage, activity log, help."""

import random

from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse

from web.shared import templates
from web.deps import get_child_store
from web.helpers import (
    _ERROR_MESSAGES, base_ctx, shorts_enabled,
    get_time_limit_info, get_category_time_info, get_schedule_info,
    annotate_categories,
)
from web.cache import (
    get_profile_cache, build_catalog, build_shorts_catalog, build_requests_row,
)
from utils import get_today_str, get_day_utc_bounds

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, error: str = Query("", max_length=50)):
    """Homepage: search bar + unified video catalog."""
    state = request.app.state
    wl_cfg = state.wl_config
    cs = get_child_store(request)
    profile_id = cs.profile_id
    page_size = 12
    full_catalog = build_catalog(state, profile_id=profile_id)
    catalog = full_catalog[:page_size]
    requests_page = 4
    full_requests = build_requests_row(state, limit=50, profile_id=profile_id)
    requests_row = full_requests[:requests_page]
    has_more_requests = len(full_requests) > requests_page
    shorts_page = 9
    full_shorts = build_shorts_catalog(state, profile_id=profile_id)
    shorts_catalog = full_shorts[:shorts_page]
    has_more_shorts = len(full_shorts) > shorts_page
    time_info = get_time_limit_info(store=cs, wl_cfg=wl_cfg)
    schedule_info = get_schedule_info(store=cs, wl_cfg=wl_cfg)
    cat_info = get_category_time_info(store=cs, wl_cfg=wl_cfg)
    cache = get_profile_cache(state, profile_id)
    channel_videos = cache.get("channels", {})
    id_to_name = cache.get("id_to_name", {})
    hero_highlights = []
    for cache_key, ch_vids in channel_videos.items():
        if ch_vids:
            hero_highlights.append(random.choice(ch_vids))
    random.shuffle(hero_highlights)
    channel_pills = {}
    for cache_key in channel_videos:
        display = id_to_name.get(cache_key, cache_key)
        channel_pills[cache_key] = display
    error_message = _ERROR_MESSAGES.get(error, "") if error else ""
    return templates.TemplateResponse("index.html", {
        **base_ctx(request),
        "catalog": catalog,
        "has_more": len(full_catalog) > page_size,
        "total_catalog": len(full_catalog),
        "requests_row": requests_row,
        "has_more_requests": has_more_requests,
        "shorts_catalog": shorts_catalog,
        "has_more_shorts": has_more_shorts,
        "shorts_enabled": shorts_enabled(request, cs),
        "time_info": time_info,
        "schedule_info": schedule_info,
        "cat_info": cat_info,
        "channel_pills": channel_pills,
        "hero_highlights": hero_highlights,
        "error_message": error_message,
    })


@router.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request):
    """Today's watch log -- per-video breakdown and total."""
    wl_cfg = request.app.state.wl_config
    cs = get_child_store(request)
    tz = wl_cfg.timezone if wl_cfg else ""
    today = get_today_str(tz)
    bounds = get_day_utc_bounds(today, tz)
    breakdown = cs.get_daily_watch_breakdown(today, utc_bounds=bounds)
    time_info = get_time_limit_info(store=cs, wl_cfg=wl_cfg)
    cat_info = get_category_time_info(store=cs, wl_cfg=wl_cfg)
    total_min = sum(v["minutes"] for v in breakdown)
    annotate_categories(breakdown, cs)
    return templates.TemplateResponse("activity.html", {
        **base_ctx(request),
        "breakdown": breakdown,
        "total_min": round(total_min, 1),
        "time_info": time_info,
        "cat_info": cat_info,
    })


@router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    """Telegram bot commands reference (no auth required)."""
    return templates.TemplateResponse("help.html", {"request": request})
