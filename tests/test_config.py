"""Tests for config.py — loading, env var expansion, validation."""

import os
import pytest

from config import AppConfig, Config, TelegramConfig, expand_env_vars, load_config, WebConfig


class TestExpandEnvVars:
    def test_dollar_brace_syntax(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "hello")
        assert expand_env_vars("${TEST_VAR}") == "hello"

    def test_dollar_prefix_syntax(self, monkeypatch):
        monkeypatch.setenv("MY_VAR", "world")
        assert expand_env_vars("$MY_VAR") == "world"

    def test_missing_var_becomes_empty(self, monkeypatch):
        monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
        assert expand_env_vars("${NONEXISTENT_VAR}") == ""

    def test_nested_dict(self, monkeypatch):
        monkeypatch.setenv("TOKEN", "abc123")
        result = expand_env_vars({"bot": {"token": "${TOKEN}"}})
        assert result == {"bot": {"token": "abc123"}}

    def test_list(self, monkeypatch):
        monkeypatch.setenv("ITEM", "x")
        result = expand_env_vars(["${ITEM}", "literal"])
        assert result == ["x", "literal"]

    def test_non_string_passthrough(self):
        assert expand_env_vars(42) == 42
        assert expand_env_vars(True) is True
        assert expand_env_vars(None) is None


