"""Tests for data/video_store.py — CRUD, channels, profiles, settings, watch tracking."""

import pytest

from data.video_store import VideoStore


class TestVideoStoreVideoCRUD:
    def test_add_and_get_video(self, video_store):
        v = video_store.add_video("dQw4w9WgXcQ", "Test Video", "Test Channel")
        assert v["video_id"] == "dQw4w9WgXcQ"
        assert v["title"] == "Test Video"
        assert v["status"] == "pending"

        fetched = video_store.get_video("dQw4w9WgXcQ")
        assert fetched["video_id"] == "dQw4w9WgXcQ"

    def test_add_duplicate_returns_existing(self, video_store):
        video_store.add_video("abc12345678", "Original", "Channel")
        v2 = video_store.add_video("abc12345678", "Different Title", "Channel")
        assert v2["title"] == "Original"  # INSERT OR IGNORE keeps original

    def test_get_nonexistent_returns_none(self, video_store):
        assert video_store.get_video("nonexistent1") is None

    def test_add_video_with_metadata(self, video_store):
        v = video_store.add_video(
            "meta1234567", "With Meta", "Ch",
            thumbnail_url="https://i.ytimg.com/vi/meta1234567/hq.jpg",
            duration=300,
            channel_id="UCtest123",
            is_short=True,
        )
        assert v["duration"] == 300
        assert v["channel_id"] == "UCtest123"
        assert v["is_short"] == 1

    def test_add_video_invalid_thumbnail_stripped(self, video_store):
        v = video_store.add_video(
            "thumb123456", "Thumb Test", "Ch",
            thumbnail_url="https://evil.com/thumb.jpg",
        )
        assert v["thumbnail_url"] is None  # Invalid host → stripped

    def test_update_status(self, video_store):
        video_store.add_video("stat1234567", "Status Test", "Ch")
        assert video_store.update_status("stat1234567", "approved") is True
        v = video_store.get_video("stat1234567")
        assert v["status"] == "approved"
        assert v["decided_at"] is not None

    def test_update_status_nonexistent(self, video_store):
        assert video_store.update_status("nonexistent1", "approved") is False

    def test_record_view(self, video_store):
        video_store.add_video("view1234567", "View Test", "Ch")
        video_store.record_view("view1234567")
        v = video_store.get_video("view1234567")
        assert v["view_count"] == 1
        video_store.record_view("view1234567")
        v = video_store.get_video("view1234567")
        assert v["view_count"] == 2

    def test_get_by_status(self, video_store):
        video_store.add_video("pend1234567", "Pending", "Ch")
        video_store.add_video("pend2345678", "Pending 2", "Ch")
        video_store.add_video("appr1234567", "Approved", "Ch")
        video_store.update_status("appr1234567", "approved")

        pending = video_store.get_by_status("pending")
        assert len(pending) == 2
        approved = video_store.get_by_status("approved")
        assert len(approved) == 1

    def test_get_approved_and_pending(self, video_store):
        video_store.add_video("a___1234567", "A", "Ch")
        video_store.add_video("b___1234567", "B", "Ch")
        video_store.update_status("a___1234567", "approved")

        assert len(video_store.get_approved()) == 1
        assert len(video_store.get_pending()) == 1

    def test_find_video_fuzzy(self, video_store):
        video_store.add_video("a-b_c-d-e-f", "Fuzzy", "Ch")
        # Hyphens encoded as underscores
        result = video_store.find_video_fuzzy("a_b_c_d_e_f")
        assert result is not None
        assert result["video_id"] == "a-b_c-d-e-f"

    def test_search_approved(self, video_store):
        video_store.add_video("srch1234567", "Dinosaur Adventures", "EduCh")
        video_store.update_status("srch1234567", "approved")
        video_store.add_video("srch2345678", "Cat Videos", "FunCh")
        video_store.update_status("srch2345678", "approved")

        results = video_store.search_approved("dinosaur")
        assert len(results) == 1
        assert results[0]["title"] == "Dinosaur Adventures"

    def test_get_denied_video_ids(self, video_store):
        video_store.add_video("deny1234567", "Denied", "Ch")
        video_store.update_status("deny1234567", "denied")
        denied = video_store.get_denied_video_ids()
        assert "deny1234567" in denied


