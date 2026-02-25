"""Tests for data/child_store.py — profile-scoped delegation."""

import pytest

from data.child_store import ChildStore


class TestChildStoreSettings:
    def test_set_and_get_prefixed_setting(self, video_store):
        cs = ChildStore(video_store, "kid1")
        cs.set_setting("access_start", "08:00")
        # Stored as "kid1:access_start" in the underlying store
        assert video_store.get_setting("kid1:access_start") == "08:00"
        assert cs.get_setting("access_start") == "08:00"

    def test_default_profile_falls_back_to_unprefixed(self, video_store):
        """The 'default' profile tries prefixed first, then bare key."""
        video_store.set_setting("access_start", "09:00")  # Unprefixed
        cs = ChildStore(video_store, "default")
        assert cs.get_setting("access_start") == "09:00"

    def test_default_profile_prefixed_wins(self, video_store):
        """Prefixed key takes priority even for default profile."""
        video_store.set_setting("access_start", "09:00")
        video_store.set_setting("default:access_start", "10:00")
        cs = ChildStore(video_store, "default")
        assert cs.get_setting("access_start") == "10:00"

    def test_non_default_no_fallback(self, video_store):
        """Non-default profiles don't fall back to unprefixed keys."""
        video_store.set_setting("access_start", "09:00")
        cs = ChildStore(video_store, "kid1")
        assert cs.get_setting("access_start") == ""  # No fallback
        assert cs.get_setting("access_start", "default_val") == "default_val"

    def test_setting_isolation(self, video_store):
        """Different profiles have isolated settings."""
        cs1 = ChildStore(video_store, "kid1")
        cs2 = ChildStore(video_store, "kid2")
        cs1.set_setting("limit", "60")
        cs2.set_setting("limit", "120")
        assert cs1.get_setting("limit") == "60"
        assert cs2.get_setting("limit") == "120"


class TestChildStoreVideoDelegation:
    def test_add_and_get_video(self, video_store):
        cs = ChildStore(video_store, "kid1")
        v = cs.add_video("del_1234567", "Test", "Channel")
        assert v["profile_id"] == "kid1"
        fetched = cs.get_video("del_1234567")
        assert fetched is not None

    def test_video_isolation(self, video_store):
        cs1 = ChildStore(video_store, "kid1")
        cs2 = ChildStore(video_store, "kid2")
        cs1.add_video("iso_1234567", "Isolated", "Ch")
        assert cs1.get_video("iso_1234567") is not None
        assert cs2.get_video("iso_1234567") is None

    def test_update_status_delegation(self, video_store):
        cs = ChildStore(video_store, "kid1")
        cs.add_video("upd_1234567", "Update", "Ch")
        cs.update_status("upd_1234567", "approved")
        assert cs.get_video("upd_1234567")["status"] == "approved"

    def test_record_view_delegation(self, video_store):
        cs = ChildStore(video_store, "kid1")
        cs.add_video("vw__1234567", "View", "Ch")
        cs.record_view("vw__1234567")
        assert cs.get_video("vw__1234567")["view_count"] == 1


class TestChildStoreChannelDelegation:
    def test_add_and_get_channels(self, video_store):
        cs = ChildStore(video_store, "kid1")
        cs.add_channel("TestCh", "allowed")
        channels = cs.get_channels("allowed")
        assert "TestCh" in channels

    def test_channel_isolation(self, video_store):
        cs1 = ChildStore(video_store, "kid1")
        cs2 = ChildStore(video_store, "kid2")
        cs1.add_channel("OnlyKid1", "allowed")
        assert "OnlyKid1" in cs1.get_channels("allowed")
        assert "OnlyKid1" not in cs2.get_channels("allowed")

    def test_is_channel_allowed_delegation(self, video_store):
        cs = ChildStore(video_store, "kid1")
        cs.add_channel("AllowedCh", "allowed")
        assert cs.is_channel_allowed("AllowedCh") is True
        assert cs.is_channel_blocked("AllowedCh") is False


class TestChildStoreWatchTracking:
    def test_record_and_get_watch(self, video_store):
        cs = ChildStore(video_store, "kid1")
        cs.add_video("wt__1234567", "Watch", "Ch")
        cs.record_watch_seconds("wt__1234567", 300)
        assert cs.get_video_watch_minutes("wt__1234567") == 5.0


class TestChildStoreGetattr:
    def test_delegates_global_ops(self, video_store):
        """__getattr__ delegates unknown methods to underlying store."""
        cs = ChildStore(video_store, "kid1")
        # prune_old_data is a global op on VideoStore, not overridden in ChildStore
        result = cs.prune_old_data()
        assert result == (0, 0)

    def test_profile_id_attribute(self, video_store):
        cs = ChildStore(video_store, "kid1")
        assert cs.profile_id == "kid1"
