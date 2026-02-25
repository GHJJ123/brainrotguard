"""FastAPI application — creates app, mounts routers, configures startup."""

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded

from web.shared import templates, limiter, static_dir, register_filters
from web.cache import channel_cache_loop, init_app_state, invalidate_channel_cache, invalidate_catalog_cache
from web.middleware import SecurityHeadersMiddleware, PinAuthMiddleware

from web.routers.auth import router as auth_router
from web.routers.profile import router as profile_router
from web.routers.ytproxy import router as ytproxy_router
from web.routers.catalog import router as catalog_router
from web.routers.pages import router as pages_router
from web.routers.search import router as search_router
from web.routers.watch import router as watch_router

app = FastAPI(title="BrainRotGuard")
app.state.limiter = limiter
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Register custom Jinja2 filters
register_filters()

# Include routers
app.include_router(auth_router)
app.include_router(profile_router)
app.include_router(ytproxy_router)
app.include_router(catalog_router)
app.include_router(pages_router)
app.include_router(search_router)
app.include_router(watch_router)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return HTMLResponse(
        content="<h1>Too many requests</h1><p>Please wait a moment and try again.</p>",
        status_code=429,
    )


@app.on_event("startup")
async def _start_channel_cache():
    state = app.state
    state.channel_cache_task = asyncio.create_task(channel_cache_loop(state))