class TestVideoStoreProfiles:
    def test_create_and_get_profiles(self, video_store):
        video_store.create_profile("kid1", "Kid One", pin="1111")
        profiles = video_store.get_profiles()
        assert len(profiles) == 1
        assert profiles[0]["id"] == "kid1"
        assert profiles[0]["display_name"] == "Kid One"

    def test_create_duplicate_returns_false(self, video_store):
        video_store.create_profile("dup", "First")
        assert video_store.create_profile("dup", "Second") is False

    def test_get_profile_by_id(self, video_store):
        video_store.create_profile("test", "Test")
        p = video_store.get_profile("test")
        assert p is not None
        assert p["display_name"] == "Test"

    def test_get_profile_nonexistent(self, video_store):
        assert video_store.get_profile("nope") is None

    def test_get_profile_by_pin(self, video_store):
        video_store.create_profile("pintest", "Pin Test", pin="9999")
        p = video_store.get_profile_by_pin("9999")
        assert p["id"] == "pintest"

    def test_get_profile_by_empty_pin(self, video_store):
        assert video_store.get_profile_by_pin("") is None

    def test_update_profile(self, video_store):
        video_store.create_profile("upd", "Original")
        video_store.update_profile("upd", display_name="Updated")
        p = video_store.get_profile("upd")
        assert p["display_name"] == "Updated"

    def test_update_profile_avatar(self, video_store):
        video_store.create_profile("ava", "Avatar")
        video_store.update_profile_avatar("ava", icon="🦊", color="#ff5733")
        p = video_store.get_profile("ava")
        assert p["avatar_icon"] == "🦊"
        assert p["avatar_color"] == "#ff5733"

    def test_delete_profile_cascades(self, video_store):
        video_store.create_profile("del", "Delete Me", pin="0000")
        video_store.add_video("delvid12345", "Del Video", "Ch", profile_id="del")
        video_store.add_channel("DelCh", "allowed", profile_id="del")
        video_store.set_setting("del:some_key", "value")

        assert video_store.delete_profile("del") is True
        assert video_store.get_profile("del") is None
        assert video_store.get_video("delvid12345", profile_id="del") is None
        assert video_store.get_channels("allowed", profile_id="del") == []

    def test_video_isolation_between_profiles(self, video_store):
        video_store.create_profile("p1", "Profile 1")
        video_store.create_profile("p2", "Profile 2")
        video_store.add_video("iso_1234567", "Shared ID", "Ch", profile_id="p1")
        video_store.add_video("iso_1234567", "Shared ID P2", "Ch", profile_id="p2")

        v1 = video_store.get_video("iso_1234567", profile_id="p1")
        v2 = video_store.get_video("iso_1234567", profile_id="p2")
        assert v1 is not None
        assert v2 is not None

        video_store.update_status("iso_1234567", "approved", profile_id="p1")
        assert video_store.get_video("iso_1234567", profile_id="p1")["status"] == "approved"
        assert video_store.get_video("iso_1234567", profile_id="p2")["status"] == "pending"

    def test_find_video_approved_for_others(self, video_store):
        video_store.create_profile("a", "A")
        video_store.create_profile("b", "B")
        video_store.add_video("cross123456", "Cross", "Ch", profile_id="a")
        video_store.update_status("cross123456", "approved", profile_id="a")

        result = video_store.find_video_approved_for_others("cross123456", "b")
        assert result is not None
        assert result["profile_id"] == "a"

        assert video_store.find_video_approved_for_others("cross123456", "a") is None


class TestVideoStoreChannels:
    def test_add_and_get_channels(self, video_store):
        video_store.add_channel("Science Channel", "allowed")
        channels = video_store.get_channels("allowed")
        assert "Science Channel" in channels

    def test_add_channel_with_metadata(self, video_store):
        video_store.add_channel("Edu Chan", "allowed", channel_id="UC123",
                                handle="@educhan", category="edu")
        with_ids = video_store.get_channels_with_ids("allowed")
        assert len(with_ids) == 1
        name, cid, handle, cat = with_ids[0]
        assert name == "Edu Chan"
        assert cid == "UC123"
        assert handle == "@educhan"
        assert cat == "edu"

    def test_remove_channel(self, video_store):
        video_store.add_channel("Remove Me", "allowed")
        assert video_store.remove_channel("Remove Me") is True
        assert video_store.get_channels("allowed") == []

    def test_remove_channel_by_handle(self, video_store):
        video_store.add_channel("HandleCh", "allowed", handle="@handlech")
        assert video_store.remove_channel("@handlech") is True

    def test_is_channel_allowed_blocked(self, video_store):
        video_store.add_channel("Good", "allowed")
        video_store.add_channel("Bad", "blocked")
        assert video_store.is_channel_allowed("Good") is True
        assert video_store.is_channel_blocked("Bad") is True
        assert video_store.is_channel_allowed("Bad") is False
        assert video_store.is_channel_blocked("Good") is False

    def test_is_channel_allowed_by_id(self, video_store):
        video_store.add_channel("IdCh", "allowed", channel_id="UCABC")
        assert video_store.is_channel_allowed("Unknown", channel_id="UCABC") is True

    def test_channel_category(self, video_store):
        video_store.add_channel("CatCh", "allowed", category="edu")
        assert video_store.get_channel_category("CatCh") == "edu"
        video_store.set_channel_category("CatCh", "fun")
        assert video_store.get_channel_category("CatCh") == "fun"

    def test_resolve_channel_name(self, video_store):
        video_store.add_channel("Resolved", "allowed", handle="@resolved")
        assert video_store.resolve_channel_name("@resolved") == "Resolved"
        assert video_store.resolve_channel_name("Resolved") == "Resolved"

    def test_channel_handle_set(self, video_store):
        video_store.add_channel("Ch1", "allowed", handle="@ch1")
        video_store.add_channel("Ch2", "allowed", handle="@ch2")
        handles = video_store.get_channel_handles_set()
        assert "@ch1" in handles
        assert "@ch2" in handles

    def test_blocked_channels_set(self, video_store):
        video_store.add_channel("BlockedA", "blocked")
        video_store.add_channel("AllowedB", "allowed")
        blocked = video_store.get_blocked_channels_set()
        assert "blockeda" in blocked
        assert "allowedb" not in blocked

    def test_channel_isolation_between_profiles(self, video_store):
        video_store.create_profile("cp1", "CP1")
        video_store.create_profile("cp2", "CP2")
        video_store.add_channel("SharedCh", "allowed", profile_id="cp1")
        video_store.add_channel("SharedCh", "blocked", profile_id="cp2")

        assert video_store.is_channel_allowed("SharedCh", profile_id="cp1") is True
        assert video_store.is_channel_blocked("SharedCh", profile_id="cp2") is True


