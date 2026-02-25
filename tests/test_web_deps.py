"""Tests for web/deps.py — dependency injection from app.state."""

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from web.deps import (
    get_video_store,
    get_child_store,
    get_web_config,
    get_wl_config,
    get_youtube_config,
    get_notify_cb,
    get_time_limit_cb,
    get_extractor,
)


def _make_request(state_attrs=None, session=None):
    """Build a fake Request with app.state and session."""
    state = SimpleNamespace(**(state_attrs or {}))
    app = SimpleNamespace(state=state)
    req = SimpleNamespace(app=app, session=session or {})
    return req


class TestGetVideoStore:
    def test_returns_store_from_state(self):
        store = MagicMock()
        req = _make_request({"video_store": store})
        assert get_video_store(req) is store


class TestGetChildStore:
    def test_default_profile(self):
        store = MagicMock()
        req = _make_request({"video_store": store}, session={})
        cs = get_child_store(req)
        assert cs.profile_id == "default"

    def test_session_profile(self):
        store = MagicMock()
        req = _make_request({"video_store": store}, session={"child_id": "kid1"})
        cs = get_child_store(req)
        assert cs.profile_id == "kid1"


class TestConfigDeps:
    def test_get_web_config(self):
        cfg = MagicMock()
        req = _make_request({"web_config": cfg})
        assert get_web_config(req) is cfg

    def test_get_wl_config(self):
        cfg = MagicMock()
        req = _make_request({"wl_config": cfg})
        assert get_wl_config(req) is cfg

    def test_get_youtube_config(self):
        cfg = MagicMock()
        req = _make_request({"youtube_config": cfg})
        assert get_youtube_config(req) is cfg


class TestCallbackDeps:
    def test_get_notify_cb(self):
        cb = MagicMock()
        req = _make_request({"notify_callback": cb})
        assert get_notify_cb(req) is cb

    def test_get_time_limit_cb(self):
        cb = MagicMock()
        req = _make_request({"time_limit_notify_cb": cb})
        assert get_time_limit_cb(req) is cb


class TestExtractorDep:
    def test_get_extractor(self):
        ext = MagicMock()
        req = _make_request({"extractor": ext})
        assert get_extractor(req) is ext
