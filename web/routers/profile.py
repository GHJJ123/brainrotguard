"""Profile management routes: avatar customization."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from web.shared import limiter
from web.helpers import AVATAR_ICONS, AVATAR_COLORS

router = APIRouter()


@router.post("/api/avatar")
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
