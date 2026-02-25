"""
SQLite-backed video storage for BrainRotGuard.
Tracks video requests, approval status, view history, watch time, and channel lists.
Supports per-child profiles with isolated data.
"""

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from youtube.extractor import THUMB_ALLOWED_HOSTS

logger = logging.getLogger(__name__)


def _validate_thumbnail_url(url: Optional[str]) -> Optional[str]:
    """Return the URL only if it points to an allowlisted YouTube CDN host."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.hostname in THUMB_ALLOWED_HOSTS:
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
        # --- Profiles table (new for multi-child) ---
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                pin TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL,
                title TEXT NOT NULL,
                channel_name TEXT NOT NULL,
                thumbnail_url TEXT,
                duration INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                requested_at TEXT NOT NULL DEFAULT (datetime('now')),
                decided_at TEXT,
                view_count INTEGER DEFAULT 0,
                last_viewed_at TEXT,
                profile_id TEXT NOT NULL DEFAULT 'default',
                UNIQUE(video_id, profile_id)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS watch_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL,
                duration INTEGER NOT NULL,
                watched_at TEXT NOT NULL DEFAULT (datetime('now')),
                profile_id TEXT NOT NULL DEFAULT 'default'
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_watch_log_date ON watch_log(watched_at)
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_name TEXT NOT NULL COLLATE NOCASE,
                status TEXT NOT NULL DEFAULT 'allowed',
                channel_id TEXT,
                added_at TEXT NOT NULL DEFAULT (datetime('now')),
                profile_id TEXT NOT NULL DEFAULT 'default',
                UNIQUE(channel_name, profile_id)
            )
        """)
        # Migrate: add columns if missing (pre-profile legacy columns)
        self._add_column_if_missing("channels", "channel_id", "TEXT")
        self._add_column_if_missing("channels", "handle", "TEXT")
        self._add_column_if_missing("videos", "channel_id", "TEXT")
        self._add_column_if_missing("channels", "category", "TEXT")
        self._add_column_if_missing("videos", "category", "TEXT")
        self._add_column_if_missing("videos", "is_short", "INTEGER DEFAULT 0")
        self._add_column_if_missing("profiles", "avatar_icon", "TEXT")
        self._add_column_if_missing("profiles", "avatar_color", "TEXT")
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
                searched_at TEXT NOT NULL DEFAULT (datetime('now')),
                profile_id TEXT NOT NULL DEFAULT 'default'
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

        # Run multi-child profile migrations for existing databases
        self._migrate_profile_id()

    _ALLOWED_TABLES = {"channels", "videos", "watch_log", "settings", "search_log", "word_filters", "profiles"}
    _ALLOWED_COLUMNS = {"channel_id", "handle", "category", "is_short", "profile_id", "avatar_icon", "avatar_color"}

    def _add_column_if_missing(self, table: str, column: str, col_type: str) -> None:
        """Add a column to a table if it doesn't already exist (migration helper)."""
        if table not in self._ALLOWED_TABLES or column not in self._ALLOWED_COLUMNS:
            raise ValueError(f"Disallowed migration target: {table}.{column}")
        cursor = self.conn.execute(f'PRAGMA table_info("{table}")')
        columns = {row[1] for row in cursor.fetchall()}
        if column not in columns:
            self.conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {col_type}')
            self.conn.commit()

    def _has_column(self, table: str, column: str) -> bool:
        """Check if a table has a specific column."""
        cursor = self.conn.execute(f'PRAGMA table_info("{table}")')
        return column in {row[1] for row in cursor.fetchall()}

    def _migrate_profile_id(self) -> None:
        """Migrate existing tables to include profile_id column.

        For videos and channels, the unique constraint changes require a table rebuild.
        For watch_log and search_log, a simple column add suffices.
        """
        # Simple column adds for log tables
        if not self._has_column("watch_log", "profile_id"):
            self._add_column_if_missing("watch_log", "profile_id", "TEXT NOT NULL DEFAULT 'default'")
            logger.info("Migrated watch_log: added profile_id column")
        if not self._has_column("search_log", "profile_id"):
            self._add_column_if_missing("search_log", "profile_id", "TEXT NOT NULL DEFAULT 'default'")
            logger.info("Migrated search_log: added profile_id column")

        # Table rebuilds for videos (unique constraint change: video_id → video_id+profile_id)
        if not self._has_column("videos", "profile_id"):
            self._rebuild_videos_table()
            logger.info("Migrated videos: added profile_id, updated unique constraint")

        # Table rebuild for channels (unique constraint change: channel_name → channel_name+profile_id)
        if not self._has_column("channels", "profile_id"):
            self._rebuild_channels_table()
            logger.info("Migrated channels: added profile_id, updated unique constraint")

    def _rebuild_videos_table(self) -> None:
        """Rebuild videos table with profile_id and new unique constraint."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS videos_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL,
                title TEXT NOT NULL,
                channel_name TEXT NOT NULL,
                thumbnail_url TEXT,
                duration INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                requested_at TEXT NOT NULL DEFAULT (datetime('now')),
                decided_at TEXT,
                view_count INTEGER DEFAULT 0,
                last_viewed_at TEXT,
                channel_id TEXT,
                category TEXT,
                is_short INTEGER DEFAULT 0,
                profile_id TEXT NOT NULL DEFAULT 'default',
                UNIQUE(video_id, profile_id)
            );
            INSERT OR IGNORE INTO videos_new
                (id, video_id, title, channel_name, thumbnail_url, duration, status,
                 requested_at, decided_at, view_count, last_viewed_at,
                 channel_id, category, is_short, profile_id)
            SELECT id, video_id, title, channel_name, thumbnail_url, duration, status,
                   requested_at, decided_at, view_count, last_viewed_at,
                   channel_id, category, COALESCE(is_short, 0), 'default'
            FROM videos;
            DROP TABLE videos;
            ALTER TABLE videos_new RENAME TO videos;
            CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
            CREATE INDEX IF NOT EXISTS idx_videos_profile ON videos(profile_id);
        """)
        self.conn.commit()

    def _rebuild_channels_table(self) -> None:
        """Rebuild channels table with profile_id and new unique constraint."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS channels_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_name TEXT NOT NULL COLLATE NOCASE,
                status TEXT NOT NULL DEFAULT 'allowed',
                channel_id TEXT,
                added_at TEXT NOT NULL DEFAULT (datetime('now')),
                handle TEXT,
                category TEXT,
                profile_id TEXT NOT NULL DEFAULT 'default',
                UNIQUE(channel_name, profile_id)
            );
            INSERT OR IGNORE INTO channels_new
                (id, channel_name, status, channel_id, added_at, handle, category, profile_id)
            SELECT id, channel_name, status, channel_id, added_at, handle, category, 'default'
            FROM channels;
            DROP TABLE channels;
            ALTER TABLE channels_new RENAME TO channels;
            CREATE INDEX IF NOT EXISTS idx_channels_profile ON channels(profile_id);
        """)
        self.conn.commit()

    # --- Profile CRUD ---

    def get_profiles(self) -> list[dict]:
        """Get all profiles."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT id, display_name, pin, created_at, avatar_icon, avatar_color FROM profiles ORDER BY created_at"
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_profile(self, profile_id: str) -> Optional[dict]:
        """Get a profile by ID."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT id, display_name, pin, created_at, avatar_icon, avatar_color FROM profiles WHERE id = ?",
                (profile_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_profile_by_pin(self, pin: str) -> Optional[dict]:
        """Get a profile by PIN. Returns None if no match or PIN is empty."""
        if not pin:
            return None
        with self._lock:
            cursor = self.conn.execute(
                "SELECT id, display_name, pin, created_at, avatar_icon, avatar_color FROM profiles WHERE pin = ?",
                (pin,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def create_profile(self, profile_id: str, display_name: str, pin: str = "") -> bool:
        """Create a new profile. Returns True if created."""
        with self._lock:
            try:
                self.conn.execute(
                    "INSERT INTO profiles (id, display_name, pin) VALUES (?, ?, ?)",
                    (profile_id, display_name, pin),
                )
                self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def update_profile(self, profile_id: str, display_name: str = None,
                       pin: str = None) -> bool:
        """Update a profile's display_name and/or pin. Returns True if updated."""
        parts = []
        params = []
        if display_name is not None:
            parts.append("display_name = ?")
            params.append(display_name)
        if pin is not None:
            parts.append("pin = ?")
            params.append(pin)
        if not parts:
            return False
        params.append(profile_id)
        with self._lock:
            cursor = self.conn.execute(
                f"UPDATE profiles SET {', '.join(parts)} WHERE id = ?",
                params,
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def update_profile_avatar(self, profile_id: str, icon: Optional[str] = None,
                              color: Optional[str] = None) -> bool:
        """Update a profile's avatar icon and/or color. Returns True if updated."""
        parts = []
        params = []
        if icon is not None:
            parts.append("avatar_icon = ?")
            params.append(icon)
        if color is not None:
            parts.append("avatar_color = ?")
            params.append(color)
        if not parts:
            return False
        params.append(profile_id)
        with self._lock:
            cursor = self.conn.execute(
                f"UPDATE profiles SET {', '.join(parts)} WHERE id = ?",
                params,
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def delete_profile(self, profile_id: str) -> bool:
        """Hard delete a profile and all associated data. Returns True if deleted."""
        with self._lock:
            cursor = self.conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
            if cursor.rowcount == 0:
                self.conn.commit()
                return False
            # Cascade delete all child data
            self.conn.execute("DELETE FROM videos WHERE profile_id = ?", (profile_id,))
            self.conn.execute("DELETE FROM watch_log WHERE profile_id = ?", (profile_id,))
            self.conn.execute("DELETE FROM channels WHERE profile_id = ?", (profile_id,))
            self.conn.execute("DELETE FROM search_log WHERE profile_id = ?", (profile_id,))
            # Delete prefixed settings
            self.conn.execute(
                "DELETE FROM settings WHERE key LIKE ?",
                (f"{profile_id}:%",),
            )
            self.conn.commit()
            return True

    def find_video_approved_for_others(self, video_id: str, exclude_profile: str) -> Optional[dict]:
        """Check if a video is approved under a different profile.
        Returns the video row dict if found, else None.
        """
        with self._lock:
            cursor = self.conn.execute(
                "SELECT * FROM videos WHERE video_id = ? AND profile_id != ? AND status = 'approved' LIMIT 1",
                (video_id, exclude_profile),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    # --- Video CRUD ---

    def add_video(
        self,
        video_id: str,
        title: str,
        channel_name: str,
        thumbnail_url: Optional[str] = None,
        duration: Optional[int] = None,
        channel_id: Optional[str] = None,
        is_short: bool = False,
        profile_id: str = "default",
    ) -> dict:
        """
        Add a new video request. If already exists for this profile, return existing.
        Returns the video row as a dict.
        """
        thumbnail_url = _validate_thumbnail_url(thumbnail_url)
        with self._lock:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO videos
                (video_id, title, channel_name, thumbnail_url, duration, channel_id, is_short, profile_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (video_id, title, channel_name, thumbnail_url, duration, channel_id, int(is_short), profile_id)
            )
            self.conn.commit()
            return self._get_video_unlocked(video_id, profile_id)

    def _get_video_unlocked(self, video_id: str, profile_id: str = "default") -> Optional[dict]:
        """Get video by video_id and profile_id (caller must hold _lock)."""
        cursor = self.conn.execute(
            "SELECT * FROM videos WHERE video_id = ? AND profile_id = ?",
            (video_id, profile_id)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_video(self, video_id: str, profile_id: str = "default") -> Optional[dict]:
        """Get video by video_id and profile_id."""
        with self._lock:
            return self._get_video_unlocked(video_id, profile_id)

    def find_video_fuzzy(self, encoded_id: str, profile_id: str = "default") -> Optional[dict]:
        """Find a video where hyphens were encoded as underscores (Telegram command compat)."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT * FROM videos WHERE REPLACE(video_id, '-', '_') = ? AND profile_id = ?",
                (encoded_id, profile_id),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_by_status(self, status: str, channel_name: str = "", channel_id: str = "",
                      profile_id: str = "default") -> list[dict]:
        """Get videos with given status for a profile."""
        with self._lock:
            if channel_id:
                cursor = self.conn.execute(
                    "SELECT * FROM videos WHERE status = ? AND channel_id = ? AND profile_id = ? "
                    "ORDER BY requested_at DESC",
                    (status, channel_id, profile_id),
                )
            elif channel_name:
                cursor = self.conn.execute(
                    "SELECT * FROM videos WHERE status = ? AND channel_name = ? COLLATE NOCASE AND profile_id = ? "
                    "ORDER BY requested_at DESC",
                    (status, channel_name, profile_id),
                )
            else:
                cursor = self.conn.execute(
                    "SELECT * FROM videos WHERE status = ? AND profile_id = ? ORDER BY requested_at DESC",
                    (status, profile_id),
                )
            return [dict(row) for row in cursor.fetchall()]

    def get_denied_video_ids(self, profile_id: str = "default") -> set[str]:
        """Get set of denied/revoked video IDs for a profile."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT video_id FROM videos WHERE status = 'denied' AND profile_id = ?",
                (profile_id,),
            )
            return {row[0] for row in cursor.fetchall()}

    def get_approved(self, profile_id: str = "default") -> list[dict]:
        """Get all approved videos for a profile."""
        return self.get_by_status("approved", profile_id=profile_id)

    def get_pending(self, profile_id: str = "default") -> list[dict]:
        """Get all pending videos for a profile."""
        return self.get_by_status("pending", profile_id=profile_id)

    def get_approved_page(self, page: int = 0, page_size: int = 24,
                          profile_id: str = "default") -> tuple[list[dict], int]:
        """Get a page of approved videos with total count for a profile."""
        with self._lock:
            total = self.conn.execute(
                "SELECT COUNT(*) FROM videos WHERE status = 'approved' AND profile_id = ?",
                (profile_id,),
            ).fetchone()[0]
            cursor = self.conn.execute(
                "SELECT * FROM videos WHERE status = 'approved' AND profile_id = ? "
                "ORDER BY requested_at DESC LIMIT ? OFFSET ?",
                (profile_id, page_size, page * page_size),
            )
            return [dict(row) for row in cursor.fetchall()], total

    def get_approved_shorts(self, limit: int = 50, profile_id: str = "default") -> list[dict]:
        """Get approved Shorts for a profile."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT * FROM videos WHERE status = 'approved' AND is_short = 1 AND profile_id = ? "
                "ORDER BY requested_at DESC LIMIT ?",
                (profile_id, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def search_approved(self, query: str, limit: int = 50, profile_id: str = "default") -> list[dict]:
        """Search approved videos by title or channel name for a profile."""
        pattern = f"%{query}%"
        with self._lock:
            cursor = self.conn.execute(
                "SELECT * FROM videos WHERE status = 'approved' AND profile_id = ? "
                "AND (title LIKE ? COLLATE NOCASE OR channel_name LIKE ? COLLATE NOCASE) "
                "ORDER BY requested_at DESC LIMIT ?",
                (profile_id, pattern, pattern, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_recent_requests(self, limit: int = 50, profile_id: str = "default") -> list[dict]:
        """Get recently approved non-Short videos for a profile."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT * FROM videos WHERE status = 'approved' AND is_short = 0 AND profile_id = ? "
                "ORDER BY decided_at DESC, requested_at DESC LIMIT ?",
                (profile_id, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def update_status(self, video_id: str, status: str, profile_id: str = "default") -> bool:
        """Update video status for a profile. Returns True if updated."""
        with self._lock:
            cursor = self.conn.execute(
                """
                UPDATE videos
                SET status = ?, decided_at = datetime('now')
                WHERE video_id = ? AND profile_id = ?
                """,
                (status, video_id, profile_id)
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def record_view(self, video_id: str, profile_id: str = "default") -> None:
        """Increment view count for a profile's video."""
        with self._lock:
            self.conn.execute(
                """
                UPDATE videos
                SET view_count = view_count + 1, last_viewed_at = datetime('now')
                WHERE video_id = ? AND profile_id = ?
                """,
                (video_id, profile_id)
            )
            self.conn.commit()

    # --- Search logging ---

    def record_search(self, query: str, result_count: int, profile_id: str = "default") -> None:
        """Log a search query for a profile."""
        query = query[:200]
        with self._lock:
            self.conn.execute(
                "INSERT INTO search_log (query, result_count, profile_id) VALUES (?, ?, ?)",
                (query, result_count, profile_id),
            )
            self.conn.commit()

    def get_recent_searches(self, days: int = 7, limit: int = 50,
                            profile_id: str = "default") -> list[dict]:
        """Get recent searches for a profile."""
        with self._lock:
            cursor = self.conn.execute(
                """SELECT query, result_count, searched_at
                   FROM search_log
                   WHERE searched_at >= datetime('now', ?) AND profile_id = ?
                   ORDER BY searched_at DESC
                   LIMIT ?""",
                (f"-{days} days", profile_id, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    # --- Word filters (global — not per-profile) ---

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

    def set_channel_category(self, name_or_handle: str, category: Optional[str],
                             profile_id: str = "default") -> bool:
        """Set a channel's category for a profile."""
        with self._lock:
            cursor = self.conn.execute(
                "UPDATE channels SET category = ? WHERE "
                "(channel_name = ? COLLATE NOCASE OR handle = ? COLLATE NOCASE) AND profile_id = ?",
                (category, name_or_handle, name_or_handle, profile_id),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def set_video_category(self, video_id: str, category: Optional[str],
                           profile_id: str = "default") -> bool:
        """Set a video's category for a profile."""
        with self._lock:
            cursor = self.conn.execute(
                "UPDATE videos SET category = ? WHERE video_id = ? AND profile_id = ?",
                (category, video_id, profile_id),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def set_channel_videos_category(self, channel_name: str, category: str,
                                     channel_id: str = "",
                                     profile_id: str = "default") -> int:
        """Update category on all videos belonging to a channel for a profile."""
        with self._lock:
            if channel_id:
                cursor = self.conn.execute(
                    "UPDATE videos SET category = ? WHERE channel_id = ? AND profile_id = ?",
                    (category, channel_id, profile_id),
                )
                self.conn.execute(
                    "UPDATE videos SET category = ? WHERE channel_name = ? COLLATE NOCASE "
                    "AND (channel_id IS NULL OR channel_id = '') AND profile_id = ?",
                    (category, channel_name, profile_id),
                )
            else:
                cursor = self.conn.execute(
                    "UPDATE videos SET category = ? WHERE channel_name = ? COLLATE NOCASE AND profile_id = ?",
                    (category, channel_name, profile_id),
                )
            self.conn.commit()
            return cursor.rowcount

    def get_channel_category(self, channel_name: str, profile_id: str = "default") -> Optional[str]:
        """Get a channel's assigned category for a profile."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT category FROM channels WHERE channel_name = ? COLLATE NOCASE AND profile_id = ?",
                (channel_name, profile_id),
            )
            row = cursor.fetchone()
            return row[0] if row and row[0] else None

    def get_daily_watch_by_category(self, date_str: str, utc_bounds: tuple[str, str] | None = None,
                                     profile_id: str = "default") -> dict:
        """Sum watch time per category for a date and profile."""
        start, end = utc_bounds if utc_bounds else (date_str, date_str)
        end_clause = "?" if utc_bounds else "date(?, '+1 day')"
        with self._lock:
            cursor = self.conn.execute(
                "SELECT COALESCE(v.category, c.category) as cat, "
                "       COALESCE(SUM(w.duration), 0) as total_sec "
                "FROM watch_log w "
                "LEFT JOIN videos v ON w.video_id = v.video_id AND v.profile_id = ? "
                "LEFT JOIN channels c ON v.channel_id IS NOT NULL AND v.channel_id != '' "
                "  AND v.channel_id = c.channel_id AND c.profile_id = ? "
                f"WHERE w.watched_at >= ? AND w.watched_at < {end_clause} "
                "AND w.profile_id = ? "
                "GROUP BY cat",
                (profile_id, profile_id, start, end, profile_id),
            )
            return {row[0]: row[1] / 60.0 for row in cursor.fetchall()}

    # --- Watch time tracking ---

    def record_watch_seconds(self, video_id: str, seconds: int,
                             profile_id: str = "default") -> None:
        """Log playback seconds from heartbeat for a profile."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO watch_log (video_id, duration, profile_id) VALUES (?, ?, ?)",
                (video_id, seconds, profile_id),
            )
            self.conn.commit()

    def get_video_watch_minutes(self, video_id: str, profile_id: str = "default") -> float:
        """Get cumulative watch time for a video within a profile."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT COALESCE(SUM(duration), 0) FROM watch_log WHERE video_id = ? AND profile_id = ?",
                (video_id, profile_id),
            )
            return cursor.fetchone()[0] / 60.0

    def get_batch_watch_minutes(self, video_ids: list[str],
                                profile_id: str = "default") -> dict[str, float]:
        """Get cumulative watch time for multiple videos in a profile."""
        if not video_ids:
            return {}
        with self._lock:
            placeholders = ",".join("?" for _ in video_ids)
            cursor = self.conn.execute(
                f"SELECT video_id, COALESCE(SUM(duration), 0) "
                f"FROM watch_log WHERE video_id IN ({placeholders}) AND profile_id = ? GROUP BY video_id",
                video_ids + [profile_id],
            )
            result = {row[0]: row[1] / 60.0 for row in cursor.fetchall()}
            for vid in video_ids:
                if vid not in result:
                    result[vid] = 0.0
            return result

    def get_daily_watch_minutes(self, date_str: str, utc_bounds: tuple[str, str] | None = None,
                                profile_id: str = "default") -> float:
        """Sum watch time for a date and profile."""
        start, end = utc_bounds if utc_bounds else (date_str, date_str)
        end_clause = "?" if utc_bounds else "date(?, '+1 day')"
        with self._lock:
            cursor = self.conn.execute(
                "SELECT COALESCE(SUM(duration), 0) FROM watch_log "
                f"WHERE watched_at >= ? AND watched_at < {end_clause} AND profile_id = ?",
                (start, end, profile_id),
            )
            total_seconds = cursor.fetchone()[0]
            return total_seconds / 60.0

    def get_daily_watch_breakdown(self, date_str: str, utc_bounds: tuple[str, str] | None = None,
                                  profile_id: str = "default") -> list[dict]:
        """Per-video watch time for a date and profile."""
        start, end = utc_bounds if utc_bounds else (date_str, date_str)
        end_clause = "?" if utc_bounds else "date(?, '+1 day')"
        with self._lock:
            cursor = self.conn.execute(
                "SELECT w.video_id, COALESCE(SUM(w.duration), 0) as total_sec,"
                "       v.title, v.channel_name, v.thumbnail_url,"
                "       v.duration, v.channel_id,"
                "       COALESCE(v.category, c.category) as category "
                "FROM watch_log w "
                "LEFT JOIN videos v ON w.video_id = v.video_id AND v.profile_id = ? "
                "LEFT JOIN channels c ON v.channel_id IS NOT NULL AND v.channel_id != '' "
                "  AND v.channel_id = c.channel_id AND c.profile_id = ? "
                f"WHERE w.watched_at >= ? AND w.watched_at < {end_clause} "
                "AND w.profile_id = ? "
                "GROUP BY w.video_id ORDER BY total_sec DESC",
                (profile_id, profile_id, start, end, profile_id),
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
                    handle: Optional[str] = None, category: Optional[str] = None,
                    profile_id: str = "default") -> bool:
        """Add or update a channel for a profile."""
        with self._lock:
            self.conn.execute(
                """INSERT INTO channels (channel_name, status, channel_id, handle, category, profile_id)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(channel_name, profile_id) DO UPDATE SET status = ?,
                   channel_id = COALESCE(?, channel_id),
                   handle = COALESCE(?, handle),
                   category = COALESCE(?, category),
                   added_at = datetime('now')""",
                (name, status, channel_id, handle, category, profile_id,
                 status, channel_id, handle, category),
            )
            self.conn.commit()
            return True

    def remove_channel(self, name_or_handle: str, profile_id: str = "default") -> bool:
        """Remove a channel from a profile's list."""
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM channels WHERE "
                "(channel_name = ? COLLATE NOCASE OR handle = ? COLLATE NOCASE) AND profile_id = ?",
                (name_or_handle, name_or_handle, profile_id),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def delete_channel_videos(self, channel_name: str, channel_id: str = "",
                              profile_id: str = "default") -> int:
        """Delete all videos belonging to a channel for a profile."""
        with self._lock:
            if channel_id:
                cursor = self.conn.execute(
                    "DELETE FROM videos WHERE channel_id = ? AND profile_id = ?",
                    (channel_id, profile_id),
                )
                extra = self.conn.execute(
                    "DELETE FROM videos WHERE channel_name = ? COLLATE NOCASE "
                    "AND (channel_id IS NULL OR channel_id = '') AND profile_id = ?",
                    (channel_name, profile_id),
                )
                total = cursor.rowcount + extra.rowcount
            else:
                cursor = self.conn.execute(
                    "DELETE FROM videos WHERE channel_name = ? COLLATE NOCASE AND profile_id = ?",
                    (channel_name, profile_id),
                )
                total = cursor.rowcount
            self.conn.commit()
            return total

    def resolve_channel_name(self, name_or_handle: str, profile_id: str = "default") -> Optional[str]:
        """Look up channel_name by name or @handle for a profile."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT channel_name FROM channels WHERE "
                "(channel_name = ? COLLATE NOCASE OR handle = ? COLLATE NOCASE) AND profile_id = ?",
                (name_or_handle, name_or_handle, profile_id),
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def get_channels_missing_handles(self, profile_id: str = "default") -> list[tuple[str, str]]:
        """Get (channel_name, channel_id) for channels with a channel_id but no handle."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT channel_name, channel_id FROM channels "
                "WHERE channel_id IS NOT NULL AND (handle IS NULL OR handle = '') AND profile_id = ?",
                (profile_id,),
            )
            return [(row[0], row[1]) for row in cursor.fetchall()]

    def get_channels_missing_ids(self, profile_id: str = "default") -> list[tuple[str, Optional[str]]]:
        """Get (channel_name, handle) for channels missing channel_id."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT channel_name, handle FROM channels "
                "WHERE (channel_id IS NULL OR channel_id = '') AND profile_id = ?",
                (profile_id,),
            )
            return [(row[0], row[1]) for row in cursor.fetchall()]

    def get_videos_missing_channel_id(self, limit: int = 50, profile_id: str = "default") -> list[dict]:
        """Get approved videos missing channel_id for a profile."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT video_id, channel_name FROM videos "
                "WHERE (channel_id IS NULL OR channel_id = '') AND profile_id = ? "
                "ORDER BY requested_at DESC LIMIT ?",
                (profile_id, limit),
            )
            return [{"video_id": row[0], "channel_name": row[1]} for row in cursor.fetchall()]

    def update_channel_id(self, channel_name: str, channel_id: str,
                          profile_id: str = "default") -> bool:
        """Set a channel's channel_id by name for a profile."""
        with self._lock:
            cursor = self.conn.execute(
                "UPDATE channels SET channel_id = ? WHERE channel_name = ? COLLATE NOCASE "
                "AND (channel_id IS NULL OR channel_id = '') AND profile_id = ?",
                (channel_id, channel_name, profile_id),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def update_video_channel_id(self, video_id: str, channel_id: str,
                                profile_id: str = "default") -> bool:
        """Set a video's channel_id for a profile."""
        with self._lock:
            cursor = self.conn.execute(
                "UPDATE videos SET channel_id = ? WHERE video_id = ? "
                "AND (channel_id IS NULL OR channel_id = '') AND profile_id = ?",
                (channel_id, video_id, profile_id),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def update_channel_handle(self, channel_name: str, handle: str,
                              profile_id: str = "default") -> bool:
        """Set a channel's handle by name for a profile."""
        with self._lock:
            cursor = self.conn.execute(
                "UPDATE channels SET handle = ? WHERE channel_name = ? COLLATE NOCASE AND profile_id = ?",
                (handle, channel_name, profile_id),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def get_channels(self, status: str, profile_id: str = "default") -> list[str]:
        """List channel names by status for a profile."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT channel_name FROM channels WHERE status = ? AND profile_id = ? ORDER BY channel_name",
                (status, profile_id),
            )
            return [row[0] for row in cursor.fetchall()]

    def get_channels_with_ids(self, status: str,
                              profile_id: str = "default") -> list[tuple[str, Optional[str], Optional[str], Optional[str]]]:
        """List (channel_name, channel_id, handle, category) tuples by status for a profile."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT channel_name, channel_id, handle, category FROM channels "
                "WHERE status = ? AND profile_id = ? ORDER BY channel_name",
                (status, profile_id),
            )
            return [(row[0], row[1], row[2], row[3]) for row in cursor.fetchall()]

    def is_channel_allowed(self, name: str, channel_id: str = "",
                           profile_id: str = "default") -> bool:
        """Check if channel is on the allowlist for a profile."""
        with self._lock:
            if channel_id:
                cursor = self.conn.execute(
                    "SELECT 1 FROM channels WHERE channel_id = ? AND status = 'allowed' AND profile_id = ?",
                    (channel_id, profile_id),
                )
                if cursor.fetchone() is not None:
                    return True
            cursor = self.conn.execute(
                "SELECT 1 FROM channels WHERE channel_name = ? COLLATE NOCASE AND status = 'allowed' AND profile_id = ?",
                (name, profile_id),
            )
            return cursor.fetchone() is not None

    def is_channel_blocked(self, name: str, channel_id: str = "",
                           profile_id: str = "default") -> bool:
        """Check if channel is on the blocklist for a profile."""
        with self._lock:
            if channel_id:
                cursor = self.conn.execute(
                    "SELECT 1 FROM channels WHERE channel_id = ? AND status = 'blocked' AND profile_id = ?",
                    (channel_id, profile_id),
                )
                if cursor.fetchone() is not None:
                    return True
            cursor = self.conn.execute(
                "SELECT 1 FROM channels WHERE channel_name = ? COLLATE NOCASE AND status = 'blocked' AND profile_id = ?",
                (name, profile_id),
            )
            return cursor.fetchone() is not None

    def get_channel_handles_set(self, profile_id: str = "default") -> set[str]:
        """Get lowercased set of all channel handles for a profile."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT handle FROM channels WHERE handle IS NOT NULL AND handle != '' AND profile_id = ?",
                (profile_id,),
            )
            return {row[0].lower() for row in cursor.fetchall()}

    def get_blocked_channels_set(self, profile_id: str = "default") -> set[str]:
        """Get set of blocked channel names (lowercased) for a profile."""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT channel_name FROM channels WHERE status = 'blocked' AND profile_id = ?",
                (profile_id,),
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

    def get_recent_activity(self, days: int = 7, limit: int = 50,
                            profile_id: str = "default") -> list[dict]:
        """Get recent video requests for a profile."""
        with self._lock:
            cursor = self.conn.execute(
                """SELECT video_id, title, channel_name, status, requested_at, view_count
                   FROM videos
                   WHERE requested_at >= datetime('now', ?) AND profile_id = ?
                   ORDER BY requested_at DESC
                   LIMIT ?""",
                (f"-{days} days", profile_id, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    # --- Stats ---

    def get_stats(self, profile_id: str = "default") -> dict:
        """Get aggregate statistics for a profile."""
        with self._lock:
            cursor = self.conn.execute("""
                SELECT
                    COUNT(*) as total,
                    COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) as pending,
                    COALESCE(SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END), 0) as approved,
                    COALESCE(SUM(CASE WHEN status = 'denied' THEN 1 ELSE 0 END), 0) as denied,
                    COALESCE(SUM(view_count), 0) as total_views
                FROM videos WHERE profile_id = ?
            """, (profile_id,))
            row = cursor.fetchone()
            return dict(row) if row else {"total": 0, "pending": 0, "approved": 0, "denied": 0, "total_views": 0}

    def prune_old_data(self, watch_days: int = 180, search_days: int = 90) -> tuple[int, int]:
        """Delete watch_log and search_log entries older than N days (global)."""
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
