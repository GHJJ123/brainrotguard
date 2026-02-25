"""FastAPI dependency providers — read from app.state, set by main.py."""

from fastapi import Request

from data.child_store import ChildStore


def get_video_store(request: Request):
    """VideoStore instance."""
    return request.app.state.video_store


def get_child_store(request: Request) -> ChildStore:
    """ChildStore scoped to the current session's profile."""
    child_id = request.session.get("child_id", "default")
    return ChildStore(request.app.state.video_store, child_id)


def get_web_config(request: Request):
    """WebConfig instance."""
    return request.app.state.web_config


def get_wl_config(request: Request):
    """WatchLimitsConfig instance."""
    return request.app.state.wl_config


def get_youtube_config(request: Request):
    """YouTubeConfig instance."""
    return request.app.state.youtube_config


def get_notify_cb(request: Request):
    """Async callback for new video request notifications."""
    return request.app.state.notify_callback


def get_time_limit_cb(request: Request):
    """Async callback for time limit reached notifications."""
    return request.app.state.time_limit_notify_cb
