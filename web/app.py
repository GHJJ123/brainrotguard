import asyncio
import hashlib
import hmac
import logging
import random
import re
import secrets
import time
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request, Form, Query, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from data.child_store import ChildStore
from utils import get_today_str, get_day_utc_bounds, get_weekday, is_within_schedule, format_time_12h, resolve_setting, DAY_NAMES, CAT_LABELS
from youtube.extractor import extract_video_id, extract_metadata, search, fetch_channel_videos, fetch_channel_shorts, format_duration, configure_timeout
from web.deps import get_child_store, get_video_store, get_web_config, get_wl_config, get_youtube_config, get_notify_cb, get_time_limit_cb

VIDEO_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{11}$')

logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="BrainRotGuard")
app.state.limiter = limiter

templates_dir = Path(__file__).parent / "templates"
static_dir = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(templates_dir))
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Dependencies are set on app.state by main.py:
#   app.state.video_store       - VideoStore instance
#   app.state.notify_callback   - async fn(video, profile_id)
#   app.state.time_limit_notify_cb - async fn(used, limit, cat, profile_id)
#   app.state.youtube_config    - YouTubeConfig
#   app.state.web_config        - WebConfig
#   app.state.wl_config         - WatchLimitsConfig
#
# Cache state (also on app.state, initialized at startup):
#   app.state.channel_caches    - dict[profile_id, dict]
#   app.state.channel_cache_task - asyncio.Task
#   app.state.catalog_cache     - list[dict]
#   app.state.catalog_cache_time - float
#   app.state.word_filter_cache - list[re.Pattern] | None
#   app.state.yt_iframe_api_cache - str | None
#   app.state.yt_widget_api_cache - str | None
#   app.state.yt_widget_api_url - str | None
#   app.state.yt_cache_time     - float
#   app.state.last_heartbeat    - dict[str, float]
#   app.state.heartbeat_last_cleanup - float

AVATAR_ICONS = ["🐱", "🐶", "🐻", "🦊", "🐸", "🐼", "🚀", "⭐", "🌙", "⚽", "🏀", "🎮", "🎨", "🎵", "🦖", "🌈"]
AVATAR_COLORS = ["#f4a0b0", "#a8d8a8", "#8ec5e8", "#f5c890", "#c8a0d8", "#90d4d4", "#f0b8a0", "#b0bec5"]


class HeartbeatRequest(BaseModel):
    video_id: str
    seconds: int


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' https://ko-fi.com https://i.ytimg.com https://i1.ytimg.com https://i2.ytimg.com "
            "https://i3.ytimg.com https://i4.ytimg.com https://i9.ytimg.com https://img.youtube.com; "
            "frame-src https://www.youtube-nocookie.com; "
            "connect-src 'self'; "
            "media-src https://*.googlevideo.com; "
            "object-src 'none'; "
            "base-uri 'self'"
        )
        return response


# API paths that are safe to access without PIN auth:
# - /api/status/ — needed for pending page polling (only leaks approved/denied/pending status)
# - /api/yt-iframe-api.js, /api/yt-widget-api.js — proxied YouTube player scripts
_API_AUTH_EXEMPT = ("/api/status/", "/api/yt-iframe-api.js", "/api/yt-widget-api.js")


class PinAuthMiddleware(BaseHTTPMiddleware):
    """Require profile-based authentication when any profile has a PIN."""

    def __init__(self, app, pin: str = ""):
        super().__init__(app)
        self.pin = pin  # legacy single-PIN (used for backwards compat check)

    async def dispatch(self, request: Request, call_next) -> Response:
        # Allow unauthenticated access to login, static assets, and specific read-only APIs
        if request.url.path.startswith(("/login", "/static", "/help")):
            return await call_next(request)
        if request.url.path.startswith(_API_AUTH_EXEMPT):
            return await call_next(request)

        # Profile-based auth: check if child_id is in session
        if request.session.get("child_id"):
            return await call_next(request)

        # Auto-login: if only one profile and it has no PIN, set session directly
        vs = getattr(request.app.state, "video_store", None)
        if vs:
            profiles = vs.get_profiles()
            if len(profiles) == 1 and not profiles[0]["pin"]:
                request.session["child_id"] = profiles[0]["id"]
                request.session["child_name"] = profiles[0]["display_name"]
                request.session["avatar_icon"] = profiles[0].get("avatar_icon") or ""
                request.session["avatar_color"] = profiles[0].get("avatar_color") or ""
                return await call_next(request)
            if not profiles:
                # No profiles at all — shouldn't happen after bootstrap, but handle gracefully
                return await call_next(request)

        # Legacy: if no profiles exist but PIN auth is disabled
        if not self.pin and (not vs or not vs.get_profiles()):
            return await call_next(request)

        # Return JSON 401 for API endpoints instead of redirect
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse(url="/login", status_code=303)


def _get_csrf_token(request: Request) -> str:
    """Get or create a CSRF token in the session."""
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_hex(32)
        request.session["csrf_token"] = token
    return token


def _validate_csrf(request: Request, token: str) -> bool:
    """Validate a submitted CSRF token against the session."""
    expected = request.session.get("csrf_token")
    if not expected or not token:
        return False
    return secrets.compare_digest(expected, token)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return HTMLResponse(
        content="<h1>Too many requests</h1><p>Please wait a moment and try again.</p>",
        status_code=429,
    )


# YouTube script proxy constants
_YT_CACHE_TTL = 86400  # 24 hours
_YT_SCRIPTURL_RE = re.compile(r"(var\s+scriptUrl\s*=\s*)'([^']+)'")
_YT_ALLOWED_HOSTS = {"www.youtube.com", "youtube.com", "s.ytimg.com", "www.google.com"}


