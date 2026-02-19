import hashlib
import hmac
import logging
import random
import re
import secrets
import time
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request, Form, Query
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

from utils import get_today_str, get_day_utc_bounds, is_within_schedule, format_time_12h
from youtube.extractor import extract_video_id, extract_metadata, search, fetch_channel_videos, format_duration, configure_timeout

VIDEO_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{11}$')

logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="BrainRotGuard")
app.state.limiter = limiter

templates_dir = Path(__file__).parent / "templates"
static_dir = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(templates_dir))
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# These get set by main.py after initialization
video_store = None
notify_callback = None
time_limit_notify_cb = None
youtube_config = None
web_config = None
wl_config = None  # WatchLimitsConfig


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
            "img-src 'self' https://i.ytimg.com https://i1.ytimg.com https://i2.ytimg.com "
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
    """Require PIN authentication when configured."""

    def __init__(self, app, pin: str = ""):
        super().__init__(app)
        self.pin = pin

    async def dispatch(self, request: Request, call_next) -> Response:
        if not self.pin:
            return await call_next(request)
        # Allow unauthenticated access to login, static assets, and specific read-only APIs
        if request.url.path.startswith(("/login", "/static")):
            return await call_next(request)
        if request.url.path.startswith(_API_AUTH_EXEMPT):
            return await call_next(request)
        if request.session.get("authenticated"):
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


# Cached YouTube IFrame API + widget API scripts (fetched once, refreshed after TTL)
_yt_iframe_api_cache: str | None = None
_yt_widget_api_cache: str | None = None
_yt_widget_api_url: str | None = None
_yt_cache_time: float = 0.0
_YT_CACHE_TTL = 86400  # 24 hours
_YT_SCRIPTURL_RE = re.compile(r"(var\s+scriptUrl\s*=\s*)'([^']+)'")


_YT_ALLOWED_HOSTS = {"www.youtube.com", "youtube.com", "s.ytimg.com", "www.google.com"}


async def _fetch_yt_scripts():
    """Fetch and cache the iframe API loader + widget API script from youtube.com."""
    global _yt_iframe_api_cache, _yt_widget_api_cache, _yt_widget_api_url, _yt_cache_time
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://www.youtube.com/iframe_api")
            resp.raise_for_status()
            raw = resp.text

            # Extract the widget API URL and rewrite it to our proxy
            m = _YT_SCRIPTURL_RE.search(raw)
            if m:
                extracted_url = m.group(2).replace("\\/", "/")
                # Validate extracted URL against allowlist before fetching
                parsed = urlparse(extracted_url)
                if parsed.hostname not in _YT_ALLOWED_HOSTS:
                    logger.error("Rejected widget API URL with unexpected host: %s", parsed.hostname)
                    extracted_url = None
                _yt_widget_api_url = extracted_url
                # Only rewrite scriptUrl to our proxy if the widget URL passed validation
                if extracted_url:
                    raw = _YT_SCRIPTURL_RE.sub(r"\1'\\/api\\/yt-widget-api.js'", raw)
            else:
                logger.warning("scriptUrl pattern not found in YouTube iframe API response")

            _yt_iframe_api_cache = raw
            logger.info("YT iframe API SHA-256: %s", hashlib.sha256(raw.encode()).hexdigest())

            if _yt_widget_api_url:
                resp2 = await client.get(_yt_widget_api_url)
                resp2.raise_for_status()
                _yt_widget_api_cache = resp2.text
                logger.info("YT widget API SHA-256: %s", hashlib.sha256(resp2.text.encode()).hexdigest())

            _yt_cache_time = time.monotonic()
    except httpx.HTTPError as e:
        if _yt_iframe_api_cache is not None:
            logger.warning("Failed to refresh YouTube scripts, serving stale cache: %s", e)
        else:
            logger.error("Failed to fetch YouTube scripts (no cache available): %s", e)


