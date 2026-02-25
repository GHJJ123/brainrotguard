"""Shared pytest fixtures for BrainRotGuard tests."""

import os
import pytest

from config import Config, WebConfig, TelegramConfig, YouTubeConfig, DatabaseConfig, WatchLimitsConfig
from data.video_store import VideoStore
from data.child_store import ChildStore


@pytest.fixture
def video_store(tmp_path):
    """VideoStore backed by a temp-dir SQLite file (not :memory: due to Path.mkdir in __init__)."""
    db = tmp_path / "test.db"
    store = VideoStore(db_path=str(db))
    yield store
    store.close()


@pytest.fixture
def child_store(video_store):
    """ChildStore wrapping the test VideoStore with 'default' profile."""
    return ChildStore(video_store, "default")


@pytest.fixture
def sample_config(tmp_path):
    """Minimal Config with safe defaults for testing."""
    return Config(
        web=WebConfig(host="127.0.0.1", port=9999, pin="1234"),
        telegram=TelegramConfig(bot_token="fake:token", admin_chat_id="12345"),
        youtube=YouTubeConfig(search_max_results=5, ydl_timeout=10),
        database=DatabaseConfig(path=str(tmp_path / "test.db")),
        watch_limits=WatchLimitsConfig(
            daily_limit_minutes=60,
            timezone="America/New_York",
        ),
    )


@pytest.fixture
def config_yaml(tmp_path):
    """Write a minimal config.yaml and return its path."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""\
web:
  host: 0.0.0.0
  port: 8080
  pin: "4321"
  poll_interval: 2000
telegram:
  bot_token: "fake:token123"
  admin_chat_id: "99999"
youtube:
  search_max_results: 10
  ydl_timeout: 15
database:
  path: "{db_path}"
watch_limits:
  daily_limit_minutes: 120
  timezone: "America/New_York"
""".format(db_path=str(tmp_path / "cfg_test.db")))
    return cfg
