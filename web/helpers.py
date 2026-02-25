"""Shared constants, models, and helper functions used across web routers."""

import re
import secrets

from fastapi import Request
from pydantic import BaseModel

from data.child_store import ChildStore
from utils import (
    get_today_str, get_day_utc_bounds, get_weekday,
    is_within_schedule, format_time_12h, resolve_setting,
    get_bonus_minutes, DAY_NAMES, CAT_LABELS,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VIDEO_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{11}$')

AVATAR_ICONS = [
    "\U0001f431", "\U0001f436", "\U0001f43b", "\U0001f98a", "\U0001f438",
    "\U0001f43c", "\U0001f680", "\u2b50", "\U0001f319", "\u26bd",
    "\U0001f3c0", "\U0001f3ae", "\U0001f3a8", "\U0001f3b5", "\U0001f996",
    "\U0001f308",
]

AVATAR_COLORS = [
    "#f4a0b0", "#a8d8a8", "#8ec5e8", "#f5c890",
    "#c8a0d8", "#90d4d4", "#f0b8a0", "#b0bec5",
]

_ERROR_MESSAGES = {
    "invalid_video": "That doesn't look like a valid YouTube link or video ID.",
    "fetch_failed": "Couldn't load video info \u2014 it may be private, age-restricted, or region-locked.",
}

# Heartbeat dedup
_HEARTBEAT_MIN_INTERVAL = 10   # seconds (must be < client heartbeat interval)
_HEARTBEAT_EVICT_AGE = 120     # evict entries older than this (seconds)


class HeartbeatRequest(BaseModel):
    video_id: str
    seconds: int


# ---------------------------------------------------------------------------
# CSRF helpers
# ---------------------------------------------------------------------------

def get_csrf_token(request: Request) -> str:
    """Get or create a CSRF token in the session."""
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_hex(32)
        request.session["csrf_token"] = token
    return token


def validate_csrf(request: Request, token: str) -> bool:
    """Validate a submitted CSRF token against the session."""
    expected = request.session.get("csrf_token")
    if not expected or not token:
        return False
    return secrets.compare_digest(expected, token)


# ---------------------------------------------------------------------------
# Template context helpers
# ---------------------------------------------------------------------------

def get_child_name(request: Request) -> str:
    """Get the current child's display name from session."""
    return request.session.get("child_name", "")


def base_ctx(request: Request) -> dict:
    """Common template context: child_name + multi_profile for base.html header."""
    vs = request.app.state.video_store
    profiles = vs.get_profiles() if vs else []
    # Populate avatar fields from session (or DB on first load after upgrade)
    avatar_icon = request.session.get("avatar_icon", "")
    avatar_color = request.session.get("avatar_color", "")
    if not avatar_icon and not avatar_color and request.session.get("child_id") and vs:
        p = vs.get_profile(request.session["child_id"])
        if p:
            avatar_icon = p.get("avatar_icon") or ""
            avatar_color = p.get("avatar_color") or ""
            if avatar_icon:
                request.session["avatar_icon"] = avatar_icon
            if avatar_color:
                request.session["avatar_color"] = avatar_color
    return {
        "request": request,
        "child_name": get_child_name(request),
        "multi_profile": len(profiles) > 1,
        "avatar_icon": avatar_icon,
        "avatar_color": avatar_color,
        "avatar_icons": AVATAR_ICONS,
        "avatar_colors": AVATAR_COLORS,
    }


def format_views(count) -> str:
    """Format view count: 847, 527K, 2.3M."""
    if not count:
        return ""
    count = int(count)
    if count < 1_000:
        return str(count)
    if count < 999_500:
        k = count / 1_000
        if k >= 10:
            return f"{k:.0f}K"
        return f"{k:.1f}".rstrip("0").rstrip(".") + "K"
    m = count / 1_000_000
    if m >= 10:
        return f"{m:.0f}M"
    return f"{m:.1f}".rstrip("0").rstrip(".") + "M"


def shorts_enabled(request: Request, child_store=None) -> bool:
    """Check if Shorts are enabled (DB override > config default)."""
    store = child_store or getattr(request.app.state, "video_store", None)
    if store:
        db_val = store.get_setting("shorts_enabled", "")
        if db_val:
            return db_val.lower() == "true"
    yt_cfg = getattr(request.app.state, "youtube_config", None)
    if yt_cfg:
        return yt_cfg.shorts_enabled
    return False


# ---------------------------------------------------------------------------
# Time / schedule / category helpers
# ---------------------------------------------------------------------------

def resolve_setting_web(base_key: str, default: str = "", store=None, wl_cfg=None) -> str:
    """Resolve a setting with per-day override. Accepts a ChildStore."""
    if not store:
        return default
    tz = wl_cfg.timezone if wl_cfg else ""
    return resolve_setting(base_key, store, tz_name=tz, default=default)


def get_time_limit_info(store, wl_cfg) -> dict | None:
    """Get time limit info. Returns None if limits disabled."""
    if not store:
        return None
    limit_str = resolve_setting_web("daily_limit_minutes", "", store=store, wl_cfg=wl_cfg)
    profile_id = getattr(store, "profile_id", "default")
    if not limit_str and wl_cfg and profile_id == "default":
        limit_min = wl_cfg.daily_limit_minutes
    else:
        limit_min = int(limit_str) if limit_str else 0
    if limit_min == 0:
        return None
    tz = wl_cfg.timezone if wl_cfg else ""
    today = get_today_str(tz)
    bounds = get_day_utc_bounds(today, tz)
    limit_min += get_bonus_minutes(store, today)
    used_min = store.get_daily_watch_minutes(today, utc_bounds=bounds)
    remaining_min = max(0.0, limit_min - used_min)
    return {
        "limit_min": limit_min,
        "used_min": round(used_min, 1),
        "remaining_min": round(remaining_min, 1),
        "remaining_sec": int(remaining_min * 60),
        "exceeded": remaining_min <= 0,
    }


def resolve_video_category(video: dict, store=None) -> str:
    """Resolve effective category: video override > channel default > fun."""
    cat = video.get("category")
    if cat:
        return cat
    channel_name = video.get("channel_name", "")
    if channel_name and store:
        ch_cat = store.get_channel_category(channel_name)
        if ch_cat:
            return ch_cat
    return "fun"


def get_category_time_info(store, wl_cfg) -> dict | None:
    """Get per-category time budget info."""
    if not store:
        return None
    edu_limit_str = resolve_setting_web("edu_limit_minutes", "", store=store, wl_cfg=wl_cfg)
    fun_limit_str = resolve_setting_web("fun_limit_minutes", "", store=store, wl_cfg=wl_cfg)
    edu_limit = int(edu_limit_str) if edu_limit_str else 0
    fun_limit = int(fun_limit_str) if fun_limit_str else 0
    if edu_limit == 0 and fun_limit == 0:
        return None
    tz = wl_cfg.timezone if wl_cfg else ""
    today = get_today_str(tz)
    bounds = get_day_utc_bounds(today, tz)
    usage = store.get_daily_watch_by_category(today, utc_bounds=bounds)
    bonus = get_bonus_minutes(store, today)

    result = {"categories": {}}
    for cat, limit in [("edu", edu_limit), ("fun", fun_limit)]:
        used = usage.get(cat, 0.0)
        if cat == "fun":
            used += usage.get(None, 0.0)
        effective_limit = limit + bonus if limit > 0 else 0
        if effective_limit == 0:
            result["categories"][cat] = {
                "limit_min": 0, "used_min": round(used, 1),
                "remaining_min": -1, "remaining_sec": -1, "exceeded": False,
            }
        else:
            remaining = max(0.0, effective_limit - used)
            result["categories"][cat] = {
                "limit_min": effective_limit, "used_min": round(used, 1),
                "remaining_min": round(remaining, 1),
                "remaining_sec": int(remaining * 60),
                "exceeded": remaining <= 0,
            }
    return result


def get_schedule_info(store, wl_cfg) -> dict | None:
    """Get schedule window info."""
    if not store:
        return None
    start = resolve_setting_web("schedule_start", "", store=store, wl_cfg=wl_cfg)
    end = resolve_setting_web("schedule_end", "", store=store, wl_cfg=wl_cfg)
    if not start and not end:
        return None
    tz = wl_cfg.timezone if wl_cfg else ""
    allowed, unlock_time = is_within_schedule(start, end, tz)
    if not allowed and end:
        from datetime import datetime as _dt
        if tz:
            from zoneinfo import ZoneInfo
            now = _dt.now(ZoneInfo(tz))
        else:
            from datetime import timezone as _tz
            now = _dt.now(_tz.utc)
        try:
            eh, em = map(int, end.split(":"))
            if now.hour * 60 + now.minute >= eh * 60 + em:
                next_start = get_next_start_time(store=store, wl_cfg=wl_cfg)
                if next_start:
                    unlock_time = f"tomorrow at {next_start}"
        except (ValueError, AttributeError):
            pass
    return {
        "allowed": allowed,
        "unlock_time": unlock_time,
        "start": format_time_12h(start) if start else "midnight",
        "end": format_time_12h(end) if end else "midnight",
    }


def get_next_start_time(store=None, wl_cfg=None) -> str | None:
    """Get the next day's schedule start time formatted for display."""
    if not store:
        return None
    tz_name = wl_cfg.timezone if wl_cfg else ""
    today = get_weekday(tz_name)
    tomorrow = DAY_NAMES[(DAY_NAMES.index(today) + 1) % 7]
    next_start = store.get_setting(f"{tomorrow}_schedule_start", "")
    if not next_start:
        next_start = store.get_setting("schedule_start", "")
    return format_time_12h(next_start) if next_start else None


# ---------------------------------------------------------------------------
# Category annotation
# ---------------------------------------------------------------------------

def annotate_categories(videos: list[dict], child_store) -> None:
    """Annotate each video dict with its effective category in-place."""
    cat_by_cid: dict[str, str] = {}
    cat_by_name: dict[str, str] = {}
    for ch_name, cid, _h, cat in child_store.get_channels_with_ids("allowed"):
        if cat:
            if cid:
                cat_by_cid[cid] = cat
            cat_by_name[ch_name] = cat
    for v in videos:
        vid_cid = v.get("channel_id", "")
        cat = cat_by_cid.get(vid_cid) if vid_cid else None
        if not cat:
            cat = cat_by_name.get(v.get("channel_name", ""))
        v["category"] = cat or v.get("category") or "fun"