def _yt_cache_stale() -> bool:
    return _yt_cache_time == 0.0 or (time.monotonic() - _yt_cache_time) > _YT_CACHE_TTL


@app.get("/api/yt-iframe-api.js")
async def yt_iframe_api_proxy():
    """Proxy the YouTube IFrame API loader with widget URL rewritten to local."""
    if _yt_iframe_api_cache is None or _yt_cache_stale():
        await _fetch_yt_scripts()
    if not _yt_iframe_api_cache:
        return PlainTextResponse("// iframe API unavailable", media_type="application/javascript")
    return PlainTextResponse(_yt_iframe_api_cache, media_type="application/javascript")


@app.get("/api/yt-widget-api.js")
async def yt_widget_api_proxy():
    """Proxy the YouTube widget API script."""
    if _yt_widget_api_cache is None or _yt_cache_stale():
        await _fetch_yt_scripts()
    if not _yt_widget_api_cache:
        return PlainTextResponse("// widget API unavailable", media_type="application/javascript")
    return PlainTextResponse(_yt_widget_api_cache, media_type="application/javascript")


def setup(store, notify_cb, yt_config=None, w_config=None,
          wl_cfg=None, time_limit_cb=None):
    """Called by main.py to inject dependencies."""
    global video_store, notify_callback, youtube_config, web_config
    global wl_config, time_limit_notify_cb
    video_store = store
    notify_callback = notify_cb
    youtube_config = yt_config
    web_config = w_config
    wl_config = wl_cfg
    time_limit_notify_cb = time_limit_cb
    if yt_config and yt_config.ydl_timeout:
        configure_timeout(yt_config.ydl_timeout)

    # Configure middleware (must happen before first request)
    # Priority: config value > persisted DB value > generate + persist new
    if w_config and w_config.session_secret:
        session_secret = w_config.session_secret
    else:
        session_secret = store.get_setting("session_secret")
        if not session_secret:
            session_secret = secrets.token_hex(32)
            store.set_setting("session_secret", session_secret)
            logger.info("Generated and persisted new session secret")
    pin = w_config.pin if w_config else ""

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(PinAuthMiddleware, pin=pin)
    app.add_middleware(SessionMiddleware, secret_key=session_secret)


# Add format_duration to Jinja2 globals
templates.env.globals["format_duration"] = format_duration


# Heartbeat dedup: video_id -> monotonic timestamp of last heartbeat
_last_heartbeat: dict[str, float] = {}
_HEARTBEAT_MIN_INTERVAL = 10  # seconds (must be < client heartbeat interval)
_HEARTBEAT_EVICT_AGE = 120  # evict entries older than this (seconds)
_heartbeat_last_cleanup: float = 0.0

# Channel sidebar cache: allowlisted channels → fresh YouTube videos
_channel_cache: dict = {"channels": {}, "updated_at": 0.0}
_channel_cache_task = None  # prevent GC of background task
_CHANNEL_CACHE_TTL = 1800  # default; overridden by youtube_config.channel_cache_ttl


async def _refresh_channel_cache():
    """Fetch latest videos for each allowlisted channel and update cache."""
    import asyncio
    if not video_store:
        return
    allowed = video_store.get_channels_with_ids("allowed")
    if not allowed:
        _channel_cache["channels"] = {}
        _channel_cache["updated_at"] = time.monotonic()
        return
    max_vids = youtube_config.channel_cache_results if youtube_config else 200
    tasks = [fetch_channel_videos(name, max_results=max_vids, channel_id=cid) for name, cid, _handle, _cat in allowed]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    channels = {}
    for (ch_name, _, _h, _c), result in zip(allowed, results):
        if isinstance(result, Exception):
            logger.error("Channel cache fetch failed for '%s': %s", ch_name, result)
            channels[ch_name] = []
        else:
            channels[ch_name] = result
    _channel_cache["channels"] = channels
    _channel_cache["updated_at"] = time.monotonic()
    logger.info("Refreshed channel cache: %d channels", len(channels))


