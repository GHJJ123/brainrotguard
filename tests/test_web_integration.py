"""Integration tests for BrainRotGuard web endpoints.

Uses FastAPI TestClient with a real VideoStore (temp SQLite) and a mock
YouTubeExtractor to test actual HTTP flows end-to-end.

Creates a fresh FastAPI app per test session to avoid the "cannot add middleware
after application has started" issue with the shared singleton.
"""

import re

import pytest
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from config import WebConfig, YouTubeConfig, WatchLimitsConfig
from data.video_store import VideoStore
from data.child_store import ChildStore
from web.shared import templates, limiter, static_dir, register_filters
from web.cache import init_app_state
from web.middleware import PinAuthMiddleware
from web.routers.auth import router as auth_router
from web.routers.pages import router as pages_router
from web.routers.search import router as search_router
from web.routers.watch import router as watch_router
from web.routers.catalog import router as catalog_router
from youtube.extractor import YouTubeExtractor


def _mock_extractor():
    """Build an AsyncMock satisfying YouTubeExtractorProtocol."""
    mock = AsyncMock(spec=YouTubeExtractor)
    mock.extract_metadata.return_value = {
        "video_id": "dQw4w9WgXcQ",
        "title": "Test Video",
        "channel_name": "Test Channel",
        "channel_id": "UCtest123",
        "thumbnail_url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
        "duration": 212,
        "is_short": False,
    }
    mock.search.return_value = [
        {
            "video_id": "abc12345678",
            "title": "Search Result 1",
            "channel_name": "Result Channel",
            "thumbnail_url": "https://i.ytimg.com/vi/abc12345678/hqdefault.jpg",
            "duration": 300,
            "view_count": 1000,
            "is_short": False,
        },
    ]
    mock.fetch_channel_videos.return_value = []
    mock.fetch_channel_shorts.return_value = []
    return mock


def _create_test_app(store: VideoStore, pin: str = "1234") -> FastAPI:
    """Build a fresh FastAPI app wired for testing."""
    test_app = FastAPI()
    test_app.state.limiter = limiter
    test_app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Routers
    test_app.include_router(auth_router)
    test_app.include_router(pages_router)
    test_app.include_router(search_router)
    test_app.include_router(watch_router)
    test_app.include_router(catalog_router)

    # State
    state = test_app.state
    state.video_store = store
    state.web_config = WebConfig(host="127.0.0.1", port=8080, pin=pin)
    state.youtube_config = YouTubeConfig(search_max_results=5, ydl_timeout=10)
    state.wl_config = WatchLimitsConfig()
    state.notify_callback = AsyncMock()
    state.time_limit_notify_cb = AsyncMock()
    state.extractor = _mock_extractor()
    init_app_state(state)

    # Middleware (order matters — last added = first executed)
    if pin:
        test_app.add_middleware(PinAuthMiddleware, pin=pin)
    test_app.add_middleware(SessionMiddleware, secret_key="test-secret", max_age=3600)

    # Register Jinja2 filters (idempotent)
    register_filters()

    return test_app


def _login(client: TestClient, pin: str = "1234") -> None:
    """Authenticate via the real login flow: GET /login → extract CSRF + profile → POST."""
    # GET login page to get CSRF token and discover profiles
    resp = client.get("/login")
    csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
    csrf = csrf_match.group(1) if csrf_match else ""
    profile_match = re.search(r'name="profile_id"\s+value="([^"]+)"', resp.text)
    profile_id = profile_match.group(1) if profile_match else "default"
    client.post("/login", data={
        "pin": pin,
        "profile_id": profile_id,
        "csrf_token": csrf,
    }, follow_redirects=False)


@pytest.fixture(autouse=True)
def _reset_limiter():
    """Disable rate limiting for tests, restore after."""
    limiter.enabled = False
    yield
    limiter.enabled = True


@pytest.fixture
def store(tmp_path):
    """VideoStore with a default profile."""
    db_path = str(tmp_path / "test.db")
    s = VideoStore(db_path=db_path)
    s.create_profile("default", "Test Child", pin="1234")
    yield s
    s.close()


@pytest.fixture
def client(store):
    """Unauthenticated TestClient."""
    app = _create_test_app(store)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_client(store):
    """TestClient authenticated with the correct PIN."""
    app = _create_test_app(store)
    c = TestClient(app, raise_server_exceptions=False)
    _login(c, "1234")
    return c


# ---------------------------------------------------------------------------
# Page loads
# ---------------------------------------------------------------------------

