"""FastAPI application — creates app, mounts routers, configures startup."""

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded

from i18n import app_name, t, normalize_locale
from web.shared import limiter, static_dir, register_filters
from web.cache import channel_cache_loop

from web.routers.auth import router as auth_router
from web.routers.profile import router as profile_router
from web.routers.ytproxy import router as ytproxy_router
from web.routers.catalog import router as catalog_router
from web.routers.pages import router as pages_router
from web.routers.pwa import router as pwa_router
from web.routers.search import router as search_router
from web.routers.watch import router as watch_router

app = FastAPI(title=app_name("en"))
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
app.include_router(pwa_router)
app.include_router(search_router)
app.include_router(watch_router)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    locale = normalize_locale(getattr(request.app.state, "locale", "en"))
    headers = {"Retry-After": "5"}
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            content={
                "error": "rate_limited",
                "message": t(locale, "Please wait a moment and try again."),
            },
            status_code=429,
            headers=headers,
        )
    return HTMLResponse(
        content=f"<h1>{t(locale, 'Too many requests')}</h1><p>{t(locale, 'Please wait a moment and try again.')}</p>",
        status_code=429,
        headers=headers,
    )


@app.on_event("startup")
async def _start_channel_cache():
    state = app.state
    state.channel_cache_task = asyncio.create_task(channel_cache_loop(state))
