"""
SQLite-backed video storage for BrainRotGuard.
Tracks video requests, approval status, view history, watch time, and channel lists.
"""

import sqlite3
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# Allowlisted thumbnail CDN hostnames (YouTube image servers)
_THUMB_ALLOWED_HOSTS = frozenset({
    "i.ytimg.com", "i1.ytimg.com", "i2.ytimg.com", "i3.ytimg.com",
    "i4.ytimg.com", "i9.ytimg.com", "img.youtube.com",
})


def _validate_thumbnail_url(url: Optional[str]) -> Optional[str]:
    """Return the URL only if it points to an allowlisted YouTube CDN host."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https") and parsed.hostname in _THUMB_ALLOWED_HOSTS:
            return url
    except Exception:
        pass
    return None


class VideoStore:
    """SQLite database for video approval and parental control tracking."""

    def __init__(self, db_path: str = "db/videos.db"):
        """Initialize database connection and create schema."""
        db_file = Path(db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        """Create all tables if they don't exist."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                channel_name TEXT NOT NULL,
                thumbnail_url TEXT,
                duration INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                requested_at TEXT NOT NULL DEFAULT (datetime('now')),
                decided_at TEXT,
                view_count INTEGER DEFAULT 0,
                last_viewed_at TEXT
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS watch_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL,
                duration INTEGER NOT NULL,
                watched_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_watch_log_date ON watch_log(watched_at)
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                status TEXT NOT NULL DEFAULT 'allowed',
                channel_id TEXT,
                added_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Migrate: add channel_id columns if missing
        self._add_column_if_missing("channels", "channel_id", "TEXT")
        self._add_column_if_missing("channels", "handle", "TEXT")
        self._add_column_if_missing("videos", "channel_id", "TEXT")
        self._add_column_if_missing("channels", "category", "TEXT")
        self._add_column_if_missing("videos", "category", "TEXT")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS search_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                result_count INTEGER NOT NULL DEFAULT 0,
                searched_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_search_log_date ON search_log(searched_at)
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_watch_log_video ON watch_log(video_id)
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status)
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS word_filters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word TEXT NOT NULL UNIQUE COLLATE NOCASE,
                added_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

    _ALLOWED_TABLES = {"channels", "videos", "watch_log", "settings", "search_log", "word_filters"}
    _ALLOWED_COLUMNS = {"channel_id", "handle", "category"}

    def _add_column_if_missing(self, table: str, column: str, col_type: str) -> None:
        """Add a column to a table if it doesn't already exist (migration helper)."""
        if table not in self._ALLOWED_TABLES or column not in self._ALLOWED_COLUMNS:
            raise ValueError(f"Disallowed migration target: {table}.{column}")
        cursor = self.conn.execute(f'PRAGMA table_info("{table}")')
        columns = {row[1] for row in cursor.fetchall()}
        if column not in columns:
            self.conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {col_type}')
            self.conn.commit()

    def add_video(
        self,
        video_id: str,
        title: str,
        channel_name: str,
        thumbnail_url: Optional[str] = None,
        duration: Optional[int] = None,
        channel_id: Optional[str] = None,
    ) -> dict:
        """
        Add a new video request. If already exists, return existing.
        Returns the video row as a dict.
        """
        thumbnail_url = _validate_thumbnail_url(thumbnail_url)
        with self._lock:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO videos
                (video_id, title, channel_name, thumbnail_url, duration, channel_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (video_id, title, channel_name, thumbnail_url, duration, channel_id)
            )
            self.conn.commit()
            return self._get_video_unlocked(video_id)

    def _get_video_unlocked(self, video_id: str) -> Optional[dict]:
        """Get video by video_id (caller must hold _lock)."""
        cursor = self.conn.execute(
            "SELECT * FROM videos WHERE video_id = ?",
            (video_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_video(self, video_id: str) -> Optional[dict]:
        """Get video by video_id."""
        with self._lock:
            return self._get_video_unlocked(video_id)

    def find_video_fuzzy(self, encoded_id: str) -> Optional[dict]:
        """Find a video where hyphens were encoded as underscores (Telegram command compat).

        Theoretically ambiguous if two IDs differ only by - vs _, but YouTube
        ID collisions at those positions are astronomically unlikely.
        """
        with self._lock:
            cursor = self.conn.execute(
                "SELECT * FROM videos WHERE REPLACE(video_id, '-', '_') = ?",
                (encoded_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_by_status(self, status: str, channel_name: str = "") -> list[dict]:
        """Get videos with given status, optionally filtered by channel_name."""
        with self._lock:
            if channel_name:
                cursor = self.conn.execute(
                    "SELECT * FROM videos WHERE status = ? AND channel_name = ? COLLATE NOCASE "
                    "ORDER BY requested_at DESC",
                    (status, channel_name),
                )
            else:
                cursor = self.conn.execute(
                    "SELECT * FROM videos WHERE status = ? ORDER BY requested_at DESC",
                    (status,),
                )
            return [dict(row) for row in cursor.fetchall()]

    def get_denied_video_ids(self) -> set[str]:
        """Get set of denied/revoked video IDs (for catalog filtering)."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT video_id FROM videos WHERE status = 'denied'"
            )
            return {row[0] for row in cursor.fetchall()}

    def get_approved(self) -> list[dict]:
        """Get all approved videos."""
        return self.get_by_status("approved")

    def get_pending(self) -> list[dict]:
        """Get all pending videos."""
        return self.get_by_status("pending")

    def get_approved_page(self, page: int = 0, page_size: int = 24) -> tuple[list[dict], int]:
        """Get a page of approved videos with total count for pagination."""
        with self._lock:
            total = self.conn.execute(
                "SELECT COUNT(*) FROM videos WHERE status = 'approved'"
            ).fetchone()[0]
            cursor = self.conn.execute(
                "SELECT * FROM videos WHERE status = 'approved' "
                "ORDER BY requested_at DESC LIMIT ? OFFSET ?",
                (page_size, page * page_size),
            )
            return [dict(row) for row in cursor.fetchall()], total

    def update_status(self, video_id: str, status: str) -> bool:
        """Update video status and set decided_at timestamp. Returns True if updated."""
        with self._lock:
            cursor = self.conn.execute(
                """
                UPDATE videos
                SET status = ?, decided_at = datetime('now')
                WHERE video_id = ?
                """,
                (status, video_id)
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def record_view(self, video_id: str) -> None:
        """Increment view count and update last_viewed_at timestamp."""
        with self._lock:
            self.conn.execute(
                """
                UPDATE videos
                SET view_count = view_count + 1, last_viewed_at = datetime('now')
                WHERE video_id = ?
                """,
                (video_id,)
            )
            self.conn.commit()

    # --- Search logging ---

    def record_search(self, query: str, result_count: int) -> None:
        """Log a search query."""
        query = query[:200]
        with self._lock:
            self.conn.execute(
                "INSERT INTO search_log (query, result_count) VALUES (?, ?)",
                (query, result_count),
            )
            self.conn.commit()

    def get_recent_searches(self, days: int = 7, limit: int = 50) -> list[dict]:
        """Get recent searches within the last N days."""
        with self._lock:
            cursor = self.conn.execute(
                """SELECT query, result_count, searched_at
                   FROM search_log
                   WHERE searched_at >= datetime('now', ?)
                   ORDER BY searched_at DESC
                   LIMIT ?""",
                (f"-{days} days", limit),
            )
            return [dict(row) for row in cursor.fetchall()]


    # --- Word filters ---

    def add_word_filter(self, word: str) -> bool:
        """Add a word to the filter list. Returns True if added."""
        with self._lock:
            try:
                self.conn.execute(
                    "INSERT INTO word_filters (word) VALUES (?)", (word.lower(),)
                )
                self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def remove_word_filter(self, word: str) -> bool:
        """Remove a word from the filter list. Returns True if removed."""
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM word_filters WHERE word = ? COLLATE NOCASE", (word,)
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def get_word_filters(self) -> list[str]:
        """Get all filtered words."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT word FROM word_filters ORDER BY word"
            )
            return [row[0] for row in cursor.fetchall()]

    def get_word_filters_set(self) -> set[str]:
        """Get set of filtered words (lowercased)."""
        with self._lock:
            cursor = self.conn.execute("SELECT word FROM word_filters")
            return {row[0].lower() for row in cursor.fetchall()}

    # --- Categories (edu / fun) ---

    def set_channel_category(self, name_or_handle: str, category: Optional[str]) -> bool:
        """Set a channel's category by name or @handle. Pass None to unset."""
        with self._lock:
            cursor = self.conn.execute(
                "UPDATE channels SET category = ? WHERE channel_name = ? COLLATE NOCASE OR handle = ? COLLATE NOCASE",
                (category, name_or_handle, name_or_handle),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def set_video_category(self, video_id: str, category: Optional[str]) -> bool:
        """Set a video's category (overrides channel default). Pass None to unset."""
        with self._lock:
            cursor = self.conn.execute(
                "UPDATE videos SET category = ? WHERE video_id = ?",
                (category, video_id),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def set_channel_videos_category(self, channel_name: str, category: str) -> int:
        """Update category on all videos belonging to a channel. Returns count updated."""
        with self._lock:
            cursor = self.conn.execute(
                "UPDATE videos SET category = ? WHERE channel_name = ? COLLATE NOCASE",
                (category, channel_name),
            )
            self.conn.commit()
            return cursor.rowcount

    def get_channel_category(self, channel_name: str) -> Optional[str]:
        """Get a channel's assigned category."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT category FROM channels WHERE channel_name = ? COLLATE NOCASE",
                (channel_name,),
            )
            row = cursor.fetchone()
            return row[0] if row and row[0] else None

    def get_daily_watch_by_category(self, date_str: str, utc_bounds: tuple[str, str] | None = None) -> dict:
        """Sum watch time per effective category for a date. Returns {category_or_None: minutes}."""
        start, end = utc_bounds if utc_bounds else (date_str, date_str)
        end_clause = "?" if utc_bounds else "date(?, '+1 day')"
        with self._lock:
            cursor = self.conn.execute(
                "SELECT COALESCE(v.category, c.category) as cat, "
                "       COALESCE(SUM(w.duration), 0) as total_sec "
                "FROM watch_log w "
                "LEFT JOIN videos v ON w.video_id = v.video_id "
                "LEFT JOIN channels c ON v.channel_name = c.channel_name COLLATE NOCASE "
                f"WHERE w.watched_at >= ? AND w.watched_at < {end_clause} "
                "GROUP BY cat",
                (start, end),
            )
            return {row[0]: row[1] / 60.0 for row in cursor.fetchall()}

    # --- Watch time tracking ---

    def record_watch_seconds(self, video_id: str, seconds: int) -> None:
        """Log playback seconds from heartbeat."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO watch_log (video_id, duration) VALUES (?, ?)",
                (video_id, seconds),
            )
            self.conn.commit()

    def get_video_watch_minutes(self, video_id: str) -> float:
        """Get cumulative watch time for a specific video. Returns minutes."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT COALESCE(SUM(duration), 0) FROM watch_log WHERE video_id = ?",
                (video_id,),
            )
            return cursor.fetchone()[0] / 60.0

    def get_batch_watch_minutes(self, video_ids: list[str]) -> dict[str, float]:
        """Get cumulative watch time for multiple videos in one query."""
        if not video_ids:
            return {}
        with self._lock:
            placeholders = ",".join("?" for _ in video_ids)
            cursor = self.conn.execute(
                f"SELECT video_id, COALESCE(SUM(duration), 0) "
                f"FROM watch_log WHERE video_id IN ({placeholders}) GROUP BY video_id",
                video_ids,
            )
            result = {row[0]: row[1] / 60.0 for row in cursor.fetchall()}
            for vid in video_ids:
                if vid not in result:
                    result[vid] = 0.0
            return result

    def get_daily_watch_minutes(self, date_str: str, utc_bounds: tuple[str, str] | None = None) -> float:
        """Sum watch time for a date (YYYY-MM-DD). Returns minutes."""
        start, end = utc_bounds if utc_bounds else (date_str, date_str)
        end_clause = "?" if utc_bounds else "date(?, '+1 day')"
        with self._lock:
            cursor = self.conn.execute(
                "SELECT COALESCE(SUM(duration), 0) FROM watch_log "
                f"WHERE watched_at >= ? AND watched_at < {end_clause}",
                (start, end),
            )
            total_seconds = cursor.fetchone()[0]
            return total_seconds / 60.0

    def get_daily_watch_breakdown(self, date_str: str, utc_bounds: tuple[str, str] | None = None) -> list[dict]:
        """Per-video watch time for a date, sorted by most watched. Returns list of dicts."""
        start, end = utc_bounds if utc_bounds else (date_str, date_str)
        end_clause = "?" if utc_bounds else "date(?, '+1 day')"
        with self._lock:
            cursor = self.conn.execute(
                "SELECT w.video_id, COALESCE(SUM(w.duration), 0) as total_sec,"
                "       v.title, v.channel_name, v.thumbnail_url,"
                "       v.duration, v.channel_id,"
                "       COALESCE(v.category, c.category) as category "
                "FROM watch_log w LEFT JOIN videos v ON w.video_id = v.video_id "
                "LEFT JOIN channels c ON v.channel_name = c.channel_name COLLATE NOCASE "
                f"WHERE w.watched_at >= ? AND w.watched_at < {end_clause} "
                "GROUP BY w.video_id ORDER BY total_sec DESC",
                (start, end),
            )
            return [
                {
                    "video_id": row[0],
                    "minutes": round(row[1] / 60.0, 1),
                    "title": row[2] or row[0],
                    "channel_name": row[3] or "Unknown",
                    "thumbnail_url": row[4] or "",
                    "duration": row[5],
                    "channel_id": row[6],
                    "category": row[7],
                }
                for row in cursor.fetchall()
            ]

    # --- Channel allow/block lists ---

    def add_channel(self, name: str, status: str, channel_id: Optional[str] = None,
                    handle: Optional[str] = None, category: Optional[str] = None) -> bool:
        """Add or update a channel in allow/block list. Returns True if inserted/updated."""
        with self._lock:
            self.conn.execute(
                """INSERT INTO channels (channel_name, status, channel_id, handle, category) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(channel_name) DO UPDATE SET status = ?,
                   channel_id = COALESCE(?, channel_id),
                   handle = COALESCE(?, handle),
                   category = COALESCE(?, category),
                   added_at = datetime('now')""",
                (name, status, channel_id, handle, category, status, channel_id, handle, category),
            )
            self.conn.commit()
            return True

    def remove_channel(self, name_or_handle: str) -> bool:
        """Remove a channel by name or @handle. Returns True if deleted."""
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM channels WHERE channel_name = ? COLLATE NOCASE OR handle = ? COLLATE NOCASE",
                (name_or_handle, name_or_handle),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def resolve_channel_name(self, name_or_handle: str) -> Optional[str]:
        """Look up channel_name by name or @handle. Returns the display name or None."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT channel_name FROM channels WHERE channel_name = ? COLLATE NOCASE OR handle = ? COLLATE NOCASE",
                (name_or_handle, name_or_handle),
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def get_channels_missing_handles(self) -> list[tuple[str, str]]:
        """Get (channel_name, channel_id) for channels with a channel_id but no handle."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT channel_name, channel_id FROM channels "
                "WHERE channel_id IS NOT NULL AND (handle IS NULL OR handle = '')"
            )
            return [(row[0], row[1]) for row in cursor.fetchall()]

    def update_channel_handle(self, channel_name: str, handle: str) -> bool:
        """Set a channel's handle by name."""
        with self._lock:
            cursor = self.conn.execute(
                "UPDATE channels SET handle = ? WHERE channel_name = ? COLLATE NOCASE",
                (handle, channel_name),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def get_channels(self, status: str) -> list[str]:
        """List channel names by status."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT channel_name FROM channels WHERE status = ? ORDER BY channel_name",
                (status,),
            )
            return [row[0] for row in cursor.fetchall()]

    def get_channels_with_ids(self, status: str) -> list[tuple[str, Optional[str], Optional[str], Optional[str]]]:
        """List (channel_name, channel_id, handle, category) tuples by status."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT channel_name, channel_id, handle, category FROM channels WHERE status = ? ORDER BY channel_name",
                (status,),
            )
            return [(row[0], row[1], row[2], row[3]) for row in cursor.fetchall()]

    def is_channel_allowed(self, name: str) -> bool:
        """Check if channel is on the allowlist."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT 1 FROM channels WHERE channel_name = ? COLLATE NOCASE AND status = 'allowed'",
                (name,),
            )
            return cursor.fetchone() is not None

    def is_channel_blocked(self, name: str) -> bool:
        """Check if channel is on the blocklist."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT 1 FROM channels WHERE channel_name = ? COLLATE NOCASE AND status = 'blocked'",
                (name,),
            )
            return cursor.fetchone() is not None

    def get_blocked_channels_set(self) -> set[str]:
        """Get set of blocked channel names (lowercased for bulk filtering).

        Uses Python .lower() for efficient batch comparison in search results.
        Point lookups (is_channel_blocked) use SQL COLLATE NOCASE instead.
        Both are equivalent for ASCII YouTube channel names.
        """
        with self._lock:
            cursor = self.conn.execute(
                "SELECT channel_name FROM channels WHERE status = 'blocked'"
            )
            return {row[0].lower() for row in cursor.fetchall()}

    # --- Settings ---

    def get_setting(self, key: str, default: str = "") -> str:
        """Read a setting value."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            )
            row = cursor.fetchone()
            return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        """Write a setting (upsert)."""
        with self._lock:
            self.conn.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = datetime('now')""",
                (key, value, value),
            )
            self.conn.commit()

    # --- Activity report ---

    def get_recent_activity(self, days: int = 7, limit: int = 50) -> list[dict]:
        """Get recent video requests within the last N days."""
        with self._lock:
            cursor = self.conn.execute(
                """SELECT video_id, title, channel_name, status, requested_at, view_count
                   FROM videos
                   WHERE requested_at >= datetime('now', ?)
                   ORDER BY requested_at DESC
                   LIMIT ?""",
                (f"-{days} days", limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    # --- Stats ---

    def get_stats(self) -> dict:
        """Get aggregate statistics across all videos."""
        with self._lock:
            cursor = self.conn.execute("""
                SELECT
                    COUNT(*) as total,
                    COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) as pending,
                    COALESCE(SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END), 0) as approved,
                    COALESCE(SUM(CASE WHEN status = 'denied' THEN 1 ELSE 0 END), 0) as denied,
                    COALESCE(SUM(view_count), 0) as total_views
                FROM videos
            """)
            row = cursor.fetchone()
            return dict(row) if row else {"total": 0, "pending": 0, "approved": 0, "denied": 0, "total_views": 0}

    def prune_old_data(self, watch_days: int = 180, search_days: int = 90) -> tuple[int, int]:
        """Delete watch_log and search_log entries older than N days."""
        with self._lock:
            c1 = self.conn.execute(
                "DELETE FROM watch_log WHERE watched_at < datetime('now', ?)",
                (f"-{watch_days} days",),
            )
            c2 = self.conn.execute(
                "DELETE FROM search_log WHERE searched_at < datetime('now', ?)",
                (f"-{search_days} days",),
            )
            self.conn.commit()
            return c1.rowcount, c2.rowcount

    def close(self) -> None:
        """Close database connection."""
        self.conn.close()