async def _fetch_yt_scripts(state):
    """Fetch and cache the iframe API loader + widget API script from youtube.com."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://www.youtube.com/iframe_api")
            resp.raise_for_status()
            raw = resp.text

            # Extract the widget API URL and rewrite it to our proxy
            m = _YT_SCRIPTURL_RE.search(raw)
            extracted_url = None
            if m:
                extracted_url = m.group(2).replace("\\/", "/")
                # Validate extracted URL against allowlist before fetching
                parsed = urlparse(extracted_url)
                if parsed.hostname not in _YT_ALLOWED_HOSTS:
                    logger.error("Rejected widget API URL with unexpected host: %s", parsed.hostname)
                    extracted_url = None
                state.yt_widget_api_url = extracted_url
                # Only rewrite scriptUrl to our proxy if the widget URL passed validation
                if extracted_url:
                    raw = _YT_SCRIPTURL_RE.sub(r"\1'\\/api\\/yt-widget-api.js'", raw)
            else:
                logger.warning("scriptUrl pattern not found in YouTube iframe API response")

            state.yt_iframe_api_cache = raw
            logger.info("YT iframe API SHA-256: %s", hashlib.sha256(raw.encode()).hexdigest())

            if extracted_url:
                resp2 = await client.get(extracted_url)
                resp2.raise_for_status()
                state.yt_widget_api_cache = resp2.text
                logger.info("YT widget API SHA-256: %s", hashlib.sha256(resp2.text.encode()).hexdigest())

            state.yt_cache_time = time.monotonic()
    except httpx.HTTPError as e:
        if getattr(state, "yt_iframe_api_cache", None) is not None:
            logger.warning("Failed to refresh YouTube scripts, serving stale cache: %s", e)
        else:
            logger.error("Failed to fetch YouTube scripts (no cache available): %s", e)


def _yt_cache_stale(state) -> bool:
    return getattr(state, "yt_cache_time", 0.0) == 0.0 or (time.monotonic() - state.yt_cache_time) > _YT_CACHE_TTL


@app.get("/api/yt-iframe-api.js")
async def yt_iframe_api_proxy(request: Request):
    """Proxy the YouTube IFrame API loader with widget URL rewritten to local."""
    state = request.app.state
    if getattr(state, "yt_iframe_api_cache", None) is None or _yt_cache_stale(state):
        await _fetch_yt_scripts(state)
    if not getattr(state, "yt_iframe_api_cache", None):
        return PlainTextResponse("// iframe API unavailable", media_type="application/javascript")
    return PlainTextResponse(state.yt_iframe_api_cache, media_type="application/javascript")


@app.get("/api/yt-widget-api.js")
async def yt_widget_api_proxy(request: Request):
    """Proxy the YouTube widget API script."""
    state = request.app.state
    if getattr(state, "yt_widget_api_cache", None) is None or _yt_cache_stale(state):
        await _fetch_yt_scripts(state)
    if not getattr(state, "yt_widget_api_cache", None):
        return PlainTextResponse("// widget API unavailable", media_type="application/javascript")
    return PlainTextResponse(state.yt_widget_api_cache, media_type="application/javascript")


def init_app_state(state):
    """Initialize cache state on app.state. Called by main.py after setting deps."""
    # YouTube script cache
    state.yt_iframe_api_cache = None
    state.yt_widget_api_cache = None
    state.yt_widget_api_url = None
    state.yt_cache_time = 0.0
    # Channel cache (per-profile)
    state.channel_caches = {}
    state.channel_cache_task = None
    # Catalog cache
    state.catalog_cache = []
    state.catalog_cache_time = 0.0
    # Word filter cache
    state.word_filter_cache = None
    # Heartbeat dedup
    state.last_heartbeat = {}
    state.heartbeat_last_cleanup = 0.0


# Add globals and filters to Jinja2
templates.env.globals["format_duration"] = format_duration
from version import __version__
templates.env.globals["app_version"] = __version__


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


templates.env.filters["format_views"] = format_views


def _get_child_name(request: Request) -> str:
    """Get the current child's display name from session."""
    return request.session.get("child_name", "")


def _base_ctx(request: Request) -> dict:
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
        "child_name": _get_child_name(request),
        "multi_profile": len(profiles) > 1,
        "avatar_icon": avatar_icon,
        "avatar_color": avatar_color,
        "avatar_icons": AVATAR_ICONS,
        "avatar_colors": AVATAR_COLORS,
    }


def _shorts_enabled(request: Request, child_store=None) -> bool:
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


# Heartbeat dedup constants
_HEARTBEAT_MIN_INTERVAL = 10  # seconds (must be < client heartbeat interval)
_HEARTBEAT_EVICT_AGE = 120  # evict entries older than this (seconds)

# Channel cache default TTL
_CHANNEL_CACHE_TTL = 1800  # default; overridden by youtube_config.channel_cache_ttl


def _get_profile_cache(state, profile_id: str) -> dict:
    """Get or create the channel cache for a profile."""
    caches = state.channel_caches
    if profile_id not in caches:
        caches[profile_id] = {"channels": {}, "shorts": {}, "id_to_name": {}, "updated_at": 0.0}
    return caches[profile_id]


