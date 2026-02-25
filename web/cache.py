"""Cache management: channel cache, catalog builders, YT script proxy, word filters."""

import asyncio
import hashlib
import logging
import re
import time
from urllib.parse import urlparse

import httpx

from data.child_store import ChildStore
from web.helpers import annotate_categories

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# YouTube script proxy
# ---------------------------------------------------------------------------

_YT_CACHE_TTL = 86400  # 24 hours
_YT_SCRIPTURL_RE = re.compile(r"(var\s+scriptUrl\s*=\s*)'([^']+)'")
_YT_ALLOWED_HOSTS = {"www.youtube.com", "youtube.com", "s.ytimg.com", "www.google.com"}


async def fetch_yt_scripts(state):
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


def yt_cache_stale(state) -> bool:
    return getattr(state, "yt_cache_time", 0.0) == 0.0 or (time.monotonic() - state.yt_cache_time) > _YT_CACHE_TTL


# ---------------------------------------------------------------------------
# App state initialization
# ---------------------------------------------------------------------------

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
    # Catalog cache (per-profile)
    state.catalog_caches = {}
    state.catalog_cache_times = {}
    # Word filter cache
    state.word_filter_cache = None
    # Heartbeat dedup
    state.last_heartbeat = {}
    state.heartbeat_last_cleanup = 0.0


# ---------------------------------------------------------------------------
# Channel cache
# ---------------------------------------------------------------------------

_CHANNEL_CACHE_TTL = 1800  # default; overridden by youtube_config.channel_cache_ttl


def get_profile_cache(state, profile_id: str) -> dict:
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
    cache = get_profile_cache(state, profile_id)
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
    extractor = getattr(state, "extractor", None)
    if extractor:
        tasks = [extractor.fetch_channel_videos(name, max_results=max_vids, channel_id=cid) for name, cid, _handle, _cat in allowed]
    else:
        from youtube.extractor import fetch_channel_videos
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
    shorts_enabled_val = False
    db_val = child_store.get_setting("shorts_enabled", "")
    if db_val:
        shorts_enabled_val = db_val.lower() == "true"
    elif yt_cfg:
        shorts_enabled_val = yt_cfg.shorts_enabled

    if shorts_enabled_val:
        shorts_max = max(max_vids // 4, 20)
        if extractor:
            shorts_tasks = [extractor.fetch_channel_shorts(name, max_results=shorts_max, channel_id=cid) for name, cid, _handle, _cat in allowed]
        else:
            from youtube.extractor import fetch_channel_shorts
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
    invalidate_catalog_cache(state)
    if profile_id:
        cache = get_profile_cache(state, profile_id)
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


async def channel_cache_loop(state):
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


# ---------------------------------------------------------------------------
# Word filter cache
# ---------------------------------------------------------------------------

def get_word_filter_patterns(state) -> list[re.Pattern]:
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


def title_matches_filter(title: str, patterns: list[re.Pattern]) -> bool:
    """Check if a video title matches any word filter pattern."""
    return any(p.search(title) for p in patterns)


# ---------------------------------------------------------------------------
# Catalog cache
# ---------------------------------------------------------------------------

def invalidate_catalog_cache(state, profile_id: str = ""):
    """Mark catalog cache and word filter cache as stale."""
    if profile_id:
        state.catalog_cache_times[profile_id] = 0.0
    else:
        for k in list(state.catalog_cache_times):
            state.catalog_cache_times[k] = 0.0
    state.word_filter_cache = None


def build_shorts_catalog(state, profile_id: str = "default") -> list[dict]:
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

    cache = get_profile_cache(state, profile_id)
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
        annotate_categories(shorts, child_store)

    wf = get_word_filter_patterns(state)
    if wf:
        shorts = [v for v in shorts if not title_matches_filter(v.get("title", ""), wf)]

    return shorts


def build_requests_row(state, limit: int = 50, profile_id: str = "default") -> list[dict]:
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
    annotate_categories(filtered, child_store)

    # Filter out titles matching word filters
    wf = get_word_filter_patterns(state)
    if wf:
        filtered = [v for v in filtered if not title_matches_filter(v.get("title", ""), wf)]

    return filtered


def build_catalog(state, channel_filter: str = "", profile_id: str = "default") -> list[dict]:
    """Build unified catalog for a profile."""
    cache = get_profile_cache(state, profile_id)
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
            annotate_categories(filtered, child_store)
        wf = get_word_filter_patterns(state)
        if wf:
            filtered = [v for v in filtered if not title_matches_filter(v.get("title", ""), wf)]
        return filtered

    # Check catalog cache (per-profile)
    cache_age = cache.get("updated_at", 0.0)
    cached = state.catalog_caches.get(profile_id)
    cache_time = state.catalog_cache_times.get(profile_id, 0.0)
    if cached and cache_time >= cache_age and cache_time > 0:
        return cached

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
        annotate_categories(catalog, child_store)

    wf = get_word_filter_patterns(state)
    if wf:
        catalog = [v for v in catalog if not title_matches_filter(v.get("title", ""), wf)]

    state.catalog_caches[profile_id] = catalog
    state.catalog_cache_times[profile_id] = time.monotonic()
    return catalog