def invalidate_channel_cache():
    """Mark cache as stale so next page load triggers a refresh."""
    _channel_cache["updated_at"] = 0.0
    _invalidate_catalog_cache()
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_refresh_channel_cache())
    except RuntimeError:
        pass


async def _channel_cache_loop():
    """Background loop to refresh channel cache periodically."""
    import asyncio
    # Initial delay to let app fully start
    await asyncio.sleep(5)
    while True:
        try:
            await _refresh_channel_cache()
        except Exception as e:
            logger.error("Channel cache refresh error: %s", e)
        ttl = youtube_config.channel_cache_ttl if youtube_config else _CHANNEL_CACHE_TTL
        await asyncio.sleep(ttl)


@app.on_event("startup")
async def _start_channel_cache():
    import asyncio
    global _channel_cache_task
    _channel_cache_task = asyncio.create_task(_channel_cache_loop())


# Cached full catalog (invalidated on channel cache refresh or video status change)
_catalog_cache: list[dict] = []
_catalog_cache_time: float = 0.0


def _invalidate_catalog_cache():
    """Mark catalog cache as stale."""
    global _catalog_cache_time
    _catalog_cache_time = 0.0


def _build_catalog(channel_filter: str = "") -> list[dict]:
    """Build unified catalog: interleaved channel-cache videos + DB approved videos."""
    global _catalog_cache, _catalog_cache_time
    channels = _channel_cache.get("channels", {})

    # If filtering by channel, build on demand (not cached — small result set)
    if channel_filter:
        seen_ids = set()
        filtered = []
        for v in channels.get(channel_filter, []):
            vid = v.get("video_id", "")
            if vid and vid not in seen_ids:
                seen_ids.add(vid)
                filtered.append(dict(v))
        if video_store:
            for v in video_store.get_by_status("approved", channel_name=channel_filter):
                vid = v.get("video_id", "")
                if vid and vid not in seen_ids:
                    seen_ids.add(vid)
                    filtered.append(v)
        filtered.sort(key=lambda v: v.get("timestamp") or 0, reverse=True)
        # Attach category to each filtered video
        if video_store:
            _chan_cats = {}
            for ch_name, _cid, _h, cat in video_store.get_channels_with_ids("allowed"):
                if cat:
                    _chan_cats[ch_name] = cat
            for v in filtered:
                v["category"] = _chan_cats.get(v.get("channel_name", ""), v.get("category") or "fun")
        return filtered

    # Return cached full catalog if fresh
    cache_age = _channel_cache.get("updated_at", 0.0)
    if _catalog_cache and _catalog_cache_time >= cache_age and _catalog_cache_time > 0:
        return _catalog_cache

    # Round-robin interleave channels (each already newest-first from YouTube)
    # Copy dicts to avoid mutating shared channel cache references
    seen_ids = set()
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
                    if vid and vid not in seen_ids:
                        seen_ids.add(vid)
                        catalog.append(dict(v))
                    indices[i] += 1
                    added = True
            if not added:
                break

    # Append individually approved DB videos not already in channel set
    if video_store:
        for v in video_store.get_by_status("approved"):
            vid = v.get("video_id", "")
            if vid and vid not in seen_ids:
                seen_ids.add(vid)
                catalog.append(v)

    # Attach category to each catalog video (always refresh from channel setting)
    if video_store:
        _chan_cats = {}
        for ch_name, _cid, _h, cat in video_store.get_channels_with_ids("allowed"):
            if cat:
                _chan_cats[ch_name] = cat
        for v in catalog:
            v["category"] = _chan_cats.get(v.get("channel_name", ""), v.get("category") or "fun")

    _catalog_cache = catalog
    _catalog_cache_time = time.monotonic()
    return catalog