async def _refresh_channel_cache_for_profile(state, profile_id: str):
    """Fetch latest videos and Shorts for a profile's allowlisted channels."""
    vs = getattr(state, "video_store", None)
    if not vs:
        return
    cache = _get_profile_cache(state, profile_id)
    child_store = ChildStore(vs, profile_id)
    allowed = child_store.get_channels_with_ids("allowed")
    if not allowed:
        cache["channels"] = {}
        cache["shorts"] = {}
        cache["id_to_name"] = {}
        cache["updated_at"] = time.monotonic()
        return
    yt_cfg = getattr(state, "youtube_config", None)
    max_vids = yt_cfg.channel_cache_results if yt_cfg else 200
    tasks = [fetch_channel_videos(name, max_results=max_vids, channel_id=cid) for name, cid, _handle, _cat in allowed]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    channels = {}
    channel_id_to_name = {}
    for (ch_name, cid, _h, _c), result in zip(allowed, results):
        cache_key = cid or ch_name
        if cid:
            channel_id_to_name[cid] = ch_name
        if isinstance(result, Exception):
            logger.error("Channel cache fetch failed for '%s': %s", ch_name, result)
            channels[cache_key] = []
        else:
            channels[cache_key] = result

    # Fetch Shorts from each channel's /shorts tab
    # Build a fake request-like object is not needed — we use state directly
    shorts_enabled_val = False
    db_val = child_store.get_setting("shorts_enabled", "")
    if db_val:
        shorts_enabled_val = db_val.lower() == "true"
    elif yt_cfg:
        shorts_enabled_val = yt_cfg.shorts_enabled

    if shorts_enabled_val:
        shorts_max = max(max_vids // 4, 20)
        shorts_tasks = [fetch_channel_shorts(name, max_results=shorts_max, channel_id=cid) for name, cid, _handle, _cat in allowed]
        shorts_results = await asyncio.gather(*shorts_tasks, return_exceptions=True)
        shorts = {}
        for (ch_name, cid, _h, _c), result in zip(allowed, shorts_results):
            cache_key = cid or ch_name
            if isinstance(result, Exception):
                logger.debug("Channel shorts fetch failed for '%s': %s", ch_name, result)
                shorts[cache_key] = []
            else:
                shorts[cache_key] = result
    else:
        shorts = {}

    cache["channels"] = channels
    cache["shorts"] = shorts
    cache["id_to_name"] = channel_id_to_name
    cache["updated_at"] = time.monotonic()
    logger.info("Refreshed channel cache for profile '%s': %d channels, %d with shorts",
                profile_id, len(channels), sum(1 for v in shorts.values() if v))


async def _refresh_all_channel_caches(state):
    """Refresh channel caches for all profiles."""
    vs = getattr(state, "video_store", None)
    if not vs:
        return
    profiles = vs.get_profiles()
    if not profiles:
        await _refresh_channel_cache_for_profile(state, "default")
        return
    for p in profiles:
        await _refresh_channel_cache_for_profile(state, p["id"])


def invalidate_channel_cache(state, profile_id: str = ""):
    """Mark cache as stale. If profile_id given, only that profile; otherwise all."""
    _invalidate_catalog_cache(state)
    if profile_id:
        cache = _get_profile_cache(state, profile_id)
        cache["updated_at"] = 0.0
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_refresh_channel_cache_for_profile(state, profile_id))
        except RuntimeError:
            pass
    else:
        for cache in state.channel_caches.values():
            cache["updated_at"] = 0.0
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_refresh_all_channel_caches(state))
        except RuntimeError:
            pass


async def _channel_cache_loop(state):
    """Background loop to refresh channel caches periodically."""
    await asyncio.sleep(5)
    while True:
        try:
            await _refresh_all_channel_caches(state)
        except Exception as e:
            logger.error("Channel cache refresh error: %s", e)
        yt_cfg = getattr(state, "youtube_config", None)
        ttl = yt_cfg.channel_cache_ttl if yt_cfg else _CHANNEL_CACHE_TTL
        await asyncio.sleep(ttl)


@app.on_event("startup")
async def _start_channel_cache():
    state = app.state
    state.channel_cache_task = asyncio.create_task(_channel_cache_loop(state))


def _get_word_filter_patterns(state) -> list[re.Pattern]:
    """Compile word filter patterns (cached; invalidated with catalog cache)."""
    if getattr(state, "word_filter_cache", None) is not None:
        return state.word_filter_cache
    vs = getattr(state, "video_store", None)
    if not vs:
        return []
    words = vs.get_word_filters_set()
    if not words:
        state.word_filter_cache = []
        return state.word_filter_cache
    state.word_filter_cache = [re.compile(r'\b' + re.escape(w) + r'\b', re.IGNORECASE) for w in words]
    return state.word_filter_cache


def _title_matches_filter(title: str, patterns: list[re.Pattern]) -> bool:
    """Check if a video title matches any word filter pattern."""
    return any(p.search(title) for p in patterns)


def _annotate_categories(videos: list[dict], child_store) -> None:
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


def _invalidate_catalog_cache(state):
    """Mark catalog cache and word filter cache as stale."""
    state.catalog_cache_time = 0.0
    state.word_filter_cache = None


def _build_shorts_catalog(state, profile_id: str = "default") -> list[dict]:
    """Build Shorts catalog from channel cache + DB approved shorts for a profile."""
    vs = getattr(state, "video_store", None)
    child_store = ChildStore(vs, profile_id) if vs else None

    # Check shorts enabled without request — use state directly
    shorts_enabled_val = False
    if child_store:
        db_val = child_store.get_setting("shorts_enabled", "")
        if db_val:
            shorts_enabled_val = db_val.lower() == "true"
        elif getattr(state, "youtube_config", None):
            shorts_enabled_val = state.youtube_config.shorts_enabled
    if not shorts_enabled_val:
        return []

    denied_ids = child_store.get_denied_video_ids() if child_store else set()
    seen_ids = set(denied_ids)
    shorts = []

    cache = _get_profile_cache(state, profile_id)
    shorts_channels = cache.get("shorts", {})
    if shorts_channels:
        chan_lists = [list(vids) for vids in shorts_channels.values() if vids]
        indices = [0] * len(chan_lists)
        while True:
            added = False
            for i, vids in enumerate(chan_lists):
                if indices[i] < len(vids):
                    v = vids[indices[i]]
                    vid = v.get("video_id", "")
                    indices[i] += 1
                    if vid and vid not in seen_ids:
                        seen_ids.add(vid)
                        shorts.append(dict(v))
                    added = True
            if not added:
                break

    if child_store:
        for v in child_store.get_approved_shorts():
            vid = v.get("video_id", "")
            if vid and vid not in seen_ids:
                seen_ids.add(vid)
                shorts.append(dict(v))

    if child_store:
        _annotate_categories(shorts, child_store)

    wf = _get_word_filter_patterns(state)
    if wf:
        shorts = [v for v in shorts if not _title_matches_filter(v.get("title", ""), wf)]

    return shorts


