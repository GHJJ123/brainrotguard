"""Search and video request routes."""

from fastapi import APIRouter, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from web.shared import templates, limiter
from web.deps import get_child_store, get_extractor
from web.helpers import (
    VIDEO_ID_RE, _ERROR_MESSAGES,
    base_ctx, get_csrf_token, validate_csrf, shorts_enabled,
)
from web.cache import (
    get_word_filter_patterns, title_matches_filter, invalidate_catalog_cache,
)
from youtube.extractor import extract_video_id

router = APIRouter()


@router.get("/search", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def search_videos(request: Request, q: str = Query("", max_length=200)):
    """Search results via yt-dlp."""
    if not q:
        return RedirectResponse(url="/", status_code=303)

    state = request.app.state
    cs = get_child_store(request)
    extractor = get_extractor(request)

    # Block search queries that contain filtered words
    word_patterns = get_word_filter_patterns(state)
    if word_patterns:
        if any(p.search(q) for p in word_patterns):
            cs.record_search(q, 0)
            csrf_token = get_csrf_token(request)
            return templates.TemplateResponse("search.html", {
                **base_ctx(request),
                "results": [],
                "query": q,
                "csrf_token": csrf_token,
            })

    video_id = extract_video_id(q)
    fetch_failed = False

    if video_id:
        metadata = await extractor.extract_metadata(video_id)
        results = [metadata] if metadata else []
        if not metadata:
            fetch_failed = True
    else:
        yt_cfg = state.youtube_config
        max_results = yt_cfg.search_max_results if yt_cfg else 10
        results = await extractor.search(q, max_results=max_results)

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
    if not shorts_enabled(request, cs):
        results = [r for r in results if not r.get('is_short')]

    # Log search query
    cs.record_search(q, len(results))

    csrf_token = get_csrf_token(request)
    error_message = _ERROR_MESSAGES["fetch_failed"] if fetch_failed else ""
    return templates.TemplateResponse("search.html", {
        **base_ctx(request),
        "results": results,
        "query": q,
        "csrf_token": csrf_token,
        "error_message": error_message,
    })


@router.post("/request")
@limiter.limit("10/minute")
async def request_video(
    request: Request,
    video_id: str = Form(..., max_length=100),
    csrf_token: str = Form(""),
):
    """Submit video for approval."""
    if not validate_csrf(request, csrf_token):
        return RedirectResponse(url="/", status_code=303)

    extracted_id = extract_video_id(video_id)
    if extracted_id:
        video_id = extracted_id

    if not VIDEO_ID_RE.match(video_id):
        return RedirectResponse(url="/?error=invalid_video", status_code=303)

    state = request.app.state
    cs = get_child_store(request)
    extractor = get_extractor(request)
    profile_id = cs.profile_id

    existing = cs.get_video(video_id)
    if existing:
        if existing["status"] == "approved":
            return RedirectResponse(url=f"/watch/{video_id}", status_code=303)
        return RedirectResponse(url=f"/pending/{video_id}", status_code=303)

    metadata = await extractor.extract_metadata(video_id)
    if not metadata:
        return RedirectResponse(url="/?error=fetch_failed", status_code=303)

    channel_name = metadata['channel_name']
    channel_id = metadata.get('channel_id')
    is_short = metadata.get('is_short', False)

    # Check if channel is blocked -> auto-deny
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
        invalidate_catalog_cache(state)
        return templates.TemplateResponse("denied.html", {
            **base_ctx(request),
            "video": cs.get_video(video_id),
        })

    # Check if channel is allowlisted -> auto-approve
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
        invalidate_catalog_cache(state)
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
