"""Tests for utils.py pure functions."""

from unittest.mock import MagicMock
from datetime import datetime, timezone

import pytest

from utils import (
    parse_time_input,
    format_time_12h,
    is_within_schedule,
    resolve_setting,
    get_day_utc_bounds,
    get_weekday,
    get_today_str,
    DAY_NAMES,
)


# --- parse_time_input ---

class TestParseTimeInput:
    """Test flexible time parsing into HH:MM format."""

    @pytest.mark.parametrize("raw, expected", [
        # 24-hour formats
        ("800", "08:00"),
        ("0800", "08:00"),
        ("8:00", "08:00"),
        ("20:00", "20:00"),
        ("2000", "20:00"),
        ("0000", "00:00"),
        ("23:59", "23:59"),
        ("0:00", "00:00"),
        # 12-hour with AM/PM
        ("8am", "08:00"),
        ("8AM", "08:00"),
        ("800am", "08:00"),
        ("8:00am", "08:00"),
        ("12pm", "12:00"),
        ("12am", "00:00"),
        ("12:00pm", "12:00"),
        ("12:00am", "00:00"),
        ("800pm", "20:00"),
        ("8:00PM", "20:00"),
        ("1:30pm", "13:30"),
        ("9PM", "21:00"),
        # Whitespace
        ("  800  ", "08:00"),
        ("8:00 am", "08:00"),
    ])
    def test_valid_inputs(self, raw, expected):
        assert parse_time_input(raw) == expected

    @pytest.mark.parametrize("raw", [
        "",
        "abc",
        "25:00",
        "24:00",
        "8:60",
        "13am",
        "13pm",
        "0",
        "12345",
    ])
    def test_invalid_inputs(self, raw):
        assert parse_time_input(raw) is None


# --- format_time_12h ---

class TestFormatTime12h:
    @pytest.mark.parametrize("hhmm, expected", [
        ("08:00", "8 AM"),
        ("08:30", "8:30 AM"),
        ("12:00", "12 PM"),
        ("00:00", "12 AM"),
        ("20:00", "8 PM"),
        ("13:45", "1:45 PM"),
        ("23:59", "11:59 PM"),
    ])
    def test_format(self, hhmm, expected):
        assert format_time_12h(hhmm) == expected

    def test_invalid_passthrough(self):
        assert format_time_12h("garbage") == "garbage"
        assert format_time_12h(None) is None


# --- is_within_schedule ---