def _build_requests_row(state, limit: int = 50, profile_id: str = "default") -> list[dict]:
    """Build 'Your Requests' row from DB-approved non-Short videos for a profile."""
    vs = getattr(state, "video_store", None)
    if not vs:
        return []
    child_store = ChildStore(vs, profile_id)
    requests = child_store.get_recent_requests(limit=limit)
    allowed_channel_ids = set()
    allowed_names = set()
    for ch_name, cid, _h, _cat in child_store.get_channels_with_ids("allowed"):
        if cid:
            allowed_channel_ids.add(cid)
        else:
            allowed_names.add(ch_name.lower())
    filtered = []
    for v in requests:
        vid_cid = v.get("channel_id")
        if vid_cid and vid_cid in allowed_channel_ids:
            continue
        if not vid_cid and v.get("channel_name", "").lower() in allowed_names:
            continue
        filtered.append(v)
    _annotate_categories(filtered, child_store)

    # Filter out titles matching word filters
    wf = _get_word_filter_patterns(state)
    if wf:
        filtered = [v for v in filtered if not _title_matches_filter(v.get("title", ""), wf)]

    return filtered


def _build_catalog(state, channel_filter: str = "", profile_id: str = "default") -> list[dict]:
    """Build unified catalog for a profile."""
    cache = _get_profile_cache(state, profile_id)
    channels = cache.get("channels", {})
    vs = getattr(state, "video_store", None)
    child_store = ChildStore(vs, profile_id) if vs else None
    denied_ids = child_store.get_denied_video_ids() if child_store else set()

    if channel_filter:
        seen_ids = set(denied_ids)
        filtered = []
        for v in channels.get(channel_filter, []):
            vid = v.get("video_id", "")
            if vid and vid not in seen_ids and not v.get("is_short"):
                seen_ids.add(vid)
                filtered.append(dict(v))
        id_to_name = cache.get("id_to_name", {})
        is_channel_id = channel_filter in id_to_name
        if child_store:
            if is_channel_id:
                db_vids = child_store.get_by_status("approved", channel_id=channel_filter)
            else:
                db_vids = child_store.get_by_status("approved", channel_name=channel_filter)
            for v in db_vids:
                vid = v.get("video_id", "")
                if vid and vid not in seen_ids and not v.get("is_short"):
                    seen_ids.add(vid)
                    filtered.append(v)
        filtered.sort(key=lambda v: v.get("timestamp") or 0, reverse=True)
        if child_store:
            _annotate_categories(filtered, child_store)
        wf = _get_word_filter_patterns(state)
        if wf:
            filtered = [v for v in filtered if not _title_matches_filter(v.get("title", ""), wf)]
        return filtered

    # Check catalog cache
    cache_age = cache.get("updated_at", 0.0)
    if state.catalog_cache and state.catalog_cache_time >= cache_age and state.catalog_cache_time > 0:
        # Only use cache for default profile (multi-profile always rebuilds)
        if profile_id == "default":
            return state.catalog_cache

    seen_ids = set(denied_ids)
    catalog = []
    if channels:
        chan_lists = [list(vids) for vids in channels.values() if vids]
        indices = [0] * len(chan_lists)
        while True:
            added = False
            for i, vids in enumerate(chan_lists):
                if indices[i] < len(vids):
                    v = vids[indices[i]]
                    vid = v.get("video_id", "")
                    indices[i] += 1
                    if v.get("is_short"):
                        added = True
                        continue
                    if vid and vid not in seen_ids:
                        seen_ids.add(vid)
                        catalog.append(dict(v))
                    added = True
            if not added:
                break

    if child_store:
        for v in child_store.get_by_status("approved"):
            vid = v.get("video_id", "")
            if vid and vid not in seen_ids and not v.get("is_short"):
                seen_ids.add(vid)
                catalog.append(v)

    if child_store:
        _annotate_categories(catalog, child_store)

    wf = _get_word_filter_patterns(state)
    if wf:
        catalog = [v for v in catalog if not _title_matches_filter(v.get("title", ""), wf)]

    if profile_id == "default":
        state.catalog_cache = catalog
        state.catalog_cache_time = time.monotonic()
    return catalog


@app.get("/api/catalog")
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
        full = _build_requests_row(state, limit=200, profile_id=profile_id)
    elif shorts:
        full = _build_shorts_catalog(state, profile_id=profile_id)
    else:
        full = _build_catalog(state, channel_filter=channel, profile_id=profile_id)
    if category:
        full = [v for v in full if v.get("category", "fun") == category]
    page = full[offset:offset + limit]
    return JSONResponse({
        "videos": page,
        "has_more": offset + limit < len(full),
        "total": len(full),
    })


def _resolve_setting_web(base_key: str, default: str = "", store=None, wl_cfg=None) -> str:
    """Resolve a setting with per-day override. Accepts a ChildStore."""
    if not store:
        return default
    tz = wl_cfg.timezone if wl_cfg else ""
    return resolve_setting(base_key, store, tz_name=tz, default=default)


