"""Tests for Telegram bot localization and onboarding flows."""

import asyncio

from bot.telegram_bot import BrainRotGuardBot
from config import AppConfig, Config
from data.video_store import VideoStore


class _DummyMessage:
    def __init__(self, text: str, chat_id: int = 1):
        self.text = text
        self.chat_id = chat_id
        self.replies: list[tuple[str, dict]] = []

    async def reply_text(self, text: str, **kwargs):
        self.replies.append((text, kwargs))


class _DummyUpdate:
    def __init__(self, text: str, chat_id: int = 1):
        self.effective_chat = type("Chat", (), {"id": chat_id})()
        self.message = _DummyMessage(text, chat_id=chat_id)
        self.effective_message = self.message


class _DummyQueryMessage:
    def __init__(self, chat_id: int = 1):
        self.chat_id = chat_id


class _DummyQuery:
    def __init__(self, chat_id: int = 1):
        self.message = _DummyQueryMessage(chat_id=chat_id)
        self.cleared = False
        self.edits: list[dict] = []
        self.answers: list[str] = []

    async def answer(self, text: str = ""):
        self.answers.append(text)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.cleared = reply_markup is None

    async def edit_message_caption(self, **kwargs):
        self.edits.append(kwargs)

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)


class _FailingMarkdownBot:
    def __init__(self):
        self.calls: list[dict] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("parse_mode"):
            raise RuntimeError("markdown failed")


def _make_bot(tmp_path, locale: str = "nb") -> tuple[BrainRotGuardBot, VideoStore]:
    store = VideoStore(str(tmp_path / "videos.db"))
    store.create_profile("default", "Default")
    bot = BrainRotGuardBot(
        bot_token="token",
        admin_chat_id="-100123456",
        video_store=store,
        config=Config(app=AppConfig(locale=locale)),
    )
    return bot, store


def test_setup_hub_uses_configured_locale(tmp_path):
    bot, store = _make_bot(tmp_path)
    try:
        text, _ = bot._build_setup_hub(1)
        assert "Barn" in text
        assert "Tidsgrenser" in text
        assert "Kanaler" in text
    finally:
        store.close()


def test_time_setup_mode_uses_configured_locale(tmp_path):
    bot, store = _make_bot(tmp_path)
    try:
        text, keyboard = bot._render_setup_mode()
        assert "Oppsett av tidsgrenser" in text
        assert "Hvordan vil du styre skjermtid?" in text
        assert keyboard.inline_keyboard[0][0].text == "Enkel grense"
        assert keyboard.inline_keyboard[0][1].text == "Kategorigrenser"
    finally:
        store.close()


def test_time_status_uses_configured_locale(tmp_path):
    bot, store = _make_bot(tmp_path)
    try:
        store.set_setting("daily_limit_minutes", "120")
        update = _DummyUpdate("/time", chat_id=1)

        asyncio.run(bot._time_show_status(update))

        assert update.message.replies
        text, _ = update.message.replies[0]
        assert "I dag" in text
        assert "ÅPEN" in text
        assert "Grense: 120 min" in text
        assert "Uke" in text
        assert "Alle dager: samme tidsplan" in text
        assert "Man åpen" in text
    finally:
        store.close()


def test_onboard_child_name_reply_creates_profile(tmp_path):
    bot, store = _make_bot(tmp_path)
    try:
        state = {"step": "onboard_child_name:add", "hub_message_id": 99}
        bot._pending_wizard[1] = state
        update = _DummyUpdate("Ola", chat_id=1)

        handled = asyncio.run(bot._handle_onboard_reply(update, state))

        assert handled is True
        assert store.get_profile("ola")["display_name"] == "Ola"
        assert update.message.replies
        assert "PIN" in update.message.replies[0][0]
    finally:
        store.close()


def test_onboard_child_name_prompt_avoids_markdown_parse_mode(tmp_path):
    bot, store = _make_bot(tmp_path)
    try:
        state = {"step": "onboard_child_name:add", "hub_message_id": 99}
        bot._pending_wizard[1] = state
        update = _DummyUpdate("Ola (test)", chat_id=1)

        handled = asyncio.run(bot._handle_onboard_reply(update, state))

        assert handled is True
        assert update.message.replies
        _, kwargs = update.message.replies[0]
        assert "parse_mode" not in kwargs
    finally:
        store.close()


def test_request_notification_falls_back_to_plain_text(tmp_path):
    bot, store = _make_bot(tmp_path)
    try:
        dummy_bot = _FailingMarkdownBot()
        bot._app = type("DummyApp", (), {"bot": dummy_bot})()

        video = {
            "video_id": "dQw4w9WgXcQ",
            "title": "Title with [brackets]",
            "channel_name": "Channel Name",
            "duration": 42,
            "thumbnail_url": None,
            "channel_id": None,
            "is_short": False,
        }

        asyncio.run(bot.notify_new_request(video))

        assert len(dummy_bot.calls) == 2
        assert dummy_bot.calls[0]["parse_mode"] == "MarkdownV2"
        assert "parse_mode" not in dummy_bot.calls[1]
        assert "Channel Name" in dummy_bot.calls[1]["text"]
    finally:
        store.close()


def test_switch_confirm_keep_clears_inline_buttons(tmp_path):
    bot, store = _make_bot(tmp_path)
    try:
        query = _DummyQuery()

        asyncio.run(bot._cb_switch_confirm(query, "keep"))

        assert query.cleared is True
        assert query.edits
        assert "Beholder gjeldende innstillinger" in query.edits[0]["text"]
        assert query.edits[0]["reply_markup"] is None
    finally:
        store.close()


def test_revoke_toast_uses_dedicated_localized_key(tmp_path):
    bot, store = _make_bot(tmp_path)
    try:
        store.add_video("dQw4w9WgXcQ", "Test Video", "Test Channel", profile_id="default")
        store.update_status("dQw4w9WgXcQ", "approved", profile_id="default")
        query = _DummyQuery()

        async def _run():
            await bot._cb_video_action(query, "revoke", "default", "dQw4w9WgXcQ")
            await asyncio.sleep(0)

        asyncio.run(_run())

        assert query.answers == ["Fjernet!"]
    finally:
        store.close()
