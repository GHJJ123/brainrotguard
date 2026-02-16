"""BrainRotGuard Telegram Bot - parent approval for YouTube videos."""

import logging
from io import BytesIO
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

from utils import get_today_str, parse_time_input, format_time_12h, is_within_schedule
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

    def __init__(self, bot_token: str, admin_chat_id: str, video_store, config=None):
        self.bot_token = bot_token
        self.admin_chat_id = admin_chat_id
        self.video_store = video_store
        self.config = config
        self._app = None
        self._limit_notified_today = None  # date string of last limit notification
        self.on_channel_change = None  # callback when channel lists change
        self.on_video_change = None  # callback when video status changes

    def _check_admin(self, update: Update) -> bool:
        """Check if message is from admin (parent)."""
        if not self.admin_chat_id:
            return False
        return str(update.effective_user.id) == str(self.admin_chat_id)

    async def start(self) -> None:
        """Start the bot."""
        logger.info("Starting BrainRotGuard bot...")
        self._app = ApplicationBuilder().token(self.bot_token).build()

        self._app.add_handler(CommandHandler("start", self._cmd_help))
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
                InlineKeyboardButton("Approve", callback_data=f"approve:{video_id}"),
                InlineKeyboardButton("Deny", callback_data=f"deny:{video_id}"),
            ],
            [
                InlineKeyboardButton("Allow Channel", callback_data=f"allowchan:{video_id}"),
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
        except (ValueError, IndexError):
            await query.answer("Invalid callback.")
            return

        # Channel management callbacks (unallow:name or unblock:name)
        if parts[0] in ("unallow", "unblock") and len(parts) == 2:
            ch_name = parts[1]
            if self.video_store.remove_channel(ch_name):
                if self.on_channel_change:
                    self.on_channel_change()
                await query.answer(f"Removed: {ch_name}")
                # Refresh the channel list message
                await self._update_channel_list_message(query)
            else:
                await query.answer(f"Not found: {ch_name}")
            return

        if len(parts) != 2:
            await query.answer("Invalid callback.")
            return

        action, video_id = parts
        video = self.video_store.get_video(video_id)
        if not video:
            await query.answer("Video not found.")
            return

        yt_link = f"https://www.youtube.com/watch?v={video_id}"
        duration = format_duration(video.get('duration'))

        if action == "approve" and video['status'] == 'pending':
            self.video_store.update_status(video_id, "approved")
            await query.answer("Approved!")
            status_label = "APPROVED"
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
            self.video_store.add_channel(channel, "allowed", channel_id=video.get('channel_id'))
            if video['status'] == 'pending':
                self.video_store.update_status(video_id, "approved")
            await query.answer(f"Allowlisted: {channel}")
            status_label = "APPROVED + CHANNEL ALLOWED"
            if self.on_channel_change:
                self.on_channel_change()
        elif action == "blockchan":
            channel = video['channel_name']
            self.video_store.add_channel(channel, "blocked", channel_id=video.get('channel_id'))
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

        # After approval, show Revoke button; otherwise remove all buttons
        if status_label == "APPROVED":
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("Revoke", callback_data=f"revoke:{video_id}"),
            ]])
        else:
            reply_markup = None

        try:
            await query.edit_message_caption(caption=result_text, reply_markup=reply_markup, parse_mode=MD2)
        except Exception:
            await query.edit_message_text(text=result_text, reply_markup=reply_markup, parse_mode=MD2)

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
            "`/channel allow @handle`\n"
            "`/channel unallow|block|unblock <name>`\n\n"
            "**Search:**\n"
            "`/search` - List word filters\n"
            "`/search history [days|today|all]`\n"
            "`/search filter add|remove <word>`\n\n"
            "`/time [min|off]` - Watch limit\n"
            "`/time add <min>` - Bonus for today\n"
            "`/time start|stop [time|off]` - Schedule\n"
            "`/changelog` - Latest changes"
        ), parse_mode=MD2)

    async def _cmd_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return
        pending = self.video_store.get_pending()
        if not pending:
            await update.message.reply_text("No pending requests.")
            return
        lines = ["**Pending Requests:**\n"]
        for v in pending:
            ch = _channel_md_link(v['channel_name'], v.get('channel_id'))
            lines.append(f"- {v['title']} _{ch}_")
        await update.message.reply_text(_md("\n".join(lines)), parse_mode=MD2)

    _APPROVED_PAGE_SIZE = 10

    async def _cmd_approved(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return
        approved = self.video_store.get_approved()
        if not approved:
            await update.message.reply_text("No approved videos.")
            return
        text, keyboard = self._render_approved_page(approved, 0)
        await update.message.reply_text(
            text, parse_mode=MD2, reply_markup=keyboard, disable_web_page_preview=True,
        )

    def _render_approved_page(self, approved: list, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
        """Render a page of the approved list."""
        total = len(approved)
        ps = self._APPROVED_PAGE_SIZE
        start = page * ps
        end = min(start + ps, total)
        page_items = approved[start:end]
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
        remaining = total - end
        if remaining > 0:
            nav.append(InlineKeyboardButton(
                f"Show more ({remaining})", callback_data=f"approved_page:{page + 1}",
            ))
        keyboard = InlineKeyboardMarkup([nav]) if nav else None
        return _md("\n".join(lines)), keyboard

    async def _cb_approved_page(self, query, page: int) -> None:
        """Handle approved list pagination."""
        approved = self.video_store.get_approved()
        if not approved:
            await query.answer("No approved videos.")
            return
        await query.answer()
        text, keyboard = self._render_approved_page(approved, page)
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
                days = int(arg)

        tz = self._get_tz()
        from datetime import timedelta
        import datetime as _dt

        if days == 0:
            # Single day: today
            today = get_today_str(tz)
            dates = [today]
            header = "Today's Watch Activity"
        elif days == 1:
            yesterday = (_dt.datetime.now(tz) - timedelta(days=1)).strftime("%Y-%m-%d")
            dates = [yesterday]
            header = "Yesterday's Watch Activity"
        else:
            dates = [
                (_dt.datetime.now(tz) - timedelta(days=i)).strftime("%Y-%m-%d")
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
                limit_min = int(limit_str) if limit_str else 120
            used = self.video_store.get_daily_watch_minutes(today)

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

        # Per-day breakdown
        for date_str in dates:
            breakdown = self.video_store.get_daily_watch_breakdown(date_str)
            if not breakdown:
                if len(dates) == 1:
                    lines.append("_No videos watched._")
                continue

            if len(dates) > 1:
                total_day = sum(v['minutes'] for v in breakdown)
                lines.append(f"**{date_str}** \u2014 {int(total_day)} min total")

            for v in breakdown:
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

            if len(dates) > 1:
                lines.append("")

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

    async def notify_time_limit_reached(self, used_min: float, limit_min: int) -> None:
        """Send notification when daily time limit is reached (once per day)."""
        if not self._app:
            return
        today = get_today_str(self._get_tz())
        if self._limit_notified_today == today:
            return
        self._limit_notified_today = today
        try:
            await self._app.bot.send_message(
                chat_id=self.admin_chat_id,
                text=_md(
                    f"**Daily watch limit reached**\n\n"
                    f"**Used:** {int(used_min)} min / {limit_min} min limit\n"
                    f"Videos are blocked until tomorrow."
                ),
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
        else:
            await update.message.reply_text(
                "Usage: /channel allow|unallow|block|unblock <name>"
            )

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
        self.video_store.add_channel(channel_name, "allowed", channel_id=channel_id, handle=handle)
        if self.on_channel_change:
            self.on_channel_change()
        await update.message.reply_text(
            f"Allowlisted: {channel_name}\n"
            f"Handle: {raw}\n"
            f"Channel ID: {channel_id or 'unknown'}"
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
            await update.message.reply_text("Usage: /channel block <channel name>")
            return
        channel = " ".join(args)
        self.video_store.add_channel(channel, "blocked")
        if self.on_channel_change:
            self.on_channel_change()
        await update.message.reply_text(f"Blocked: {channel}")

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

    _CHANNEL_PAGE_SIZE = 10

    def _render_channel_page(self, page: int = 0) -> tuple[str, InlineKeyboardMarkup | None]:
        """Build text + inline buttons for a page of the channel list."""
        allowed = self.video_store.get_channels_with_ids("allowed")
        blocked = self.video_store.get_channels_with_ids("blocked")
        if not allowed and not blocked:
            return "No channels configured.", None

        # Build flat list: (channel_name, channel_id, handle, status)
        entries = [(ch, cid, h, "allowed") for ch, cid, h in allowed]
        entries += [(ch, cid, h, "blocked") for ch, cid, h in blocked]
        total = len(entries)
        page_size = self._CHANNEL_PAGE_SIZE
        start = page * page_size
        end = min(start + page_size, total)
        page_entries = entries[start:end]

        lines = [f"**Channels** ({total} total)\n"]
        buttons = []
        for ch, cid, handle, status in page_entries:
            label = "allowed" if status == "allowed" else "blocked"
            if cid:
                url = f"https://www.youtube.com/channel/{cid}"
            elif handle:
                url = f"https://www.youtube.com/{handle}"
            else:
                url = f"https://www.youtube.com/results?search_query={quote(ch)}"
            lines.append(f"  [{ch}]({url}) *{label}*")
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
                days = int(arg)
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
                days = int(arg)
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
                    "       /time add <minutes>"
                )
                return

        # Show current status
        limit_str = self.video_store.get_setting("daily_limit_minutes", "")
        if not limit_str and self.config:
            limit_min = self.config.watch_limits.daily_limit_minutes
        else:
            limit_min = int(limit_str) if limit_str else 120
        today = get_today_str(self._get_tz())
        used = self.video_store.get_daily_watch_minutes(today)

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
