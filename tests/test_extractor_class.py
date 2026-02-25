"""Tests for YouTubeExtractor class and YouTubeExtractorProtocol."""

import pytest
from youtube.extractor import YouTubeExtractor, YouTubeExtractorProtocol


class TestYouTubeExtractorProtocol:
    """Protocol compliance tests."""

    def test_class_satisfies_protocol(self):
        assert isinstance(YouTubeExtractor(), YouTubeExtractorProtocol)

    def test_class_has_all_protocol_methods(self):
        import inspect
        protocol_methods = {
            name for name, _ in inspect.getmembers(
                YouTubeExtractorProtocol, predicate=inspect.isfunction
            ) if not name.startswith("_")
        }
        for method in protocol_methods:
            assert hasattr(YouTubeExtractor, method), f"Missing: {method}"
            assert callable(getattr(YouTubeExtractor, method))


class TestYouTubeExtractorHasAllMethods:
    """Verify all Protocol methods exist on the class."""

    @pytest.fixture
    def extractor(self):
        return YouTubeExtractor()

    @pytest.mark.parametrize("method", [
        "extract_metadata",
        "search",
        "fetch_channel_videos",
        "fetch_channel_shorts",
        "resolve_channel_handle",
        "resolve_handle_from_channel_id",
    ])
    def test_method_exists(self, extractor, method):
        assert hasattr(extractor, method)
        assert callable(getattr(extractor, method))
