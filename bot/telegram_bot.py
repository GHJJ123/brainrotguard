"""BrainRotGuard Telegram Bot - parent approval for YouTube videos."""

import logging
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

import aiohttp
import telegramify_markdown
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    CallbackQueryHandler, ContextTypes,
    MessageHandler, filters,
)

from utils import get_today_str, get_day_utc_bounds, parse_time_input, format_time_12h, is_within_schedule
from youtube.extractor import format_duration

logger = logging.getLogger(__name__)

MD2 = "MarkdownV2"


def _md(text: str) -> str:
    """Convert markdown to Telegram MarkdownV2 format."""
    try:
        return telegramify_markdown.markdownify(text)
    except Exception:
        return text


def _channel_md_link(name: str, channel_id: Optional[str] = None) -> str:
    """Build a markdown link to a YouTube channel page, falling back to search."""
    if channel_id:
        return f"[{name}](https://www.youtube.com/channel/{channel_id})"
    return f"[{name}](https://www.youtube.com/results?search_query={quote(name)})"


class BrainRotGuardBot:
    """Telegram bot for parent video approval."""

    def __init__(self, bot_token: str, admin_chat_id: str, video_store, config=None,
                 starter_channels_path: Optional[Path] = None):
        self.bot_token = bot_token
        self.admin_chat_id = admin_chat_id
        self.video_store = video_store
        self.config = config
        self._app = None
        self._limit_notified_cats: dict[str, str] = {}  # category -> date string of last limit notification
        self.on_channel_change = None  # callback when channel lists change
        self.on_video_change = None  # callback when video status changes
        # Load starter channels
        from data.starter_channels import load_starter_channels
        self._starter_channels = load_starter_channels(starter_channels_path)

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

    def _resolve_handle_bg(self, channel_name: str, channel_id: str) -> None:
        """Fire a background task to resolve and store the @handle for a channel."""
        import asyncio
        async def _resolve():
            try:
                from youtube.extractor import resolve_handle_from_channel_id
                handle = await resolve_handle_from_channel_id(channel_id)
                if handle:
                    self.video_store.update_channel_handle(channel_name, handle)
                    logger.info(f"Resolved handle: {channel_name} → {handle}")
            except Exception as e:
                logger.debug(f"Background handle resolve failed for {channel_name}: {e}")
        asyncio.create_task(_resolve())

    async def start(self) -> None:
        """Start the bot."""
        logger.info("Starting BrainRotGuard bot...")
        self._app = ApplicationBuilder().token(self.bot_token).build()

        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("pending", self._cmd_pending))
        self._app.add_handler(CommandHandler("approved", self._cmd_approved))
        self._app.add_handler(CommandHandler("stats", self._cmd_stats))
        self._app.add_handler(CommandHandler("logs", self._cmd_logs))
        self._app.add_handler(CommandHandler("channel", self._cmd_channel))
        self._app.add_handler(CommandHandler("search", self._cmd_search))
        self._app.add_handler(CommandHandler("watch", self._cmd_watch))
        self._app.add_handler(CommandHandler("time", self._cmd_timelimit))
        self._app.add_handler(CommandHandler("changelog", self._cmd_changelog))
        self._app.add_handler(MessageHandler(
            filters.Regex(r'^/revoke_[a-zA-Z0-9_]{11}$'), self._cmd_revoke,
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

    async def stop(self) -> None:
        """Stop the bot."""
        if self._app:
            logger.info("Stopping BrainRotGuard bot...")
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            logger.info("BrainRotGuard bot stopped")

    async def notify_new_request(self, video: dict) -> None:
        """Send parent a notification about a new video request with Approve/Deny buttons.

        Sends a photo message with the video thumbnail, caption with title/channel/duration/link,
        and inline Approve/Deny buttons.
        """
        if not self._app:
            logger.warning("Bot not started, cannot send notification")
            return

        video_id = video['video_id']
        title = video['title']
        channel_link = _channel_md_link(video['channel_name'], video.get('channel_id'))
        duration = format_duration(video.get('duration'))
        yt_link = f"https://www.youtube.com/watch?v={video_id}"

        caption = _md(
            f"**New Video Request**\n\n"
            f"**Title:** {title}\n"
            f"**Channel:** {channel_link}\n"
            f"**Duration:** {duration}\n"
            f"[Watch on YouTube]({yt_link})"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Watch on YouTube", url=yt_link)],
            [
                InlineKeyboardButton("Approve (Edu)", callback_data=f"approve_edu:{video_id}"),
                InlineKeyboardButton("Approve (Fun)", callback_data=f"approve_fun:{video_id}"),
            ],
            [
                InlineKeyboardButton("Deny", callback_data=f"deny:{video_id}"),
            ],
            [
                InlineKeyboardButton("Allow Ch (Edu)", callback_data=f"allowchan_edu:{video_id}"),
                InlineKeyboardButton("Allow Ch (Fun)", callback_data=f"allowchan_fun:{video_id}"),
            ],
            [
                InlineKeyboardButton("Block Channel", callback_data=f"blockchan:{video_id}"),
            ],
        ])

        _THUMB_HOSTS = {
            "i.ytimg.com", "i1.ytimg.com", "i2.ytimg.com", "i3.ytimg.com",
            "i4.ytimg.com", "i9.ytimg.com", "img.youtube.com",
        }

        try:
            # Try to send with thumbnail (only fetch from known YouTube CDN domains)
            thumbnail_url = video.get('thumbnail_url')
            if thumbnail_url:
                parsed = urlparse(thumbnail_url)
                if not parsed.hostname or parsed.hostname not in _THUMB_HOSTS:
                    thumbnail_url = None
            if thumbnail_url:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(thumbnail_url) as resp:
                            if resp.status == 200:
                                photo_data = BytesIO(await resp.read())
                                await self._app.bot.send_photo(
                                    chat_id=self.admin_chat_id,
                                    photo=photo_data,
                                    caption=caption,
                                    reply_markup=keyboard,
                                    parse_mode=MD2,
                                )
                                return
                except Exception as e:
                    logger.warning(f"Failed to send thumbnail: {e}")

            # Fallback: send text message without photo
            await self._app.bot.send_message(
                chat_id=self.admin_chat_id,
                text=caption,
                reply_markup=keyboard,
                parse_mode=MD2,
            )
        except Exception as e:
            logger.error(f"Failed to notify about video {video_id}: {e}")

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline button callbacks for approve/deny/pagination."""
        query = update.callback_query
        if not self._check_admin(update):
            await query.answer("Unauthorized.")
            return

        data = query.data
        parts = data.split(":")

        # Pagination callbacks
        try:
            if parts[0] == "approved_page" and len(parts) == 2:
                await self._cb_approved_page(query, int(parts[1]))
                return
            if parts[0] == "logs_page" and len(parts) == 3:
                await self._cb_logs_page(query, int(parts[1]), int(parts[2]))
                return
            if parts[0] == "search_page" and len(parts) == 3:
                await self._cb_search_page(query, int(parts[1]), int(parts[2]))
                return
            if parts[0] == "chan_page" and len(parts) == 2:
                await self._cb_channel_page(query, int(parts[1]))
                return
            if parts[0] == "pending_page" and len(parts) == 2:
                await self._cb_pending_page(query, int(parts[1]))
                return
            if parts[0] == "starter_page" and len(parts) == 2:
                await self._cb_starter_page(query, int(parts[1]))
                return
        except (ValueError, IndexError):
            await query.answer("Invalid callback.")
            return

        # Starter channels prompt (Yes/No from welcome message)
        if parts[0] == "starter_prompt" and len(parts) == 2:
            if parts[1] == "yes":
                await query.answer()
                text, markup = self._render_starter_message()
                await query.edit_message_text(
                    text=text, reply_markup=markup, parse_mode=MD2,
                    disable_web_page_preview=True,
                )
            else:
                await query.answer("Got it!")
                await query.edit_message_reply_markup(reply_markup=None)
            return

        # Starter channel import
        if parts[0] == "starter_import" and len(parts) == 2:
            try:
                await self._cb_starter_import(query, int(parts[1]))
            except (ValueError, IndexError):
                await query.answer("Invalid callback.")
            return

        # Channel management callbacks (unallow:name or unblock:name)
        # Channel names may contain colons, so rejoin everything after first ':'
        if parts[0] in ("unallow", "unblock") and len(parts) >= 2:
            ch_name = ":".join(parts[1:])
            if self.video_store.remove_channel(ch_name):
                if self.on_channel_change:
                    self.on_channel_change()
                await query.answer(f"Removed: {ch_name}")
                # Refresh the channel list message
                await self._update_channel_list_message(query)
            else:
                await query.answer(f"Not found: {ch_name}")
            return

        # Resend notification callback from /pending
        if parts[0] == "resend" and len(parts) == 2:
            video = self.video_store.get_video(parts[1])
            if not video or video['status'] != 'pending':
                await query.answer("No longer pending.")
                return
            await query.answer("Resending...")
            await self.notify_new_request(video)
            return

        if len(parts) != 2:
            await query.answer("Invalid callback.")
            return

        action, video_id = parts
        video = self.video_store.get_video(video_id)
        if not video:
            await query.answer("Video not found.")
            return

        # Category toggle on approved videos (no status change)
        if action in ("setcat_edu", "setcat_fun") and video["status"] == "approved":
            cat = "edu" if action == "setcat_edu" else "fun"
            self.video_store.set_video_category(video_id, cat)
            cat_label = "Educational" if cat == "edu" else "Entertainment"
            await query.answer(f"→ {cat_label}")
            # Refresh buttons with updated toggle
            toggle_cat = "edu" if cat == "fun" else "fun"
            toggle_label = "→ Edu" if toggle_cat == "edu" else "→ Fun"
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("Revoke", callback_data=f"revoke:{video_id}"),
                InlineKeyboardButton(toggle_label, callback_data=f"setcat_{toggle_cat}:{video_id}"),
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
            self.video_store.update_status(video_id, "approved")
            self.video_store.set_video_category(video_id, "fun")
            await query.answer("Approved!")
            status_label = "APPROVED"
        elif action in ("approve_edu", "approve_fun") and video['status'] == 'pending':
            cat = "edu" if action == "approve_edu" else "fun"
            self.video_store.update_status(video_id, "approved")
            self.video_store.set_video_category(video_id, cat)
            cat_label = "Educational" if cat == "edu" else "Entertainment"
            await query.answer(f"Approved ({cat_label})!")
            status_label = f"APPROVED ({cat_label})"
        elif action == "deny" and video['status'] == 'pending':
            self.video_store.update_status(video_id, "denied")
            await query.answer("Denied.")
            status_label = "DENIED"
        elif action == "revoke" and video['status'] == 'approved':
            self.video_store.update_status(video_id, "denied")
            await query.answer("Revoked!")
            status_label = "REVOKED"
        elif action == "allowchan":
            channel = video['channel_name']
            cid = video.get('channel_id')
            self.video_store.add_channel(channel, "allowed", channel_id=cid)
            if cid:
                self._resolve_handle_bg(channel, cid)
            if video['status'] == 'pending':
                self.video_store.update_status(video_id, "approved")
                self.video_store.set_video_category(video_id, "fun")
            await query.answer(f"Allowlisted: {channel}")
            status_label = "APPROVED + CHANNEL ALLOWED"
            if self.on_channel_change:
                self.on_channel_change()
        elif action in ("allowchan_edu", "allowchan_fun"):
            cat = "edu" if action == "allowchan_edu" else "fun"
            channel = video['channel_name']
            cid = video.get('channel_id')
            self.video_store.add_channel(channel, "allowed", channel_id=cid, category=cat)
            if cid:
                self._resolve_handle_bg(channel, cid)
            if video['status'] == 'pending':
                self.video_store.update_status(video_id, "approved")
                self.video_store.set_video_category(video_id, cat)
            cat_label = "Educational" if cat == "edu" else "Entertainment"
            await query.answer(f"Allowlisted ({cat_label}): {channel}")
            status_label = f"APPROVED + CHANNEL ALLOWED ({cat_label})"
            if self.on_channel_change:
                self.on_channel_change()
        elif action == "blockchan":
            channel = video['channel_name']
            cid = video.get('channel_id')
            self.video_store.add_channel(channel, "blocked", channel_id=cid)
            if cid:
                self._resolve_handle_bg(channel, cid)
            if video['status'] == 'pending':
                self.video_store.update_status(video_id, "denied")
            await query.answer(f"Blocked: {channel}")
            status_label = "DENIED + CHANNEL BLOCKED"
            if self.on_channel_change:
                self.on_channel_change()
        else:
            await query.answer(f"Already {video['status']}.")
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

        # After approval, show Revoke + category toggle; otherwise remove all buttons
        if status_label.startswith("APPROVED"):
            video = self.video_store.get_video(video_id)
            cur_cat = video.get("category", "fun") if video else "fun"
            toggle_cat = "edu" if cur_cat == "fun" else "fun"
            toggle_label = "→ Edu" if toggle_cat == "edu" else "→ Fun"
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("Revoke", callback_data=f"revoke:{video_id}"),
                InlineKeyboardButton(toggle_label, callback_data=f"setcat_{toggle_cat}:{video_id}"),
            ]])
        else:
            reply_markup = None

        try:
            await query.edit_message_caption(caption=result_text, reply_markup=reply_markup, parse_mode=MD2)
        except Exception:
            await query.edit_message_text(text=result_text, reply_markup=reply_markup, parse_mode=MD2)

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Welcome message on first /start contact."""
        if not self._check_admin(update):
            await update.message.reply_text("Unauthorized.")
            return
        text, markup = self._build_welcome_message()
        await update.message.reply_text(text, parse_mode=MD2, reply_markup=markup)

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
        if not self._check_admin(update):
            await update.message.reply_text("Unauthorized.")
            return
        from version import __version__
        await update.message.reply_text(_md(
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
            "**Search:**\n"
            "`/search` - List word filters\n"
            "`/search history [days|today|all]`\n"
            "`/search filter add|remove <word>`\n\n"
            "`/time [min|off]` - Watch limit\n"
            "`/time add <min>` - Bonus for today\n"
            "`/time start|stop [time|off]` - Schedule\n"
            "`/time edu|fun <min|off>` - Category limits\n"
            "`/changelog` - Latest changes\n\n"
            "☕ [Buy me a coffee](https://ko-fi.com/coffee4jj)"
        ), parse_mode=MD2, disable_web_page_preview=True)

    _PENDING_PAGE_SIZE = 5

    async def _cmd_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return
        pending = self.video_store.get_pending()
        if not pending:
            await update.message.reply_text("No pending requests.")
            return
        text, keyboard = self._render_pending_page(pending, 0)
        await update.message.reply_text(text, parse_mode=MD2, reply_markup=keyboard)

    def _render_pending_page(self, pending: list, page: int) -> tuple[str, InlineKeyboardMarkup]:
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
                f"Resend: {v['title'][:30]}", callback_data=f"resend:{v['video_id']}",
            )])

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("\u25c0 Back", callback_data=f"pending_page:{page - 1}"))
        remaining = total - end
        if remaining > 0:
            nav.append(InlineKeyboardButton(
                f"Show more ({remaining})", callback_data=f"pending_page:{page + 1}",
            ))
        if nav:
            buttons.append(nav)
        return _md("\n".join(lines)), InlineKeyboardMarkup(buttons)

    async def _cb_pending_page(self, query, page: int) -> None:
        """Handle pending list pagination."""
        pending = self.video_store.get_pending()
        if not pending:
            await query.answer("No pending requests.")
            return
        await query.answer()
        text, keyboard = self._render_pending_page(pending, page)
        await query.edit_message_text(text=text, parse_mode=MD2, reply_markup=keyboard)

    _APPROVED_PAGE_SIZE = 10

    async def _cmd_approved(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return
        page_items, total = self.video_store.get_approved_page(0, self._APPROVED_PAGE_SIZE)
        if not page_items:
            await update.message.reply_text("No approved videos.")
            return
        text, keyboard = self._render_approved_page(page_items, total, 0)
        await update.message.reply_text(
            text, parse_mode=MD2, reply_markup=keyboard, disable_web_page_preview=True,
        )

    def _render_approved_page(self, page_items: list, total: int, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
        """Render a page of the approved list."""
        ps = self._APPROVED_PAGE_SIZE
        end = (page + 1) * ps
        total_pages = (total + ps - 1) // ps

        header = f"\U0001f4cb **Approved ({total})**"
        if total_pages > 1:
            header += f" \u00b7 pg {page + 1}/{total_pages}"
        lines = [header, ""]
        watch_mins = self.video_store.get_batch_watch_minutes(
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

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("\u25c0 Back", callback_data=f"approved_page:{page - 1}"))
        remaining = total - min(end, total)
        if remaining > 0:
            nav.append(InlineKeyboardButton(
                f"Show more ({remaining})", callback_data=f"approved_page:{page + 1}",
            ))
        keyboard = InlineKeyboardMarkup([nav]) if nav else None
        return _md("\n".join(lines)), keyboard

    async def _cb_approved_page(self, query, page: int) -> None:
        """Handle approved list pagination."""
        page_items, total = self.video_store.get_approved_page(page, self._APPROVED_PAGE_SIZE)
        if not page_items and page == 0:
            await query.answer("No approved videos.")
            return
        await query.answer()
        text, keyboard = self._render_approved_page(page_items, total, page)
        await query.edit_message_text(
            text=text, parse_mode=MD2, reply_markup=keyboard, disable_web_page_preview=True,
        )

    async def _cmd_revoke(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return
        # Extract video_id from /revoke_VIDEOID (hyphens encoded as underscores)
        text = update.message.text.strip()
        raw_id = text.split("_", 1)[1] if "_" in text else ""
        video = self.video_store.get_video(raw_id)
        if not video:
            # Try restoring hyphens — Telegram commands can't contain them
            video = self.video_store.find_video_fuzzy(raw_id)
        video_id = video['video_id'] if video else raw_id
        if not video:
            await update.message.reply_text("Video not found.")
            return
        if video['status'] != 'approved':
            await update.message.reply_text(f"Already {video['status']}.")
            return
        self.video_store.update_status(video_id, "denied")
        await update.message.reply_text(
            _md(f"**Revoked:** {video['title']}"), parse_mode=MD2,
        )

    # --- /watch command ---

    def _progress_bar(self, fraction: float, width: int = 20) -> str:
        filled = min(width, int(fraction * width))
        return "\u2593" * filled + "\u2591" * (width - filled)

    async def _cmd_watch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return
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
            # Single day: today
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
        if today in dates:
            limit_str = self.video_store.get_setting("daily_limit_minutes", "")
            if not limit_str and self.config:
                limit_min = self.config.watch_limits.daily_limit_minutes
            else:
                limit_min = int(limit_str) if limit_str else 0
            bounds = get_day_utc_bounds(today, self._get_tz())
            used = self.video_store.get_daily_watch_minutes(today, utc_bounds=bounds)

            bonus = 0
            bonus_date = self.video_store.get_setting("daily_bonus_date", "")
            if bonus_date == today:
                bonus = int(self.video_store.get_setting("daily_bonus_minutes", "0") or "0")

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
                lines.append(f"`{self._progress_bar(pct)}` {int(pct * 100)}%")
            lines.append("")

        # Pre-fetch all breakdowns
        all_breakdowns: dict[str, list[dict]] = {}
        daily_totals: dict[str, float] = {}
        for date_str in dates:
            bd = self.video_store.get_daily_watch_breakdown(date_str, utc_bounds=get_day_utc_bounds(date_str, self._get_tz()))
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
                bar = self._progress_bar(frac, bar_width)
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
                # Group by category (uncategorized treated as fun)
                by_cat: dict = {}
                for v in breakdown:
                    cat = v.get('category') or 'fun'
                    by_cat.setdefault(cat, []).append(v)

                for cat, cat_label in [("edu", "Educational"), ("fun", "Entertainment")]:
                    vids = by_cat.get(cat, [])
                    if not vids:
                        continue
                    cat_total = sum(v['minutes'] for v in vids)
                    cat_limit_str = self.video_store.get_setting(f"{cat}_limit_minutes", "")
                    cat_limit = int(cat_limit_str) if cat_limit_str else 0
                    if cat_limit > 0:
                        lines.append(f"\n**{cat_label}** \u2014 {int(cat_total)}/{cat_limit} min")
                        pct = min(1.0, cat_total / cat_limit) if cat_limit > 0 else 0
                        lines.append(f"`{self._progress_bar(pct)}` {int(pct * 100)}%")
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

        await update.message.reply_text(
            _md("\n".join(lines)), parse_mode=MD2, disable_web_page_preview=True,
        )

    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return
        stats = self.video_store.get_stats()
        await update.message.reply_text(_md(
            f"**BrainRotGuard Stats**\n\n"
            f"**Total videos:** {stats['total']}\n"
            f"**Pending:** {stats['pending']}\n"
            f"**Approved:** {stats['approved']}\n"
            f"**Denied:** {stats['denied']}\n"
            f"**Total views:** {stats['total_views']}"
        ), parse_mode=MD2)

    async def _cmd_changelog(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            await update.message.reply_text("Unauthorized.")
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
            await update.message.reply_text(latest)
        except FileNotFoundError:
            await update.message.reply_text("Changelog not available.")

    # --- Notification methods ---

    def _get_tz(self) -> str:
        """Return the configured timezone string (or empty for UTC)."""
        return self.config.watch_limits.timezone if self.config else ""

    async def notify_time_limit_reached(self, used_min: float, limit_min: int, category: str = "") -> None:
        """Send notification when daily time limit is reached (once per day per category)."""
        if not self._app:
            return
        today = get_today_str(self._get_tz())
        if self._limit_notified_cats.get(category) == today:
            return
        self._limit_notified_cats[category] = today
        cat_label = {"edu": "Educational", "fun": "Entertainment"}.get(category, "")
        cat_text = f" ({cat_label})" if cat_label else ""
        text = _md(
            f"**Daily watch limit reached{cat_text}**\n\n"
            f"**Used:** {int(used_min)} min / {limit_min} min limit\n"
            f"{'Videos in this category are' if cat_label else 'Videos are'} blocked until tomorrow."
        )
        try:
            await self._app.bot.send_message(
                chat_id=self.admin_chat_id,
                text=text,
                parse_mode=MD2,
            )
        except Exception as e:
            logger.error(f"Failed to send time limit notification: {e}")

    # --- /channel subcommands ---

    async def _cmd_channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return
        if not context.args:
            await self._channel_list(update)
            return
        sub = context.args[0].lower()
        rest = context.args[1:]

        if sub == "allow":
            await self._channel_allow(update, rest)
        elif sub == "unallow":
            await self._channel_unallow(update, rest)
        elif sub == "block":
            await self._channel_block(update, rest)
        elif sub == "unblock":
            await self._channel_unblock(update, rest)
        elif sub == "cat":
            await self._channel_set_cat(update, rest)
        elif sub == "starter":
            await self._channel_starter(update)
        else:
            await update.message.reply_text(
                "Usage: /channel allow|unallow|block|unblock|cat|starter <name>"
            )

    async def _channel_starter(self, update: Update) -> None:
        """Handle /channel starter — show importable starter channels."""
        if not self._starter_channels:
            await update.message.reply_text("No starter channels configured.")
            return
        text, markup = self._render_starter_message()
        await update.message.reply_text(
            text, parse_mode=MD2, reply_markup=markup, disable_web_page_preview=True,
        )

    _STARTER_PAGE_SIZE = 10

    def _render_starter_message(self, page: int = 0) -> tuple[str, InlineKeyboardMarkup | None]:
        """Build starter channels message with per-channel Import buttons and pagination."""
        existing = self.video_store.get_channel_handles_set()
        total = len(self._starter_channels)
        ps = self._STARTER_PAGE_SIZE
        start = page * ps
        end = min(start + ps, total)
        total_pages = (total + ps - 1) // ps

        header = f"**Starter Channels** ({total})"
        if total_pages > 1:
            header += f" \u00b7 pg {page + 1}/{total_pages}"
        lines = [header, ""]
        buttons = []
        for idx in range(start, end):
            ch = self._starter_channels[idx]
            handle = ch["handle"]
            name = ch["name"]
            cat = ch.get("category") or ""
            desc = ch.get("description") or ""
            url = f"https://www.youtube.com/{handle}"
            cat_badge = f" [{cat}]" if cat else ""
            lines.append(f"[{name}]({url}){cat_badge}")
            if desc:
                lines.append(f"_{desc}_")
            if handle.lower() in existing:
                lines.append("\u2705 _imported_\n")
            else:
                lines.append("")
                buttons.append([InlineKeyboardButton(
                    f"Import: {name}", callback_data=f"starter_import:{idx}",
                )])

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("\u25c0 Back", callback_data=f"starter_page:{page - 1}"))
        if end < total:
            remaining = total - end
            nav.append(InlineKeyboardButton(
                f"Show more ({remaining})", callback_data=f"starter_page:{page + 1}",
            ))
        if nav:
            buttons.append(nav)
        markup = InlineKeyboardMarkup(buttons) if buttons else None
        return _md("\n".join(lines)), markup

    async def _cb_starter_page(self, query, page: int) -> None:
        """Handle starter channels pagination."""
        await query.answer()
        text, markup = self._render_starter_message(page)
        await query.edit_message_text(
            text=text, reply_markup=markup, parse_mode=MD2,
            disable_web_page_preview=True,
        )

    async def _cb_starter_import(self, query, idx: int) -> None:
        """Handle Import button press from starter channels message."""
        if idx < 0 or idx >= len(self._starter_channels):
            await query.answer("Invalid channel.")
            return
        ch = self._starter_channels[idx]
        handle = ch["handle"]
        name = ch["name"]
        cat = ch.get("category")

        # Idempotency: already imported?
        existing = self.video_store.get_channel_handles_set()
        already = handle.lower() in existing
        if not already:
            self.video_store.add_channel(name, "allowed", channel_id=None, handle=handle, category=cat)
            if self.on_channel_change:
                self.on_channel_change()

        # Re-render first (priority: update the UI before the toast)
        page = idx // self._STARTER_PAGE_SIZE
        text, markup = self._render_starter_message(page)
        try:
            await query.edit_message_text(
                text=text, reply_markup=markup, parse_mode=MD2,
                disable_web_page_preview=True,
            )
        except Exception:
            pass  # Message unchanged (all already imported)

        # Toast notification (non-critical, may time out)
        try:
            msg = f"Already imported: {name}" if already else f"Imported: {name}"
            await query.answer(msg)
        except Exception:
            pass

    async def _channel_allow(self, update: Update, args: list[str]) -> None:
        if not args:
            await update.message.reply_text("Usage: /channel allow @handle\nExample: /channel allow @MarkRober")
            return
        raw = args[0]
        if not raw.startswith("@"):
            await update.message.reply_text(
                "Please use the channel's @handle (e.g. @MarkRober).\n"
                "You can find it on the channel's YouTube page."
            )
            return
        await update.message.reply_text(f"Resolving {raw}...")
        from youtube.extractor import resolve_channel_handle
        info = await resolve_channel_handle(raw)
        if not info or not info.get("channel_name"):
            await update.message.reply_text(f"Could not find a YouTube channel for {raw}")
            return
        channel_name = info["channel_name"]
        channel_id = info.get("channel_id")
        handle = info.get("handle")
        cat = None
        if len(args) > 1 and args[1].lower() in ("edu", "fun"):
            cat = args[1].lower()
        self.video_store.add_channel(channel_name, "allowed", channel_id=channel_id, handle=handle, category=cat)
        if self.on_channel_change:
            self.on_channel_change()
        cat_label = {"edu": "Educational", "fun": "Entertainment"}.get(cat, "No category")
        await update.message.reply_text(
            f"Allowlisted: {channel_name}\n"
            f"Handle: {raw}\n"
            f"Channel ID: {channel_id or 'unknown'}\n"
            f"Category: {cat_label}"
        )

    async def _channel_unallow(self, update: Update, args: list[str]) -> None:
        if not args:
            await update.message.reply_text("Usage: /channel unallow <channel name>")
            return
        channel = " ".join(args)
        if self.video_store.remove_channel(channel):
            if self.on_channel_change:
                self.on_channel_change()
            await update.message.reply_text(f"Removed from allowlist: {channel}")
        else:
            await update.message.reply_text(f"Not found: {channel}")

    async def _channel_block(self, update: Update, args: list[str]) -> None:
        if not args:
            await update.message.reply_text("Usage: /channel block @handle\nExample: /channel block @Slurry")
            return
        raw = args[0]
        if not raw.startswith("@"):
            await update.message.reply_text(
                "Please use the channel's @handle (e.g. @Slurry).\n"
                "You can find it on the channel's YouTube page."
            )
            return
        await update.message.reply_text(f"Resolving {raw}...")
        from youtube.extractor import resolve_channel_handle
        info = await resolve_channel_handle(raw)
        if not info or not info.get("channel_name"):
            await update.message.reply_text(f"Could not find a YouTube channel for {raw}")
            return
        channel_name = info["channel_name"]
        channel_id = info.get("channel_id")
        handle = info.get("handle")
        self.video_store.add_channel(channel_name, "blocked", channel_id=channel_id, handle=handle)
        if self.on_channel_change:
            self.on_channel_change()
        await update.message.reply_text(f"Blocked: {channel_name}")

    async def _channel_unblock(self, update: Update, args: list[str]) -> None:
        if not args:
            await update.message.reply_text("Usage: /channel unblock <channel name>")
            return
        channel = " ".join(args)
        if self.video_store.remove_channel(channel):
            if self.on_channel_change:
                self.on_channel_change()
            await update.message.reply_text(f"Unblocked: {channel}")
        else:
            await update.message.reply_text(f"Not found: {channel}")

    async def _channel_set_cat(self, update: Update, args: list[str]) -> None:
        """Handle /channel cat <name> edu|fun."""
        if len(args) < 2:
            await update.message.reply_text("Usage: /channel cat <channel name> edu|fun")
            return
        cat = args[-1].lower()
        if cat not in ("edu", "fun"):
            await update.message.reply_text("Category must be `edu` or `fun`.")
            return
        raw = " ".join(args[:-1])
        channel = self.video_store.resolve_channel_name(raw) or raw
        if self.video_store.set_channel_category(channel, cat):
            self.video_store.set_channel_videos_category(channel, cat)
            cat_label = "Educational" if cat == "edu" else "Entertainment"
            if self.on_channel_change:
                self.on_channel_change()
            await update.message.reply_text(f"**{channel}** → {cat_label}", parse_mode=MD2)
        else:
            await update.message.reply_text(f"Channel not found: {raw}")

    _CHANNEL_PAGE_SIZE = 10

    def _render_channel_page(self, page: int = 0) -> tuple[str, InlineKeyboardMarkup | None]:
        """Build text + inline buttons for a page of the channel list."""
        allowed = self.video_store.get_channels_with_ids("allowed")
        blocked = self.video_store.get_channels_with_ids("blocked")
        if not allowed and not blocked:
            return "No channels configured.", None

        # Build flat list: (channel_name, channel_id, handle, category, status)
        entries = [(ch, cid, h, cat, "allowed") for ch, cid, h, cat in allowed]
        entries += [(ch, cid, h, cat, "blocked") for ch, cid, h, cat in blocked]
        total = len(entries)
        page_size = self._CHANNEL_PAGE_SIZE
        start = page * page_size
        end = min(start + page_size, total)
        page_entries = entries[start:end]

        lines = [f"**Channels** ({total} total)\n"]
        buttons = []
        for ch, cid, handle, cat, status in page_entries:
            label = "allowed" if status == "allowed" else "blocked"
            cat_tag = f" [{cat}]" if cat else ""
            if cid:
                url = f"https://www.youtube.com/channel/{cid}"
            elif handle:
                url = f"https://www.youtube.com/{handle}"
            else:
                url = f"https://www.youtube.com/results?search_query={quote(ch)}"
            handle_tag = f" `{handle}`" if handle else ""
            lines.append(f"  [{ch}]({url}){handle_tag} *{label}{cat_tag}*")
            btn_label = f"Unallow: {ch}" if status == "allowed" else f"Unblock: {ch}"
            btn_action = "unallow" if status == "allowed" else "unblock"
            buttons.append([InlineKeyboardButton(
                btn_label, callback_data=f"{btn_action}:{ch}"
            )])

        # Navigation row
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("\u25c0 Back", callback_data=f"chan_page:{page - 1}"))
        remaining = total - end
        if remaining > 0:
            nav.append(InlineKeyboardButton(
                f"Show more ({remaining})", callback_data=f"chan_page:{page + 1}",
            ))
        if nav:
            buttons.append(nav)

        text = _md("\n".join(lines))
        markup = InlineKeyboardMarkup(buttons) if buttons else None
        return text, markup

    async def _channel_list(self, update: Update) -> None:
        text, markup = self._render_channel_page(0)
        await update.message.reply_text(
            text, parse_mode=MD2, disable_web_page_preview=True,
            reply_markup=markup,
        )

    async def _cb_channel_page(self, query, page: int) -> None:
        """Handle channel list pagination."""
        text, markup = self._render_channel_page(page)
        await query.edit_message_text(
            text, parse_mode=MD2, disable_web_page_preview=True,
            reply_markup=markup,
        )

    async def _update_channel_list_message(self, query) -> None:
        """Refresh the channel list message after a button press (stay on page 0)."""
        text, markup = self._render_channel_page(0)
        await query.edit_message_text(
            text, parse_mode=MD2, disable_web_page_preview=True,
            reply_markup=markup,
        )

    # --- Activity report ---

    _LOGS_PAGE_SIZE = 10

    async def _cmd_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return
        days = 7
        if context.args:
            arg = context.args[0].lower()
            if arg == "today":
                days = 1
            elif arg.isdigit():
                days = min(int(arg), 365)
        activity = self.video_store.get_recent_activity(days)
        if not activity:
            period = "today" if days == 1 else f"last {days} days"
            await update.message.reply_text(f"No activity in the {period}.")
            return
        text, keyboard = self._render_logs_page(activity, days, 0)
        await update.message.reply_text(text, parse_mode=MD2, reply_markup=keyboard)

    def _render_logs_page(self, activity: list, days: int, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
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

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("\u25c0 Back", callback_data=f"logs_page:{days}:{page - 1}"))
        remaining = total - end
        if remaining > 0:
            nav.append(InlineKeyboardButton(
                f"Show more ({remaining})", callback_data=f"logs_page:{days}:{page + 1}",
            ))
        keyboard = InlineKeyboardMarkup([nav]) if nav else None
        return _md("\n".join(lines)), keyboard

    async def _cb_logs_page(self, query, days: int, page: int) -> None:
        """Handle logs pagination."""
        activity = self.video_store.get_recent_activity(days)
        if not activity:
            await query.answer("No activity.")
            return
        await query.answer()
        text, keyboard = self._render_logs_page(activity, days, page)
        await query.edit_message_text(text=text, parse_mode=MD2, reply_markup=keyboard)

    # --- /search subcommands ---

    async def _cmd_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return
        if not context.args:
            await self._search_filter_list(update)
            return
        sub = context.args[0].lower()
        rest = context.args[1:]

        if sub == "history":
            await self._search_history(update, rest)
        elif sub == "filter":
            await self._search_filter(update, rest)
        else:
            await update.message.reply_text(
                "Usage: /search history|filter"
            )

    _SEARCH_PAGE_SIZE = 20

    async def _search_history(self, update: Update, args: list[str]) -> None:
        days = 7
        if args:
            arg = args[0].lower()
            if arg == "today":
                days = 1
            elif arg.isdigit():
                days = min(int(arg), 365)
        searches = self.video_store.get_recent_searches(days)
        if not searches:
            period = "today" if days == 1 else f"last {days} days"
            await update.message.reply_text(f"No searches in the {period}.")
            return
        text, keyboard = self._render_search_page(searches, days, 0)
        await update.message.reply_text(
            text, parse_mode=MD2, reply_markup=keyboard, disable_web_page_preview=True,
        )

    def _render_search_page(self, searches: list, days: int, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
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

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("\u25c0 Back", callback_data=f"search_page:{days}:{page - 1}"))
        remaining = total - end
        if remaining > 0:
            nav.append(InlineKeyboardButton(
                f"Show more ({remaining})", callback_data=f"search_page:{days}:{page + 1}",
            ))
        keyboard = InlineKeyboardMarkup([nav]) if nav else None
        return _md("\n".join(lines)), keyboard

    async def _cb_search_page(self, query, days: int, page: int) -> None:
        """Handle search history pagination."""
        searches = self.video_store.get_recent_searches(days)
        if not searches:
            await query.answer("No searches.")
            return
        await query.answer()
        text, keyboard = self._render_search_page(searches, days, page)
        await query.edit_message_text(
            text=text, parse_mode=MD2, reply_markup=keyboard, disable_web_page_preview=True,
        )

    async def _search_filter(self, update: Update, args: list[str]) -> None:
        if not args:
            await self._search_filter_list(update)
            return
        action = args[0].lower()
        if action == "list":
            await self._search_filter_list(update)
            return
        if len(args) < 2:
            await update.message.reply_text("Usage: /search filter add|remove <word>")
            return
        word = " ".join(args[1:])
        if action == "add":
            if self.video_store.add_word_filter(word):
                await update.message.reply_text(f"Filter added: \"{word}\"")
            else:
                await update.message.reply_text(f"Already filtered: \"{word}\"")
        elif action in ("remove", "rm", "del"):
            if self.video_store.remove_word_filter(word):
                await update.message.reply_text(f"Filter removed: \"{word}\"")
            else:
                await update.message.reply_text(f"Not found: \"{word}\"")
        else:
            await update.message.reply_text("Usage: /search filter add|remove <word>")

    async def _search_filter_list(self, update: Update) -> None:
        words = self.video_store.get_word_filters()
        if not words:
            await update.message.reply_text("No word filters set.")
            return
        lines = ["**Filtered Words:**\n"]
        for w in words:
            lines.append(f"- `{w}`")
        await update.message.reply_text(_md("\n".join(lines)), parse_mode=MD2)

    # --- Time limit command ---

    async def _cmd_timelimit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return
        if context.args:
            arg = context.args[0].lower()

            # /time start <time|off>
            if arg == "start":
                await self._time_schedule(update, context.args[1:], "schedule_start")
                return
            # /time stop <time|off>
            if arg == "stop":
                await self._time_schedule(update, context.args[1:], "schedule_end")
                return

            # /time add <minutes> — daily bonus (today only, stacks)
            if arg == "add":
                await self._time_add_bonus(update, context.args[1:])
                return

            # /time edu <minutes|off> — set educational category limit
            if arg == "edu":
                await self._time_set_category_limit(update, context.args[1:], "edu")
                return
            # /time fun <minutes|off> — set entertainment category limit
            if arg == "fun":
                await self._time_set_category_limit(update, context.args[1:], "fun")
                return

            if arg == "off":
                self.video_store.set_setting("daily_limit_minutes", "0")
                await update.message.reply_text("Watch time limit disabled.")
                return
            elif arg.isdigit():
                minutes = int(arg)
                self.video_store.set_setting("daily_limit_minutes", str(minutes))
                await update.message.reply_text(f"Daily limit set to {minutes} minutes.")
                return
            else:
                await update.message.reply_text(
                    "Usage: /time [minutes|off]\n"
                    "       /time start <time|off>\n"
                    "       /time stop <time|off>\n"
                    "       /time add <minutes>\n"
                    "       /time edu|fun <minutes|off>"
                )
                return

        # Show current status
        limit_str = self.video_store.get_setting("daily_limit_minutes", "")
        if not limit_str and self.config:
            limit_min = self.config.watch_limits.daily_limit_minutes
        else:
            limit_min = int(limit_str) if limit_str else 0
        today = get_today_str(self._get_tz())
        bounds = get_day_utc_bounds(today, self._get_tz())
        used = self.video_store.get_daily_watch_minutes(today, utc_bounds=bounds)

        # Check for today's bonus
        bonus = 0
        bonus_date = self.video_store.get_setting("daily_bonus_date", "")
        if bonus_date == today:
            bonus = int(self.video_store.get_setting("daily_bonus_minutes", "0") or "0")

        lines = []
        if limit_min == 0:
            lines.append(f"**Watch limit:** OFF")
            lines.append(f"**Watched today:** {int(used)} min")
        else:
            effective = limit_min + bonus
            remaining = max(0, effective - used)
            lines.append(f"**Daily limit:** {limit_min} min")
            if bonus > 0:
                lines.append(f"**Bonus today:** +{bonus} min")
            lines.append(f"**Used today:** {int(used)} min")
            lines.append(f"**Remaining:** {int(remaining)} min")

        # Per-category limits
        cat_usage = self.video_store.get_daily_watch_by_category(today, utc_bounds=bounds)
        for cat, cat_label in [("edu", "Educational"), ("fun", "Entertainment")]:
            cat_limit_str = self.video_store.get_setting(f"{cat}_limit_minutes", "")
            cat_limit = int(cat_limit_str) if cat_limit_str else 0
            cat_used = cat_usage.get(cat, 0.0)
            # Include uncategorized in fun
            if cat == "fun":
                cat_used += cat_usage.get(None, 0.0)
            if cat_limit == 0:
                lines.append(f"\n**{cat_label}:** {int(cat_used)} min watched (no limit)")
            else:
                cat_remaining = max(0, cat_limit - cat_used)
                lines.append(f"\n**{cat_label}:** {int(cat_used)}/{cat_limit} min")
                pct = min(1.0, cat_used / cat_limit) if cat_limit > 0 else 0
                lines.append(f"`{self._progress_bar(pct)}` {int(pct * 100)}%")

        # Schedule info
        sched_start = self.video_store.get_setting("schedule_start", "")
        sched_end = self.video_store.get_setting("schedule_end", "")
        if sched_start or sched_end:
            start_display = format_time_12h(sched_start) if sched_start else "not set"
            end_display = format_time_12h(sched_end) if sched_end else "not set"
            lines.append(f"\n**Schedule:** {start_display} \u2013 {end_display}")
            allowed, unlock_time = is_within_schedule(
                sched_start, sched_end, self._get_tz(),
            )
            if allowed:
                lines.append("**Status:** OPEN")
            else:
                lines.append(f"**Status:** CLOSED (unlocks at {unlock_time})")
        else:
            lines.append("\n**Schedule:** not set")

        await update.message.reply_text(_md("\n".join(lines)), parse_mode=MD2)

    async def _time_add_bonus(self, update: Update, args: list[str]) -> None:
        """Handle /time add <minutes> — grant bonus screen time for today only."""
        if not args or not args[0].isdigit():
            await update.message.reply_text("Usage: /time add <minutes>")
            return
        add_min = int(args[0])
        if add_min <= 0:
            await update.message.reply_text("Minutes must be positive.")
            return
        today = get_today_str(self._get_tz())
        bonus_date = self.video_store.get_setting("daily_bonus_date", "")
        if bonus_date == today:
            existing = int(self.video_store.get_setting("daily_bonus_minutes", "0") or "0")
        else:
            existing = 0
        new_bonus = existing + add_min
        self.video_store.set_setting("daily_bonus_minutes", str(new_bonus))
        self.video_store.set_setting("daily_bonus_date", today)
        await update.message.reply_text(
            f"Added {add_min} bonus minutes for today (+{new_bonus} total)."
        )

    async def _time_set_category_limit(self, update: Update, args: list[str], category: str) -> None:
        """Handle /time edu|fun <minutes|off>."""
        cat_label = "Educational" if category == "edu" else "Entertainment"
        setting_key = f"{category}_limit_minutes"
        if not args:
            current = self.video_store.get_setting(setting_key, "")
            limit = int(current) if current else 0
            if limit == 0:
                await update.message.reply_text(f"{cat_label} limit: OFF (unlimited)")
            else:
                await update.message.reply_text(f"{cat_label} limit: {limit} minutes/day")
            return
        value = args[0].lower()
        if value in ("off", "0"):
            self.video_store.set_setting(setting_key, "0")
            await update.message.reply_text(f"{cat_label} limit disabled (unlimited).")
            return
        if value.isdigit():
            minutes = int(value)
            self.video_store.set_setting(setting_key, str(minutes))
            await update.message.reply_text(f"{cat_label} limit set to {minutes} minutes/day.")
            return
        await update.message.reply_text(f"Usage: /time {category} <minutes|off>")

    async def _time_schedule(
        self, update: Update, args: list[str], setting_key: str,
    ) -> None:
        """Handle /time start|stop subcommands."""
        label = "Start" if setting_key == "schedule_start" else "Stop"
        if not args:
            current = self.video_store.get_setting(setting_key, "")
            if current:
                await update.message.reply_text(f"{label} time: {format_time_12h(current)}")
            else:
                await update.message.reply_text(f"{label} time: not set")
            return

        value = args[0].lower()
        if value == "off":
            self.video_store.set_setting(setting_key, "")
            await update.message.reply_text(f"{label} time cleared.")
            return

        parsed = parse_time_input(args[0])
        if not parsed:
            await update.message.reply_text(
                f"Invalid time. Examples: 800am, 8:00, 2000, 8:00PM"
            )
            return

        self.video_store.set_setting(setting_key, parsed)
        await update.message.reply_text(
            f"{label} time set to {format_time_12h(parsed)}"
        )
