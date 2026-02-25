"""Authentication routes: profile picker, PIN login, session management."""

import hmac
import secrets

from fastapi import APIRouter, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from web.shared import templates, limiter
from web.helpers import AVATAR_ICONS, AVATAR_COLORS, get_csrf_token, validate_csrf

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, profile: str = Query("", max_length=50)):
    """Profile picker -> optional PIN entry."""
    vs = request.app.state.video_store
    profiles = vs.get_profiles() if vs else []

    # Auto-login: single profile with no PIN
    if len(profiles) == 1 and not profiles[0]["pin"]:
        request.session["child_id"] = profiles[0]["id"]
        request.session["child_name"] = profiles[0]["display_name"]
        request.session["avatar_icon"] = profiles[0].get("avatar_icon") or ""
        request.session["avatar_color"] = profiles[0].get("avatar_color") or ""
        return RedirectResponse(url="/", status_code=303)

    csrf_token = get_csrf_token(request)

    # If a profile is selected and it needs a PIN, show PIN input
    if profile:
        p = vs.get_profile(profile) if vs else None
        if p and not p["pin"]:
            # No PIN required -- log in immediately
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

    # Single profile with PIN -- go straight to PIN entry
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


@router.post("/login")
@limiter.limit("5/hour")
async def login_submit(
    request: Request,
    pin: str = Form(""),
    profile_id: str = Form(""),
    csrf_token: str = Form(""),
):
    """Validate PIN and create session for selected profile."""
    if not validate_csrf(request, csrf_token):
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


@router.get("/switch-profile")
async def switch_profile(request: Request):
    """Clear current session and return to profile picker."""
    request.session.pop("child_id", None)
    request.session.pop("child_name", None)
    request.session.pop("avatar_icon", None)
    request.session.pop("avatar_color", None)
    return RedirectResponse(url="/login", status_code=303)