class TestConfigFromYaml:
    def test_load_basic_yaml(self, config_yaml):
        cfg = Config.from_yaml(config_yaml)
        assert cfg.app.locale == "en"
        assert cfg.app.time_format == "locale"
        assert cfg.web.port == 8080
        assert cfg.web.pin == "4321"
        assert cfg.telegram.bot_token == "fake:token123"
        assert cfg.telegram.admin_chat_id == "99999"
        assert cfg.youtube.search_max_results == 10
        assert cfg.youtube.ydl_timeout == 15

    def test_env_var_expansion_in_yaml(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRG_BOT_TOKEN", "env_token_val")
        monkeypatch.setenv("BRG_ADMIN_CHAT_ID", "77777")
        cfg_file = tmp_path / "env_config.yaml"
        cfg_file.write_text("""\
web:
  host: 0.0.0.0
  port: 8080
telegram:
  bot_token: "${BRG_BOT_TOKEN}"
  admin_chat_id: "${BRG_ADMIN_CHAT_ID}"
youtube:
  search_max_results: 5
database:
  path: "test.db"
watch_limits:
  daily_limit_minutes: 60
""")
        cfg = Config.from_yaml(cfg_file)
        assert cfg.telegram.bot_token == "env_token_val"
        assert cfg.telegram.admin_chat_id == "77777"


class TestAppConfig:
    def test_log_level_default(self):
        cfg = AppConfig()
        assert cfg.log_level == "info"

    def test_log_level_valid_values(self):
        for level in ("debug", "info", "warning", "error"):
            cfg = AppConfig(log_level=level)
            assert cfg.log_level == level

    def test_log_level_case_insensitive(self):
        cfg = AppConfig(log_level="WARNING")
        assert cfg.log_level == "warning"

    def test_log_level_invalid_falls_back(self):
        cfg = AppConfig(log_level="banana")
        assert cfg.log_level == "info"

    def test_log_level_from_yaml(self, tmp_path):
        cfg_file = tmp_path / "log.yaml"
        cfg_file.write_text("""\
app:
  log_level: warning
web:
  host: 0.0.0.0
  port: 8080
telegram:
  bot_token: "t"
  admin_chat_id: "1"
youtube:
  search_max_results: 5
database:
  path: "test.db"
watch_limits:
  daily_limit_minutes: 60
""")
        cfg = Config.from_yaml(cfg_file)
        assert cfg.app.log_level == "warning"

    def test_log_level_from_env(self, monkeypatch):
        monkeypatch.setenv("BRG_LOG_LEVEL", "error")
        cfg = Config.from_env()
        assert cfg.app.log_level == "error"


class TestConfigFromEnv:
    def test_defaults(self, monkeypatch):
        # Clear any env vars that might interfere
        for var in ["BRG_WEB_HOST", "BRG_WEB_PORT", "BRG_BOT_TOKEN",
                     "BRG_ADMIN_CHAT_ID", "BRG_BASE_URL", "BRG_PIN", "BRG_LOCALE",
                     "BRG_TIME_FORMAT", "BRG_LOG_LEVEL"]:
            monkeypatch.delenv(var, raising=False)
        cfg = Config.from_env()
        assert cfg.app.locale == "en"
        assert cfg.app.time_format == "locale"
        assert cfg.app.log_level == "info"
        assert cfg.web.host == "0.0.0.0"
        assert cfg.web.port == 8080
        assert cfg.telegram.bot_token == ""

    def test_from_env_vars(self, monkeypatch):
        monkeypatch.setenv("BRG_WEB_PORT", "9090")
        monkeypatch.setenv("BRG_BOT_TOKEN", "test_token")
        monkeypatch.setenv("BRG_ADMIN_CHAT_ID", "11111")
        monkeypatch.setenv("BRG_LOCALE", "nb_NO")
        monkeypatch.setenv("BRG_TIME_FORMAT", "24h")
        cfg = Config.from_env()
        assert cfg.app.locale == "nb_NO"
        assert cfg.app.time_format == "24h"
        assert cfg.web.port == 9090
        assert cfg.telegram.bot_token == "test_token"


class TestLoadConfig:
    def test_load_from_path(self, config_yaml):
        cfg = load_config(str(config_yaml))
        assert cfg.app.locale == "en"
        assert cfg.web.pin == "4321"

    def test_normalizes_locale(self, tmp_path):
        cfg_file = tmp_path / "locale.yaml"
        cfg_file.write_text("""\
app:
  locale: "nb_NO"
web:
  host: 0.0.0.0
  port: 8080
telegram:
  bot_token: "t"
  admin_chat_id: "1"
youtube:
  search_max_results: 5
database:
  path: "test.db"
watch_limits:
  daily_limit_minutes: 60
""")
        cfg = load_config(str(cfg_file))
        assert cfg.app.locale == "nb"

    def test_normalizes_time_format(self, tmp_path):
        cfg_file = tmp_path / "time_format.yaml"
        cfg_file.write_text("""\
app:
  time_format: "24hour"
web:
  host: 0.0.0.0
  port: 8080
telegram:
  bot_token: "t"
  admin_chat_id: "1"
youtube:
  search_max_results: 5
database:
  path: "test.db"
watch_limits:
  daily_limit_minutes: 60
""")
        cfg = load_config(str(cfg_file))
        assert cfg.app.time_format == "24h"

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/path/config.yaml")

    def test_fallback_to_env(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)  # No config.yaml present
        monkeypatch.delenv("BRG_BOT_TOKEN", raising=False)
        cfg = load_config(None)
        assert isinstance(cfg, Config)

    def test_invalid_timezone_warning(self, tmp_path, caplog):
        cfg_file = tmp_path / "tz_config.yaml"
        cfg_file.write_text("""\
web:
  host: 0.0.0.0
  port: 8080
telegram:
  bot_token: "t"
  admin_chat_id: "1"
youtube:
  search_max_results: 5
database:
  path: "test.db"
watch_limits:
  timezone: "Invalid/Timezone"
""")
        cfg = load_config(str(cfg_file))
        assert cfg.watch_limits.timezone == ""  # Reset to empty on invalid

    def test_non_numeric_admin_chat_id_warning(self, tmp_path, caplog):
        cfg_file = tmp_path / "bad_admin.yaml"
        cfg_file.write_text("""\
web:
  host: 0.0.0.0
  port: 8080
telegram:
  bot_token: "t"
  admin_chat_id: "not_a_number"
youtube:
  search_max_results: 5
database:
  path: "test.db"
watch_limits:
  daily_limit_minutes: 60
""")
        import logging
        with caplog.at_level(logging.WARNING):
            load_config(str(cfg_file))
        assert "not numeric" in caplog.text


class TestWebConfig:
    def test_base_url_from_env(self, monkeypatch):
        monkeypatch.setenv("BRG_BASE_URL", "http://10.0.0.1:8080")
        cfg = WebConfig()
        assert cfg.base_url == "http://10.0.0.1:8080"

    def test_base_url_explicit_wins(self, monkeypatch):
        monkeypatch.setenv("BRG_BASE_URL", "http://10.0.0.1:8080")
        cfg = WebConfig(base_url="http://custom:9090")
        assert cfg.base_url == "http://custom:9090"

    def test_embed_host_defaults_to_nocookie(self, monkeypatch):
        monkeypatch.delenv("BRG_EMBED_HOST", raising=False)
        assert WebConfig().embed_host == "https://www.youtube-nocookie.com"

    def test_embed_host_from_env(self, monkeypatch):
        monkeypatch.setenv("BRG_EMBED_HOST", "https://www.youtube.com")
        assert WebConfig().embed_host == "https://www.youtube.com"

    @pytest.mark.parametrize("value", [
        "https://evil.example.com",
        "javascript:alert(1)",
        "https://www.youtube.com.evil.example.com",
        "",
    ])
    def test_embed_host_rejects_unknown_origin(self, monkeypatch, value):
        """embed_host lands in an iframe src — only the two known origins may pass."""
        monkeypatch.setenv("BRG_EMBED_HOST", value)
        assert WebConfig().embed_host == "https://www.youtube-nocookie.com"


class TestTelegramApiUrl:
    def test_defaults_empty(self, monkeypatch):
        monkeypatch.delenv("BRG_TELEGRAM_API_URL", raising=False)
        assert TelegramConfig().api_url == ""

    def test_from_env_strips_trailing_slash(self, monkeypatch):
        monkeypatch.setenv("BRG_TELEGRAM_API_URL", " https://relay.example.com/ ")
        assert TelegramConfig().api_url == "https://relay.example.com"

    def test_explicit_value_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("BRG_TELEGRAM_API_URL", "https://env.example.com")
        assert TelegramConfig(api_url="https://yaml.example.com").api_url == "https://yaml.example.com"