class TestIsWithinSchedule:
    def test_no_schedule_returns_allowed(self):
        allowed, unlock = is_within_schedule("", "")
        assert allowed is True
        assert unlock == ""

    def test_only_start_set_before_start(self, monkeypatch):
        """Before start time, should be blocked."""
        # Mock datetime.now to return 7:00 AM UTC
        import utils
        _orig = datetime.now
        def mock_now(tz=None):
            return datetime(2025, 1, 15, 7, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(utils, "datetime", type("MockDT", (datetime,), {"now": staticmethod(mock_now)}))

        allowed, unlock = is_within_schedule("08:00", "")
        assert allowed is False
        assert "8 AM" in unlock

    def test_only_start_set_after_start(self, monkeypatch):
        """After start time, should be allowed."""
        import utils
        def mock_now(tz=None):
            return datetime(2025, 1, 15, 9, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(utils, "datetime", type("MockDT", (datetime,), {"now": staticmethod(mock_now)}))

        allowed, unlock = is_within_schedule("08:00", "")
        assert allowed is True
        assert unlock == ""

    def test_only_end_set_before_end(self, monkeypatch):
        """Before end time, should be allowed."""
        import utils
        def mock_now(tz=None):
            return datetime(2025, 1, 15, 18, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(utils, "datetime", type("MockDT", (datetime,), {"now": staticmethod(mock_now)}))

        allowed, unlock = is_within_schedule("", "20:00")
        assert allowed is True

    def test_only_end_set_after_end(self, monkeypatch):
        """After end time, should be blocked."""
        import utils
        def mock_now(tz=None):
            return datetime(2025, 1, 15, 21, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(utils, "datetime", type("MockDT", (datetime,), {"now": staticmethod(mock_now)}))

        allowed, unlock = is_within_schedule("", "20:00")
        assert allowed is False
        assert unlock == "tomorrow"

    def test_normal_range_inside(self, monkeypatch):
        """Within [08:00, 20:00) → allowed."""
        import utils
        def mock_now(tz=None):
            return datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(utils, "datetime", type("MockDT", (datetime,), {"now": staticmethod(mock_now)}))

        allowed, unlock = is_within_schedule("08:00", "20:00")
        assert allowed is True

    def test_normal_range_outside(self, monkeypatch):
        """Outside [08:00, 20:00) → blocked."""
        import utils
        def mock_now(tz=None):
            return datetime(2025, 1, 15, 21, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(utils, "datetime", type("MockDT", (datetime,), {"now": staticmethod(mock_now)}))

        allowed, unlock = is_within_schedule("08:00", "20:00")
        assert allowed is False
        assert "8 AM" in unlock

    def test_overnight_range_inside(self, monkeypatch):
        """Within overnight [22:00, 06:00) at 23:00 → allowed."""
        import utils
        def mock_now(tz=None):
            return datetime(2025, 1, 15, 23, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(utils, "datetime", type("MockDT", (datetime,), {"now": staticmethod(mock_now)}))

        allowed, unlock = is_within_schedule("22:00", "06:00")
        assert allowed is True

    def test_overnight_range_outside(self, monkeypatch):
        """Outside overnight [22:00, 06:00) at 12:00 → blocked."""
        import utils
        def mock_now(tz=None):
            return datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(utils, "datetime", type("MockDT", (datetime,), {"now": staticmethod(mock_now)}))

        allowed, unlock = is_within_schedule("22:00", "06:00")
        assert allowed is False


# --- resolve_setting ---

class TestResolveSetting:
    def test_base_key_fallback(self):
        """No day override → falls back to base key."""
        store = MagicMock()
        store.get_setting = MagicMock(side_effect=lambda k, d="": d)
        result = resolve_setting("access_start", store, default="08:00")
        assert result == "08:00"

    def test_day_override_wins(self, monkeypatch):
        """Day-specific override takes precedence."""
        import utils
        monkeypatch.setattr(utils, "get_weekday", lambda tz="": "mon")
        store = MagicMock()
        store.get_setting = MagicMock(side_effect=lambda k, d="": "09:00" if k == "mon_access_start" else d)
        result = resolve_setting("access_start", store, default="08:00")
        assert result == "09:00"


# --- get_day_utc_bounds ---

class TestGetDayUtcBounds:
    def test_no_timezone_returns_date_range(self):
        start, end = get_day_utc_bounds("2025-06-15")
        assert start == "2025-06-15"
        assert end == "2025-06-16"

    def test_with_timezone_returns_utc(self):
        start, end = get_day_utc_bounds("2025-06-15", "America/New_York")
        # NY is UTC-4 in summer, so midnight local = 04:00 UTC
        assert start == "2025-06-15 04:00:00"
        assert end == "2025-06-16 04:00:00"

    def test_invalid_timezone_fallback(self):
        start, end = get_day_utc_bounds("2025-06-15", "Invalid/Zone")
        assert start == "2025-06-15"
        assert end == "2025-06-16"


# --- get_weekday ---

class TestGetWeekday:
    def test_returns_valid_day_name(self):
        result = get_weekday()
        assert result in DAY_NAMES

    def test_with_timezone(self):
        result = get_weekday("America/New_York")
        assert result in DAY_NAMES

    def test_invalid_timezone_fallback(self):
        result = get_weekday("Invalid/Zone")
        assert result in DAY_NAMES


# --- get_today_str ---

class TestGetTodayStr:
    def test_returns_date_format(self):
        result = get_today_str()
        assert len(result) == 10  # YYYY-MM-DD
        assert result[4] == "-" and result[7] == "-"

    def test_with_timezone(self):
        result = get_today_str("America/New_York")
        assert len(result) == 10
