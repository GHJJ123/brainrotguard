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
    _md, _answer_bg, _edit_msg, _channel_md_link,
    MD2, _GITHUB_REPO, _UPDATE_CHECK_INTERVAL,
)
from bot.activity import ActivityMixin
from bot.approval import ApprovalMixin
from bot.channels import ChannelMixin
from bot.commands import CommandsMixin
from bot.timelimits import TimeLimitMixin
from data.child_store import ChildStore
from utils import CAT_LABELS
from youtube.extractor import format_duration

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

