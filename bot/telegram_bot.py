"""BrainRotGuard Telegram Bot - parent approval for YouTube videos."""

import asyncio
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    CallbackQueryHandler, ContextTypes,
    MessageHandler, filters,
)

from bot.helpers import (
    _md, _answer_bg, _edit_msg,
    MD2, _GITHUB_REPO, _UPDATE_CHECK_INTERVAL,
)
from bot.callback_router import CallbackRoute, match_route
from bot.activity import ActivityMixin
from bot.approval import ApprovalMixin
from bot.channels import ChannelMixin
from bot.commands import CommandsMixin
from bot.timelimits import TimeLimitMixin
from data.child_store import ChildStore

logger = logging.getLogger(__name__)


class BrainRotGuardBot(ApprovalMixin, ChannelMixin, TimeLimitMixin, CommandsMixin, ActivityMixin):
    """Telegram bot for parent video approval."""

    def __init__(self, bot_token: str, admin_chat_id: str, video_store, config=None,
                 starter_channels_path: Optional[Path] = None):
        self.bot_token = bot_token
        self.admin_chat_id = admin_chat_id
        self.video_store = video_store
        self.config = config
        self._app = None
        self._limit_notified_cats: dict[tuple, str] = {}  # (profile_id, category) -> date
        self._pending_wizard: dict[int, dict] = {}  # chat_id -> wizard state for custom input
        self._pending_cmd: dict[int, dict] = {}  # chat_id -> pending child-scoped command
        self.on_channel_change = None  # callback when channel lists change
        self.on_video_change = None  # callback when video status changes
        self._update_check_task = None  # background version check loop
        # Load starter channels
        from data.starter_channels import load_starter_channels
        self._starter_channels = load_starter_channels(starter_channels_path)

    def _child_store(self, profile_id: str) -> ChildStore:
        """Get a ChildStore for a specific profile."""
        return ChildStore(self.video_store, profile_id)

    def _get_profiles(self) -> list[dict]:
        """Get all profiles."""
        return self.video_store.get_profiles()

    def _single_profile(self) -> Optional[dict]:
        """If there's only one profile, return it. Otherwise None."""
        profiles = self._get_profiles()
        return profiles[0] if len(profiles) == 1 else None

    async def _with_child_context(self, update: Update, context, handler_fn,
                                   allow_all: bool = False) -> None:
        """Route a child-scoped command through profile selection.

        If only one profile, execute directly. Otherwise show selector buttons.
        handler_fn signature: handler_fn(update, context, child_store, profile)
        """
        profiles = self._get_profiles()
        if len(profiles) == 1:
            cs = self._child_store(profiles[0]["id"])
            await handler_fn(update, context, cs, profiles[0])
            return
        if not profiles:
            await update.effective_message.reply_text("No profiles. Use /child add <name> to create one.")
            return

        # Store pending command for callback
        chat_id = update.effective_chat.id
        self._pending_cmd[chat_id] = {"handler": handler_fn, "context": context}

        # Build child selector keyboard
        buttons = []
        row = []
        for p in profiles:
            row.append(InlineKeyboardButton(p["display_name"], callback_data=f"child_sel:{p['id']}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        if allow_all:
            buttons.append([InlineKeyboardButton("All Children", callback_data="child_sel:__all__")])
        keyboard = InlineKeyboardMarkup(buttons)
        await update.effective_message.reply_text("Which child?", reply_markup=keyboard)

    def _check_admin(self, update: Update) -> bool:
        """Check if interaction is from an authorized admin context.

        Matches when:
        - DM from admin user (effective_user.id == admin_chat_id)
        - Message/callback in admin group chat (effective_chat.id == admin_chat_id)
        """
        if not self.admin_chat_id:
            return False
        admin = str(self.admin_chat_id)
        return (str(update.effective_chat.id) == admin
                or str(update.effective_user.id) == admin)

    async def _require_admin(self, update: Update) -> bool:
        """Check admin access; send denial if unauthorized. Returns True if authorized."""
        if self._check_admin(update):
            return True
        msg = "This bot is for the parent/admin only."
        if update.callback_query:
            await update.callback_query.answer(msg)
        elif update.message:
            await update.effective_message.reply_text(msg)
        return False

    def _resolve_channel_bg(self, channel_name: str, channel_id: Optional[str] = None,
                             video_id: Optional[str] = None, profile_id: str = "default") -> None:
        """Fire a background task to resolve and store missing channel identifiers.

        Resolves channel_id (via video metadata or @name lookup) and @handle
        (via channel_id) for the channel row. Also backfills channel_id on the
        video row if provided.
        """
        import asyncio
        cs = self._child_store(profile_id)
        async def _resolve():
            try:
                cid = channel_id
                if not cid:
                    if video_id:
                        from youtube.extractor import extract_metadata
                        metadata = await extract_metadata(video_id)
                        if metadata and metadata.get("channel_id"):
                            cid = metadata["channel_id"]
                            cs.update_video_channel_id(video_id, cid)
                    if not cid:
                        from youtube.extractor import resolve_channel_handle
                        info = await resolve_channel_handle(f"@{channel_name}")
                        if info and info.get("channel_id"):
                            cid = info["channel_id"]
                            if info.get("handle"):
                                cs.update_channel_handle(channel_name, info["handle"])
                    if cid:
                        cs.update_channel_id(channel_name, cid)
                        logger.info(f"Resolved channel_id: {channel_name} → {cid}")
                if cid:
                    from youtube.extractor import resolve_handle_from_channel_id
                    handle = await resolve_handle_from_channel_id(cid)
                    if handle:
                        cs.update_channel_handle(channel_name, handle)
                        logger.info(f"Resolved handle: {channel_name} → {handle}")
            except Exception as e:
                logger.debug(f"Background channel resolve failed for {channel_name}: {e}")
        asyncio.create_task(_resolve())

    async def start(self) -> None:
        """Start the bot."""
        logger.info("Starting BrainRotGuard bot...")
        from telegram.request import HTTPXRequest
        request = HTTPXRequest(
            connect_timeout=10.0, read_timeout=15.0, write_timeout=15.0,
            connection_pool_size=10, pool_timeout=5.0,
        )
        self._app = ApplicationBuilder().token(self.bot_token).request(request).build()

        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("pending", self._cmd_pending))
        self._app.add_handler(CommandHandler("approved", self._cmd_approved))
        self._app.add_handler(CommandHandler("stats", self._cmd_stats))
        self._app.add_handler(CommandHandler("logs", self._cmd_logs))
        self._app.add_handler(CommandHandler("channel", self._cmd_channel))
        self._app.add_handler(CommandHandler("search", self._cmd_search))
        self._app.add_handler(CommandHandler("filter", self._cmd_filter))
        self._app.add_handler(CommandHandler("watch", self._cmd_watch))
        self._app.add_handler(CommandHandler("time", self._cmd_timelimit))
        self._app.add_handler(CommandHandler("changelog", self._cmd_changelog))
        self._app.add_handler(CommandHandler("shorts", self._cmd_shorts))
        self._app.add_handler(CommandHandler("child", self._cmd_child))
        self._app.add_handler(MessageHandler(
            filters.Regex(r'^/revoke_[a-zA-Z0-9_]{11}$'), self._cmd_revoke,
        ))
        self._app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, self._handle_wizard_reply,
        ))
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("BrainRotGuard bot started")

        # First-run: send welcome with starter prompt if channel list is empty
        if self._starter_channels and not self.video_store.get_channel_handles_set():
            try:
                text, markup = self._build_welcome_message()
                await self._app.bot.send_message(
                    chat_id=self.admin_chat_id,
                    text=text,
                    reply_markup=markup,
                    parse_mode=MD2,
                )
                logger.info("Sent welcome message to admin (first run)")
            except Exception as e:
                logger.error(f"Failed to send first-run message: {e}")

        self._update_check_task = asyncio.create_task(self._version_check_loop())

    async def stop(self) -> None:
        """Stop the bot."""
        if self._update_check_task:
            self._update_check_task.cancel()
        if self._app:
            logger.info("Stopping BrainRotGuard bot...")
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            logger.info("BrainRotGuard bot stopped")

    async def _version_check_loop(self) -> None:
        """Periodically check GitHub for new releases. Stops after notifying."""
        await asyncio.sleep(60)  # initial delay
        while True:
            try:
                if await self._check_for_updates():
                    return  # notified — stop checking
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug(f"Version check failed: {e}")
            await asyncio.sleep(_UPDATE_CHECK_INTERVAL)

    async def _check_for_updates(self) -> bool:
        """Fetch latest GitHub release and notify admin if newer. Returns True if notified."""
        from version import __version__

        # Already notified once — don't notify again
        if self.video_store.get_setting("last_notified_version"):
            return True

        url = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return False
                # Cap response size to prevent memory abuse
                raw = await resp.read()
                if len(raw) > 100_000:
                    return False
                import json as _json
                data = _json.loads(raw)

        tag = data.get("tag_name", "")
        latest = tag.lstrip("v")
        if not latest:
            return False

        def _ver(v: str) -> tuple:
            return tuple(int(x) for x in v.split("."))

        try:
            if _ver(latest) <= _ver(__version__):
                return False
        except (ValueError, TypeError):
            return False

        body = data.get("body", "") or ""
        if len(body) > 500:
            body = body[:500] + "..."
        html_url = data.get("html_url", "")
        if not html_url or urlparse(html_url).netloc != "github.com":
            return False

        text = (
            f"**BrainRotGuard v{latest} available** (you have v{__version__})\n\n"
            f"{body}\n\n"
            f"[View release]({html_url})"
        )
        try:
            await self._app.bot.send_message(
                chat_id=self.admin_chat_id,
                text=_md(text),
                parse_mode=MD2,
                disable_web_page_preview=True,
            )
            logger.info(f"Notified admin about v{latest}")
        except Exception as e:
            logger.error(f"Failed to send update notification: {e}")
            return False

        self.video_store.set_setting("last_notified_version", latest)
        return True

    # -- Callback route table ------------------------------------------------
    # Each route maps a callback_data prefix to a handler method.
    # See bot/callback_router.py for field semantics.

    _AB = frozenset({"allowed", "blocked"})  # constraint shorthand

    _CALLBACK_ROUTES: list[CallbackRoute] = [
        # Approval / child management
        CallbackRoute("child_sel",       "_cb_child_select",        min_parts=2, answer="", pass_update=True),
        CallbackRoute("child_del",       "_cb_child_delete_confirm", min_parts=2, answer=""),
        CallbackRoute("autoapprove",     "_cb_auto_approve",        min_parts=3, answer="Auto-approved!"),
        CallbackRoute("resend",          "_cb_resend",              min_parts=3, answer=None),

        # Pagination (int conversion on page/day indices)
        CallbackRoute("approved_page",   "_cb_approved_page",       min_parts=3, answer=None, int_parts=frozenset({2})),
        CallbackRoute("pending_page",    "_cb_pending_page",        min_parts=3, answer=None, int_parts=frozenset({2})),
        CallbackRoute("starter_page",    "_cb_starter_page",        min_parts=3, answer=None, int_parts=frozenset({2})),
        CallbackRoute("starter_import",  "_cb_starter_import",      min_parts=3, answer=None, int_parts=frozenset({2})),
        CallbackRoute("logs_page",       "_cb_logs_page",           min_parts=4, answer=None, int_parts=frozenset({2, 3})),
        CallbackRoute("search_page",     "_cb_search_page",         min_parts=4, answer=None, int_parts=frozenset({2, 3})),

        # Channel management
        CallbackRoute("chan_page",       "_cb_channel_page",        min_parts=4, answer=None,
                       constraints={2: _AB}, int_parts=frozenset({3})),
        CallbackRoute("chan_filter",     "_cb_channel_filter",      min_parts=3, answer=None,
                       constraints={2: _AB}),
        CallbackRoute("chan_menu",       "_cb_channel_menu",        min_parts=2, answer=None),
        CallbackRoute("starter_prompt",  "_cb_starter_prompt",      min_parts=2, answer=None),
        # unallow/unblock: channel names may contain colons → rejoin from index 2
        CallbackRoute("unallow",         "_cb_channel_remove",      min_parts=3, answer=None, rejoin_from=2),
        CallbackRoute("unblock",         "_cb_channel_remove",      min_parts=3, answer=None, rejoin_from=2),

        # Time limit wizard
        CallbackRoute("setup_top",          "_cb_setup_top",          min_parts=2, answer=""),
        CallbackRoute("setup_sched_start",  "_cb_setup_sched_start",  min_parts=2, answer="", rejoin_from=1),
        CallbackRoute("setup_sched_stop",   "_cb_setup_sched_stop",   min_parts=2, answer="", rejoin_from=1),
        CallbackRoute("setup_sched_day",    "_cb_setup_sched_day",    min_parts=2, answer=""),
        CallbackRoute("setup_sched_apply",  "_cb_setup_sched_apply",  min_parts=2, answer=""),
        CallbackRoute("setup_sched_done",   "_cb_setup_sched_done",   min_parts=1, answer=""),
        CallbackRoute("setup_daystart",     "_cb_setup_daystart",     min_parts=3, answer="", rejoin_from=2),
        CallbackRoute("setup_daystop",      "_cb_setup_daystop",      min_parts=3, answer="", rejoin_from=2),
        CallbackRoute("setup_mode",         "_cb_setup_mode",         min_parts=2, answer=""),
        CallbackRoute("setup_simple",       "_cb_setup_simple",       min_parts=2, answer=""),
        CallbackRoute("setup_edu",          "_cb_setup_edu",          min_parts=2, answer=""),
        CallbackRoute("setup_fun",          "_cb_setup_fun",          min_parts=2, answer=""),
        CallbackRoute("switch_confirm",     "_cb_switch_confirm",     min_parts=2, answer="", rejoin_from=1),
    ]

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Dispatch inline button callbacks via the route table."""
        query = update.callback_query
        if not await self._require_admin(update):
            return

        data = query.data
        if data == "noop":
            await query.answer()
            return
        parts = data.split(":")

        result = match_route(self._CALLBACK_ROUTES, parts)
        if result is not None:
            route, args = result
            # Auto-answer the callback query
            if route.answer is not None:
                _answer_bg(query, route.answer)
            handler = getattr(self, route.handler)
            try:
                if route.pass_update:
                    await handler(query, update, context, *args)
                elif route.prefix in ("unallow", "unblock"):
                    # Channel remove needs the action prefix as first arg
                    await handler(query, route.prefix, *args)
                else:
                    await handler(query, *args)
            except (ValueError, IndexError):
                await query.answer("Invalid callback.")
            return

        # Fallthrough: video action callbacks (approve/deny/revoke/allowchan/blockchan/setcat)
        # Format: action:profile_id:video_id (3 parts) or legacy action:video_id (2 parts)
        _VIDEO_ACTIONS = {
            "approve", "approve_edu", "approve_fun", "deny", "revoke",
            "allowchan", "allowchan_edu", "allowchan_fun", "blockchan",
            "setcat_edu", "setcat_fun",
        }
        if len(parts) == 3:
            action, profile_id, video_id = parts
        elif len(parts) == 2:
            action, video_id = parts
            profile_id = "default"
        else:
            await query.answer("Invalid callback.")
            return

        if action not in _VIDEO_ACTIONS:
            await query.answer("Invalid callback.")
            return

        await self._cb_video_action(query, action, profile_id, video_id)