@app.get("/api/catalog")
@limiter.limit("30/minute")
async def api_catalog(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=100),
    channel: str = Query("", max_length=200),
    category: str = Query("", max_length=10),
):
    """Paginated catalog of all watchable videos."""
    full = _build_catalog(channel_filter=channel)
    if category:
        full = [v for v in full if v.get("category", "fun") == category]
    page = full[offset:offset + limit]
    return JSONResponse({
        "videos": page,
        "has_more": offset + limit < len(full),
        "total": len(full),
    })


def _get_time_limit_info() -> dict | None:
    """Get time limit info. Returns None if limits disabled."""
    if not video_store:
        return None
    limit_str = video_store.get_setting("daily_limit_minutes", "")
    if not limit_str and wl_config:
        limit_min = wl_config.daily_limit_minutes
    else:
        limit_min = int(limit_str) if limit_str else 0
    if limit_min == 0:
        return None
    tz = wl_config.timezone if wl_config else ""
    today = get_today_str(tz)
    bounds = get_day_utc_bounds(today, tz)
    # Add today's bonus minutes (auto-expires when date doesn't match)
    bonus_date = video_store.get_setting("daily_bonus_date", "")
    if bonus_date == today:
        bonus = int(video_store.get_setting("daily_bonus_minutes", "0") or "0")
        limit_min += bonus
    used_min = video_store.get_daily_watch_minutes(today, utc_bounds=bounds)
    remaining_min = max(0.0, limit_min - used_min)
    return {
        "limit_min": limit_min,
        "used_min": round(used_min, 1),
        "remaining_min": round(remaining_min, 1),
        "remaining_sec": int(remaining_min * 60),
        "exceeded": remaining_min <= 0,
    }


def _resolve_video_category(video: dict) -> str:
    """Resolve effective category: video override > channel default > fun."""
    cat = video.get("category")
    if cat:
        return cat
    channel_name = video.get("channel_name", "")
    if channel_name and video_store:
        ch_cat = video_store.get_channel_category(channel_name)
        if ch_cat:
            return ch_cat
    return "fun"


def _get_category_time_info() -> dict | None:
    """Get per-category time budget info. Returns None if no category limits configured."""
    if not video_store:
        return None
    edu_limit_str = video_store.get_setting("edu_limit_minutes", "")
    fun_limit_str = video_store.get_setting("fun_limit_minutes", "")
    edu_limit = int(edu_limit_str) if edu_limit_str else 0
    fun_limit = int(fun_limit_str) if fun_limit_str else 0
    # If neither category limit is set, fall back to legacy global limit
    if edu_limit == 0 and fun_limit == 0:
        return None
    tz = wl_config.timezone if wl_config else ""
    today = get_today_str(tz)
    bounds = get_day_utc_bounds(today, tz)
    usage = video_store.get_daily_watch_by_category(today, utc_bounds=bounds)
    # Bonus minutes apply to both categories
    bonus = 0
    bonus_date = video_store.get_setting("daily_bonus_date", "")
    if bonus_date == today:
        bonus = int(video_store.get_setting("daily_bonus_minutes", "0") or "0")

    result = {"categories": {}}
    for cat, limit in [("edu", edu_limit), ("fun", fun_limit)]:
        used = usage.get(cat, 0.0)
        # Uncategorized counts as fun
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


def _get_schedule_info() -> dict | None:
    """Get schedule window info. Returns None if no schedule configured."""
    if not video_store:
        return None
    start = video_store.get_setting("schedule_start", "")
    end = video_store.get_setting("schedule_end", "")
    if not start and not end:
        return None
    tz = wl_config.timezone if wl_config else ""
    allowed, unlock_time = is_within_schedule(start, end, tz)
    return {
        "allowed": allowed,
        "unlock_time": unlock_time,
        "start": format_time_12h(start) if start else "midnight",
        "end": format_time_12h(end) if end else "midnight",
    }


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """PIN entry page."""
    csrf_token = _get_csrf_token(request)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "csrf_token": csrf_token,
        "error": False,
    })


