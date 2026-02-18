"""Shared utilities for BrainRotGuard."""

import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Matches: 800, 0800, 8:00, 800am, 8:00am, 800pm, 8:00PM, 2000, 20:00
_TIME_RE = re.compile(
    r'^(\d{1,2}):?(\d{2})\s*(am|pm)?$',
    re.IGNORECASE,
)


def get_today_str(tz_name: str = "") -> str:
    """Get today's date as YYYY-MM-DD in the given timezone.

    Falls back to UTC if tz_name is empty or invalid.
    """
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
            return datetime.now(tz).strftime("%Y-%m-%d")
        except Exception:
            logger.warning("Invalid timezone %r, falling back to UTC", tz_name)
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_day_utc_bounds(date_str: str, tz_name: str = "") -> tuple[str, str]:
    """Convert a local date (YYYY-MM-DD) to UTC start/end timestamps.

    Returns (start_utc, end_utc) as ISO strings for use in SQL queries
    against UTC-stored watched_at timestamps.
    """
    from datetime import timedelta
    local_date = datetime.strptime(date_str, "%Y-%m-%d")
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
            start_local = local_date.replace(tzinfo=tz)
            end_local = (local_date + timedelta(days=1)).replace(tzinfo=tz)
            start_utc = start_local.astimezone(timezone.utc)
            end_utc = end_local.astimezone(timezone.utc)
            return (start_utc.strftime("%Y-%m-%d %H:%M:%S"),
                    end_utc.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            logger.warning("Invalid timezone %r for day bounds, using date as-is", tz_name)
    return (date_str, f"{date_str} 23:59:59")


def parse_time_input(raw: str) -> str | None:
    """Parse flexible time input into 24-hour "HH:MM" format.

    Accepts: 800, 0800, 8:00, 800am, 8:00am, 800pm, 8:00PM, 2000, 20:00
    Returns normalized "HH:MM" string or None if invalid.
    """
    raw = raw.strip()
    m = _TIME_RE.match(raw)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2))
    meridiem = (m.group(3) or "").lower()

    if meridiem == "am":
        if hour == 12:
            hour = 0
        elif hour > 12:
            return None
    elif meridiem == "pm":
        if hour == 12:
            pass  # 12pm = 12
        elif hour > 12:
            return None
        else:
            hour += 12
    else:
        # 24-hour format
        if hour > 23:
            return None

    if minute > 59:
        return None

    return f"{hour:02d}:{minute:02d}"


def format_time_12h(hhmm: str) -> str:
    """Convert "HH:MM" to human-readable 12-hour format.

    "08:00" -> "8:00 AM", "20:00" -> "8:00 PM", "00:00" -> "12:00 AM"
    """
    try:
        h, m = map(int, hhmm.split(":"))
    except (ValueError, AttributeError):
        return hhmm
    if h == 0:
        return f"12:{m:02d} AM"
    elif h < 12:
        return f"{h}:{m:02d} AM"
    elif h == 12:
        return f"12:{m:02d} PM"
    else:
        return f"{h - 12}:{m:02d} PM"


def is_within_schedule(start_str: str, end_str: str, tz_name: str = "") -> tuple[bool, str]:
    """Check if current time falls within the scheduled access window.

    Returns (allowed, unlock_time_display).
    - Both empty → (True, "")
    - Only start set → allowed from start to midnight (blocked before start)
    - Only end set → allowed from midnight to end (blocked after end)
    - Both set → allowed within [start, end), handles overnight wrap
    """
    if not start_str and not end_str:
        return (True, "")

    # Get current local time
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(tz_name))
        except Exception:
            now = datetime.now(timezone.utc)
    else:
        now = datetime.now(timezone.utc)

    now_minutes = now.hour * 60 + now.minute

    # Only start set: blocked before start, allowed from start onward
    if start_str and not end_str:
        try:
            sh, sm = map(int, start_str.split(":"))
        except (ValueError, AttributeError):
            return (True, "")
        allowed = now_minutes >= sh * 60 + sm
        unlock_time = format_time_12h(start_str) if not allowed else ""
        return (allowed, unlock_time)

    # Only end set: allowed until end, blocked after
    if end_str and not start_str:
        try:
            eh, em = map(int, end_str.split(":"))
        except (ValueError, AttributeError):
            return (True, "")
        allowed = now_minutes < eh * 60 + em
        unlock_time = "tomorrow" if not allowed else ""
        return (allowed, unlock_time)

    # Both set
    try:
        start_h, start_m = map(int, start_str.split(":"))
        end_h, end_m = map(int, end_str.split(":"))
    except (ValueError, AttributeError):
        return (True, "")

    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m

    if start_minutes <= end_minutes:
        # Normal range (e.g. 08:00 - 20:00)
        allowed = start_minutes <= now_minutes < end_minutes
    else:
        # Overnight range (e.g. 22:00 - 06:00)
        allowed = now_minutes >= start_minutes or now_minutes < end_minutes

    unlock_time = format_time_12h(start_str) if not allowed else ""
    return (allowed, unlock_time)
