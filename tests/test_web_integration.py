"""Integration tests for BrainRotGuard web endpoints.

Uses httpx ASGITransport with a real VideoStore (temp SQLite) and a mock
YouTubeExtractor to test actual HTTP flows end-to-end.

Creates a fresh FastAPI app per test session to avoid the "cannot add middleware
after application has started" issue with the shared singleton.
"""

import asyncio
import html
import re

import pytest
import httpx
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request as StarletteRequest
from starlette.middleware.sessions import SessionMiddleware

from config import WebConfig, YouTubeConfig, WatchLimitsConfig
from data.video_store import VideoStore
from data.child_store import ChildStore
from web.shared import templates, limiter, static_dir, register_filters, _rate_limit_key
from web.cache import init_app_state, get_profile_cache
from web.middleware import PinAuthMiddleware
from web.routers.auth import router as auth_router
from web.routers.pages import router as pages_router
from web.routers.pwa import router as pwa_router
from web.routers.search import router as search_router
from web.routers.watch import router as watch_router
from web.routers.catalog import router as catalog_router
from youtube.extractor import YouTubeExtractor


class AppClient:
    """Small sync wrapper around AsyncClient to avoid TestClient hangs on Python 3.13."""

    def __init__(self, app: FastAPI, raise_server_exceptions: bool = False):
        self.app = app
        self._raise_server_exceptions = raise_server_exceptions
        self.cookies = httpx.Cookies()
        self.base_url = "http://testserver"

    async def _request_async(self, method: str, url: str, **kwargs) -> httpx.Response:
        follow_redirects = kwargs.pop("follow_redirects", True)
        transport = httpx.ASGITransport(
            app=self.app,
            raise_app_exceptions=self._raise_server_exceptions,
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url=self.base_url,
            cookies=self.cookies,
            follow_redirects=follow_redirects,
        ) as client:
            response = await client.request(method, url, **kwargs)
            self.cookies = client.cookies
            return response

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        return asyncio.run(self._request_async(method, url, **kwargs))

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)


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
    test_app.include_router(pwa_router)
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