def _get_time_limit_info(store, wl_cfg) -> dict | None:
    """Get time limit info. Returns None if limits disabled."""
    if not store:
        return None
    limit_str = _resolve_setting_web("daily_limit_minutes", "", store=store, wl_cfg=wl_cfg)
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
    bonus_date = store.get_setting("daily_bonus_date", "")
    if bonus_date == today:
        bonus = int(store.get_setting("daily_bonus_minutes", "0") or "0")
        limit_min += bonus
    used_min = store.get_daily_watch_minutes(today, utc_bounds=bounds)
    remaining_min = max(0.0, limit_min - used_min)
    return {
        "limit_min": limit_min,
        "used_min": round(used_min, 1),
        "remaining_min": round(remaining_min, 1),
        "remaining_sec": int(remaining_min * 60),
        "exceeded": remaining_min <= 0,
    }


def _resolve_video_category(video: dict, store=None) -> str:
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


def _get_category_time_info(store, wl_cfg) -> dict | None:
    """Get per-category time budget info."""
    if not store:
        return None
    edu_limit_str = _resolve_setting_web("edu_limit_minutes", "", store=store, wl_cfg=wl_cfg)
    fun_limit_str = _resolve_setting_web("fun_limit_minutes", "", store=store, wl_cfg=wl_cfg)
    edu_limit = int(edu_limit_str) if edu_limit_str else 0
    fun_limit = int(fun_limit_str) if fun_limit_str else 0
    if edu_limit == 0 and fun_limit == 0:
        return None
    tz = wl_cfg.timezone if wl_cfg else ""
    today = get_today_str(tz)
    bounds = get_day_utc_bounds(today, tz)
    usage = store.get_daily_watch_by_category(today, utc_bounds=bounds)
    bonus = 0
    bonus_date = store.get_setting("daily_bonus_date", "")
    if bonus_date == today:
        bonus = int(store.get_setting("daily_bonus_minutes", "0") or "0")

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


def _get_schedule_info(store, wl_cfg) -> dict | None:
    """Get schedule window info."""
    if not store:
        return None
    start = _resolve_setting_web("schedule_start", "", store=store, wl_cfg=wl_cfg)
    end = _resolve_setting_web("schedule_end", "", store=store, wl_cfg=wl_cfg)
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
                next_start = _get_next_start_time(store=store, wl_cfg=wl_cfg)
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


def _get_next_start_time(store=None, wl_cfg=None) -> str | None:
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


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, profile: str = Query("", max_length=50)):
    """Profile picker → optional PIN entry."""
    vs = request.app.state.video_store
    profiles = vs.get_profiles() if vs else []

    # Auto-login: single profile with no PIN
    if len(profiles) == 1 and not profiles[0]["pin"]:
        request.session["child_id"] = profiles[0]["id"]
        request.session["child_name"] = profiles[0]["display_name"]
        request.session["avatar_icon"] = profiles[0].get("avatar_icon") or ""
        request.session["avatar_color"] = profiles[0].get("avatar_color") or ""
        return RedirectResponse(url="/", status_code=303)

    csrf_token = _get_csrf_token(request)

    # If a profile is selected and it needs a PIN, show PIN input
    if profile:
        p = vs.get_profile(profile) if vs else None
        if p and not p["pin"]:
            # No PIN required — log in immediately
            request.session["child_id"] = p["id"]
            request.session["child_name"] = p["display_name"]
            request.session["avatar_icon"] = p.get("avatar_icon") or ""
            request.session["avatar_color"] = p.get("avatar_color") or ""
            request.session["csrf_token"] = secrets.token_hex(32)
            return RedirectResponse(url="/", status_code=303)
        if p:
            return templates.TemplateResponse("login.html", {
                "request": request,
                "csrf_token": csrf_token,
                "error": False,
                "profiles": profiles,
                "selected_profile": p,
                "step": "pin",
            })

    # Single profile with PIN — go straight to PIN entry
    if len(profiles) == 1:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "csrf_token": csrf_token,
            "error": False,
            "profiles": profiles,
            "selected_profile": profiles[0],
            "step": "pin",
        })

    # Show profile picker
    return templates.TemplateResponse("login.html", {
        "request": request,
        "csrf_token": csrf_token,
        "error": False,
        "profiles": profiles,
        "selected_profile": None,
        "step": "pick",
    })


@app.post("/login")
@limiter.limit("5/hour")
async def login_submit(
    request: Request,
    pin: str = Form(""),
    profile_id: str = Form(""),
    csrf_token: str = Form(""),
):
    """Validate PIN and create session for selected profile."""
    if not _validate_csrf(request, csrf_token):
        return RedirectResponse(url="/login", status_code=303)

    vs = request.app.state.video_store
    if not vs:
        return RedirectResponse(url="/", status_code=303)

    # Find the profile
    profile = vs.get_profile(profile_id) if profile_id else None
    if not profile:
        return RedirectResponse(url="/login", status_code=303)

    # No PIN required
    if not profile["pin"]:
        request.session["child_id"] = profile["id"]
        request.session["child_name"] = profile["display_name"]
        request.session["avatar_icon"] = profile.get("avatar_icon") or ""
        request.session["avatar_color"] = profile.get("avatar_color") or ""
        request.session["csrf_token"] = secrets.token_hex(32)
        return RedirectResponse(url="/", status_code=303)

    # Validate PIN
    if pin and hmac.compare_digest(pin, profile["pin"]):
        request.session["child_id"] = profile["id"]
        request.session["child_name"] = profile["display_name"]
        request.session["avatar_icon"] = profile.get("avatar_icon") or ""
        request.session["avatar_color"] = profile.get("avatar_color") or ""
        request.session["csrf_token"] = secrets.token_hex(32)
        return RedirectResponse(url="/", status_code=303)

    # Failed PIN
    profiles = vs.get_profiles()
    new_csrf = secrets.token_hex(32)
    request.session["csrf_token"] = new_csrf
    return templates.TemplateResponse("login.html", {
        "request": request,
        "csrf_token": new_csrf,
        "error": True,
        "profiles": profiles,
        "selected_profile": profile,
        "step": "pin",
    })


