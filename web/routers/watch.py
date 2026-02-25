"""Watch, pending, status polling, and heartbeat routes."""

import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from web.shared import templates, limiter
from web.deps import get_child_store, get_extractor
from web.helpers import (
    VIDEO_ID_RE, HeartbeatRequest,
    _HEARTBEAT_MIN_INTERVAL, _HEARTBEAT_EVICT_AGE,
    base_ctx, resolve_video_category,
    get_time_limit_info, get_category_time_info,
    get_schedule_info, get_next_start_time,
)
from web.cache import invalidate_catalog_cache
from utils import CAT_LABELS

router = APIRouter()


@router.get("/pending/{video_id}", response_class=HTMLResponse)
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
            **base_ctx(request),
            "video": video,
        })
    else:
        w_cfg = request.app.state.web_config
        poll_interval = w_cfg.poll_interval if w_cfg else 3000
        return templates.TemplateResponse("pending.html", {
            **base_ctx(request),
            "video": video,
            "poll_interval": poll_interval,
        })


@router.get("/watch/{video_id}", response_class=HTMLResponse)
async def watch_video(request: Request, video_id: str):
    """Play approved video (embed)."""
    if not VIDEO_ID_RE.match(video_id):
        return RedirectResponse(url="/", status_code=303)
    state = request.app.state
    wl_cfg = state.wl_config
    cs = get_child_store(request)
    video = cs.get_video(video_id)

    if not video:
        # Video not in DB -- auto-approve if channel is allowlisted
        extractor = get_extractor(request)
        metadata = await extractor.extract_metadata(video_id)
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
        invalidate_catalog_cache(state)
        video = cs.get_video(video_id)

    if not video or video["status"] != "approved":
        return RedirectResponse(url="/", status_code=303)

    video_cat = resolve_video_category(video, store=cs)
    cat_label = CAT_LABELS.get(video_cat, "Entertainment")
    cat_info = get_category_time_info(store=cs, wl_cfg=wl_cfg)
    base = base_ctx(request)
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
                "next_start": get_next_start_time(store=cs, wl_cfg=wl_cfg),
            })
        if cat_budget.get("limit_min", 0) > 0:
            time_info = cat_budget
    else:
        time_info = get_time_limit_info(store=cs, wl_cfg=wl_cfg)
        if time_info and time_info["exceeded"]:
            return templates.TemplateResponse("timesup.html", {
                **base,
                "time_info": time_info,
                "next_start": get_next_start_time(store=cs, wl_cfg=wl_cfg),
            })

    schedule_info = get_schedule_info(store=cs, wl_cfg=wl_cfg)
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


@router.get("/api/status/{video_id}")
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


@router.post("/api/watch-heartbeat")
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
    schedule_info = get_schedule_info(store=cs, wl_cfg=wl_cfg)
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
    video_cat = resolve_video_category(video, store=cs) if video else "fun"
    cat_info = get_category_time_info(store=cs, wl_cfg=wl_cfg)
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
        time_info = get_time_limit_info(store=cs, wl_cfg=wl_cfg)
        remaining = time_info["remaining_sec"] if time_info else -1
        if time_info and time_info["exceeded"] and time_limit_cb:
            await time_limit_cb(time_info["used_min"], time_info["limit_min"], "", profile_id)

    return JSONResponse({"remaining": remaining})
