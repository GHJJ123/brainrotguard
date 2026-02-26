"""Configuration management for BrainRotGuard."""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def expand_env_vars(value: Any) -> Any:
    """Recursively expand environment variables in strings, dicts, and lists.

    Supports both ${VAR} and $VAR patterns.
    """
    if isinstance(value, str):
        # Expand ${VAR} pattern
        pattern = re.compile(r'\$\{([^}]+)\}')
        result = pattern.sub(lambda m: os.environ.get(m.group(1), ''), value)
        # Expand $VAR pattern
        pattern = re.compile(r'\$([A-Za-z_][A-Za-z0-9_]*)')
        result = pattern.sub(lambda m: os.environ.get(m.group(1), ''), result)
        return result
    elif isinstance(value, dict):
        return {k: expand_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [expand_env_vars(item) for item in value]
    else:
        return value


@dataclass
class WebConfig:
    """Web server configuration."""
    host: str = "0.0.0.0"
    port: int = 8080
    poll_interval: int = 3000  # ms between status checks on pending page
    pin: str = ""  # empty = no auth required
    session_secret: str = ""  # auto-generated if not set
    base_url: str = ""  # e.g. http://10.0.0.1:8080 — used for links in Telegram messages

    def __post_init__(self):
        if not self.base_url:
            self.base_url = os.environ.get("BRG_BASE_URL", "")


@dataclass
class TelegramConfig:
    """Telegram bot configuration."""
    bot_token: str = ""
    admin_chat_id: str = ""


@dataclass
class YouTubeConfig:
    """YouTube API configuration."""
    search_max_results: int = 50
    channel_cache_results: int = 200  # max videos per channel in cache
    channel_cache_ttl: int = 1800  # seconds between channel cache refreshes
    ydl_timeout: int = 30  # seconds — max wall-clock time for a single yt-dlp operation
    shorts_enabled: bool = False  # enable Shorts row on homepage


@dataclass
class DatabaseConfig:
    """Database configuration."""
    path: str = "db/videos.db"


@dataclass
class WatchLimitsConfig:
    """Watch time limits configuration."""
    daily_limit_minutes: int = 0
    timezone: str = "America/New_York"
    notify_on_limit: bool = True


@dataclass
class Config:
    """Main configuration container."""
    web: WebConfig = field(default_factory=WebConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    youtube: YouTubeConfig = field(default_factory=YouTubeConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    watch_limits: WatchLimitsConfig = field(default_factory=WatchLimitsConfig)

    @classmethod
    def from_yaml(cls, path: Path | str) -> "Config":
        """Load configuration from YAML file with environment variable expansion."""
        path = Path(path)
        with open(path, "r") as f:
            raw_config = yaml.safe_load(f)

        # Expand environment variables
        expanded_config = expand_env_vars(raw_config)

        # Construct Config from expanded data
        web_data = expanded_config.get("web", {})
        telegram_data = expanded_config.get("telegram", {})
        youtube_data = expanded_config.get("youtube", {})
        database_data = expanded_config.get("database", {})
        watch_limits_data = expanded_config.get("watch_limits", {})

        return cls(
            web=WebConfig(**web_data),
            telegram=TelegramConfig(**telegram_data),
            youtube=YouTubeConfig(**youtube_data),
            database=DatabaseConfig(**database_data),
            watch_limits=WatchLimitsConfig(**watch_limits_data),
        )

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration directly from environment variables."""
        return cls(
            web=WebConfig(
                host=os.environ.get("BRG_WEB_HOST", "0.0.0.0"),
                port=int(os.environ.get("BRG_WEB_PORT", "8080")),
                poll_interval=int(os.environ.get("BRG_POLL_INTERVAL", "3000")),
                pin=os.environ.get("BRG_PIN", ""),
                session_secret=os.environ.get("BRG_SESSION_SECRET", ""),
                base_url=os.environ.get("BRG_BASE_URL", ""),
            ),
            telegram=TelegramConfig(
                bot_token=os.environ.get("BRG_BOT_TOKEN", ""),
                admin_chat_id=os.environ.get("BRG_ADMIN_CHAT_ID", ""),
            ),
            youtube=YouTubeConfig(
                search_max_results=int(os.environ.get("BRG_YOUTUBE_MAX_RESULTS", "50")),
                channel_cache_results=int(os.environ.get("BRG_CHANNEL_CACHE_RESULTS", "200")),
                channel_cache_ttl=int(os.environ.get("BRG_CHANNEL_CACHE_TTL", "1800")),
                ydl_timeout=int(os.environ.get("BRG_YDL_TIMEOUT", "30")),
                shorts_enabled=os.environ.get("BRG_SHORTS_ENABLED", "false").lower() == "true",
            ),
            database=DatabaseConfig(
                path=os.environ.get("BRG_DB_PATH", "db/videos.db"),
            ),
            watch_limits=WatchLimitsConfig(
                daily_limit_minutes=int(os.environ.get("BRG_DAILY_LIMIT_MINUTES", "0")),
                timezone=os.environ.get("BRG_TIMEZONE", "America/New_York"),
                notify_on_limit=os.environ.get("BRG_NOTIFY_ON_LIMIT", "true").lower() == "true",
            ),
        )


def load_config(config_path: str | None = None) -> Config:
    """Load configuration from file or environment.

    Tries in order:
    1. Provided config_path
    2. Default paths: config.yaml, config.yml
    3. Environment variables (fallback)
    """
    config: Config | None = None

    if config_path:
        path = Path(config_path)
        if path.exists():
            config = Config.from_yaml(path)
        else:
            raise FileNotFoundError(f"Config file not found: {config_path}")
    else:
        # Try default paths
        for default_path in ["config.yaml", "config.yml"]:
            path = Path(default_path)
            if path.exists():
                config = Config.from_yaml(path)
                break

    if config is None:
        # Fallback to environment variables
        config = Config.from_env()

    # Validate admin_chat_id
    admin_id = config.telegram.admin_chat_id
    if not admin_id:
        logger.warning("telegram.admin_chat_id is empty — bot commands will be unauthorized")
    elif not admin_id.lstrip("-").isdigit():
        logger.warning("telegram.admin_chat_id %r is not numeric — admin checks will fail", admin_id)

    # Validate timezone at startup
    tz = config.watch_limits.timezone
    if tz:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)
        except Exception:
            logger.warning("Invalid timezone %r in config, falling back to UTC", tz)
            config.watch_limits.timezone = ""

    return config