@app.get("/switch-profile")
async def switch_profile(request: Request):
    """Clear current session and return to profile picker."""
    request.session.pop("child_id", None)
    request.session.pop("child_name", None)
    request.session.pop("avatar_icon", None)
    request.session.pop("avatar_color", None)
    return RedirectResponse(url="/login", status_code=303)


@app.post("/api/avatar")
@limiter.limit("30/minute")
async def update_avatar(request: Request):
    """Update the current profile's avatar icon and/or color."""
    child_id = request.session.get("child_id")
    vs = request.app.state.video_store
    if not child_id or not vs:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    icon = body.get("icon", "")
    color = body.get("color", "")

    if icon and icon not in AVATAR_ICONS:
        return JSONResponse({"error": "invalid icon"}, status_code=400)
    if color and color not in AVATAR_COLORS:
        return JSONResponse({"error": "invalid color"}, status_code=400)

    vs.update_profile_avatar(
        child_id,
        icon=icon if icon else None,
        color=color if color else None,
    )

    if icon:
        request.session["avatar_icon"] = icon
    if color:
        request.session["avatar_color"] = color

    return JSONResponse({"ok": True})


@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    """Telegram bot commands reference (no auth required)."""
    return templates.TemplateResponse("help.html", {"request": request})


_ERROR_MESSAGES = {
    "invalid_video": "That doesn't look like a valid YouTube link or video ID.",
    "fetch_failed": "Couldn't load video info — it may be private, age-restricted, or region-locked.",
}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, error: str = Query("", max_length=50)):
    """Homepage: search bar + unified video catalog."""
    state = request.app.state
    wl_cfg = state.wl_config
    cs = get_child_store(request)
    profile_id = cs.profile_id
    page_size = 12
    full_catalog = _build_catalog(state, profile_id=profile_id)
    catalog = full_catalog[:page_size]
    requests_page = 4
    full_requests = _build_requests_row(state, limit=50, profile_id=profile_id)
    requests_row = full_requests[:requests_page]
    has_more_requests = len(full_requests) > requests_page
    shorts_page = 9
    full_shorts = _build_shorts_catalog(state, profile_id=profile_id)
    shorts_catalog = full_shorts[:shorts_page]
    has_more_shorts = len(full_shorts) > shorts_page
    time_info = _get_time_limit_info(store=cs, wl_cfg=wl_cfg)
    schedule_info = _get_schedule_info(store=cs, wl_cfg=wl_cfg)
    cat_info = _get_category_time_info(store=cs, wl_cfg=wl_cfg)
    cache = _get_profile_cache(state, profile_id)
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
        **_base_ctx(request),
        "catalog": catalog,
        "has_more": len(full_catalog) > page_size,
        "total_catalog": len(full_catalog),
        "requests_row": requests_row,
        "has_more_requests": has_more_requests,
        "shorts_catalog": shorts_catalog,
        "has_more_shorts": has_more_shorts,
        "shorts_enabled": _shorts_enabled(request, cs),
        "time_info": time_info,
        "schedule_info": schedule_info,
        "cat_info": cat_info,
        "channel_pills": channel_pills,
        "hero_highlights": hero_highlights,
        "error_message": error_message,
    })


@app.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request):
    """Today's watch log — per-video breakdown and total."""
    wl_cfg = request.app.state.wl_config
    cs = get_child_store(request)
    tz = wl_cfg.timezone if wl_cfg else ""
    today = get_today_str(tz)
    bounds = get_day_utc_bounds(today, tz)
    breakdown = cs.get_daily_watch_breakdown(today, utc_bounds=bounds)
    time_info = _get_time_limit_info(store=cs, wl_cfg=wl_cfg)
    cat_info = _get_category_time_info(store=cs, wl_cfg=wl_cfg)
    total_min = sum(v["minutes"] for v in breakdown)
    _annotate_categories(breakdown, cs)
    return templates.TemplateResponse("activity.html", {
        **_base_ctx(request),
        "breakdown": breakdown,
        "total_min": round(total_min, 1),
        "time_info": time_info,
        "cat_info": cat_info,
    })


