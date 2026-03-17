"""Minimal localization helpers for BrainRotGuard."""

from __future__ import annotations

from datetime import datetime

from i18n.locales.en import TRANSLATIONS as EN_TRANSLATIONS
from i18n.locales.nb import MONTHS_SHORT as NB_MONTHS_SHORT
from i18n.locales.nb import TRANSLATIONS as NB_TRANSLATIONS

DEFAULT_LOCALE = "en"
SUPPORTED_LOCALES = {"en", "nb"}
DEFAULT_TIME_FORMAT = "locale"
SUPPORTED_TIME_FORMATS = {"locale", "12h", "24h"}

_TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": EN_TRANSLATIONS,
    "nb": NB_TRANSLATIONS,
}


def normalize_locale(locale: str | None) -> str:
    """Normalize user/config locale to a supported code."""
    if not locale:
        return DEFAULT_LOCALE
    value = locale.strip().lower().replace("_", "-")
    if value in ("no", "nn", "nb-no", "no-no"):
        return "nb"
    if value.startswith("nb"):
        return "nb"
    if value.startswith("en"):
        return "en"
    return DEFAULT_LOCALE


def get_locale(config) -> str:
    """Extract normalized locale from config."""
    app = getattr(config, "app", None)
    return normalize_locale(getattr(app, "locale", DEFAULT_LOCALE))


def normalize_time_format(value: str | None) -> str:
    """Normalize configured time format to a supported value."""
    if not value:
        return DEFAULT_TIME_FORMAT
    normalized = value.strip().lower().replace("_", "").replace("-", "")
    if normalized in ("locale", "default", "auto"):
        return "locale"
    if normalized in ("12h", "12hr", "12hour", "ampm"):
        return "12h"
    if normalized in ("24h", "24hr", "24hour"):
        return "24h"
    return DEFAULT_TIME_FORMAT


def get_time_format(config) -> str:
    """Extract normalized time format from config."""
    app = getattr(config, "app", None)
    return normalize_time_format(getattr(app, "time_format", DEFAULT_TIME_FORMAT))


def t(locale: str | None, key: str, **kwargs) -> str:
    """Translate a text key with English fallback."""
    normalized = normalize_locale(locale)
    text = _TRANSLATIONS.get(normalized, {}).get(key, key)
    if kwargs:
        return text.format(**kwargs)
    return text


def category_label(category: str, locale: str | None, short: bool = False) -> str:
    """Return localized category label."""
    if short:
        if category == "edu":
            return t(locale, "Edu")
        return t(locale, "Fun")
    if category == "edu":
        return t(locale, "Educational")
    return t(locale, "Entertainment")


def day_label(day: str, locale: str | None, short: bool = False) -> str:
    """Return localized day label from canonical day code."""
    mapping = {
        "mon": ("Monday", "Mon"),
        "tue": ("Tuesday", "Tue"),
        "wed": ("Wednesday", "Wed"),
        "thu": ("Thursday", "Thu"),
        "fri": ("Friday", "Fri"),
        "sat": ("Saturday", "Sat"),
        "sun": ("Sunday", "Sun"),
    }
    long_key, short_key = mapping.get(day, (day, day.capitalize()[:3]))
    return t(locale, short_key if short else long_key)


def _uses_24h(locale: str | None, time_format: str | None) -> bool:
    """Decide whether to render times in 24-hour format."""
    normalized_format = normalize_time_format(time_format)
    if normalized_format == "24h":
        return True
    if normalized_format == "12h":
        return False
    return normalize_locale(locale) == "nb"


def format_time(hhmm: str | None, locale: str | None, time_format: str | None = None) -> str | None:
    """Locale-aware time formatting with optional explicit time format."""
    if hhmm is None:
        return None
    try:
        hour, minute = map(int, hhmm.split(":"))
    except (ValueError, AttributeError):
        return hhmm

    if _uses_24h(locale, time_format):
        return f"{hour:02d}:{minute:02d}"

    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    if minute == 0:
        return f"{display_hour} {suffix}"
    return f"{display_hour}:{minute:02d} {suffix}"


def format_time_compact(hhmm: str | None, locale: str | None, time_format: str | None = None) -> str | None:
    """Shorter locale-aware time formatting for compact grids."""
    if hhmm is None:
        return None
    if _uses_24h(locale, time_format):
        try:
            hour, minute = map(int, hhmm.split(":"))
        except (ValueError, AttributeError):
            return hhmm
        return f"{hour:02d}" if minute == 0 else f"{hour:02d}:{minute:02d}"
    display = format_time(hhmm, locale, time_format=time_format)
    if display is None:
        return None
    return display.replace(" AM", "a").replace(" PM", "p").replace(":00", "")


def format_month_day(date_str: str, locale: str | None) -> str:
    """Format YYYY-MM-DD as a short month/day label."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    if normalize_locale(locale) == "nb":
        return f"{NB_MONTHS_SHORT[dt.month - 1]} {dt.day:02d}"
    return dt.strftime("%b %d")


def html_lang(locale: str | None) -> str:
    """Return HTML lang attribute value."""
    return normalize_locale(locale)


__all__ = [
    "DEFAULT_LOCALE",
    "DEFAULT_TIME_FORMAT",
    "SUPPORTED_LOCALES",
    "SUPPORTED_TIME_FORMATS",
    "category_label",
    "day_label",
    "format_month_day",
    "format_time",
    "format_time_compact",
    "get_locale",
    "get_time_format",
    "html_lang",
    "normalize_locale",
    "normalize_time_format",
    "t",
]
