"""Page routes: homepage, activity log, and watch history."""

import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse

from web.shared import limiter, templates
from web.deps import get_child_store
from web.helpers import (
    _ERROR_MESSAGES, base_ctx, shorts_enabled, autoload_enabled,
    get_time_limit_info, get_category_time_info, get_schedule_info,
    annotate_categories,
)
from web.cache import (
    get_profile_cache, build_catalog, build_shorts_catalog, build_requests_row,
)
from utils import get_today_str, get_day_utc_bounds
from i18n import format_month_day, t

router = APIRouter()
_HISTORY_PAGE_SIZE = 30


def _history_date_label(date_str: str, today_str: str, locale: str) -> str:
    """Render a child-friendly date label for watch history groups."""
    if date_str == today_str:
        return t(locale, "Today")
    yesterday = (datetime.strptime(today_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    if date_str == yesterday:
        return t(locale, "Yesterday")
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    label = format_month_day(date_str, locale)
    if dt.year != datetime.strptime(today_str, "%Y-%m-%d").year:
        return f"{label}, {dt.year}"
    return label


def _last_viewed_date_key(last_viewed_at: str, tz_name: str) -> str:
    """Convert stored UTC timestamp to a local YYYY-MM-DD date key."""
    if len(last_viewed_at) < 10:
        return ""
    if not tz_name:
        return last_viewed_at[:10]
    try:
        dt = datetime.fromisoformat(last_viewed_at.replace(" ", "T"))
        dt = dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return last_viewed_at[:10]


def _build_history_groups(history: list[dict], cs, wl_cfg, locale: str) -> list[dict]:
    """Group watched videos by local watch date and decorate for the UI."""
    annotate_categories(history, cs)
    video_ids = [video["video_id"] for video in history]
    watch_minutes = cs.get_batch_watch_minutes(video_ids)
    tz = wl_cfg.timezone if wl_cfg else ""
    today = get_today_str(tz)
    grouped_history: list[dict] = []
    groups_by_date: dict[str, dict] = {}
    seen_video_ids: set[str] = set()
    for video in history:
        video_id = video["video_id"]
        if video_id in seen_video_ids:
            continue
        seen_video_ids.add(video_id)
        watched_at = video.get("last_viewed_at") or ""
        date_key = _last_viewed_date_key(watched_at, tz) or today
        group = groups_by_date.get(date_key)
        if group is None:
            group = {
                "date": date_key,
                "label": _history_date_label(date_key, today, locale),
                "videos": [],
            }
            groups_by_date[date_key] = group
            grouped_history.append(group)
        watched_minutes = round(watch_minutes.get(video_id, 0.0), 1)
        total_minutes = int((video["duration"] + 59) // 60) if video.get("duration") else None
        watched_percent = None
        if video.get("duration"):
            progress_seconds = max(int(round(watched_minutes * 60)), int(video.get("resume_seconds") or 0))
            watched_percent = max(0, min(100, int(round((progress_seconds / video["duration"]) * 100))))
        group["videos"].append({
            **video,
            "watched_minutes": watched_minutes,
            "watched_label": str(int(watched_minutes)) if watched_minutes >= 1 else "<1",
            "total_minutes": total_minutes,
            "watched_percent": watched_percent,
        })
    return grouped_history


def _history_payload(request: Request, offset: int, limit: int) -> dict:
    """Build paginated history payload for HTML and JSON responses."""
    state = request.app.state
    cs = get_child_store(request)
    history, total = cs.get_watch_history_page(offset=offset, limit=limit)
    groups = _build_history_groups(
        history,
        cs,
        state.wl_config,
        getattr(state, "locale", "en"),
    )
    return {
        "groups": groups,
        "offset": offset,
        "limit": limit,
        "total": total,
        "count": len(history),
        "has_more": offset + len(history) < total,
    }


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, error: str = Query("", max_length=50)):
    """Homepage: search bar + unified video catalog."""
    state = request.app.state
    wl_cfg = state.wl_config
    cs = get_child_store(request)
    profile_id = cs.profile_id
    allowed_channel_count = len(cs.get_channels_with_ids("allowed"))
    autoload = autoload_enabled(request, cs)
    page_size = 12
    full_catalog = build_catalog(state, profile_id=profile_id)
    catalog = full_catalog[:page_size]
    requests_page = 6
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
    locale = getattr(request.app.state, "locale", "en")
    error_message = t(locale, _ERROR_MESSAGES.get(error, "")) if error else ""
    return templates.TemplateResponse(request, "index.html", {
        **base_ctx(request),
        "catalog": catalog,
        "has_more": len(full_catalog) > page_size,
        "total_catalog": len(full_catalog),
        "requests_row": requests_row,
        "has_more_requests": has_more_requests,
        "shorts_catalog": shorts_catalog,
        "has_more_shorts": has_more_shorts,
        "shorts_enabled": shorts_enabled(request, cs),
        "autoload": autoload,
        "time_info": time_info,
        "schedule_info": schedule_info,
        "cat_info": cat_info,
        "channel_pills": channel_pills,
        "allowed_channel_count": allowed_channel_count,
        "channel_cache_updated_at": cache.get("updated_at", 0.0),
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
    return templates.TemplateResponse(request, "activity.html", {
        **base_ctx(request),
        "breakdown": breakdown,
        "total_min": round(total_min, 1),
        "time_info": time_info,
        "cat_info": cat_info,
    })


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    """Archive of watched videos grouped by last watched date."""
    payload = _history_payload(request, offset=0, limit=_HISTORY_PAGE_SIZE)
    return templates.TemplateResponse(request, "history.html", {
        **base_ctx(request),
        "history_groups": payload["groups"],
        "history_count": payload["count"],
        "history_has_more": payload["has_more"],
        "history_page_size": _HISTORY_PAGE_SIZE,
    })


@router.get("/api/history")
@limiter.limit("90/minute")
async def history_api(request: Request,
                      offset: int = Query(0, ge=0),
                      limit: int = Query(_HISTORY_PAGE_SIZE, ge=1, le=100)):
    """Paginated watched-history feed for infinite scroll."""
    return JSONResponse(_history_payload(request, offset=offset, limit=limit))