@app.post("/login")
@limiter.limit("5/hour")
async def login_submit(
    request: Request,
    pin: str = Form(...),
    csrf_token: str = Form(""),
):
    """Validate PIN and create session."""
    if not _validate_csrf(request, csrf_token):
        return RedirectResponse(url="/login", status_code=303)

    expected_pin = web_config.pin if web_config else ""
    if not expected_pin or hmac.compare_digest(pin, expected_pin):
        request.session["authenticated"] = True
        request.session["csrf_token"] = secrets.token_hex(32)
        return RedirectResponse(url="/", status_code=303)

    # Regenerate CSRF token on failed attempt to prevent replay
    new_csrf = secrets.token_hex(32)
    request.session["csrf_token"] = new_csrf
    return templates.TemplateResponse("login.html", {
        "request": request,
        "csrf_token": new_csrf,
        "error": True,
    })


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Homepage: search bar + unified video catalog."""
    page_size = 24
    full_catalog = _build_catalog()
    catalog = full_catalog[:page_size]
    time_info = _get_time_limit_info()
    schedule_info = _get_schedule_info()
    cat_info = _get_category_time_info()
    channel_videos = _channel_cache.get("channels", {})
    # Pick a random video from each channel for the hero carousel
    hero_highlights = []
    for ch_name, ch_vids in channel_videos.items():
        if ch_vids:
            hero_highlights.append(random.choice(ch_vids))
    random.shuffle(hero_highlights)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "catalog": catalog,
        "has_more": len(full_catalog) > page_size,
        "total_catalog": len(full_catalog),
        "time_info": time_info,
        "schedule_info": schedule_info,
        "cat_info": cat_info,
        "channel_videos": channel_videos,
        "hero_highlights": hero_highlights,
    })


@app.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request):
    """Today's watch log — per-video breakdown and total."""
    tz = wl_config.timezone if wl_config else ""
    today = get_today_str(tz)
    bounds = get_day_utc_bounds(today, tz)
    breakdown = video_store.get_daily_watch_breakdown(today, utc_bounds=bounds)
    time_info = _get_time_limit_info()
    cat_info = _get_category_time_info()
    total_min = sum(v["minutes"] for v in breakdown)
    # Resolve effective category for each breakdown entry
    if video_store:
        _chan_cats = {}
        for ch_name, _cid, _h, cat in video_store.get_channels_with_ids("allowed"):
            if cat:
                _chan_cats[ch_name] = cat
        for v in breakdown:
            v["category"] = _chan_cats.get(v.get("channel_name", ""), v.get("category") or "fun")
    return templates.TemplateResponse("activity.html", {
        "request": request,
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

    # Block search queries that contain filtered words
    filtered_words = video_store.get_word_filters_set()
    if filtered_words:
        word_patterns = [
            re.compile(r'\b' + re.escape(w) + r'\b', re.IGNORECASE)
            for w in filtered_words
        ]
        if any(p.search(q) for p in word_patterns):
            video_store.record_search(q, 0)
            csrf_token = _get_csrf_token(request)
            return templates.TemplateResponse("search.html", {
                "request": request,
                "results": [],
                "query": q,
                "csrf_token": csrf_token,
            })
    else:
        word_patterns = []

    video_id = extract_video_id(q)

    if video_id:
        metadata = await extract_metadata(video_id)
        results = [metadata] if metadata else []
    else:
        max_results = youtube_config.search_max_results if youtube_config else 10
        results = await search(q, max_results=max_results)

    # Filter out blocked channels
    blocked = video_store.get_blocked_channels_set()
    if blocked:
        results = [r for r in results if r.get('channel_name', '').lower() not in blocked]

    # Filter out videos with blocked words in title (word-boundary match)
    if word_patterns:
        results = [
            r for r in results
            if not any(p.search(r.get('title', '')) for p in word_patterns)
        ]

    # Log search query
    video_store.record_search(q, len(results))

    csrf_token = _get_csrf_token(request)
    return templates.TemplateResponse("search.html", {
        "request": request,
        "results": results,
        "query": q,
        "csrf_token": csrf_token,
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

    existing = video_store.get_video(video_id)
    if existing:
        if existing["status"] == "approved":
            return RedirectResponse(url=f"/watch/{video_id}", status_code=303)
        return RedirectResponse(url=f"/pending/{video_id}", status_code=303)

    metadata = await extract_metadata(video_id)
    if not metadata:
        return RedirectResponse(url="/?error=invalid_video", status_code=303)

    channel_name = metadata['channel_name']
    channel_id = metadata.get('channel_id')

    # Check if channel is blocked → auto-deny
    if video_store.is_channel_blocked(channel_name):
        video = video_store.add_video(
            video_id=metadata['video_id'],
            title=metadata['title'],
            channel_name=channel_name,
            thumbnail_url=metadata.get('thumbnail_url'),
            duration=metadata.get('duration'),
            channel_id=channel_id,
        )
        video_store.update_status(video_id, "denied")
        _invalidate_catalog_cache()
        return templates.TemplateResponse("denied.html", {
            "request": request,
            "video": video_store.get_video(video_id),
        })

    # Check if channel is allowlisted → auto-approve
    if video_store.is_channel_allowed(channel_name):
        video = video_store.add_video(
            video_id=metadata['video_id'],
            title=metadata['title'],
            channel_name=channel_name,
            thumbnail_url=metadata.get('thumbnail_url'),
            duration=metadata.get('duration'),
            channel_id=channel_id,
        )
        video_store.update_status(video_id, "approved")
        _invalidate_catalog_cache()
        return RedirectResponse(url=f"/watch/{video_id}", status_code=303)

    video = video_store.add_video(
        video_id=metadata['video_id'],
        title=metadata['title'],
        channel_name=channel_name,
        thumbnail_url=metadata.get('thumbnail_url'),
        duration=metadata.get('duration'),
        channel_id=channel_id,
    )

    if notify_callback:
        await notify_callback(video)

    return RedirectResponse(url=f"/pending/{video_id}", status_code=303)


@app.get("/pending/{video_id}", response_class=HTMLResponse)
async def pending_video(request: Request, video_id: str):
    """Waiting screen with polling."""
    video = video_store.get_video(video_id)

    if not video:
        return RedirectResponse(url="/", status_code=303)

    if video["status"] == "approved":
        return RedirectResponse(url=f"/watch/{video_id}", status_code=303)
    elif video["status"] == "denied":
        return templates.TemplateResponse("denied.html", {
            "request": request,
            "video": video
        })
    else:
        poll_interval = web_config.poll_interval if web_config else 3000
        return templates.TemplateResponse("pending.html", {
            "request": request,
            "video": video,
            "poll_interval": poll_interval,
        })


@app.get("/watch/{video_id}", response_class=HTMLResponse)
async def watch_video(request: Request, video_id: str):
    """Play approved video (embed)."""
    video = video_store.get_video(video_id)

    if not video:
        # Video not in DB — auto-approve if channel is allowlisted
        if not VIDEO_ID_RE.match(video_id):
            return RedirectResponse(url="/", status_code=303)
        metadata = await extract_metadata(video_id)
        if not metadata:
            return RedirectResponse(url="/", status_code=303)
        if not video_store.is_channel_allowed(metadata['channel_name']):
            return RedirectResponse(url="/", status_code=303)
        video_store.add_video(
            video_id=metadata['video_id'],
            title=metadata['title'],
            channel_name=metadata['channel_name'],
            thumbnail_url=metadata.get('thumbnail_url'),
            duration=metadata.get('duration'),
            channel_id=metadata.get('channel_id'),
        )
        video_store.update_status(video_id, "approved")
        _invalidate_catalog_cache()
        video = video_store.get_video(video_id)

    if not video or video["status"] != "approved":
        return RedirectResponse(url="/", status_code=303)

    # Check category-specific time limit before allowing playback
    video_cat = _resolve_video_category(video)
    cat_label = "Educational" if video_cat == "edu" else "Entertainment"
    cat_info = _get_category_time_info()
    time_info = None
    if cat_info:
        cat_budget = cat_info["categories"].get(video_cat, {})
        if cat_budget.get("exceeded"):
            # Find categories that still have time
            available = []
            for c, info in cat_info["categories"].items():
                if not info["exceeded"] and c != video_cat:
                    c_label = "Educational" if c == "edu" else "Entertainment"
                    available.append({"name": c, "label": c_label, "remaining_min": info["remaining_min"]})
            return templates.TemplateResponse("timesup.html", {
                "request": request,
                "time_info": cat_budget,
                "category": cat_label,
                "available_categories": available,
            })
        if cat_budget.get("limit_min", 0) > 0:
            time_info = cat_budget
    else:
        time_info = _get_time_limit_info()
        if time_info and time_info["exceeded"]:
            return templates.TemplateResponse("timesup.html", {
                "request": request,
                "time_info": time_info,
            })

    # Check schedule window before allowing playback
    schedule_info = _get_schedule_info()
    if schedule_info and not schedule_info["allowed"]:
        return templates.TemplateResponse("outsidehours.html", {
            "request": request,
            "schedule_info": schedule_info,
        })

    video_store.record_view(video_id)

    embed_url = f"https://www.youtube-nocookie.com/embed/{video_id}?enablejsapi=1"

    return templates.TemplateResponse("watch.html", {
        "request": request,
        "video": video,
        "embed_url": embed_url,
        "time_info": time_info,
        "schedule_info": schedule_info,
        "video_cat": video_cat,
        "cat_label": cat_label,
    })


@app.get("/api/status/{video_id}")
@limiter.limit("30/minute")
async def api_status(request: Request, video_id: str):
    """JSON status endpoint for polling."""
    video = video_store.get_video(video_id)

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

    # Verify the video exists and is approved before accepting heartbeat
    video = video_store.get_video(vid)
    if not video or video["status"] != "approved":
        return JSONResponse({"error": "not_approved"}, status_code=400)

    # Check schedule window
    schedule_info = _get_schedule_info()
    if schedule_info and not schedule_info["allowed"]:
        return JSONResponse({"error": "outside_schedule"}, status_code=403)

    # Clamp seconds to 0 if heartbeat arrives faster than expected interval
    now = time.monotonic()
    last = _last_heartbeat.get(vid, 0.0)
    if last and (now - last) < _HEARTBEAT_MIN_INTERVAL:
        seconds = 0
    _last_heartbeat[vid] = now

    # Periodic cleanup: evict stale entries to prevent unbounded growth
    global _heartbeat_last_cleanup
    if now - _heartbeat_last_cleanup > _HEARTBEAT_EVICT_AGE:
        _heartbeat_last_cleanup = now
        stale = [k for k, t in _last_heartbeat.items() if now - t > _HEARTBEAT_EVICT_AGE]
        for k in stale:
            del _last_heartbeat[k]

    if seconds > 0:
        video_store.record_watch_seconds(vid, seconds)

    # Per-category time limit check
    video_cat = _resolve_video_category(video) if video else "fun"
    cat_info = _get_category_time_info()
    remaining = -1
    if cat_info:
        cat_budget = cat_info["categories"].get(video_cat, {})
        if cat_budget.get("limit_min", 0) > 0:
            remaining = cat_budget.get("remaining_sec", -1)
        # Notify parent if category limit just reached
        if cat_budget.get("exceeded") and time_limit_notify_cb:
            await time_limit_notify_cb(cat_budget["used_min"], cat_budget["limit_min"], video_cat)
    else:
        time_info = _get_time_limit_info()
        remaining = time_info["remaining_sec"] if time_info else -1
        if time_info and time_info["exceeded"] and time_limit_notify_cb:
            await time_limit_notify_cb(time_info["used_min"], time_info["limit_min"])

    return JSONResponse({"remaining": remaining})