def _login(client: AppClient, pin: str = "1234") -> None:
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
    return AppClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_client(store):
    """TestClient authenticated with the correct PIN."""
    app = _create_test_app(store)
    c = AppClient(app, raise_server_exceptions=False)
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

    def test_manifest_available_without_login(self, client):
        resp = client.get("/manifest.webmanifest")
        assert resp.status_code == 200
        assert "application/manifest+json" in resp.headers.get("content-type", "")
        assert '"display": "standalone"' in resp.text

    def test_service_worker_available_without_login(self, client):
        resp = client.get("/service-worker.js")
        assert resp.status_code == 200
        assert "application/javascript" in resp.headers.get("content-type", "")
        assert resp.headers.get("service-worker-allowed", "") == "/"
        # Version-agnostic: cache names get bumped, the test shouldn't break each time
        assert re.search(r'brainrotguard-static-v\d+', resp.text)

    def test_home_includes_pwa_metadata(self, auth_client):
        resp = auth_client.get("/")
        assert resp.status_code == 200
        assert 'rel="manifest" href="/manifest.webmanifest"' in resp.text
        assert 'navigator.serviceWorker.register("/service-worker.js")' in resp.text

    def test_home_includes_requests_link(self, auth_client):
        resp = auth_client.get("/")
        assert resp.status_code == 200
        assert 'href="/requests"' in resp.text

    def test_requests_page_shows_pending_and_one_off_approved_videos(self, auth_client, store):
        cs = ChildStore(store, "default")
        cs.add_channel("Allowed Ch", "allowed", channel_id="UCallowed")
        cs.add_video("pendreq1234", "Pending Request", "One Off Ch", channel_id="UConeoff", duration=180)
        cs.add_video("apprreq1234", "Approved Request", "One Off Ch", channel_id="UConeoff", duration=180)
        cs.update_status("apprreq1234", "approved")
        cs.add_video("allowreq1234", "Allowed Channel Video", "Allowed Ch", channel_id="UCallowed", duration=180)
        cs.update_status("allowreq1234", "approved")

        resp = auth_client.get("/requests")

        assert resp.status_code == 200
        assert "Pending Request" in resp.text
        assert "Approved Request" in resp.text
        assert "Allowed Channel Video" not in resp.text
        assert "Approved by Request" in resp.text

    def test_home_includes_active_row(self, auth_client):
        resp = auth_client.get("/")
        assert resp.status_code == 200
        assert "active-dismiss-btn" in resp.text
        assert "active-dismiss-icon" in resp.text
        assert "active-empty-state" in resp.text
        assert 'id="active-empty-title"' in resp.text
        assert 'id="active-empty-hint"' in resp.text
        assert "brg-dismissed-active:" in resp.text
        assert "countLoadedActiveQueryCards()" in resp.text
        assert "function cardMatchesActiveChannel" in resp.text
        assert "!cardMatchesActiveChannel(card, activeFilter)" in resp.text
        assert "card.style.display !== 'none'" in resp.text
        assert "window.addEventListener('pageshow'" in resp.text
        assert "var noVideosToShowText" in resp.text
        assert "var tryAnotherFilterText" in resp.text
        assert 'id="active-show-more-wrap"' not in resp.text
        assert 'id="active-collapse-btn"' in resp.text
        assert 'id="catalog-collapse-btn"' in resp.text
        assert "brg-section-collapsed:" in resp.text

    def test_home_active_row_shows_watch_progress_bar_for_started_video(self, auth_client, store):
        cs = ChildStore(store, "default")
        cs.add_video("actprog1234", "In Progress", "Test Channel", duration=180)
        cs.update_status("actprog1234", "approved")
        cs.record_view("actprog1234")
        cs.record_watch_seconds("actprog1234", 60)

        resp = auth_client.get("/")

        assert resp.status_code == 200
        assert "watch-progress-fill" in resp.text
        assert "width:33%" in resp.text

    def test_home_active_row_uses_saved_playback_position_for_progress(self, auth_client, store):
        cs = ChildStore(store, "default")
        cs.add_video("actpos12345", "Resume Position", "Test Channel", duration=180)
        cs.update_status("actpos12345", "approved")
        cs.update_playback_position("actpos12345", 150)

        resp = auth_client.get("/")

        assert resp.status_code == 200
        assert "width:83%" in resp.text

    def test_home_catalog_shows_watch_progress_bar_for_completed_video(self, auth_client, store):
        cs = ChildStore(store, "default")
        cs.add_video("catprog12345", "Finished Catalog Video", "Test Channel", duration=180)
        cs.update_status("catprog12345", "approved")
        cs.record_view("catprog12345")
        cs.record_watch_seconds("catprog12345", 180)

        resp = auth_client.get("/")

        assert resp.status_code == 200
        assert "Finished Catalog Video" in resp.text
        assert "width:100%" in resp.text

    def test_active_catalog_excludes_dismissed_ids(self, auth_client, store):
        cs = ChildStore(store, "default")
        cs.add_video("activekeep1", "Keep Active", "Test Channel", duration=180)
        cs.update_status("activekeep1", "approved")
        cs.add_video("activedrop1", "Drop Active", "Test Channel", duration=180)
        cs.update_status("activedrop1", "approved")

        resp = auth_client.get("/api/catalog?active=true&dismissed=activedrop1")

        assert resp.status_code == 200
        payload = resp.json()
        video_ids = {video["video_id"] for video in payload["videos"]}
        assert "activekeep1" in video_ids
        assert "activedrop1" not in video_ids

    def test_active_row_styles_hide_dismiss_until_hover(self, client):
        resp = client.get("/static/style.css")
        assert resp.status_code == 200
        assert ".active-card:hover .active-dismiss-btn" in resp.text
        assert ".active-dismiss-icon" in resp.text
        assert "opacity: 0;" in resp.text
        assert "pointer-events: none;" in resp.text

    def test_home_channel_pills_are_alphabetized(self, auth_client, store):
        store.add_channel("Zeta", "allowed", channel_id="UCzeta")
        store.add_channel("Alpha", "allowed", channel_id="UCalpha")
        store.add_channel("LEGO", "allowed", channel_id="UClego")
        app_state = auth_client.app.state
        profile_cache = get_profile_cache(app_state, "default")
        profile_cache["channels"] = {
            "UCzeta": [{"video_id": "zetavid0001", "channel_name": "Zeta", "channel_id": "UCzeta"}],
            "UCalpha": [{"video_id": "alphavid001", "channel_name": "Alpha", "channel_id": "UCalpha"}],
            "UClego": [{"video_id": "legovid0001", "channel_name": "LEGO", "channel_id": "UClego"}],
        }
        profile_cache["id_to_name"] = {
            "UCzeta": "Zeta",
            "UCalpha": "Alpha",
            "UClego": "LEGO",
        }

        resp = auth_client.get("/")

        assert resp.status_code == 200
        alpha_pos = resp.text.index('data-channel="UCalpha">' + html.escape("Alpha"))
        lego_pos = resp.text.index('data-channel="UClego">' + html.escape("LEGO"))
        zeta_pos = resp.text.index('data-channel="UCzeta">' + html.escape("Zeta"))
        assert alpha_pos < lego_pos < zeta_pos

    def test_home_activity_is_limited_to_six_items(self, auth_client, store):
        cs = ChildStore(store, "default")
        for idx in range(7):
            video_id = f"actlim{idx:05d}"
            cs.add_video(video_id, f"Activity {idx}", "Test Channel", duration=180)
            cs.update_status(video_id, "approved")
            cs.record_view(video_id)
            cs.update_playback_position(video_id, 30)

        resp = auth_client.get("/")

        assert resp.status_code == 200
        assert resp.text.count('class="video-card active-card"') == 6
        assert 'id="active-show-more-wrap"' not in resp.text

    def test_home_includes_history_link(self, auth_client):
        resp = auth_client.get("/")
        assert resp.status_code == 200
        assert 'href="/history"' in resp.text



