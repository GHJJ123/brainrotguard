"""HTTP middleware: security headers + PIN-based profile authentication."""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

logger = logging.getLogger(__name__)

# API paths safe to access without PIN auth
_API_AUTH_EXEMPT = ("/api/status/", "/api/yt-iframe-api.js", "/api/yt-widget-api.js")


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
        profiles = []
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
        if not self.pin and (not vs or not profiles):
            return await call_next(request)

        # Return JSON 401 for API endpoints instead of redirect
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse(url="/login", status_code=303)
