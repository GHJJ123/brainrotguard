"""Tests for pure (no-network) functions in youtube/extractor.py."""

import pytest

from youtube.extractor import (
    extract_video_id,
    format_duration,
    _safe_thumbnail,
    _is_short_url,
    THUMB_ALLOWED_HOSTS,
)


class TestExtractVideoId:
    @pytest.mark.parametrize("input_val, expected", [
        # Standard URLs
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("http://youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        # Shorts URL
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        # Plain video ID
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        # With extra params
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=120", "dQw4w9WgXcQ"),
        # With whitespace
        ("  dQw4w9WgXcQ  ", "dQw4w9WgXcQ"),
    ])
    def test_valid_extraction(self, input_val, expected):
        assert extract_video_id(input_val) == expected

    @pytest.mark.parametrize("input_val", [
        "",
        "not a video!",
        "https://example.com/watch?v=abc",
        "12345",  # Too short
        "dQw4w9WgXcQ_extra",  # Too long
    ])
    def test_invalid_returns_none(self, input_val):
        assert extract_video_id(input_val) is None


class TestFormatDuration:
    @pytest.mark.parametrize("seconds, expected", [
        (0, "?"),
        (None, "?"),
        (60, "1:00"),
        (323, "5:23"),
        (3735, "1:02:15"),
        (5, "0:05"),
        (3600, "1:00:00"),
        (7261, "2:01:01"),
    ])
    def test_format(self, seconds, expected):
        assert format_duration(seconds) == expected


class TestSafeThumbnail:
    def test_allowed_host_passes(self):
        url = "https://i.ytimg.com/vi/abc/hqdefault.jpg"
        assert _safe_thumbnail(url, "dQw4w9WgXcQ") == url

    def test_all_allowed_hosts(self):
        for host in THUMB_ALLOWED_HOSTS:
            url = f"https://{host}/vi/abc/default.jpg"
            assert _safe_thumbnail(url, "dQw4w9WgXcQ") == url

    def test_disallowed_host_falls_back(self):
        url = "https://evil.com/thumb.jpg"
        result = _safe_thumbnail(url, "dQw4w9WgXcQ")
        assert result == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"

    def test_http_scheme_falls_back(self):
        url = "http://i.ytimg.com/vi/abc/default.jpg"
        result = _safe_thumbnail(url, "dQw4w9WgXcQ")
        assert result == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"

    def test_none_url_falls_back(self):
        result = _safe_thumbnail(None, "dQw4w9WgXcQ")
        assert result == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"

    def test_empty_url_falls_back(self):
        result = _safe_thumbnail("", "dQw4w9WgXcQ")
        assert result == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"

    def test_invalid_video_id_returns_empty(self):
        assert _safe_thumbnail("", "") == ""
        assert _safe_thumbnail(None, "invalid!!!") == ""


class TestIsShortUrl:
    def test_shorts_url(self):
        assert _is_short_url("https://www.youtube.com/shorts/dQw4w9WgXcQ") is True

    def test_regular_url(self):
        assert _is_short_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is False

    def test_none(self):
        assert _is_short_url(None) is False

    def test_empty(self):
        assert _is_short_url("") is False