class TestLimiterKey:
    def test_prefers_forwarded_for_header(self):
        request = StarletteRequest({
            "type": "http",
            "headers": [
                (b"x-forwarded-for", b"203.0.113.10, 10.0.0.1"),
                (b"x-real-ip", b"198.51.100.7"),
            ],
            "client": ("127.0.0.1", 1234),
        })
        assert _rate_limit_key(request) == "203.0.113.10"

    def test_falls_back_to_real_ip_and_client(self):
        request = StarletteRequest({
            "type": "http",
            "headers": [(b"x-real-ip", b"198.51.100.7")],
            "client": ("127.0.0.1", 1234),
        })
        assert _rate_limit_key(request) == "198.51.100.7"

        request = StarletteRequest({
            "type": "http",
            "headers": [],
            "client": ("127.0.0.1", 1234),
        })
        assert _rate_limit_key(request) == "127.0.0.1"


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
        assert resp.status_code == 200
        assert "Wrong PIN" in resp.text

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

    def test_search_with_more_than_five_results_renders_auto_load_block(self, store):
        app = _create_test_app(store)
        app.state.extractor.search.return_value = [
            {
                "video_id": f"result{i:08d}"[:11],
                "title": f"Search Result {i}",
                "channel_name": "Result Channel",
                "thumbnail_url": f"https://i.ytimg.com/vi/result{i:08d}/hqdefault.jpg",
                "duration": 300,
                "view_count": i * 100,
                "is_short": False,
            }
            for i in range(1, 7)
        ]
        client = AppClient(app, raise_server_exceptions=False)
        _login(client, "1234")

        resp = client.get("/search?q=test+video")

        assert resp.status_code == 200
        assert 'id="show-more-wrap"' in resp.text
        assert "<<<<<<< HEAD" not in resp.text
        assert '"Loading..."' in resp.text

    def test_request_pending_video_resends_notification(self, auth_client):
        app = auth_client.app
        store = app.state.video_store
        store.add_video(
            video_id="jjpjjcMeujM",
            title="Pending Video",
            channel_name="Test Channel",
            thumbnail_url=None,
            duration=60,
            channel_id="UCtest123",
            is_short=False,
            profile_id="default",
        )
        notify_cb = app.state.notify_callback
        notify_cb.reset_mock()

        search_resp = auth_client.get("/search?q=test")
        csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', search_resp.text)
        csrf = csrf_match.group(1) if csrf_match else ""

        resp = auth_client.post(
            "/request",
            data={"video_id": "jjpjjcMeujM", "csrf_token": csrf},
            follow_redirects=False,
        )

        assert resp.status_code in (302, 303)
        assert resp.headers.get("location", "") == "/pending/jjpjjcMeujM"
        notify_cb.assert_awaited_once()

    def test_search_calls_extractor(self, store):
        app = _create_test_app(store)
        c = AppClient(app, raise_server_exceptions=False)
        _login(c, "1234")
        c.get("/search?q=cats")
        app.state.extractor.search.assert_called_once()


# ---------------------------------------------------------------------------
# Search disabled (allowlist-only) mode
# ---------------------------------------------------------------------------