@app.get("/search", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def search_videos(request: Request, q: str = Query("", max_length=200)):
    """Search results via yt-dlp."""
    if not q:
        return RedirectResponse(url="/", status_code=303)

    state = request.app.state
    cs = get_child_store(request)

    # Block search queries that contain filtered words
    word_patterns = _get_word_filter_patterns(state)
    if word_patterns:
        if any(p.search(q) for p in word_patterns):
            cs.record_search(q, 0)
            csrf_token = _get_csrf_token(request)
            return templates.TemplateResponse("search.html", {
                **_base_ctx(request),
                "results": [],
                "query": q,
                "csrf_token": csrf_token,
            })

    video_id = extract_video_id(q)
    fetch_failed = False

    if video_id:
        metadata = await extract_metadata(video_id)
        results = [metadata] if metadata else []
        if not metadata:
            fetch_failed = True
    else:
        yt_cfg = state.youtube_config
        max_results = yt_cfg.search_max_results if yt_cfg else 10
        results = await search(q, max_results=max_results)

    # Filter out blocked channels
    blocked = cs.get_blocked_channels_set()
    if blocked:
        results = [r for r in results if r.get('channel_name', '').lower() not in blocked]

    # Filter out videos with blocked words in title (word-boundary match)
    if word_patterns:
        results = [
            r for r in results
            if not any(p.search(r.get('title', '')) for p in word_patterns)
        ]

    # Hide Shorts from search when disabled
    if not _shorts_enabled(request, cs):
        results = [r for r in results if not r.get('is_short')]

    # Log search query
    cs.record_search(q, len(results))

    csrf_token = _get_csrf_token(request)
    error_message = _ERROR_MESSAGES["fetch_failed"] if fetch_failed else ""
    return templates.TemplateResponse("search.html", {
        **_base_ctx(request),
        "results": results,
        "query": q,
        "csrf_token": csrf_token,
        "error_message": error_message,
    })


@app.post("/request")
@limiter.limit("10/minute")
async def request_video(
    request: Request,
    video_id: str = Form(..., max_length=100),
    csrf_token: str = Form(""),
):
    """Submit video for approval."""
    if not _validate_csrf(request, csrf_token):
        return RedirectResponse(url="/", status_code=303)

    extracted_id = extract_video_id(video_id)
    if extracted_id:
        video_id = extracted_id

    if not VIDEO_ID_RE.match(video_id):
        return RedirectResponse(url="/?error=invalid_video", status_code=303)

    state = request.app.state
    cs = get_child_store(request)
    profile_id = cs.profile_id

    existing = cs.get_video(video_id)
    if existing:
        if existing["status"] == "approved":
            return RedirectResponse(url=f"/watch/{video_id}", status_code=303)
        return RedirectResponse(url=f"/pending/{video_id}", status_code=303)

    metadata = await extract_metadata(video_id)
    if not metadata:
        return RedirectResponse(url="/?error=fetch_failed", status_code=303)

    channel_name = metadata['channel_name']
    channel_id = metadata.get('channel_id')
    is_short = metadata.get('is_short', False)

    # Check if channel is blocked → auto-deny
    if cs.is_channel_blocked(channel_name, channel_id=channel_id or ""):
        video = cs.add_video(
            video_id=metadata['video_id'],
            title=metadata['title'],
            channel_name=channel_name,
            thumbnail_url=metadata.get('thumbnail_url'),
            duration=metadata.get('duration'),
            channel_id=channel_id,
            is_short=is_short,
        )
        cs.update_status(video_id, "denied")
        _invalidate_catalog_cache(state)
        return templates.TemplateResponse("denied.html", {
            **_base_ctx(request),
            "video": cs.get_video(video_id),
        })

    # Check if channel is allowlisted → auto-approve
    if cs.is_channel_allowed(channel_name, channel_id=channel_id or ""):
        video = cs.add_video(
            video_id=metadata['video_id'],
            title=metadata['title'],
            channel_name=channel_name,
            thumbnail_url=metadata.get('thumbnail_url'),
            duration=metadata.get('duration'),
            channel_id=channel_id,
            is_short=is_short,
        )
        cs.update_status(video_id, "approved")
        _invalidate_catalog_cache(state)
        return RedirectResponse(url=f"/watch/{video_id}", status_code=303)

    video = cs.add_video(
        video_id=metadata['video_id'],
        title=metadata['title'],
        channel_name=channel_name,
        thumbnail_url=metadata.get('thumbnail_url'),
        duration=metadata.get('duration'),
        channel_id=channel_id,
        is_short=is_short,
    )

    notify_cb = state.notify_callback
    if notify_cb:
        await notify_cb(video, profile_id)

    return RedirectResponse(url=f"/pending/{video_id}", status_code=303)


@app.get("/pending/{video_id}", response_class=HTMLResponse)
async def pending_video(request: Request, video_id: str):
    """Waiting screen with polling."""
    if not VIDEO_ID_RE.match(video_id):
        return RedirectResponse(url="/", status_code=303)
    cs = get_child_store(request)
    video = cs.get_video(video_id)

    if not video:
        return RedirectResponse(url="/", status_code=303)

    if video["status"] == "approved":
        return RedirectResponse(url=f"/watch/{video_id}", status_code=303)
    elif video["status"] == "denied":
        return templates.TemplateResponse("denied.html", {
            **_base_ctx(request),
            "video": video,
        })
    else:
        w_cfg = request.app.state.web_config
        poll_interval = w_cfg.poll_interval if w_cfg else 3000
        return templates.TemplateResponse("pending.html", {
            **_base_ctx(request),
            "video": video,
            "poll_interval": poll_interval,
        })


@app.get("/watch/{video_id}", response_class=HTMLResponse)
async def watch_video(request: Request, video_id: str):
    """Play approved video (embed)."""
    if not VIDEO_ID_RE.match(video_id):
        return RedirectResponse(url="/", status_code=303)
    state = request.app.state
    wl_cfg = state.wl_config
    cs = get_child_store(request)
    video = cs.get_video(video_id)

    if not video:
        # Video not in DB — auto-approve if channel is allowlisted
        metadata = await extract_metadata(video_id)
        if not metadata:
            return RedirectResponse(url="/", status_code=303)
        if not cs.is_channel_allowed(metadata['channel_name'],
                                     channel_id=metadata.get('channel_id') or ""):
            return RedirectResponse(url="/", status_code=303)
        cs.add_video(
            video_id=metadata['video_id'],
            title=metadata['title'],
            channel_name=metadata['channel_name'],
            thumbnail_url=metadata.get('thumbnail_url'),
            duration=metadata.get('duration'),
            channel_id=metadata.get('channel_id'),
            is_short=metadata.get('is_short', False),
        )
        cs.update_status(video_id, "approved")
        _invalidate_catalog_cache(state)
        video = cs.get_video(video_id)

    if not video or video["status"] != "approved":
        return RedirectResponse(url="/", status_code=303)

    video_cat = _resolve_video_category(video, store=cs)
    cat_label = CAT_LABELS.get(video_cat, "Entertainment")
    cat_info = _get_category_time_info(store=cs, wl_cfg=wl_cfg)
    base = _base_ctx(request)
    time_info = None
    if cat_info:
        cat_budget = cat_info["categories"].get(video_cat, {})
        if cat_budget.get("exceeded"):
            available = []
            for c, info in cat_info["categories"].items():
                if not info["exceeded"] and c != video_cat:
                    c_label = CAT_LABELS.get(c, "Entertainment")
                    available.append({"name": c, "label": c_label, "remaining_min": info["remaining_min"]})
            return templates.TemplateResponse("timesup.html", {
                **base,
                "time_info": cat_budget,
                "category": cat_label,
                "available_categories": available,
                "next_start": _get_next_start_time(store=cs, wl_cfg=wl_cfg),
            })
        if cat_budget.get("limit_min", 0) > 0:
            time_info = cat_budget
    else:
        time_info = _get_time_limit_info(store=cs, wl_cfg=wl_cfg)
        if time_info and time_info["exceeded"]:
            return templates.TemplateResponse("timesup.html", {
                **base,
                "time_info": time_info,
                "next_start": _get_next_start_time(store=cs, wl_cfg=wl_cfg),
            })

    schedule_info = _get_schedule_info(store=cs, wl_cfg=wl_cfg)
    if schedule_info and not schedule_info["allowed"]:
        return templates.TemplateResponse("outsidehours.html", {
            **base,
            "schedule_info": schedule_info,
        })

    cs.record_view(video_id)
    request.session["watching"] = video_id

    embed_url = f"https://www.youtube-nocookie.com/embed/{video_id}?enablejsapi=1"

    return templates.TemplateResponse("watch.html", {
        **base,
        "video": video,
        "embed_url": embed_url,
        "time_info": time_info,
        "schedule_info": schedule_info,
        "video_cat": video_cat,
        "cat_label": cat_label,
        "is_short": bool(video.get("is_short")),
    })


@app.get("/api/status/{video_id}")
@limiter.limit("30/minute")
async def api_status(request: Request, video_id: str):
    """JSON status endpoint for polling."""
    if not VIDEO_ID_RE.match(video_id):
        return JSONResponse({"status": "not_found"})

    vs = request.app.state.video_store
    profile_id = request.session.get("child_id", "default")
    video = vs.get_video(video_id, profile_id=profile_id) if vs else None

    if not video:
        return JSONResponse({"status": "not_found"})

    return JSONResponse({"status": video["status"]})


@app.post("/api/watch-heartbeat")
@limiter.limit("30/minute")
async def watch_heartbeat(request: Request, body: HeartbeatRequest):
    """Log playback seconds and return remaining budget."""
    vid = body.video_id
    seconds = min(max(body.seconds, 0), 60)  # clamp 0-60

    if not VIDEO_ID_RE.match(vid):
        return JSONResponse({"error": "invalid"}, status_code=400)

    # Verify heartbeat matches the video currently being watched in this session
    if request.session.get("watching") != vid:
        return JSONResponse({"error": "not_watching"}, status_code=400)

    # Verify the video exists and is approved before accepting heartbeat
    state = request.app.state
    wl_cfg = state.wl_config
    cs = get_child_store(request)
    video = cs.get_video(vid)
    if not video or video["status"] != "approved":
        return JSONResponse({"error": "not_approved"}, status_code=400)

    # Check schedule window
    schedule_info = _get_schedule_info(store=cs, wl_cfg=wl_cfg)
    if schedule_info and not schedule_info["allowed"]:
        return JSONResponse({"error": "outside_schedule"}, status_code=403)

    # Clamp seconds to 0 if heartbeat arrives faster than expected interval
    now = time.monotonic()
    last_hb = state.last_heartbeat
    last = last_hb.get(vid, 0.0)
    if last and (now - last) < _HEARTBEAT_MIN_INTERVAL:
        seconds = 0
    last_hb[vid] = now

    # Periodic cleanup: evict stale entries to prevent unbounded growth
    if now - state.heartbeat_last_cleanup > _HEARTBEAT_EVICT_AGE:
        state.heartbeat_last_cleanup = now
        stale = [k for k, t in last_hb.items() if now - t > _HEARTBEAT_EVICT_AGE]
        for k in stale:
            del last_hb[k]

    if seconds > 0:
        cs.record_watch_seconds(vid, seconds)

    # Per-category time limit check
    video_cat = _resolve_video_category(video, store=cs) if video else "fun"
    cat_info = _get_category_time_info(store=cs, wl_cfg=wl_cfg)
    profile_id = cs.profile_id
    remaining = -1
    time_limit_cb = state.time_limit_notify_cb
    if cat_info:
        cat_budget = cat_info["categories"].get(video_cat, {})
        if cat_budget.get("limit_min", 0) > 0:
            remaining = cat_budget.get("remaining_sec", -1)
        if cat_budget.get("exceeded") and time_limit_cb:
            await time_limit_cb(cat_budget["used_min"], cat_budget["limit_min"], video_cat, profile_id)
    else:
        time_info = _get_time_limit_info(store=cs, wl_cfg=wl_cfg)
        remaining = time_info["remaining_sec"] if time_info else -1
        if time_info and time_info["exceeded"] and time_limit_cb:
            await time_limit_cb(time_info["used_min"], time_info["limit_min"], "", profile_id)

    return JSONResponse({"remaining": remaining})
