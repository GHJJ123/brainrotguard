"""BrainRotGuard Telegram Bot - parent approval for YouTube videos."""

import asyncio
import logging
import re
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
    _md, _answer_bg, _nav_row, _edit_msg, _channel_md_link,
    MD2, _GITHUB_REPO, _UPDATE_CHECK_INTERVAL,
)
from bot.approval import ApprovalMixin
from bot.channels import ChannelMixin
from bot.timelimits import TimeLimitMixin, _progress_bar
from data.child_store import ChildStore
from utils import get_today_str, get_day_utc_bounds, resolve_setting, CAT_LABELS
from youtube.extractor import format_duration

logger = logging.getLogger(__name__)


class BrainRotGuardBot(ApprovalMixin, ChannelMixin, TimeLimitMixin):
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

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline button callbacks for approve/deny/pagination."""
        query = update.callback_query
        if not await self._require_admin(update):
            return

        data = query.data
        if data == "noop":
            await query.answer()
            return
        parts = data.split(":")

        # Child selector callback
        if parts[0] == "child_sel" and len(parts) == 2:
            _answer_bg(query)
            await self._cb_child_select(query, update, context, parts[1])
            return

        # Child profile deletion confirmation
        if parts[0] == "child_del" and len(parts) == 2:
            _answer_bg(query)
            await self._cb_child_delete_confirm(query, parts[1])
            return

        # Cross-child auto-approve
        if parts[0] == "autoapprove" and len(parts) == 3:
            _answer_bg(query, "Auto-approved!")
            await self._cb_auto_approve(query, parts[1], parts[2])
            return

        # Pagination callbacks
        try:
            if parts[0] == "approved_page" and len(parts) == 3:
                await self._cb_approved_page(query, parts[1], int(parts[2]))
                return
            if parts[0] == "logs_page" and len(parts) == 4:
                await self._cb_logs_page(query, parts[1], int(parts[2]), int(parts[3]))
                return
            if parts[0] == "search_page" and len(parts) == 4:
                await self._cb_search_page(query, parts[1], int(parts[2]), int(parts[3]))
                return
            if parts[0] == "chan_page" and len(parts) == 4 and parts[2] in ("allowed", "blocked"):
                await self._cb_channel_page(query, parts[1], parts[2], int(parts[3]))
                return
            if parts[0] == "chan_filter" and len(parts) == 3 and parts[2] in ("allowed", "blocked"):
                await self._cb_channel_filter(query, parts[1], parts[2])
                return
            if parts[0] == "chan_menu" and len(parts) == 2:
                await self._cb_channel_menu(query, parts[1])
                return
            if parts[0] == "pending_page" and len(parts) == 3:
                await self._cb_pending_page(query, parts[1], int(parts[2]))
                return
            if parts[0] == "starter_page" and len(parts) == 3:
                await self._cb_starter_page(query, parts[1], int(parts[2]))
                return
        except (ValueError, IndexError):
            await query.answer("Invalid callback.")
            return

        # Starter channels prompt (Yes/No from welcome message — first-run, default profile)
        if parts[0] == "starter_prompt" and len(parts) == 2:
            _answer_bg(query, "Got it!" if parts[1] == "no" else "")
            if parts[1] == "yes":
                cs = self._child_store("default")
                text, markup = self._render_starter_message(store=cs, profile_id="default")
                await _edit_msg(query, text, markup, disable_preview=True)
            else:
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass
            return

        # Starter channel import: starter_import:profile_id:idx
        if parts[0] == "starter_import" and len(parts) == 3:
            try:
                await self._cb_starter_import(query, parts[1], int(parts[2]))
            except (ValueError, IndexError):
                await query.answer("Invalid callback.")
            return

        # Time limit wizard callbacks
        if parts[0] == "setup_top" and len(parts) == 2:
            _answer_bg(query)
            await self._cb_setup_top(query, parts[1])
            return
        if parts[0] == "setup_sched_start" and len(parts) >= 2:
            _answer_bg(query)
            await self._cb_setup_sched_start(query, ":".join(parts[1:]))
            return
        if parts[0] == "setup_sched_stop" and len(parts) >= 2:
            _answer_bg(query)
            await self._cb_setup_sched_stop(query, ":".join(parts[1:]))
            return
        if parts[0] == "setup_sched_day" and len(parts) == 2:
            _answer_bg(query)
            await self._cb_setup_sched_day(query, parts[1])
            return
        if parts[0] == "setup_sched_apply" and len(parts) == 2:
            _answer_bg(query)
            await self._cb_setup_sched_apply(query, parts[1])
            return
        if parts[0] == "setup_sched_done":
            _answer_bg(query)
            await self._cb_setup_sched_done(query)
            return
        if parts[0] == "setup_daystart" and len(parts) >= 3:
            _answer_bg(query)
            day = parts[1]
            value = ":".join(parts[2:])
            await self._cb_setup_daystart(query, day, value)
            return
        if parts[0] == "setup_daystop" and len(parts) >= 3:
            _answer_bg(query)
            day = parts[1]
            value = ":".join(parts[2:])
            await self._cb_setup_daystop(query, day, value)
            return
        if parts[0] == "setup_mode" and len(parts) == 2:
            _answer_bg(query)
            await self._cb_setup_mode(query, parts[1])
            return
        if parts[0] == "setup_simple" and len(parts) == 2:
            _answer_bg(query)
            await self._cb_setup_simple(query, parts[1])
            return
        if parts[0] == "setup_edu" and len(parts) == 2:
            _answer_bg(query)
            await self._cb_setup_edu(query, parts[1])
            return
        if parts[0] == "setup_fun" and len(parts) == 2:
            _answer_bg(query)
            await self._cb_setup_fun(query, parts[1])
            return
        if parts[0] == "switch_confirm" and len(parts) >= 2:
            _answer_bg(query)
            await self._cb_switch_confirm(query, ":".join(parts[1:]))
            return

        # Channel management callbacks: unallow:profile_id:name or unblock:profile_id:name
        # Channel names may contain colons, so rejoin everything after second ':'
        if parts[0] in ("unallow", "unblock") and len(parts) >= 3:
            profile_id = parts[1]
            ch_name = ":".join(parts[2:])
            cs = self._child_store(profile_id)
            # Look up channel_id before removing (remove_channel deletes the row)
            ch_id = ""
            ch_rows = cs.get_channels_with_ids(
                "allowed" if parts[0] == "unallow" else "blocked"
            )
            for name, cid, _h, _c in ch_rows:
                if name.lower() == ch_name.lower():
                    ch_id = cid or ""
                    break
            if cs.remove_channel(ch_name):
                if parts[0] == "unallow":
                    cs.delete_channel_videos(ch_name, channel_id=ch_id)
                if self.on_channel_change:
                    self.on_channel_change(profile_id)
                _answer_bg(query, f"Removed: {ch_name}")
                await self._update_channel_list_message(query, profile_id=profile_id)
            else:
                _answer_bg(query, f"Not found: {ch_name}")
            return

        # Resend notification callback from /pending: resend:profile_id:video_id
        if parts[0] == "resend" and len(parts) == 3:
            profile_id = parts[1]
            cs = self._child_store(profile_id)
            video = cs.get_video(parts[2])
            if not video or video['status'] != 'pending':
                await query.answer("No longer pending.")
                return
            _answer_bg(query, "Resending...")
            await self.notify_new_request(video, profile_id=profile_id)
            return

        # New format: action:profile_id:video_id (3 parts) or legacy action:video_id (2 parts)
        if len(parts) == 3:
            action, profile_id, video_id = parts
        elif len(parts) == 2:
            action, video_id = parts
            profile_id = "default"
        else:
            await query.answer("Invalid callback.")
            return

        if not re.fullmatch(r'[a-zA-Z0-9_-]{11}', video_id):
            await query.answer("Invalid callback.")
            return
        cs = self._child_store(profile_id)
        video = cs.get_video(video_id)
        if not video:
            await query.answer("Video not found.")
            return

        # Category toggle on approved videos (no status change)
        if action in ("setcat_edu", "setcat_fun") and video["status"] == "approved":
            cat = "edu" if action == "setcat_edu" else "fun"
            cs.set_video_category(video_id, cat)
            cat_label = CAT_LABELS.get(cat, "Entertainment")
            _answer_bg(query, f"→ {cat_label}")
            toggle_cat = "edu" if cat == "fun" else "fun"
            toggle_label = "→ Edu" if toggle_cat == "edu" else "→ Fun"
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("Revoke", callback_data=f"revoke:{profile_id}:{video_id}"),
                InlineKeyboardButton(toggle_label, callback_data=f"setcat_{toggle_cat}:{profile_id}:{video_id}"),
            ]])
            try:
                await query.edit_message_reply_markup(reply_markup=reply_markup)
            except Exception:
                pass
            if self.on_video_change:
                self.on_video_change()
            return

        yt_link = f"https://www.youtube.com/watch?v={video_id}"
        duration = format_duration(video.get('duration'))

        if action == "approve" and video['status'] == 'pending':
            cs.update_status(video_id, "approved")
            cs.set_video_category(video_id, "fun")
            _answer_bg(query, "Approved!")
            status_label = "APPROVED"
        elif action in ("approve_edu", "approve_fun") and video['status'] == 'pending':
            cat = "edu" if action == "approve_edu" else "fun"
            cs.update_status(video_id, "approved")
            cs.set_video_category(video_id, cat)
            cat_label = CAT_LABELS.get(cat, "Entertainment")
            _answer_bg(query, f"Approved ({cat_label})!")
            status_label = f"APPROVED ({cat_label})"
        elif action == "deny" and video['status'] == 'pending':
            cs.update_status(video_id, "denied")
            _answer_bg(query, "Denied.")
            status_label = "DENIED"
        elif action == "revoke" and video['status'] == 'approved':
            cs.update_status(video_id, "denied")
            _answer_bg(query, "Revoked!")
            status_label = "REVOKED"
        elif action == "allowchan":
            channel = video['channel_name']
            cid = video.get('channel_id')
            cs.add_channel(channel, "allowed", channel_id=cid)
            self._resolve_channel_bg(channel, cid, video_id=video_id, profile_id=profile_id)
            if video['status'] == 'pending':
                cs.update_status(video_id, "approved")
                cs.set_video_category(video_id, "fun")
                status_label = "APPROVED + CHANNEL ALLOWED"
            else:
                status_label = f"CHANNEL ALLOWED (video already {video['status']})"
            _answer_bg(query, f"Allowlisted: {channel}")
            if self.on_channel_change:
                self.on_channel_change(profile_id)
        elif action in ("allowchan_edu", "allowchan_fun"):
            cat = "edu" if action == "allowchan_edu" else "fun"
            channel = video['channel_name']
            cid = video.get('channel_id')
            cs.add_channel(channel, "allowed", channel_id=cid, category=cat)
            self._resolve_channel_bg(channel, cid, video_id=video_id, profile_id=profile_id)
            cat_label = CAT_LABELS.get(cat, "Entertainment")
            if video['status'] == 'pending':
                cs.update_status(video_id, "approved")
                cs.set_video_category(video_id, cat)
                status_label = f"APPROVED + CHANNEL ALLOWED ({cat_label})"
            else:
                status_label = f"CHANNEL ALLOWED ({cat_label}) (video already {video['status']})"
            _answer_bg(query, f"Allowlisted ({cat_label}): {channel}")
            if self.on_channel_change:
                self.on_channel_change(profile_id)
        elif action == "blockchan":
            channel = video['channel_name']
            cid = video.get('channel_id')
            cs.add_channel(channel, "blocked", channel_id=cid)
            self._resolve_channel_bg(channel, cid, video_id=video_id, profile_id=profile_id)
            if video['status'] == 'pending':
                cs.update_status(video_id, "denied")
                status_label = "DENIED + CHANNEL BLOCKED"
            else:
                status_label = f"CHANNEL BLOCKED (video already {video['status']})"
            _answer_bg(query, f"Blocked: {channel}")
            if self.on_channel_change:
                self.on_channel_change(profile_id)
        else:
            _answer_bg(query, f"Already {video['status']}.")
            return

        if self.on_video_change:
            self.on_video_change()

        channel_link = _channel_md_link(video['channel_name'], video.get('channel_id'))
        result_text = _md(
            f"**{status_label}**\n\n"
            f"**Title:** {video['title']}\n"
            f"**Channel:** {channel_link}\n"
            f"**Duration:** {duration}\n"
            f"[Watch on YouTube]({yt_link})"
        )

        if status_label.startswith("APPROVED"):
            video = cs.get_video(video_id)
            cur_cat = video.get("category", "fun") if video else "fun"
            toggle_cat = "edu" if cur_cat == "fun" else "fun"
            toggle_label = "→ Edu" if toggle_cat == "edu" else "→ Fun"
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("Revoke", callback_data=f"revoke:{profile_id}:{video_id}"),
                InlineKeyboardButton(toggle_label, callback_data=f"setcat_{toggle_cat}:{profile_id}:{video_id}"),
            ]])
        else:
            reply_markup = None

        try:
            await query.edit_message_caption(caption=result_text, reply_markup=reply_markup, parse_mode=MD2)
        except Exception:
            await query.edit_message_text(text=result_text, reply_markup=reply_markup, parse_mode=MD2)

    async def _cmd_child(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Manage child profiles. /child [add|remove|rename|pin]."""
        if not await self._require_admin(update):
            return
        args = context.args or []
        if not args:
            await self._child_list(update)
            return
        sub = args[0].lower()
        if sub == "add":
            await self._child_add(update, args[1:])
        elif sub == "remove":
            await self._child_remove(update, args[1:])
        elif sub == "rename":
            await self._child_rename(update, args[1:])
        elif sub == "pin":
            await self._child_pin(update, args[1:])
        else:
            await update.effective_message.reply_text(
                "Usage: /child [add|remove|rename|pin]\n\n"
                "`/child` — list profiles\n"
                "`/child add <name> [pin]` — create\n"
                "`/child remove <name>` — delete\n"
                "`/child rename <name> <new>` — rename\n"
                "`/child pin <name> [pin]` — set/clear PIN"
            )

    async def _child_list(self, update: Update) -> None:
        """List all child profiles."""
        profiles = self._get_profiles()
        if not profiles:
            await update.effective_message.reply_text("No profiles. Use /child add <name> to create one.")
            return
        lines = ["**Child Profiles**\n"]
        for p in profiles:
            pin_status = "PIN set" if p["pin"] else "no PIN"
            cs = self._child_store(p["id"])
            stats = cs.get_stats()
            ch_count = len(cs.get_channels_with_ids("allowed"))
            lines.append(f"**{p['display_name']}**")
            lines.append(f"  {pin_status} · {stats['approved']} videos · {ch_count} channels")
        await update.effective_message.reply_text(_md("\n".join(lines)), parse_mode=MD2)

    async def _child_add(self, update: Update, args: list[str]) -> None:
        """Handle /child add <name> [pin]."""
        if not args:
            await update.effective_message.reply_text("Usage: /child add <name> [pin]")
            return
        name = args[0]
        pin = args[1] if len(args) > 1 else ""
        # Generate URL-safe ID from name
        pid = re.sub(r'[^a-z0-9]', '', name.lower())[:20]
        if not pid:
            await update.effective_message.reply_text("Name must contain at least one alphanumeric character.")
            return
        # Ensure unique ID
        if self.video_store.get_profile(pid):
            await update.effective_message.reply_text(f"A profile named '{name}' already exists.")
            return
        if self.video_store.create_profile(pid, name, pin=pin):
            pin_msg = " with PIN" if pin else " (no PIN)"
            await update.effective_message.reply_text(_md(f"Created profile: **{name}**{pin_msg}"), parse_mode=MD2)
        else:
            await update.effective_message.reply_text("Failed to create profile.")

    def _find_profile(self, name: str):
        """Find a profile by display name or id (case-insensitive)."""
        name_lower = name.lower()
        for p in self._get_profiles():
            if p["display_name"].lower() == name_lower or p["id"] == name_lower:
                return p
        return None

    async def _child_remove(self, update: Update, args: list[str]) -> None:
        """Handle /child remove <name>."""
        if not args:
            await update.effective_message.reply_text("Usage: /child remove <name>")
            return
        name = " ".join(args)
        target = self._find_profile(name)
        if not target:
            await update.effective_message.reply_text(f"Profile not found: {name}")
            return
        # Confirmation button
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"Delete {target['display_name']} and all data",
                callback_data=f"child_del:{target['id']}",
            ),
        ]])
        await update.effective_message.reply_text(
            _md(f"Delete **{target['display_name']}**? This removes all videos, channels, watch history, and settings."),
            parse_mode=MD2,
            reply_markup=keyboard,
        )

    async def _child_rename(self, update: Update, args: list[str]) -> None:
        """Handle /child rename <name> <new_name>."""
        if len(args) < 2:
            await update.effective_message.reply_text("Usage: /child rename <name> <new_name>")
            return
        old_name = args[0]
        new_name = " ".join(args[1:])
        target = self._find_profile(old_name)
        if not target:
            await update.effective_message.reply_text(f"Profile not found: {old_name}")
            return
        if self.video_store.update_profile(target["id"], display_name=new_name):
            await update.effective_message.reply_text(_md(f"Renamed: {target['display_name']} → **{new_name}**"), parse_mode=MD2)
        else:
            await update.effective_message.reply_text("Failed to rename profile.")

    async def _child_pin(self, update: Update, args: list[str]) -> None:
        """Handle /child pin <name> [pin]."""
        if not args:
            await update.effective_message.reply_text("Usage: /child pin <name> [pin]\nOmit pin to remove it.")
            return
        name = args[0]
        new_pin = args[1] if len(args) > 1 else ""
        target = self._find_profile(name)
        if not target:
            await update.effective_message.reply_text(f"Profile not found: {name}")
            return
        if self.video_store.update_profile(target["id"], pin=new_pin):
            if new_pin:
                await update.effective_message.reply_text(_md(f"PIN set for **{target['display_name']}**."), parse_mode=MD2)
            else:
                await update.effective_message.reply_text(_md(f"PIN removed for **{target['display_name']}**."), parse_mode=MD2)
        else:
            await update.effective_message.reply_text("Failed to update PIN.")

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Welcome message on first /start contact."""
        if not await self._require_admin(update):
            return
        text, markup = self._build_welcome_message()
        await update.effective_message.reply_text(text, parse_mode=MD2, reply_markup=markup)

    def _build_welcome_message(self) -> tuple[str, InlineKeyboardMarkup | None]:
        """Build the welcome message with optional starter channels prompt."""
        from version import __version__
        msg = (
            f"**BrainRotGuard v{__version__}**\n\n"
            "YouTube approval system for kids. Your child searches and "
            "requests videos through the web UI — you approve or deny "
            "them right here in Telegram.\n\n"
            "Use `/help` to see all available commands."
        )
        markup = None
        if self._starter_channels:
            msg += "\n\nWould you like to browse starter channels to get started?"
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("Yes, show me", callback_data="starter_prompt:yes"),
                InlineKeyboardButton("No thanks", callback_data="starter_prompt:no"),
            ]])
        return _md(msg), markup

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_admin(update):
            return
        from version import __version__
        help_link = "📖 [Full command reference](https://github.com/GHJJ123/brainrotguard/blob/main/docs/telegram-commands.md)\n"
        await update.effective_message.reply_text(_md(
            f"**BrainRotGuard v{__version__}**\n\n"
            "**Commands:**\n"
            "`/help` - Show this message\n"
            "`/pending` - List pending requests\n"
            "`/approved` - List approved videos\n"
            "`/stats` - Usage statistics\n"
            "`/watch [days]` - Watch activity & time budget\n"
            "`/logs [days|today]` - Activity report\n\n"
            "**Channel:**\n"
            "`/channel` - List all channels\n"
            "`/channel starter` - Kid-friendly starter list\n"
            "`/channel allow @handle [edu|fun]`\n"
            "`/channel cat <name> edu|fun`\n"
            "`/channel unallow|block|unblock <name>`\n\n"
            "**Filters & Search:**\n"
            "`/filter` - List word filters\n"
            "`/filter add|remove <word>`\n"
            "`/search [days|today|all]` - Search history\n\n"
            "`/time` - Show status & weekly view\n"
            "`/time setup` - Guided limit wizard\n"
            "`/time [min|off]` - Simple watch limit\n"
            "`/time edu|fun <min|off>` - Category limits\n"
            "`/time start|stop [time|off]` - Schedule\n"
            "`/time add <min>` - Bonus for today\n"
            "`/time <day> [start|stop|edu|fun|limit|off]`\n"
            "`/time <day> copy <days|weekdays|weekend|all>`\n"
            "`/shorts [on|off]` - Toggle Shorts row\n"
            "`/changelog` - Latest changes\n\n"
            "**Profiles:**\n"
            "`/child` - List child profiles\n"
            "`/child add <name> [pin]`\n"
            "`/child remove|rename|pin <name>`\n\n"
            f"{help_link}"
            "☕ [Buy me a coffee](https://ko-fi.com/coffee4jj)"
        ), parse_mode=MD2, disable_web_page_preview=True)

    async def _cmd_shorts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Toggle Shorts display on/off or show status."""
        if not await self._require_admin(update):
            return

        async def _inner(update, context, cs, profile):
            args = context.args
            is_default = cs.profile_id == "default"
            if args and args[0].lower() in ("on", "off"):
                enabled = args[0].lower() == "on"
                cs.set_setting("shorts_enabled", str(enabled).lower())
                if self.on_channel_change:
                    self.on_channel_change()
                if enabled:
                    await update.effective_message.reply_text(_md(
                        "**Shorts enabled**\n\n"
                        "- Shorts row appears on the homepage below videos\n"
                        "- Shorts from allowlisted channels are fetched on next cache refresh\n"
                        "- Shorts still count toward category time budgets (edu/fun)\n"
                        "- Shorts hidden from search results remain hidden"
                    ), parse_mode=MD2)
                else:
                    await update.effective_message.reply_text(_md(
                        "**Shorts disabled**\n\n"
                        "- Shorts row removed from homepage\n"
                        "- Shorts hidden from catalog, search results, and channel filters\n"
                        "- Existing approved Shorts stay in the database\n"
                        "- Use `/shorts on` to re-enable anytime"
                    ), parse_mode=MD2)
            else:
                db_val = cs.get_setting("shorts_enabled", "")
                if db_val:
                    current = db_val.lower() == "true"
                elif is_default and self.config and hasattr(self.config.youtube, 'shorts_enabled'):
                    current = self.config.youtube.shorts_enabled
                else:
                    current = False
                if current:
                    await update.effective_message.reply_text(_md(
                        "**Shorts: enabled**\n\n"
                        "Shorts appear in a dedicated row on the homepage and are "
                        "fetched from allowlisted channels. They count toward "
                        "edu/fun time budgets like regular videos.\n\n"
                        "`/shorts off` — hide Shorts everywhere"
                    ), parse_mode=MD2)
                else:
                    await update.effective_message.reply_text(_md(
                        "**Shorts: disabled**\n\n"
                        "Shorts are hidden from the homepage, catalog, and search results. "
                        "No Shorts are fetched from channels.\n\n"
                        "`/shorts on` — show Shorts in a dedicated row"
                    ), parse_mode=MD2)

        await self._with_child_context(update, context, _inner)

    _PENDING_PAGE_SIZE = 5

    async def _cmd_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return

        async def _inner(update, context, cs, profile):
            pending = cs.get_pending()
            if not pending:
                await update.effective_message.reply_text("No pending requests. Videos requested from the web app will appear here.")
                return
            text, keyboard = self._render_pending_page(pending, 0, profile_id=profile["id"])
            await update.effective_message.reply_text(text, parse_mode=MD2, reply_markup=keyboard)

        await self._with_child_context(update, context, _inner)

    def _render_pending_page(self, pending: list, page: int, profile_id: str = "default") -> tuple[str, InlineKeyboardMarkup]:
        """Render a page of the pending list with resend buttons."""
        total = len(pending)
        ps = self._PENDING_PAGE_SIZE
        start = page * ps
        end = min(start + ps, total)
        page_items = pending[start:end]
        total_pages = (total + ps - 1) // ps

        header = f"**Pending Requests ({total})**"
        if total_pages > 1:
            header += f" \u00b7 pg {page + 1}/{total_pages}"
        lines = [header, ""]
        buttons = []
        for v in page_items:
            ch = _channel_md_link(v['channel_name'], v.get('channel_id'))
            duration = format_duration(v.get('duration'))
            lines.append(f"\u2022 {v['title']}")
            lines.append(f"  _{ch} \u00b7 {duration}_")
            lines.append("")
            buttons.append([InlineKeyboardButton(
                f"Resend: {v['title'][:30]}", callback_data=f"resend:{profile_id}:{v['video_id']}",
            )])

        nav = _nav_row(page, total, ps, f"pending_page:{profile_id}")
        if nav:
            buttons.append(nav)
        return _md("\n".join(lines)), InlineKeyboardMarkup(buttons)

    async def _cb_pending_page(self, query, profile_id: str, page: int) -> None:
        """Handle pending list pagination."""
        cs = self._child_store(profile_id)
        pending = cs.get_pending()
        if not pending:
            await query.answer("No pending requests.")
            return
        _answer_bg(query)
        text, keyboard = self._render_pending_page(pending, page, profile_id=profile_id)
        await _edit_msg(query, text, keyboard)

    _APPROVED_PAGE_SIZE = 10

    async def _cmd_approved(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return

        async def _inner(update, context, cs, profile):
            pid = profile["id"]
            query_str = " ".join(context.args)[:200] if context.args else ""
            if query_str:
                results = cs.search_approved(query_str)
                if not results:
                    await update.effective_message.reply_text(f"No approved videos matching \"{query_str}\".")
                    return
                text, keyboard = self._render_approved_page(results, len(results), 0, search=query_str, store=cs, profile_id=pid)
                await update.effective_message.reply_text(
                    text, parse_mode=MD2, reply_markup=keyboard, disable_web_page_preview=True,
                )
                return
            page_items, total = cs.get_approved_page(0, self._APPROVED_PAGE_SIZE)
            if not page_items:
                await update.effective_message.reply_text("No approved videos yet. Approve requests or use /channel to allow channels.")
                return
            text, keyboard = self._render_approved_page(page_items, total, 0, store=cs, profile_id=pid)
            await update.effective_message.reply_text(
                text, parse_mode=MD2, reply_markup=keyboard, disable_web_page_preview=True,
            )

        await self._with_child_context(update, context, _inner)

    def _render_approved_page(self, page_items: list, total: int, page: int, search: str = "", store=None, profile_id: str = "default") -> tuple[str, InlineKeyboardMarkup | None]:
        """Render a page of the approved list."""
        s = store or self.video_store
        ps = self._APPROVED_PAGE_SIZE
        end = (page + 1) * ps
        total_pages = (total + ps - 1) // ps

        if search:
            header = f"\U0001f50d **\"{search}\" ({total} result{'s' if total != 1 else ''})**"
        else:
            header = f"\U0001f4cb **Approved ({total})**"
        if total_pages > 1:
            header += f" \u00b7 pg {page + 1}/{total_pages}"
        lines = [header, ""]
        watch_mins = s.get_batch_watch_minutes(
            [v['video_id'] for v in page_items]
        )
        for v in page_items:
            vid = v['video_id']
            title = v['title'][:42]
            yt_link = f"https://www.youtube.com/watch?v={vid}"
            views = v.get('view_count', 0)
            watched = watch_mins.get(vid, 0.0)
            parts = [_channel_md_link(v['channel_name'], v.get('channel_id'))]
            if views:
                parts.append(f"{views}v")
            if watched >= 1:
                parts.append(f"{int(watched)}m")
            detail = ' \u00b7 '.join(parts)
            lines.append(f"\u2022 [{title}]({yt_link})")
            lines.append(f"  _{detail}_")
            lines.append(f"  /revoke\\_{vid.replace('-', '_')}")
            lines.append("")

        nav = _nav_row(page, total, ps, f"approved_page:{profile_id}")
        keyboard = InlineKeyboardMarkup([nav]) if nav else None
        return _md("\n".join(lines)), keyboard

    async def _cb_approved_page(self, query, profile_id: str, page: int) -> None:
        """Handle approved list pagination."""
        cs = self._child_store(profile_id)
        page_items, total = cs.get_approved_page(page, self._APPROVED_PAGE_SIZE)
        if not page_items and page == 0:
            await query.answer("No approved videos.")
            return
        _answer_bg(query)
        text, keyboard = self._render_approved_page(page_items, total, page, store=cs, profile_id=profile_id)
        await _edit_msg(query, text, keyboard, disable_preview=True)

    async def _cmd_revoke(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return
        # Extract video_id from /revoke_VIDEOID (hyphens encoded as underscores)
        text = update.message.text.strip()
        raw_id = text.split("_", 1)[1] if "_" in text else ""
        # Search all profiles for this video
        video = None
        found_profile = None
        for p in self._get_profiles():
            cs = self._child_store(p["id"])
            v = cs.get_video(raw_id)
            if not v:
                v = cs.find_video_fuzzy(raw_id)
            if v and v['status'] == 'approved':
                video = v
                found_profile = p
                break
            if v and not video:
                video = v
                found_profile = p
        if not video:
            await update.effective_message.reply_text("Video not found — it may have been removed from the database.")
            return
        video_id = video['video_id']
        if video['status'] != 'approved':
            await update.effective_message.reply_text(f"Already {video['status']} — no change needed.")
            return
        cs = self._child_store(found_profile["id"])
        cs.update_status(video_id, "denied")
        await update.effective_message.reply_text(
            _md(f"**Approval removed:** {video['title']}\nThe video is no longer watchable."), parse_mode=MD2,
        )

    # --- /watch command ---

    async def _cmd_watch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return

        async def _inner(update, context, cs, profile):
            # Parse days arg: default today, support "yesterday", int days
            days = 0
            if context.args:
                arg = context.args[0].lower()
                if arg == "yesterday":
                    days = 1
                elif arg.isdigit():
                    days = min(int(arg), 365)

            tz = self._get_tz()
            from datetime import timedelta
            import datetime as _dt
            from zoneinfo import ZoneInfo
            tz_info = ZoneInfo(tz) if tz else None

            if days == 0:
                today = get_today_str(tz)
                dates = [today]
                header = "Today's Watch Activity"
            elif days == 1:
                yesterday = (_dt.datetime.now(tz_info) - timedelta(days=1)).strftime("%Y-%m-%d")
                dates = [yesterday]
                header = "Yesterday's Watch Activity"
            else:
                dates = [
                    (_dt.datetime.now(tz_info) - timedelta(days=i)).strftime("%Y-%m-%d")
                    for i in range(days)
                ]
                header = f"Watch Activity (last {days} days)"

            lines = [f"**{header}**\n"]

            # Time budget (only for today)
            today = get_today_str(tz)
            is_default = cs.profile_id == "default"
            if today in dates:
                limit_str = self._resolve_setting("daily_limit_minutes", store=cs)
                if not limit_str and is_default and self.config:
                    limit_min = self.config.watch_limits.daily_limit_minutes
                else:
                    limit_min = int(limit_str) if limit_str else 0
                bounds = get_day_utc_bounds(today, self._get_tz())
                used = cs.get_daily_watch_minutes(today, utc_bounds=bounds)

                bonus = 0
                bonus_date = cs.get_setting("daily_bonus_date", "")
                if bonus_date == today:
                    bonus = int(cs.get_setting("daily_bonus_minutes", "0") or "0")

                if limit_min == 0:
                    lines.append(f"**Watch limit:** OFF")
                    lines.append(f"**Watched today:** {int(used)} min")
                else:
                    effective = limit_min + bonus
                    remaining = max(0, effective - used)
                    pct = min(1.0, used / effective) if effective > 0 else 0
                    lines.append(f"**Daily limit:** {limit_min} min")
                    if bonus > 0:
                        lines.append(f"**Bonus today:** +{bonus} min")
                    lines.append(f"**Used:** {int(used)} min \u00b7 **Remaining:** {int(remaining)} min")
                    lines.append(f"`{_progress_bar(pct)}` {int(pct * 100)}%")
                lines.append("")

            # Pre-fetch all breakdowns
            all_breakdowns: dict[str, list[dict]] = {}
            daily_totals: dict[str, float] = {}
            for date_str in dates:
                bd = cs.get_daily_watch_breakdown(date_str, utc_bounds=get_day_utc_bounds(date_str, self._get_tz()))
                all_breakdowns[date_str] = bd
                daily_totals[date_str] = sum(v['minutes'] for v in bd) if bd else 0

            # Multi-day summary chart
            if len(dates) > 1:
                max_min = max(daily_totals.values()) if daily_totals else 1
                if max_min == 0:
                    max_min = 1
                grand_total = sum(daily_totals.values())
                lines.append(f"**Overview** \u2014 {int(grand_total)} min total")
                bar_width = 10
                for date_str in dates:
                    total = daily_totals[date_str]
                    frac = total / max_min
                    bar = _progress_bar(frac, bar_width)
                    dt = _dt.datetime.strptime(date_str, "%Y-%m-%d")
                    day_label = dt.strftime("%b %d")
                    total_str = f"{int(total)}m" if total >= 1 else "\u2014"
                    lines.append(f"`{day_label}  {bar}` {total_str}")
                lines.append("")

            # Per-day breakdown (detailed view only for single-day)
            if len(dates) == 1:
                breakdown = all_breakdowns[dates[0]]
                if not breakdown:
                    lines.append("_No videos watched._")
                else:
                    by_cat: dict = {}
                    for v in breakdown:
                        cat = v.get('category') or 'fun'
                        by_cat.setdefault(cat, []).append(v)

                    for cat, cat_label in [("edu", "Educational"), ("fun", "Entertainment")]:
                        vids = by_cat.get(cat, [])
                        if not vids:
                            continue
                        cat_total = sum(v['minutes'] for v in vids)
                        cat_limit_str = self._resolve_setting(f"{cat}_limit_minutes", store=cs)
                        cat_limit = int(cat_limit_str) if cat_limit_str else 0
                        if cat_limit > 0:
                            lines.append(f"\n**{cat_label}** \u2014 {int(cat_total)}/{cat_limit} min")
                            pct = min(1.0, cat_total / cat_limit) if cat_limit > 0 else 0
                            lines.append(f"`{_progress_bar(pct)}` {int(pct * 100)}%")
                        else:
                            lines.append(f"\n**{cat_label}** \u2014 {int(cat_total)} min (no limit)")

                        for v in vids:
                            title = v['title'][:40]
                            ch_link = _channel_md_link(v['channel_name'], v.get('channel_id'))
                            watched_min = int(v['minutes'])
                            vid_dur = v.get('duration')
                            if vid_dur and vid_dur > 0:
                                dur_min = vid_dur // 60
                                pct = min(100, int(v['minutes'] / (vid_dur / 60) * 100)) if vid_dur > 0 else 0
                                lines.append(f"\u2022 **{title}**")
                                lines.append(f"  {ch_link} \u00b7 {watched_min}m / {dur_min}m ({pct}%)")
                            else:
                                lines.append(f"\u2022 **{title}**")
                                lines.append(f"  {ch_link} \u00b7 {watched_min}m watched")

            await update.effective_message.reply_text(
                _md("\n".join(lines)), parse_mode=MD2, disable_web_page_preview=True,
            )

        await self._with_child_context(update, context, _inner)

    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return

        async def _inner(update, context, cs, profile):
            stats = cs.get_stats()
            await update.effective_message.reply_text(_md(
                f"**BrainRotGuard Stats**\n\n"
                f"**Total videos:** {stats['total']}\n"
                f"**Pending:** {stats['pending']}\n"
                f"**Approved:** {stats['approved']}\n"
                f"**Denied:** {stats['denied']}\n"
                f"**Total views:** {stats['total_views']}"
            ), parse_mode=MD2)

        await self._with_child_context(update, context, _inner)

    async def _cmd_changelog(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_admin(update):
            return
        import os
        from version import __version__
        changelog_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "CHANGELOG.md")
        try:
            with open(changelog_path, "r") as f:
                content = f.read()
            sections = content.split("\n## ")
            if len(sections) >= 2:
                latest = "## " + sections[1].split("\n## ")[0]
            else:
                latest = content
            latest = latest.strip()
            latest = f"BrainRotGuard v{__version__}\n\n{latest}"
            if len(latest) > 3500:
                latest = latest[:3500] + "\n..."
            await update.effective_message.reply_text(latest)
        except FileNotFoundError:
            await update.effective_message.reply_text("Changelog not available.")

    # --- Activity report ---

    _LOGS_PAGE_SIZE = 10

    async def _cmd_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return

        async def _inner(update, context, cs, profile):
            days = 7
            if context.args:
                arg = context.args[0].lower()
                if arg == "today":
                    days = 1
                elif arg.isdigit():
                    days = min(int(arg), 365)
            activity = cs.get_recent_activity(days)
            if not activity:
                period = "today" if days == 1 else f"last {days} days"
                await update.effective_message.reply_text(f"No activity in the {period}.")
                return
            text, keyboard = self._render_logs_page(activity, days, 0, profile_id=profile["id"])
            await update.effective_message.reply_text(text, parse_mode=MD2, reply_markup=keyboard)

        await self._with_child_context(update, context, _inner)

    def _render_logs_page(self, activity: list, days: int, page: int, profile_id: str = "default") -> tuple[str, InlineKeyboardMarkup | None]:
        """Render a page of the activity log with pagination."""
        total = len(activity)
        page_size = self._LOGS_PAGE_SIZE
        start = page * page_size
        end = min(start + page_size, total)
        page_items = activity[start:end]
        total_pages = (total + page_size - 1) // page_size

        period = "Today" if days == 1 else f"Last {days} days"
        status_icon = {"approved": "\u2713", "denied": "\u2717", "pending": "?"}
        header = f"\U0001f4cb **Activity ({period}) \u2014 {total} videos**"
        if total_pages > 1:
            header += f" \u00b7 pg {page + 1}/{total_pages}"
        lines = [header, "", "```"]
        for v in page_items:
            icon = status_icon.get(v['status'], '?')
            ts = v['requested_at'][5:16].replace('T', ' ')
            title = v['title'][:32]
            lines.append(f"{icon} {ts}  {title}")
        lines.append("```")

        nav = _nav_row(page, total, page_size, f"logs_page:{profile_id}:{days}")
        keyboard = InlineKeyboardMarkup([nav]) if nav else None
        return _md("\n".join(lines)), keyboard

    async def _cb_logs_page(self, query, profile_id: str, days: int, page: int) -> None:
        """Handle logs pagination."""
        days = min(max(1, days), 365)
        cs = self._child_store(profile_id)
        activity = cs.get_recent_activity(days)
        if not activity:
            await query.answer("No activity.")
            return
        _answer_bg(query)
        text, keyboard = self._render_logs_page(activity, days, page, profile_id=profile_id)
        await _edit_msg(query, text, keyboard)

    # --- /search subcommands ---

    async def _cmd_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show search history. /search [days|today|all]."""
        if not self._check_admin(update):
            return

        async def _inner(update, context, cs, profile):
            await self._search_history(update, context.args or [], store=cs, profile_id=profile["id"])

        await self._with_child_context(update, context, _inner)

    _SEARCH_PAGE_SIZE = 20

    async def _search_history(self, update: Update, args: list[str], store=None, profile_id: str = "default") -> None:
        s = store or self.video_store
        days = 7
        if args:
            arg = args[0].lower()
            if arg == "today":
                days = 1
            elif arg == "all":
                days = 365
            elif arg.isdigit():
                days = min(int(arg), 365)
        searches = s.get_recent_searches(days)
        if not searches:
            period = "today" if days == 1 else f"last {days} days"
            await update.effective_message.reply_text(f"No searches in the {period}.")
            return
        text, keyboard = self._render_search_page(searches, days, 0, profile_id=profile_id)
        await update.effective_message.reply_text(
            text, parse_mode=MD2, reply_markup=keyboard, disable_web_page_preview=True,
        )

    def _render_search_page(self, searches: list, days: int, page: int, profile_id: str = "default") -> tuple[str, InlineKeyboardMarkup | None]:
        """Render a page of search history."""
        total = len(searches)
        ps = self._SEARCH_PAGE_SIZE
        start = page * ps
        end = min(start + ps, total)
        page_items = searches[start:end]
        total_pages = (total + ps - 1) // ps

        period = "Today" if days == 1 else f"Last {days} days"
        header = f"\U0001f50d **Search History ({period})**"
        if total_pages > 1:
            header += f" \u00b7 pg {page + 1}/{total_pages}"
        lines = [header, "", "```"]
        for s in page_items:
            ts = s['searched_at'][5:16].replace('T', ' ')
            query = s['query'][:40]
            lines.append(f"{ts}  {query}")
        lines.append("```")

        nav = _nav_row(page, total, ps, f"search_page:{profile_id}:{days}")
        keyboard = InlineKeyboardMarkup([nav]) if nav else None
        return _md("\n".join(lines)), keyboard

    async def _cb_search_page(self, query, profile_id: str, days: int, page: int) -> None:
        """Handle search history pagination."""
        days = min(max(1, days), 365)
        cs = self._child_store(profile_id)
        searches = cs.get_recent_searches(days)
        if not searches:
            await query.answer("No searches.")
            return
        _answer_bg(query)
        text, keyboard = self._render_search_page(searches, days, page, profile_id=profile_id)
        await _edit_msg(query, text, keyboard, disable_preview=True)

    async def _cmd_filter(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Manage word filters. /filter [add|remove <word>]."""
        if not await self._require_admin(update):
            return
        args = context.args or []
        if not args:
            await self._filter_list(update)
            return
        action = args[0].lower()
        if action == "list":
            await self._filter_list(update)
            return
        if len(args) < 2:
            await update.effective_message.reply_text("Usage: /filter add|remove <word>")
            return
        word = " ".join(args[1:])
        if action == "add":
            if self.video_store.add_word_filter(word):
                if self.on_channel_change:
                    self.on_channel_change()
                await update.effective_message.reply_text(
                    f"Filter added: \"{word}\"\n"
                    "Videos with this word in the title are hidden everywhere."
                )
            else:
                await update.effective_message.reply_text(f"Already filtered: \"{word}\"")
        elif action in ("remove", "rm", "del"):
            if self.video_store.remove_word_filter(word):
                if self.on_channel_change:
                    self.on_channel_change()
                await update.effective_message.reply_text(f"Filter removed: \"{word}\"")
            else:
                await update.effective_message.reply_text(f"\"{word}\" isn't in the filter list.")
        else:
            await update.effective_message.reply_text("Usage: /filter add|remove <word>")

    async def _filter_list(self, update: Update) -> None:
        words = self.video_store.get_word_filters()
        if not words:
            await update.effective_message.reply_text("No word filters set. Use /filter add <word> to hide videos by title.")
            return
        lines = ["**Word Filters** (hidden everywhere):\n"]
        for w in words:
            lines.append(f"- `{w}`")
        await update.effective_message.reply_text(_md("\n".join(lines)), parse_mode=MD2)