class TestSearchDisabled:
    SEARCH_FORM = '<form action="/search"'

    def test_search_bar_shown_by_default(self, auth_client):
        assert self.SEARCH_FORM in auth_client.get("/").text

    def test_search_bar_hidden_when_disabled(self, store):
        store.set_setting("default:search_disabled", "true")
        app = _create_test_app(store)
        c = AppClient(app, raise_server_exceptions=False)
        _login(c, "1234")
        assert self.SEARCH_FORM not in c.get("/").text

    def test_search_route_redirects_and_skips_extractor(self, store):
        store.set_setting("default:search_disabled", "true")
        app = _create_test_app(store)
        c = AppClient(app, raise_server_exceptions=False)
        _login(c, "1234")
        resp = c.get("/search?q=cats", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"
        app.state.extractor.search.assert_not_called()
        app.state.extractor.extract_metadata.assert_not_called()

    def test_request_blocked_when_disabled(self, store):
        app = _create_test_app(store)
        c = AppClient(app, raise_server_exceptions=False)
        _login(c, "1234")
        csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', c.get("/search?q=test").text).group(1)
        store.set_setting("default:search_disabled", "true")

        resp = c.post(
            "/request",
            data={"video_id": "dQw4w9WgXcQ", "csrf_token": csrf},
            follow_redirects=False,
        )

        assert resp.status_code == 303
        assert resp.headers["location"] == "/"
        assert store.get_video("dQw4w9WgXcQ", profile_id="default") is None

    def test_disabled_flag_is_per_profile(self, store):
        store.create_profile("sibling", "Sibling", pin="5678")
        store.set_setting("sibling:search_disabled", "true")
        app = _create_test_app(store)
        c = AppClient(app, raise_server_exceptions=False)
        csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', c.get("/login?profile=default").text).group(1)
        c.post("/login", data={"pin": "1234", "profile_id": "default", "csrf_token": csrf}, follow_redirects=False)
        assert self.SEARCH_FORM in c.get("/").text
        assert c.get("/search?q=cats", follow_redirects=False).status_code == 200


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
        # Get a valid CSRF token so we're testing video ID validation, not CSRF rejection
        search_resp = auth_client.get("/search?q=test")
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', search_resp.text)
        csrf = match.group(1) if match else ""

        resp = auth_client.post(
            "/request",
            data={"video_id": "bad!", "csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        # Verify bad video was NOT stored
        resp2 = auth_client.get("/api/status/bad!", follow_redirects=True)
        assert resp2.status_code != 200 or resp2.json().get("status") != "pending"


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
        assert "brg-watch-position:" in resp.text
        assert "localStorage.setItem(playbackStorageKey" in resp.text
        assert "pagehide" in resp.text
        assert "exitFlushSent" in resp.text
        assert "document.visibilityState === 'visible'" in resp.text
        assert "attemptAutoplayIfActive" in resp.text
        assert "brg-nav-history" in resp.text
        assert "previous.indexOf('/pending/')" in resp.text

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

    def test_watch_heartbeat_updates_playback_position(self, auth_client, store):
        cs = ChildStore(store, "default")
        cs.add_video(
            video_id="watchPos123",
            title="Watch Position",
            channel_name="Test Channel",
            duration=120,
        )
        cs.update_status("watchPos123", "approved")

        watch_resp = auth_client.get("/watch/watchPos123")
        assert watch_resp.status_code == 200

        heartbeat_resp = auth_client.post("/api/watch-heartbeat", json={
            "video_id": "watchPos123",
            "seconds": 5,
            "position_seconds": 90,
        })
        assert heartbeat_resp.status_code == 200

        video = cs.get_video("watchPos123")
        assert video["resume_seconds"] == 90


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
        c = AppClient(app, raise_server_exceptions=False)
        resp = c.get("/", follow_redirects=True)
        assert resp.status_code == 200
        s.close()


class TestCatalogAndHistory:
    def test_active_catalog_returns_only_unfinished_videos(self, auth_client, store):
        cs = ChildStore(store, "default")
        cs.add_video("act12345a1b", "Ready To Start", "Test Channel", duration=180)
        cs.update_status("act12345a1b", "approved")

        cs.add_video("act12345b2c", "Resume Me", "Test Channel", duration=180)
        cs.update_status("act12345b2c", "approved")
        cs.record_view("act12345b2c")
        cs.record_watch_seconds("act12345b2c", 60)

        cs.add_video("act12345c3d", "Finished", "Test Channel", duration=100)
        cs.update_status("act12345c3d", "approved")
        cs.record_view("act12345c3d")
        cs.record_watch_seconds("act12345c3d", 95)

        resp = auth_client.get("/api/catalog?active=true")

        assert resp.status_code == 200
        ids = {video["video_id"] for video in resp.json()["videos"]}
        assert "act12345a1b" in ids
        assert "act12345b2c" in ids
        assert "act12345c3d" not in ids

    def test_active_catalog_honors_channel_filter(self, auth_client, store):
        cs = ChildStore(store, "default")
        cs.add_video("actchan001a", "SciShow One", "SciShow", duration=180, channel_id="UCsci")
        cs.update_status("actchan001a", "approved")
        cs.record_view("actchan001a")
        cs.record_watch_seconds("actchan001a", 30)

        cs.add_video("actchan002b", "SciShow Two", "SciShow", duration=180, channel_id="UCsci")
        cs.update_status("actchan002b", "approved")
        cs.record_view("actchan002b")
        cs.record_watch_seconds("actchan002b", 45)

        cs.add_video("actchan003c", "Other Channel", "Kurzgesagt", duration=180, channel_id="UCother")
        cs.update_status("actchan003c", "approved")
        cs.record_view("actchan003c")
        cs.record_watch_seconds("actchan003c", 50)

        resp = auth_client.get("/api/catalog?active=true&channel=UCsci")

        assert resp.status_code == 200
        videos = resp.json()["videos"]
        ids = {video["video_id"] for video in videos}
        assert ids == {"actchan001a", "actchan002b"}
        assert all(video["channel_id"] == "UCsci" for video in videos)

    def test_history_page_lists_watched_videos(self, auth_client, store):
        cs = ChildStore(store, "default")
        cs.add_video("histpage123", "Watched Once", "Test Channel", duration=120)
        cs.update_status("histpage123", "approved")
        cs.record_view("histpage123")
        cs.record_watch_seconds("histpage123", 60)

        resp = auth_client.get("/history")

        assert resp.status_code == 200
        assert "Watched Once" in resp.text
        assert "History" in resp.text
        assert "/api/history" in resp.text

    def test_history_page_uses_total_time_and_progress_bar(self, auth_client, store):
        cs = ChildStore(store, "default")
        cs.add_video("histprog123", "Watched Half", "Test Channel", duration=120)
        cs.update_status("histprog123", "approved")
        cs.record_view("histprog123")
        cs.record_watch_seconds("histprog123", 60)

        resp = auth_client.get("/history")

        assert resp.status_code == 200
        assert "2 min" in resp.text
        assert "1 / 2 min" not in resp.text
        assert "watch-progress-fill" in resp.text

    def test_history_api_paginates(self, auth_client, store):
        cs = ChildStore(store, "default")
        for idx in range(35):
            video_id = f"h{idx:010d}"
            cs.add_video(video_id, f"History {idx}", "Test Channel", duration=120)
            cs.update_status(video_id, "approved")
            cs.record_view(video_id)
            store.conn.execute(
                "UPDATE videos SET last_viewed_at = ? WHERE video_id = ? AND profile_id = 'default'",
                (f"2026-03-{(idx % 9) + 10:02d} 12:{idx % 60:02d}:00", video_id),
            )
        store.conn.commit()

        resp = auth_client.get("/api/history?offset=0&limit=30")

        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 30
        assert data["has_more"] is True
        assert data["total"] == 35
        assert data["groups"]

    def test_history_api_includes_watched_percent(self, auth_client, store):
        cs = ChildStore(store, "default")
        cs.add_video("histpct1234", "History Progress", "Test Channel", duration=120)
        cs.update_status("histpct1234", "approved")
        cs.record_view("histpct1234")
        cs.record_watch_seconds("histpct1234", 60)

        resp = auth_client.get("/api/history?offset=0&limit=30")

        assert resp.status_code == 200
        data = resp.json()
        assert data["groups"]
        assert data["groups"][0]["videos"][0]["watched_percent"] == 50

    def test_history_progress_prefers_saved_playback_position(self, auth_client, store):
        cs = ChildStore(store, "default")
        cs.add_video("histseek123", "Seeked Video", "Test Channel", duration=120)
        cs.update_status("histseek123", "approved")

        watch_resp = auth_client.get("/watch/histseek123")
        assert watch_resp.status_code == 200

        heartbeat_resp = auth_client.post("/api/watch-heartbeat", json={
            "video_id": "histseek123",
            "seconds": 5,
            "position_seconds": 90,
        })
        assert heartbeat_resp.status_code == 200

        resp = auth_client.get("/api/history?offset=0&limit=30")

        assert resp.status_code == 200
        data = resp.json()
        assert data["groups"]
        assert data["groups"][0]["videos"][0]["watched_percent"] == 75