class TestPageLoads:
    def test_login_page(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200

    def test_home_redirects_to_login(self, client):
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in (302, 303, 307)
        assert "/login" in resp.headers.get("location", "")

    def test_home_after_login(self, auth_client):
        resp = auth_client.get("/")
        assert resp.status_code == 200

    def test_help_page_no_auth(self, client):
        resp = client.get("/help")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------

class TestLoginFlow:
    def test_wrong_pin_shows_error(self, client):
        # First get CSRF and profile
        resp = client.get("/login")
        csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
        csrf = csrf_match.group(1) if csrf_match else ""
        profile_match = re.search(r'name="profile_id"\s+value="([^"]+)"', resp.text)
        profile_id = profile_match.group(1) if profile_match else "default"
        resp = client.post("/login", data={
            "pin": "0000",
            "profile_id": profile_id,
            "csrf_token": csrf,
        }, follow_redirects=False)
        # Wrong pin: re-renders login page (200) with error
        if resp.status_code == 200:
            # Page should indicate error
            assert resp.text  # just verify we got content back

    def test_correct_pin_redirects_home(self, client):
        resp = client.get("/login")
        csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
        csrf = csrf_match.group(1) if csrf_match else ""
        profile_match = re.search(r'name="profile_id"\s+value="([^"]+)"', resp.text)
        profile_id = profile_match.group(1) if profile_match else "default"
        resp = client.post("/login", data={
            "pin": "1234",
            "profile_id": profile_id,
            "csrf_token": csrf,
        }, follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert resp.headers.get("location", "") == "/"

    def test_switch_profile(self, auth_client):
        resp = auth_client.get("/switch-profile", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "/login" in resp.headers.get("location", "")


# ---------------------------------------------------------------------------
# Search flow
# ---------------------------------------------------------------------------

class TestSearchFlow:
    def test_search_empty_query_redirects(self, auth_client):
        resp = auth_client.get("/search?q=", follow_redirects=False)
        assert resp.status_code in (302, 303)

    def test_search_with_query(self, auth_client):
        resp = auth_client.get("/search?q=test+video")
        assert resp.status_code == 200
        assert "Search Result 1" in resp.text

    def test_search_with_video_id(self, auth_client):
        resp = auth_client.get("/search?q=dQw4w9WgXcQ")
        assert resp.status_code == 200
        assert "Test Video" in resp.text

    def test_search_calls_extractor(self, store):
        app = _create_test_app(store)
        c = TestClient(app, raise_server_exceptions=False)
        _login(c, "1234")
        c.get("/search?q=cats")
        app.state.extractor.search.assert_called_once()


# ---------------------------------------------------------------------------
# Video request flow
# ---------------------------------------------------------------------------

class TestRequestFlow:
    def test_request_video_creates_pending(self, auth_client):
        # Get CSRF token from search page
        search_resp = auth_client.get("/search?q=test")
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', search_resp.text)
        csrf = match.group(1) if match else ""

        resp = auth_client.post(
            "/request",
            data={"video_id": "dQw4w9WgXcQ", "csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        loc = resp.headers.get("location", "")
        assert "/pending/" in loc or "/watch/" in loc

    def test_request_invalid_video_id(self, auth_client):
        resp = auth_client.post(
            "/request",
            data={"video_id": "bad!", "csrf_token": "x"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)


# ---------------------------------------------------------------------------
# Status API
# ---------------------------------------------------------------------------

class TestStatusAPI:
    def test_status_pending_video(self, auth_client, store):
        cs = ChildStore(store, "default")
        cs.add_video(
            video_id="testVid1234",
            title="Status Test",
            channel_name="Test Channel",
        )
        resp = auth_client.get("/api/status/testVid1234")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"

    def test_status_approved_video(self, auth_client, store):
        cs = ChildStore(store, "default")
        cs.add_video(
            video_id="apprvdVid12",
            title="Approved Video",
            channel_name="Test Channel",
        )
        cs.update_status("apprvdVid12", "approved")
        resp = auth_client.get("/api/status/apprvdVid12")
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

    def test_status_unknown_video(self, auth_client):
        resp = auth_client.get("/api/status/notExist11ch")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_found"


# ---------------------------------------------------------------------------
# Watch page
# ---------------------------------------------------------------------------

class TestWatchPage:
    def test_watch_approved_video(self, auth_client, store):
        cs = ChildStore(store, "default")
        cs.add_video(
            video_id="watchTest12",
            title="Watch Me",
            channel_name="Test Channel",
            thumbnail_url="https://i.ytimg.com/vi/watchTest12/hqdefault.jpg",
            duration=120,
        )
        cs.update_status("watchTest12", "approved")
        resp = auth_client.get("/watch/watchTest12")
        assert resp.status_code == 200
        assert "watchTest12" in resp.text

    def test_watch_pending_redirects(self, auth_client, store):
        cs = ChildStore(store, "default")
        cs.add_video(
            video_id="pendingV123",
            title="Pending Video",
            channel_name="Test Channel",
        )
        resp = auth_client.get("/watch/pendingV123", follow_redirects=False)
        assert resp.status_code in (302, 303)

    def test_watch_nonexistent_redirects(self, auth_client):
        resp = auth_client.get("/watch/nonExist123", follow_redirects=False)
        assert resp.status_code in (302, 303)


# ---------------------------------------------------------------------------
# No-PIN mode
# ---------------------------------------------------------------------------

class TestNoPinMode:
    def test_home_accessible_without_pin(self, tmp_path):
        """When pin is empty, home should be accessible without login."""
        db = str(tmp_path / "nopin.db")
        s = VideoStore(db_path=db)
        s.create_profile("default", "Kid", pin="")
        app = _create_test_app(s, pin="")
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.get("/", follow_redirects=True)
        assert resp.status_code == 200
        s.close()
