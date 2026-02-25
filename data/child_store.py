"""
Per-child scoped view over VideoStore.
Curries profile_id into all child-scoped operations.
"""

from typing import Optional


class ChildStore:
    """Wraps VideoStore with a fixed profile_id for per-child isolation.

    Settings are prefixed with `{profile_id}:` in the settings table.
    The `default` profile falls back to unprefixed keys for backwards compat.
    """

    def __init__(self, store, profile_id: str):
        self._store = store
        self.profile_id = profile_id

    # --- Settings (prefixed by profile_id) ---

    def get_setting(self, key: str, default: str = "") -> str:
        """Read a setting, prefixed by profile_id. Default profile falls back to unprefixed."""
        prefixed = self._store.get_setting(f"{self.profile_id}:{key}", "")
        if prefixed:
            return prefixed
        # Backwards compat: default profile tries bare key
        if self.profile_id == "default":
            return self._store.get_setting(key, default)
        return default

    def set_setting(self, key: str, value: str) -> None:
        """Write a setting, prefixed by profile_id."""
        self._store.set_setting(f"{self.profile_id}:{key}", value)

    # --- Delegated methods (profile_id curried) ---

    def add_video(self, video_id, title, channel_name, **kw):
        return self._store.add_video(video_id, title, channel_name, profile_id=self.profile_id, **kw)

    def get_video(self, video_id):
        return self._store.get_video(video_id, profile_id=self.profile_id)

    def find_video_fuzzy(self, encoded_id):
        return self._store.find_video_fuzzy(encoded_id, profile_id=self.profile_id)

    def get_by_status(self, status, **kw):
        return self._store.get_by_status(status, profile_id=self.profile_id, **kw)

    def get_denied_video_ids(self):
        return self._store.get_denied_video_ids(profile_id=self.profile_id)

    def get_approved(self):
        return self._store.get_approved(profile_id=self.profile_id)

    def get_pending(self):
        return self._store.get_pending(profile_id=self.profile_id)

    def get_approved_page(self, page=0, page_size=24):
        return self._store.get_approved_page(page, page_size, profile_id=self.profile_id)

    def get_approved_shorts(self, limit=50):
        return self._store.get_approved_shorts(limit, profile_id=self.profile_id)

    def search_approved(self, query, limit=50):
        return self._store.search_approved(query, limit, profile_id=self.profile_id)

    def get_recent_requests(self, limit=50):
        return self._store.get_recent_requests(limit, profile_id=self.profile_id)

    def update_status(self, video_id, status):
        return self._store.update_status(video_id, status, profile_id=self.profile_id)

    def record_view(self, video_id):
        return self._store.record_view(video_id, profile_id=self.profile_id)

    def record_search(self, query, result_count):
        return self._store.record_search(query, result_count, profile_id=self.profile_id)

    def get_recent_searches(self, days=7, limit=50):
        return self._store.get_recent_searches(days, limit, profile_id=self.profile_id)

    def record_watch_seconds(self, video_id, seconds):
        return self._store.record_watch_seconds(video_id, seconds, profile_id=self.profile_id)

    def get_video_watch_minutes(self, video_id):
        return self._store.get_video_watch_minutes(video_id, profile_id=self.profile_id)

    def get_batch_watch_minutes(self, video_ids):
        return self._store.get_batch_watch_minutes(video_ids, profile_id=self.profile_id)

    def get_daily_watch_minutes(self, date_str, utc_bounds=None):
        return self._store.get_daily_watch_minutes(date_str, utc_bounds, profile_id=self.profile_id)

    def get_daily_watch_breakdown(self, date_str, utc_bounds=None):
        return self._store.get_daily_watch_breakdown(date_str, utc_bounds, profile_id=self.profile_id)

    def get_daily_watch_by_category(self, date_str, utc_bounds=None):
        return self._store.get_daily_watch_by_category(date_str, utc_bounds, profile_id=self.profile_id)

    def set_channel_category(self, name_or_handle, category):
        return self._store.set_channel_category(name_or_handle, category, profile_id=self.profile_id)

    def set_video_category(self, video_id, category):
        return self._store.set_video_category(video_id, category, profile_id=self.profile_id)

    def set_channel_videos_category(self, channel_name, category, channel_id=""):
        return self._store.set_channel_videos_category(channel_name, category, channel_id, profile_id=self.profile_id)

    def get_channel_category(self, channel_name):
        return self._store.get_channel_category(channel_name, profile_id=self.profile_id)

    def add_channel(self, name, status, **kw):
        return self._store.add_channel(name, status, profile_id=self.profile_id, **kw)

    def remove_channel(self, name_or_handle):
        return self._store.remove_channel(name_or_handle, profile_id=self.profile_id)

    def delete_channel_videos(self, channel_name, channel_id=""):
        return self._store.delete_channel_videos(channel_name, channel_id, profile_id=self.profile_id)

    def resolve_channel_name(self, name_or_handle):
        return self._store.resolve_channel_name(name_or_handle, profile_id=self.profile_id)

    def get_channels_missing_handles(self):
        return self._store.get_channels_missing_handles(profile_id=self.profile_id)

    def get_channels_missing_ids(self):
        return self._store.get_channels_missing_ids(profile_id=self.profile_id)

    def get_videos_missing_channel_id(self, limit=50):
        return self._store.get_videos_missing_channel_id(limit, profile_id=self.profile_id)

    def update_channel_id(self, channel_name, channel_id):
        return self._store.update_channel_id(channel_name, channel_id, profile_id=self.profile_id)

    def update_video_channel_id(self, video_id, channel_id):
        return self._store.update_video_channel_id(video_id, channel_id, profile_id=self.profile_id)

    def update_channel_handle(self, channel_name, handle):
        return self._store.update_channel_handle(channel_name, handle, profile_id=self.profile_id)

    def get_channels(self, status):
        return self._store.get_channels(status, profile_id=self.profile_id)

    def get_channels_with_ids(self, status):
        return self._store.get_channels_with_ids(status, profile_id=self.profile_id)

    def is_channel_allowed(self, name, channel_id=""):
        return self._store.is_channel_allowed(name, channel_id, profile_id=self.profile_id)

    def is_channel_blocked(self, name, channel_id=""):
        return self._store.is_channel_blocked(name, channel_id, profile_id=self.profile_id)

    def get_channel_handles_set(self):
        return self._store.get_channel_handles_set(profile_id=self.profile_id)

    def get_blocked_channels_set(self):
        return self._store.get_blocked_channels_set(profile_id=self.profile_id)

    def get_recent_activity(self, days=7, limit=50):
        return self._store.get_recent_activity(days, limit, profile_id=self.profile_id)

    def get_stats(self):
        return self._store.get_stats(profile_id=self.profile_id)

    # --- Pass-through for global operations ---

    def __getattr__(self, name):
        """Delegate unknown attributes to the underlying store (global ops)."""
        return getattr(self._store, name)
