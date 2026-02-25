"""YouTube script proxy routes: iframe API + widget API."""

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from web.cache import fetch_yt_scripts, yt_cache_stale

router = APIRouter()


@router.get("/api/yt-iframe-api.js")
async def yt_iframe_api_proxy(request: Request):
    """Proxy the YouTube IFrame API loader with widget URL rewritten to local."""
    state = request.app.state
    if getattr(state, "yt_iframe_api_cache", None) is None or yt_cache_stale(state):
        await fetch_yt_scripts(state)
    if not getattr(state, "yt_iframe_api_cache", None):
        return PlainTextResponse("// iframe API unavailable", media_type="application/javascript")
    return PlainTextResponse(state.yt_iframe_api_cache, media_type="application/javascript")


@router.get("/api/yt-widget-api.js")
async def yt_widget_api_proxy(request: Request):
    """Proxy the YouTube widget API script."""
    state = request.app.state
    if getattr(state, "yt_widget_api_cache", None) is None or yt_cache_stale(state):
        await fetch_yt_scripts(state)
    if not getattr(state, "yt_widget_api_cache", None):
        return PlainTextResponse("// widget API unavailable", media_type="application/javascript")
    return PlainTextResponse(state.yt_widget_api_cache, media_type="application/javascript")