class TestVideoStoreSettings:
    def test_set_and_get_setting(self, video_store):
        video_store.set_setting("test_key", "test_value")
        assert video_store.get_setting("test_key") == "test_value"

    def test_get_setting_default(self, video_store):
        assert video_store.get_setting("nonexistent", "fallback") == "fallback"

    def test_upsert_setting(self, video_store):
        video_store.set_setting("key", "v1")
        video_store.set_setting("key", "v2")
        assert video_store.get_setting("key") == "v2"


class TestVideoStoreWatchTracking:
    def test_record_and_get_watch_seconds(self, video_store):
        video_store.add_video("wtch1234567", "Watch", "Ch")
        video_store.record_watch_seconds("wtch1234567", 120)
        video_store.record_watch_seconds("wtch1234567", 60)
        minutes = video_store.get_video_watch_minutes("wtch1234567")
        assert minutes == 3.0  # 180 seconds = 3 minutes

    def test_get_daily_watch_minutes(self, video_store):
        from datetime import datetime, timezone
        video_store.add_video("day_1234567", "Daily", "Ch")
        video_store.record_watch_seconds("day_1234567", 600)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        minutes = video_store.get_daily_watch_minutes(today)
        assert minutes == 10.0

    def test_batch_watch_minutes(self, video_store):
        video_store.add_video("bat_1234567", "Batch1", "Ch")
        video_store.add_video("bat_2345678", "Batch2", "Ch")
        video_store.record_watch_seconds("bat_1234567", 120)
        video_store.record_watch_seconds("bat_2345678", 240)

        batch = video_store.get_batch_watch_minutes(["bat_1234567", "bat_2345678"])
        assert batch["bat_1234567"] == 2.0
        assert batch["bat_2345678"] == 4.0


class TestVideoStoreSearch:
    def test_record_and_get_searches(self, video_store):
        video_store.record_search("dinosaurs", 5)
        video_store.record_search("cats", 10)
        searches = video_store.get_recent_searches(days=7)
        assert len(searches) == 2
        assert searches[0]["query"] == "cats"  # Most recent first


class TestVideoStoreWordFilters:
    def test_add_and_get_filters(self, video_store):
        video_store.add_word_filter("badword")
        assert "badword" in video_store.get_word_filters()

    def test_add_duplicate_returns_false(self, video_store):
        video_store.add_word_filter("dup")
        assert video_store.add_word_filter("dup") is False

    def test_remove_filter(self, video_store):
        video_store.add_word_filter("remove_me")
        assert video_store.remove_word_filter("remove_me") is True
        assert "remove_me" not in video_store.get_word_filters()

    def test_get_word_filters_set(self, video_store):
        video_store.add_word_filter("Word")
        filters = video_store.get_word_filters_set()
        assert "word" in filters  # Lowercased


class TestVideoStoreStats:
    def test_get_stats(self, video_store):
        video_store.add_video("st__1234567", "S1", "Ch")
        video_store.add_video("st__2345678", "S2", "Ch")
        video_store.update_status("st__1234567", "approved")
        stats = video_store.get_stats()
        assert stats["total"] == 2
        assert stats["approved"] == 1
        assert stats["pending"] == 1


class TestVideoStorePrune:
    def test_prune_returns_counts(self, video_store):
        w, s = video_store.prune_old_data()
        assert w == 0 and s == 0


class TestVideoStoreClose:
    def test_close(self, tmp_path):
        store = VideoStore(db_path=str(tmp_path / "close_test.db"))
        store.close()  # Should not raise
