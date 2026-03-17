"""Tests for locale and time formatting helpers."""

from i18n import format_time, format_time_compact, normalize_time_format


class TestNormalizeTimeFormat:
    def test_defaults_to_locale(self):
        assert normalize_time_format(None) == "locale"
        assert normalize_time_format("garbage") == "locale"

    def test_normalizes_aliases(self):
        assert normalize_time_format("24hour") == "24h"
        assert normalize_time_format("12hr") == "12h"


class TestFormatTime:
    def test_en_locale_defaults_to_12h(self):
        assert format_time("20:00", "en") == "8 PM"

    def test_nb_locale_defaults_to_24h(self):
        assert format_time("20:00", "nb") == "20:00"

    def test_forced_24h_overrides_locale(self):
        assert format_time("20:00", "en", time_format="24h") == "20:00"

    def test_forced_12h_overrides_locale(self):
        assert format_time("20:00", "nb", time_format="12h") == "8 PM"


class TestFormatTimeCompact:
    def test_compact_12h(self):
        assert format_time_compact("20:00", "en") == "8p"

    def test_compact_24h(self):
        assert format_time_compact("20:00", "en", time_format="24h") == "20"
