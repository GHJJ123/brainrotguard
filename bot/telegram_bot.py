"""BrainRotGuard Telegram Bot - parent approval for YouTube videos."""

import asyncio
import logging
import re
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

from data.child_store import ChildStore
from utils import (
    get_today_str, get_day_utc_bounds, get_weekday, parse_time_input,
    format_time_12h, is_within_schedule, DAY_NAMES, DAY_GROUPS,
)
from youtube.extractor import format_duration

logger = logging.getLogger(__name__)

MD2 = "MarkdownV2"

_GITHUB_REPO = "GHJJ123/brainrotguard"
_UPDATE_CHECK_INTERVAL = 43200  # 12 hours


def _md(text: str) -> str:
    """Convert markdown to Telegram MarkdownV2 format."""
    try:
        return telegramify_markdown.markdownify(text)
    except Exception:
        return text


def _answer_bg(query, text: str = "") -> None:
    """Fire answerCallbackQuery in background so it never blocks the message edit."""
    async def _do():
        try:
            await query.answer(text)
        except Exception:
            pass
    asyncio.create_task(_do())


def _nav_row(page: int, total: int, page_size: int, callback_prefix: str) -> list | None:
    """Build a pagination nav row with Back/Next buttons (disabled placeholders when at bounds).

    Returns a list of two InlineKeyboardButtons, or None if everything fits on one page.
    callback_prefix should produce valid callback_data when appended with :{page}.
    """
    if total <= page_size:
        return None
    end = min((page + 1) * page_size, total)
    has_next = end < total
    return [
        InlineKeyboardButton("\u25c0 Back", callback_data=f"{callback_prefix}:{page - 1}") if page > 0
        else InlineKeyboardButton(" ", callback_data="noop"),
        InlineKeyboardButton("Next \u25b6", callback_data=f"{callback_prefix}:{page + 1}") if has_next
        else InlineKeyboardButton(" ", callback_data="noop"),
    ]


async def _edit_msg(query, text: str, markup=None, disable_preview: bool = False) -> None:
    """Edit a callback query message, silently ignoring timeouts/conflicts."""
    try:
        await query.edit_message_text(
            text=text, parse_mode=MD2, reply_markup=markup,
            disable_web_page_preview=disable_preview,
        )
    except Exception:
        pass


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

    def _wizard_store(self, chat_id: int) -> 'ChildStore':
        """Get the ChildStore for an active wizard, based on stored profile_id."""
        state = self._pending_wizard.get(chat_id, {})
        pid = state.get("profile_id", "default")
        return self._child_store(pid)

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
            await update.message.reply_text("No profiles. Use /child add <name> to create one.")
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
        await update.message.reply_text("Which child?", reply_markup=keyboard)

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
            await update.message.reply_text(msg)
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

    async def notify_new_request(self, video: dict, profile_id: str = "default") -> None:
        """Send parent a notification about a new video request with Approve/Deny buttons."""
        if not self._app:
            logger.warning("Bot not started, cannot send notification")
            return

        video_id = video['video_id']
        title = video['title']
        channel_link = _channel_md_link(video['channel_name'], video.get('channel_id'))
        duration = format_duration(video.get('duration'))
        is_short = video.get('is_short')
        if is_short:
            yt_link = f"https://www.youtube.com/shorts/{video_id}"
        else:
            yt_link = f"https://www.youtube.com/watch?v={video_id}"

        # Include child name in notification if multiple profiles exist
        profiles = self._get_profiles()
        child_name = ""
        if len(profiles) > 1:
            p = self.video_store.get_profile(profile_id)
            child_name = p["display_name"] if p else profile_id

        # Check if already approved for another child
        other = self.video_store.find_video_approved_for_others(video_id, profile_id)
        cross_child_note = ""
        if other and len(profiles) > 1:
            other_profile = self.video_store.get_profile(other["profile_id"])
            other_name = other_profile["display_name"] if other_profile else other["profile_id"]
            cross_child_note = f"\n_Already approved for {other_name}_"

        short_label = " [SHORT]" if is_short else ""
        from_label = f" from {child_name}" if child_name else ""
        caption = _md(
            f"**New Video Request{short_label}{from_label}**\n\n"
            f"**Title:** {title}\n"
            f"**Channel:** {channel_link}\n"
            f"**Duration:** {duration}\n"
            f"[Watch on YouTube]({yt_link}){cross_child_note}"
        )

        # Use profile_id in callback data — short enough to fit 64-byte limit
        # Format: action:profile_id:video_id (profile_id max ~20 chars)
        pid = profile_id
        buttons = [
            [InlineKeyboardButton("Watch on YouTube", url=yt_link)],
        ]
        # If cross-child approved, show auto-approve button
        if other and len(profiles) > 1:
            buttons.append([
                InlineKeyboardButton("Auto-approve", callback_data=f"autoapprove:{pid}:{video_id}"),
            ])
        buttons.extend([
            [
                InlineKeyboardButton("Approve (Edu)", callback_data=f"approve_edu:{pid}:{video_id}"),
                InlineKeyboardButton("Approve (Fun)", callback_data=f"approve_fun:{pid}:{video_id}"),
            ],
            [
                InlineKeyboardButton("Deny", callback_data=f"deny:{pid}:{video_id}"),
            ],
            [
                InlineKeyboardButton("Allow Ch (Edu)", callback_data=f"allowchan_edu:{pid}:{video_id}"),
                InlineKeyboardButton("Allow Ch (Fun)", callback_data=f"allowchan_fun:{pid}:{video_id}"),
            ],
            [
                InlineKeyboardButton("Block Channel", callback_data=f"blockchan:{pid}:{video_id}"),
            ],
        ])
        keyboard = InlineKeyboardMarkup(buttons)

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
            if parts[0] == "approved_page" and len(parts) == 2:
                await self._cb_approved_page(query, int(parts[1]))
                return
            if parts[0] == "logs_page" and len(parts) == 3:
                await self._cb_logs_page(query, int(parts[1]), int(parts[2]))
                return
            if parts[0] == "search_page" and len(parts) == 3:
                await self._cb_search_page(query, int(parts[1]), int(parts[2]))
                return
            if parts[0] == "chan_page" and len(parts) == 3 and parts[1] in ("allowed", "blocked"):
                await self._cb_channel_page(query, parts[1], int(parts[2]))
                return
            if parts[0] == "chan_filter" and len(parts) == 2 and parts[1] in ("allowed", "blocked"):
                await self._cb_channel_filter(query, parts[1])
                return
            if parts[0] == "chan_menu" and len(parts) == 1:
                await self._cb_channel_menu(query)
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
            _answer_bg(query, "Got it!" if parts[1] == "no" else "")
            if parts[1] == "yes":
                text, markup = self._render_starter_message()
                await _edit_msg(query, text, markup, disable_preview=True)
            else:
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass
            return

        # Starter channel import
        if parts[0] == "starter_import" and len(parts) == 2:
            try:
                await self._cb_starter_import(query, int(parts[1]))
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

        # Channel management callbacks (unallow:name or unblock:name)
        # Channel names may contain colons, so rejoin everything after first ':'
        if parts[0] in ("unallow", "unblock") and len(parts) >= 2:
            ch_name = ":".join(parts[1:])
            # Look up channel_id before removing (remove_channel deletes the row)
            ch_id = ""
            ch_rows = self.video_store.get_channels_with_ids(
                "allowed" if parts[0] == "unallow" else "blocked"
            )
            for name, cid, _h, _c in ch_rows:
                if name.lower() == ch_name.lower():
                    ch_id = cid or ""
                    break
            if self.video_store.remove_channel(ch_name):
                if parts[0] == "unallow":
                    self.video_store.delete_channel_videos(ch_name, channel_id=ch_id)
                if self.on_channel_change:
                    self.on_channel_change()
                _answer_bg(query, f"Removed: {ch_name}")
                await self._update_channel_list_message(query)
            else:
                _answer_bg(query, f"Not found: {ch_name}")
            return

        # Resend notification callback from /pending
        if parts[0] == "resend" and len(parts) == 2:
            video = self.video_store.get_video(parts[1])
            if not video or video['status'] != 'pending':
                await query.answer("No longer pending.")
                return
            _answer_bg(query, "Resending...")
            await self.notify_new_request(video)
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
            cat_label = "Educational" if cat == "edu" else "Entertainment"
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
            cat_label = "Educational" if cat == "edu" else "Entertainment"
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
            cat_label = "Educational" if cat == "edu" else "Entertainment"
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

    # --- Child selector and profile management ---

    async def _cb_child_select(self, query, update: Update, context, profile_id: str) -> None:
        """Handle child selector button press."""
        chat_id = update.effective_chat.id
        pending = self._pending_cmd.pop(chat_id, None)
        if not pending:
            await query.answer("No pending command.")
            return

        handler_fn = pending["handler"]
        ctx = pending["context"]

        if profile_id == "__all__":
            # Execute for all profiles
            profiles = self._get_profiles()
            for p in profiles:
                cs = self._child_store(p["id"])
                await handler_fn(update, ctx, cs, p)
        else:
            p = self.video_store.get_profile(profile_id)
            if not p:
                await query.answer("Profile not found.")
                return
            cs = self._child_store(profile_id)
            await handler_fn(update, ctx, cs, p)

        # Remove the selector message
        try:
            await query.edit_message_text("Done.")
        except Exception:
            pass

    async def _cb_auto_approve(self, query, profile_id: str, video_id: str) -> None:
        """Handle auto-approve from cross-child notification."""
        cs = self._child_store(profile_id)
        video = cs.get_video(video_id)
        if not video or video["status"] != "pending":
            await query.answer("No longer pending.")
            return
        # Copy category from the other profile's approval
        other = self.video_store.find_video_approved_for_others(video_id, profile_id)
        cat = other.get("category", "fun") if other else "fun"
        cs.update_status(video_id, "approved")
        cs.set_video_category(video_id, cat)

        if self.on_video_change:
            self.on_video_change()

        channel_link = _channel_md_link(video['channel_name'], video.get('channel_id'))
        yt_link = f"https://www.youtube.com/watch?v={video_id}"
        cat_label = "Educational" if cat == "edu" else "Entertainment"
        result_text = _md(
            f"**AUTO-APPROVED ({cat_label})**\n\n"
            f"**Title:** {video['title']}\n"
            f"**Channel:** {channel_link}\n"
            f"[Watch on YouTube]({yt_link})"
        )
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("Revoke", callback_data=f"revoke:{profile_id}:{video_id}"),
        ]])
        try:
            await query.edit_message_caption(caption=result_text, reply_markup=reply_markup, parse_mode=MD2)
        except Exception:
            await query.edit_message_text(text=result_text, reply_markup=reply_markup, parse_mode=MD2)

    async def _cb_child_delete_confirm(self, query, profile_id: str) -> None:
        """Handle profile deletion confirmation."""
        p = self.video_store.get_profile(profile_id)
        if not p:
            await query.answer("Profile not found.")
            return
        if self.video_store.delete_profile(profile_id):
            if self.on_channel_change:
                self.on_channel_change()
            await _edit_msg(query, f"Deleted profile: {p['display_name']} and all associated data.")
        else:
            await query.answer("Failed to delete profile.")

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
            await update.message.reply_text(
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
            await update.message.reply_text("No profiles. Use /child add <name> to create one.")
            return
        lines = ["**Child Profiles**\n"]
        for p in profiles:
            pin_status = "PIN set" if p["pin"] else "no PIN"
            cs = self._child_store(p["id"])
            stats = cs.get_stats()
            ch_count = len(cs.get_channels_with_ids("allowed"))
            lines.append(f"**{p['display_name']}** (`{p['id']}`)")
            lines.append(f"  {pin_status} · {stats['approved']} videos · {ch_count} channels")
        await update.message.reply_text(_md("\n".join(lines)), parse_mode=MD2)

    async def _child_add(self, update: Update, args: list[str]) -> None:
        """Handle /child add <name> [pin]."""
        if not args:
            await update.message.reply_text("Usage: /child add <name> [pin]")
            return
        name = args[0]
        pin = args[1] if len(args) > 1 else ""
        # Generate URL-safe ID from name
        pid = re.sub(r'[^a-z0-9]', '', name.lower())[:20]
        if not pid:
            await update.message.reply_text("Name must contain at least one alphanumeric character.")
            return
        # Ensure unique ID
        if self.video_store.get_profile(pid):
            await update.message.reply_text(f"Profile '{pid}' already exists.")
            return
        if self.video_store.create_profile(pid, name, pin=pin):
            pin_msg = " with PIN" if pin else " (no PIN)"
            await update.message.reply_text(_md(f"Created profile: **{name}**{pin_msg}"), parse_mode=MD2)
        else:
            await update.message.reply_text("Failed to create profile.")

    async def _child_remove(self, update: Update, args: list[str]) -> None:
        """Handle /child remove <name>."""
        if not args:
            await update.message.reply_text("Usage: /child remove <name>")
            return
        name = " ".join(args)
        # Find profile by name or id
        profiles = self._get_profiles()
        target = None
        for p in profiles:
            if p["display_name"].lower() == name.lower() or p["id"] == name.lower():
                target = p
                break
        if not target:
            await update.message.reply_text(f"Profile not found: {name}")
            return
        # Confirmation button
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"Delete {target['display_name']} and all data",
                callback_data=f"child_del:{target['id']}",
            ),
        ]])
        await update.message.reply_text(
            _md(f"Delete **{target['display_name']}**? This removes all videos, channels, watch history, and settings."),
            parse_mode=MD2,
            reply_markup=keyboard,
        )

    async def _child_rename(self, update: Update, args: list[str]) -> None:
        """Handle /child rename <name> <new_name>."""
        if len(args) < 2:
            await update.message.reply_text("Usage: /child rename <name> <new_name>")
            return
        old_name = args[0]
        new_name = " ".join(args[1:])
        profiles = self._get_profiles()
        target = None
        for p in profiles:
            if p["display_name"].lower() == old_name.lower() or p["id"] == old_name.lower():
                target = p
                break
        if not target:
            await update.message.reply_text(f"Profile not found: {old_name}")
            return
        if self.video_store.update_profile(target["id"], display_name=new_name):
            await update.message.reply_text(_md(f"Renamed: {target['display_name']} → **{new_name}**"), parse_mode=MD2)
        else:
            await update.message.reply_text("Failed to rename profile.")

    async def _child_pin(self, update: Update, args: list[str]) -> None:
        """Handle /child pin <name> [pin]."""
        if not args:
            await update.message.reply_text("Usage: /child pin <name> [pin]\nOmit pin to remove it.")
            return
        name = args[0]
        new_pin = args[1] if len(args) > 1 else ""
        profiles = self._get_profiles()
        target = None
        for p in profiles:
            if p["display_name"].lower() == name.lower() or p["id"] == name.lower():
                target = p
                break
        if not target:
            await update.message.reply_text(f"Profile not found: {name}")
            return
        if self.video_store.update_profile(target["id"], pin=new_pin):
            if new_pin:
                await update.message.reply_text(_md(f"PIN set for **{target['display_name']}**."), parse_mode=MD2)
            else:
                await update.message.reply_text(_md(f"PIN removed for **{target['display_name']}**."), parse_mode=MD2)
        else:
            await update.message.reply_text("Failed to update PIN.")

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Welcome message on first /start contact."""
        if not await self._require_admin(update):
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
        if not await self._require_admin(update):
            return
        from version import __version__
        help_link = "📖 [Full command reference](https://github.com/GHJJ123/brainrotguard/blob/main/docs/telegram-commands.md)\n"
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
                    await update.message.reply_text(_md(
                        "**Shorts enabled**\n\n"
                        "- Shorts row appears on the homepage below videos\n"
                        "- Shorts from allowlisted channels are fetched on next cache refresh\n"
                        "- Shorts still count toward category time budgets (edu/fun)\n"
                        "- Shorts hidden from search results remain hidden"
                    ), parse_mode=MD2)
                else:
                    await update.message.reply_text(_md(
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
                    await update.message.reply_text(_md(
                        "**Shorts: enabled**\n\n"
                        "Shorts appear in a dedicated row on the homepage and are "
                        "fetched from allowlisted channels. They count toward "
                        "edu/fun time budgets like regular videos.\n\n"
                        "`/shorts off` — hide Shorts everywhere"
                    ), parse_mode=MD2)
                else:
                    await update.message.reply_text(_md(
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
        pending = self.video_store.get_pending()
        if not pending:
            await update.message.reply_text("No pending requests. Videos requested from the web app will appear here.")
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

        nav = _nav_row(page, total, ps, "pending_page")
        if nav:
            buttons.append(nav)
        return _md("\n".join(lines)), InlineKeyboardMarkup(buttons)

    async def _cb_pending_page(self, query, page: int) -> None:
        """Handle pending list pagination."""
        pending = self.video_store.get_pending()
        if not pending:
            await query.answer("No pending requests.")
            return
        _answer_bg(query)
        text, keyboard = self._render_pending_page(pending, page)
        await _edit_msg(query, text, keyboard)

    _APPROVED_PAGE_SIZE = 10

    async def _cmd_approved(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return
        query = " ".join(context.args)[:200] if context.args else ""
        if query:
            results = self.video_store.search_approved(query)
            if not results:
                await update.message.reply_text(f"No approved videos matching \"{query}\".")
                return
            text, keyboard = self._render_approved_page(results, len(results), 0, search=query)
            await update.message.reply_text(
                text, parse_mode=MD2, reply_markup=keyboard, disable_web_page_preview=True,
            )
            return
        page_items, total = self.video_store.get_approved_page(0, self._APPROVED_PAGE_SIZE)
        if not page_items:
            await update.message.reply_text("No approved videos yet. Approve requests or use /channel to allow channels.")
            return
        text, keyboard = self._render_approved_page(page_items, total, 0)
        await update.message.reply_text(
            text, parse_mode=MD2, reply_markup=keyboard, disable_web_page_preview=True,
        )

    def _render_approved_page(self, page_items: list, total: int, page: int, search: str = "") -> tuple[str, InlineKeyboardMarkup | None]:
        """Render a page of the approved list."""
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

        nav = _nav_row(page, total, ps, "approved_page")
        keyboard = InlineKeyboardMarkup([nav]) if nav else None
        return _md("\n".join(lines)), keyboard

    async def _cb_approved_page(self, query, page: int) -> None:
        """Handle approved list pagination."""
        page_items, total = self.video_store.get_approved_page(page, self._APPROVED_PAGE_SIZE)
        if not page_items and page == 0:
            await query.answer("No approved videos.")
            return
        _answer_bg(query)
        text, keyboard = self._render_approved_page(page_items, total, page)
        await _edit_msg(query, text, keyboard, disable_preview=True)

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
            await update.message.reply_text("Video not found — it may have been removed from the database.")
            return
        if video['status'] != 'approved':
            await update.message.reply_text(f"Already {video['status']} — no change needed.")
            return
        self.video_store.update_status(video_id, "denied")
        await update.message.reply_text(
            _md(f"**Approval removed:** {video['title']}\nThe video is no longer watchable."), parse_mode=MD2,
        )

    # --- /watch command ---

    def _progress_bar(self, fraction: float, width: int = 20) -> str:
        filled = min(width, int(fraction * width))
        return "\u2593" * filled + "\u2591" * (width - filled)

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
                    lines.append(f"`{self._progress_bar(pct)}` {int(pct * 100)}%")
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

        await self._with_child_context(update, context, _inner)

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
            await update.message.reply_text(latest)
        except FileNotFoundError:
            await update.message.reply_text("Changelog not available.")

    # --- Notification methods ---

    def _get_tz(self) -> str:
        """Return the configured timezone string (or empty for UTC)."""
        return self.config.watch_limits.timezone if self.config else ""

    async def notify_time_limit_reached(self, used_min: float, limit_min: int,
                                        category: str = "", profile_id: str = "default") -> None:
        """Send notification when daily time limit is reached (once per day per category per profile)."""
        if not self._app:
            return
        today = get_today_str(self._get_tz())
        key = (profile_id, category)
        if self._limit_notified_cats.get(key) == today:
            return
        self._limit_notified_cats[key] = today

        # Include child name if multiple profiles
        profiles = self._get_profiles()
        child_label = ""
        if len(profiles) > 1:
            p = self.video_store.get_profile(profile_id)
            if p:
                child_label = f" — {p['display_name']}"

        cat_label = {"edu": "Educational", "fun": "Entertainment"}.get(category, "")
        cat_text = f" ({cat_label})" if cat_label else ""
        text = _md(
            f"**Daily watch limit reached{cat_text}{child_label}**\n\n"
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

        nav = _nav_row(page, total, ps, "starter_page")
        if nav:
            buttons.append(nav)
        markup = InlineKeyboardMarkup(buttons) if buttons else None
        return _md("\n".join(lines)), markup

    async def _cb_starter_page(self, query, page: int) -> None:
        """Handle starter channels pagination."""
        _answer_bg(query)
        text, markup = self._render_starter_message(page)
        await _edit_msg(query, text, markup, disable_preview=True)

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
            # Resolve channel_id from @handle before inserting
            cid = None
            try:
                from youtube.extractor import resolve_channel_handle
                info = await resolve_channel_handle(handle)
                if info:
                    cid = info.get("channel_id")
                    # Use YouTube's canonical name if available
                    if info.get("channel_name"):
                        name = info["channel_name"]
            except Exception:
                pass  # proceed without channel_id; backfill loop will retry
            self.video_store.add_channel(name, "allowed", channel_id=cid, handle=handle, category=cat)
            if self.on_channel_change:
                self.on_channel_change()

        # Acknowledge callback in background (non-blocking)
        msg = f"Already imported: {name}" if already else f"Imported: {name}"
        _answer_bg(query, msg)

        # Re-render the message immediately
        page = idx // self._STARTER_PAGE_SIZE
        text, markup = self._render_starter_message(page)
        await _edit_msg(query, text, markup, disable_preview=True)

    async def _channel_allow(self, update: Update, args: list[str]) -> None:
        await self._channel_resolve_and_add(update, args, "allowed")

    async def _channel_unallow(self, update: Update, args: list[str]) -> None:
        await self._channel_remove(update, args, "unallow")

    async def _channel_block(self, update: Update, args: list[str]) -> None:
        await self._channel_resolve_and_add(update, args, "blocked")

    async def _channel_resolve_and_add(self, update: Update, args: list[str], status: str) -> None:
        """Resolve a @handle via yt-dlp and add to channel list."""
        verb = "allow" if status == "allowed" else "block"
        example = "@LEGO" if status == "allowed" else "@Slurry"
        if not args:
            await update.message.reply_text(f"Usage: /channel {verb} @handle\nExample: /channel {verb} {example}")
            return
        raw = args[0]
        if not raw.startswith("@"):
            await update.message.reply_text(
                f"Please use the channel's @handle (e.g. {example}).\n"
                "You can find it on the channel's YouTube page."
            )
            return
        await update.message.reply_text(f"Looking up {raw} on YouTube...")
        from youtube.extractor import resolve_channel_handle
        info = await resolve_channel_handle(raw)
        if not info or not info.get("channel_name"):
            await update.message.reply_text(f"Couldn't find a channel for {raw}. Check the spelling or try the full @handle from YouTube.")
            return
        channel_name = info["channel_name"]
        channel_id = info.get("channel_id")
        handle = info.get("handle")
        cat = None
        if status == "allowed" and len(args) > 1 and args[1].lower() in ("edu", "fun"):
            cat = args[1].lower()
        self.video_store.add_channel(channel_name, status, channel_id=channel_id, handle=handle, category=cat)
        if self.on_channel_change:
            self.on_channel_change()
        if status == "allowed":
            cat_label = {"edu": "Educational", "fun": "Entertainment"}.get(cat, "No category")
            await update.message.reply_text(
                f"Added to allowlist: {channel_name} ({raw})\nCategory: {cat_label}"
            )
        else:
            await update.message.reply_text(f"Blocked: {channel_name}\nVideos from this channel will be auto-denied.")

    async def _channel_unblock(self, update: Update, args: list[str]) -> None:
        await self._channel_remove(update, args, "unblock")

    async def _channel_remove(self, update: Update, args: list[str], verb: str) -> None:
        """Remove a channel from allow/block list."""
        if not args:
            await update.message.reply_text(f"Usage: /channel {verb} <channel name>")
            return
        channel = " ".join(args)
        # Look up channel_id before removing (remove_channel deletes the row)
        ch_id = ""
        status = "allowed" if verb == "unallow" else "blocked"
        for name, cid, _h, _c in self.video_store.get_channels_with_ids(status):
            if name.lower() == channel.lower():
                ch_id = cid or ""
                break
        if self.video_store.remove_channel(channel):
            if verb == "unallow":
                deleted = self.video_store.delete_channel_videos(channel, channel_id=ch_id)
            else:
                deleted = 0
            if self.on_channel_change:
                self.on_channel_change()
            label = "Removed from allowlist" if verb == "unallow" else "Unblocked"
            extra = f" Deleted {deleted} video{'s' if deleted != 1 else ''} from catalog." if deleted else ""
            await update.message.reply_text(f"{label}: {channel}.{extra}")
        else:
            await update.message.reply_text(f"Channel not in list: {channel}. Use /channel to see all channels.")

    async def _channel_set_cat(self, update: Update, args: list[str]) -> None:
        """Handle /channel cat <name> edu|fun."""
        if len(args) < 2:
            await update.message.reply_text("Usage: /channel cat <name> edu|fun\n\nThis sets which time budget the channel's videos count against.")
            return
        cat = args[-1].lower()
        if cat not in ("edu", "fun"):
            await update.message.reply_text("Category must be edu (Educational) or fun (Entertainment).")
            return
        raw = " ".join(args[:-1])
        channel = self.video_store.resolve_channel_name(raw) or raw
        if self.video_store.set_channel_category(channel, cat):
            # Look up channel_id for stable matching
            ch_id = ""
            for name, cid, _h, _c in self.video_store.get_channels_with_ids("allowed"):
                if name.lower() == channel.lower():
                    ch_id = cid or ""
                    break
            self.video_store.set_channel_videos_category(channel, cat, channel_id=ch_id)
            cat_label = "Educational" if cat == "edu" else "Entertainment"
            if self.on_channel_change:
                self.on_channel_change()
            await update.message.reply_text(f"**{channel}** → {cat_label}\nExisting videos from this channel updated too.", parse_mode=MD2)
        else:
            await update.message.reply_text(f"Channel not in list: {raw}. Use /channel to see all channels.")

    _CHANNEL_PAGE_SIZE = 10

    def _render_channel_menu(self) -> tuple[str, InlineKeyboardMarkup | None]:
        """Build the channel menu with Allowed/Blocked buttons and summary stats."""
        allowed = self.video_store.get_channels_with_ids("allowed")
        blocked = self.video_store.get_channels_with_ids("blocked")
        if not allowed and not blocked:
            return "No channels configured.", None
        total = len(allowed) + len(blocked)
        edu_count = sum(1 for _, _, _, cat in allowed + blocked if cat == "edu")
        fun_count = sum(1 for _, _, _, cat in allowed + blocked if cat == "fun")
        uncat = total - edu_count - fun_count
        lines = [f"**Channels** ({total})\n"]
        if allowed:
            lines.append(f"Allowed: {len(allowed)}")
        if blocked:
            lines.append(f"Blocked: {len(blocked)}")
        cat_parts = []
        if edu_count:
            cat_parts.append(f"{edu_count} edu")
        if fun_count:
            cat_parts.append(f"{fun_count} fun")
        if uncat:
            cat_parts.append(f"{uncat} uncategorized")
        if cat_parts:
            lines.append(f"Categories: {', '.join(cat_parts)}")
        text = _md("\n".join(lines))
        row = []
        if allowed:
            row.append(InlineKeyboardButton(
                f"Allowed ({len(allowed)})", callback_data="chan_filter:allowed",
            ))
        if blocked:
            row.append(InlineKeyboardButton(
                f"Blocked ({len(blocked)})", callback_data="chan_filter:blocked",
            ))
        return text, InlineKeyboardMarkup([row]) if row else None

    def _render_channel_page(self, status: str, page: int = 0) -> tuple[str, InlineKeyboardMarkup | None]:
        """Build text + inline buttons for a page of the channel list filtered by status."""
        entries = self.video_store.get_channels_with_ids(status)
        if not entries:
            return f"No {status} channels.", None

        total = len(entries)
        page_size = self._CHANNEL_PAGE_SIZE
        start = page * page_size
        end = min(start + page_size, total)
        page_entries = entries[start:end]

        label = "Allowed" if status == "allowed" else "Blocked"
        lines = [f"**{label} Channels** ({total})\n"]
        buttons = []
        for ch, cid, handle, cat in page_entries:
            cat_tag = f" [{cat}]" if cat else ""
            if cid:
                url = f"https://www.youtube.com/channel/{cid}"
            elif handle:
                url = f"https://www.youtube.com/{handle}"
            else:
                url = f"https://www.youtube.com/results?search_query={quote(ch)}"
            handle_tag = f" `{handle}`" if handle else ""
            lines.append(f"  [{ch}]({url}){handle_tag}{cat_tag}")
            btn_label = f"Unallow: {ch}" if status == "allowed" else f"Unblock: {ch}"
            btn_action = "unallow" if status == "allowed" else "unblock"
            buttons.append([InlineKeyboardButton(
                btn_label, callback_data=f"{btn_action}:{ch}"
            )])

        nav = _nav_row(page, total, page_size, f"chan_page:{status}")
        if nav:
            buttons.append(nav)
        # Back to menu
        buttons.append([InlineKeyboardButton("\U0001f4cb Channels", callback_data="chan_menu")])

        text = _md("\n".join(lines))
        markup = InlineKeyboardMarkup(buttons) if buttons else None
        return text, markup

    async def _channel_list(self, update: Update) -> None:
        text, markup = self._render_channel_menu()
        await update.message.reply_text(
            text, parse_mode=MD2, disable_web_page_preview=True,
            reply_markup=markup,
        )

    async def _cb_channel_filter(self, query, status: str) -> None:
        """Handle Allowed/Blocked button press from channel menu."""
        _answer_bg(query)
        text, markup = self._render_channel_page(status, 0)
        await _edit_msg(query, text, markup, disable_preview=True)

    async def _cb_channel_menu(self, query) -> None:
        """Handle back-to-menu button press."""
        _answer_bg(query)
        text, markup = self._render_channel_menu()
        await _edit_msg(query, text, markup, disable_preview=True)

    async def _cb_channel_page(self, query, status: str, page: int) -> None:
        """Handle channel list pagination."""
        _answer_bg(query)
        text, markup = self._render_channel_page(status, page)
        await _edit_msg(query, text, markup, disable_preview=True)

    async def _update_channel_list_message(self, query) -> None:
        """Refresh back to channel menu after unallow/unblock."""
        text, markup = self._render_channel_menu()
        await _edit_msg(query, text, markup, disable_preview=True)

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

        nav = _nav_row(page, total, page_size, f"logs_page:{days}")
        keyboard = InlineKeyboardMarkup([nav]) if nav else None
        return _md("\n".join(lines)), keyboard

    async def _cb_logs_page(self, query, days: int, page: int) -> None:
        """Handle logs pagination."""
        days = min(max(1, days), 365)
        activity = self.video_store.get_recent_activity(days)
        if not activity:
            await query.answer("No activity.")
            return
        _answer_bg(query)
        text, keyboard = self._render_logs_page(activity, days, page)
        await _edit_msg(query, text, keyboard)

    # --- /search subcommands ---

    async def _cmd_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show search history. /search [days|today|all]."""
        if not self._check_admin(update):
            return
        await self._search_history(update, context.args or [])

    _SEARCH_PAGE_SIZE = 20

    async def _search_history(self, update: Update, args: list[str]) -> None:
        days = 7
        if args:
            arg = args[0].lower()
            if arg == "today":
                days = 1
            elif arg == "all":
                days = 365
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

        nav = _nav_row(page, total, ps, f"search_page:{days}")
        keyboard = InlineKeyboardMarkup([nav]) if nav else None
        return _md("\n".join(lines)), keyboard

    async def _cb_search_page(self, query, days: int, page: int) -> None:
        """Handle search history pagination."""
        days = min(max(1, days), 365)
        searches = self.video_store.get_recent_searches(days)
        if not searches:
            await query.answer("No searches.")
            return
        _answer_bg(query)
        text, keyboard = self._render_search_page(searches, days, page)
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
            await update.message.reply_text("Usage: /filter add|remove <word>")
            return
        word = " ".join(args[1:])
        if action == "add":
            if self.video_store.add_word_filter(word):
                if self.on_channel_change:
                    self.on_channel_change()
                await update.message.reply_text(
                    f"Filter added: \"{word}\"\n"
                    "Videos with this word in the title are hidden everywhere."
                )
            else:
                await update.message.reply_text(f"Already filtered: \"{word}\"")
        elif action in ("remove", "rm", "del"):
            if self.video_store.remove_word_filter(word):
                if self.on_channel_change:
                    self.on_channel_change()
                await update.message.reply_text(f"Filter removed: \"{word}\"")
            else:
                await update.message.reply_text(f"\"{word}\" isn't in the filter list.")
        else:
            await update.message.reply_text("Usage: /filter add|remove <word>")

    async def _filter_list(self, update: Update) -> None:
        words = self.video_store.get_word_filters()
        if not words:
            await update.message.reply_text("No word filters set. Use /filter add <word> to hide videos by title.")
            return
        lines = ["**Word Filters** (hidden everywhere):\n"]
        for w in words:
            lines.append(f"- `{w}`")
        await update.message.reply_text(_md("\n".join(lines)), parse_mode=MD2)

    # --- Time limit command ---

    _DAY_LABELS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday",
                   "thu": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}
    _OVERRIDE_KEYS = ("schedule_start", "schedule_end", "edu_limit_minutes",
                      "fun_limit_minutes", "daily_limit_minutes")

    def _resolve_setting(self, base_key: str, default: str = "", store=None) -> str:
        """Resolve a setting with per-day override support."""
        s = store or self.video_store
        day = get_weekday(self._get_tz())
        day_val = s.get_setting(f"{day}_{base_key}", "")
        if day_val:
            return day_val
        return s.get_setting(base_key, default)

    def _effective_setting(self, day: str, base_key: str, store=None) -> str:
        """Get effective setting for a given day (day override > default)."""
        s = store or self.video_store
        day_val = s.get_setting(f"{day}_{base_key}", "")
        return day_val if day_val else s.get_setting(base_key, "")

    def _has_any_day_overrides(self, store=None) -> bool:
        """Check if any per-day overrides exist."""
        s = store or self.video_store
        for day in DAY_NAMES:
            for key in self._OVERRIDE_KEYS:
                if s.get_setting(f"{day}_{key}", ""):
                    return True
        return False

    def _get_day_overrides(self, day: str, store=None) -> dict[str, str]:
        """Get all override settings for a specific day."""
        s = store or self.video_store
        result = {}
        for key in self._OVERRIDE_KEYS:
            val = s.get_setting(f"{day}_{key}", "")
            if val:
                result[key] = val
        return result

    def _get_limit_mode(self, store=None) -> str:
        """Detect current limit mode: 'category', 'simple', or 'none'."""
        s = store or self.video_store
        edu = s.get_setting("edu_limit_minutes", "")
        fun = s.get_setting("fun_limit_minutes", "")
        flat = s.get_setting("daily_limit_minutes", "")
        if (edu and int(edu) > 0) or (fun and int(fun) > 0):
            return "category"
        if flat and int(flat) > 0:
            return "simple"
        # Config fallback only for default profile
        is_default = not hasattr(s, 'profile_id') or s.profile_id == "default"
        if is_default and self.config:
            wl = self.config.watch_limits
            if getattr(wl, "edu_limit_minutes", 0) or getattr(wl, "fun_limit_minutes", 0):
                return "category"
            if getattr(wl, "daily_limit_minutes", 0):
                return "simple"
        return "none"

    def _auto_clear_mode(self, new_mode: str, day: str = "", store=None) -> None:
        """Clear conflicting limit settings when switching modes.

        new_mode='simple': clears edu + fun limits.
        new_mode='category': clears daily flat limit.
        """
        s = store or self.video_store
        prefix = f"{day}_" if day else ""
        if new_mode == "simple":
            s.set_setting(f"{prefix}edu_limit_minutes", "0")
            s.set_setting(f"{prefix}fun_limit_minutes", "0")
        elif new_mode == "category":
            s.set_setting(f"{prefix}daily_limit_minutes", "0")

    async def _cmd_timelimit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return

        async def _inner(update, context, cs, profile):
            args = context.args
            if args:
                arg = args[0].lower()

                # /time <day> ... — per-day override
                if arg in DAY_NAMES:
                    await self._time_day(update, arg, args[1:], store=cs)
                    return

                # /time setup — guided wizard
                if arg == "setup":
                    await self._time_setup_start(update, store=cs)
                    return

                # /time start <time|off>
                if arg == "start":
                    await self._time_schedule(update, args[1:], "schedule_start", store=cs)
                    return
                # /time stop <time|off>
                if arg == "stop":
                    await self._time_schedule(update, args[1:], "schedule_end", store=cs)
                    return

                # /time add <minutes>
                if arg == "add":
                    await self._time_add_bonus(update, args[1:], store=cs)
                    return

                # /time edu|fun — category limits
                if arg == "edu":
                    await self._time_set_category_limit(update, args[1:], "edu", store=cs)
                    return
                if arg == "fun":
                    await self._time_set_category_limit(update, args[1:], "fun", store=cs)
                    return

                # /time limit <min> — explicit flat limit
                if arg == "limit":
                    await self._time_set_flat_limit(update, args[1:], store=cs)
                    return

                if arg == "off":
                    cs.set_setting("daily_limit_minutes", "0")
                    await update.message.reply_text("Watch time limit disabled. Videos can be watched without a daily cap.")
                    return
                elif arg.isdigit():
                    await self._time_set_flat_limit(update, [arg], store=cs)
                    return
                else:
                    await update.message.reply_text(
                        "Usage: /time [minutes|off]\n"
                        "       /time setup\n"
                        "       /time start|stop <time|off>\n"
                        "       /time add <minutes>\n"
                        "       /time edu|fun <minutes|off>\n"
                        "       /time <day> [start|stop|edu|fun|limit|off|copy]"
                    )
                    return

            # Show current status
            await self._time_show_status(update, store=cs)

        await self._with_child_context(update, context, _inner)

    # --- /time status display ---

    def _format_day_summary(self, day: str, is_today: bool = False, store=None) -> str:
        """Format a single day's effective settings as a compact line."""
        label = day[:3].capitalize()
        sched_start = self._effective_setting(day, "schedule_start", store=store)
        sched_end = self._effective_setting(day, "schedule_end", store=store)

        # Schedule part — use ASCII hyphen for consistent monospace width
        if sched_start or sched_end:
            s = format_time_12h(sched_start).replace(" AM", "a").replace(" PM", "p").replace(":00", "") if sched_start else "-"
            e = format_time_12h(sched_end).replace(" AM", "a").replace(" PM", "p").replace(":00", "") if sched_end else "-"
            sched = f"{s}-{e}"
        else:
            sched = "open"

        # Limits part
        edu_str = self._effective_setting(day, "edu_limit_minutes", store=store)
        fun_str = self._effective_setting(day, "fun_limit_minutes", store=store)
        flat_str = self._effective_setting(day, "daily_limit_minutes", store=store)
        edu = int(edu_str) if edu_str else 0
        fun = int(fun_str) if fun_str else 0
        flat = int(flat_str) if flat_str else 0

        if edu > 0 or fun > 0:
            parts = []
            if edu > 0:
                parts.append(f"edu {edu}")
            if fun > 0:
                parts.append(f"fun {fun}")
            limits = "/".join(parts) + "m"
        elif flat > 0:
            limits = f"{flat}m"
        else:
            limits = "-"

        marker = " \u25c0" if is_today else ""
        has_override = bool(self._get_day_overrides(day, store=store))
        override_mark = "*" if has_override else " "
        # Pad schedule to 9 chars for alignment on mobile
        sched_padded = sched.ljust(9)
        return f"`{override_mark}{label} {sched_padded} {limits}`{marker}"

    async def _time_show_status(self, update: Update, store=None) -> None:
        """Show current time settings with today's status and 7-day view."""
        s = store or self.video_store
        tz = self._get_tz()
        today_day = get_weekday(tz)
        today = get_today_str(tz)
        bounds = get_day_utc_bounds(today, tz)
        used = s.get_daily_watch_minutes(today, utc_bounds=bounds)

        # Resolve today's effective settings
        sched_start = self._resolve_setting("schedule_start", store=s)
        sched_end = self._resolve_setting("schedule_end", store=s)
        edu_limit_str = self._resolve_setting("edu_limit_minutes", store=s)
        fun_limit_str = self._resolve_setting("fun_limit_minutes", store=s)
        flat_limit_str = self._resolve_setting("daily_limit_minutes", store=s)
        edu_limit = int(edu_limit_str) if edu_limit_str else 0
        fun_limit = int(fun_limit_str) if fun_limit_str else 0
        flat_limit = int(flat_limit_str) if flat_limit_str else 0
        is_default = not hasattr(s, 'profile_id') or s.profile_id == "default"
        if not flat_limit_str and is_default and self.config:
            flat_limit = getattr(self.config.watch_limits, "daily_limit_minutes", 0)

        # Schedule status
        if sched_start or sched_end:
            allowed, unlock_time = is_within_schedule(sched_start, sched_end, tz)
            s_display = format_time_12h(sched_start) if sched_start else "midnight"
            e_display = format_time_12h(sched_end) if sched_end else "midnight"
            status = "OPEN" if allowed else f"CLOSED (unlocks {unlock_time})"
        else:
            status = "OPEN"
            s_display = e_display = ""

        day_label = self._DAY_LABELS[today_day]
        lines = [f"\u23f0 **Today ({day_label[:3]})** \u2014 {status}\n"]

        if s_display:
            lines.append(f"Schedule: {s_display} \u2013 {e_display}")

        # Bonus
        bonus = 0
        bonus_date = s.get_setting("daily_bonus_date", "")
        if bonus_date == today:
            bonus = int(s.get_setting("daily_bonus_minutes", "0") or "0")

        # Category mode
        if edu_limit > 0 or fun_limit > 0:
            cat_usage = s.get_daily_watch_by_category(today, utc_bounds=bounds)
            edu_used = cat_usage.get("edu", 0.0)
            fun_used = cat_usage.get("fun", 0.0) + cat_usage.get(None, 0.0)
            total_limit = edu_limit + fun_limit
            effective_total = total_limit + bonus
            total_used = edu_used + fun_used

            parts = []
            if edu_limit > 0:
                parts.append(f"Edu: {edu_limit}")
            if fun_limit > 0:
                parts.append(f"Fun: {fun_limit}")
            joined = " \u00b7 ".join(parts)
            lines.append(f"{joined} min ({effective_total}m total)")
            if bonus > 0:
                lines.append(f"Bonus today: +{bonus} min")
            lines.append("")

            pct = min(1.0, total_used / effective_total) if effective_total > 0 else 0
            lines.append(f"`{self._progress_bar(pct)}` {int(total_used)}/{effective_total} min ({int(pct * 100)}%)")

            # Per-category bars
            if edu_limit > 0:
                eff_edu = edu_limit + bonus
                epct = min(1.0, edu_used / eff_edu) if eff_edu > 0 else 0
                lines.append(f"  Edu `{self._progress_bar(epct, 10)}` {int(edu_used)}/{eff_edu}")
            if fun_limit > 0:
                eff_fun = fun_limit + bonus
                fpct = min(1.0, fun_used / eff_fun) if eff_fun > 0 else 0
                lines.append(f"  Fun `{self._progress_bar(fpct, 10)}` {int(fun_used)}/{eff_fun}")
        elif flat_limit > 0:
            effective = flat_limit + bonus
            remaining = max(0, effective - used)
            pct = min(1.0, used / effective) if effective > 0 else 0
            lines.append(f"Limit: {flat_limit} min")
            if bonus > 0:
                lines.append(f"Bonus today: +{bonus} min")
            lines.append("")
            lines.append(f"`{self._progress_bar(pct)}` {int(used)}/{effective} min ({int(pct * 100)}%)")
        else:
            lines.append(f"No limits set \u2014 {int(used)} min watched")
            mode = self._get_limit_mode(store=s)
            if mode == "none":
                lines.append("_Use /time setup to configure limits._")

        # 7-day view
        has_overrides = self._has_any_day_overrides(store=s)
        any_limits = edu_limit > 0 or fun_limit > 0 or flat_limit > 0
        if has_overrides or any_limits:
            lines.append(f"\n\U0001f4cb **Week**")
            for d in DAY_NAMES:
                lines.append(self._format_day_summary(d, is_today=(d == today_day), store=s))
            if not has_overrides:
                lines.append("_All days: same schedule_")
        lines.append("")

        await update.message.reply_text(_md("\n".join(lines)), parse_mode=MD2)

    # --- Per-day commands ---

    async def _time_day(self, update: Update, day: str, args: list[str], store=None) -> None:
        """Dispatch /time <day> subcommands."""
        s = store or self.video_store
        if not args:
            await self._time_day_show(update, day, store=s)
            return
        sub = args[0].lower()
        prefix = f"{day}_"

        if sub == "start":
            await self._time_schedule(update, args[1:], f"{prefix}schedule_start", day=day, store=s)
        elif sub == "stop":
            await self._time_schedule(update, args[1:], f"{prefix}schedule_end", day=day, store=s)
        elif sub == "edu":
            await self._time_set_category_limit(update, args[1:], "edu", day=day, store=s)
        elif sub == "fun":
            await self._time_set_category_limit(update, args[1:], "fun", day=day, store=s)
        elif sub == "limit":
            await self._time_set_flat_limit(update, args[1:], day=day, store=s)
        elif sub == "off":
            # Clear all overrides for this day
            for key in self._OVERRIDE_KEYS:
                s.set_setting(f"{prefix}{key}", "")
            label = self._DAY_LABELS[day]
            await update.message.reply_text(f"{label} overrides cleared — default settings will apply.")
        elif sub == "copy":
            await self._time_day_copy(update, day, args[1:], store=s)
        elif sub.isdigit():
            await self._time_set_flat_limit(update, [sub], day=day, store=s)
        else:
            label = self._DAY_LABELS[day]
            await update.message.reply_text(
                f"Usage: /time {day} [start|stop|edu|fun|limit|off|copy]\n"
                f"       /time {day} copy <days|weekdays|weekend|all>"
            )

    async def _time_day_show(self, update: Update, day: str, store=None) -> None:
        """Show effective settings for a specific day."""
        s = store or self.video_store
        label = self._DAY_LABELS[day]
        overrides = self._get_day_overrides(day, store=s)

        lines = [f"**{label}**\n"]

        # Schedule
        sched_start = self._effective_setting(day, "schedule_start", store=s)
        sched_end = self._effective_setting(day, "schedule_end", store=s)
        if sched_start or sched_end:
            s_disp = format_time_12h(sched_start) if sched_start else "midnight"
            e_disp = format_time_12h(sched_end) if sched_end else "midnight"
            lines.append(f"**Schedule:** {s_disp} \u2013 {e_disp}")
        else:
            lines.append("**Schedule:** not set")

        # Limits
        edu_str = self._effective_setting(day, "edu_limit_minutes", store=s)
        fun_str = self._effective_setting(day, "fun_limit_minutes", store=s)
        flat_str = self._effective_setting(day, "daily_limit_minutes", store=s)
        edu = int(edu_str) if edu_str else 0
        fun = int(fun_str) if fun_str else 0
        flat = int(flat_str) if flat_str else 0

        if edu > 0 or fun > 0:
            if edu > 0:
                lines.append(f"**Educational:** {edu} min")
            if fun > 0:
                lines.append(f"**Entertainment:** {fun} min")
            lines.append(f"**Total:** {edu + fun} min")
        elif flat > 0:
            lines.append(f"**Daily limit:** {flat} min")
        else:
            lines.append("**Limits:** none")

        if overrides:
            lines.append(f"\n_Has {len(overrides)} override(s) — defaults used for the rest._")
        else:
            lines.append("\n_No overrides — using default settings._")

        await update.message.reply_text(_md("\n".join(lines)), parse_mode=MD2)

    async def _time_day_copy(self, update: Update, src_day: str, args: list[str], store=None) -> None:
        """Handle /time <day> copy <targets>."""
        s = store or self.video_store
        if not args:
            await update.message.reply_text(
                f"Usage: /time {src_day} copy <day|weekdays|weekend|all>"
            )
            return

        # Resolve target days
        targets: list[str] = []
        for arg in args:
            arg_lower = arg.lower()
            if arg_lower in DAY_NAMES:
                targets.append(arg_lower)
            elif arg_lower in DAY_GROUPS:
                targets.extend(DAY_GROUPS[arg_lower])
            elif arg_lower == "all":
                targets.extend(d for d in DAY_NAMES if d != src_day)
            else:
                await update.message.reply_text(f"Unknown day: {arg}. Use day names (mon, tue...), weekdays, weekend, or all.")
                return

        # Remove source from targets and deduplicate
        targets = list(dict.fromkeys(t for t in targets if t != src_day))
        if not targets:
            await update.message.reply_text("No valid days. Use day names (mon, tue...), weekdays, weekend, or all.")
            return

        src_overrides = self._get_day_overrides(src_day, store=s)

        for target in targets:
            # Clear existing overrides on target
            for key in self._OVERRIDE_KEYS:
                s.set_setting(f"{target}_{key}", "")
            # Copy source overrides
            for key, val in src_overrides.items():
                s.set_setting(f"{target}_{key}", val)

        src_label = self._DAY_LABELS[src_day]
        target_labels = ", ".join(self._DAY_LABELS[t][:3] for t in targets)
        count = len(src_overrides)
        await update.message.reply_text(
            f"Copied {count} override(s) from {src_label} \u2192 {target_labels}."
        )

    # --- Flat limit (simple mode) ---

    async def _time_set_flat_limit(self, update: Update, args: list[str], day: str = "", store=None) -> None:
        """Handle /time [<day>] limit|<N> with mode switch warning."""
        s = store or self.video_store
        if not args or not args[0].isdigit():
            await update.message.reply_text("Usage: /time [<day>] limit <minutes>")
            return
        minutes = int(args[0])

        # Mode switch check (only for default, not per-day)
        if not day:
            mode = self._get_limit_mode(store=s)
            if mode == "category":
                edu = s.get_setting("edu_limit_minutes", "")
                fun = s.get_setting("fun_limit_minutes", "")
                edu_val = int(edu) if edu else 0
                fun_val = int(fun) if fun else 0
                text = _md(
                    f"\u26a0\ufe0f You have category limits set (edu:{edu_val} fun:{fun_val}).\n\n"
                    f"Switching to a simple limit replaces category budgets "
                    f"with a single daily cap."
                )
                # Store profile_id in callback for mode switch
                pid = s.profile_id if hasattr(s, 'profile_id') else "default"
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        f"Switch to {minutes} min flat",
                        callback_data=f"switch_confirm:{pid}:simple:{minutes}",
                    ),
                    InlineKeyboardButton(
                        "Keep categories",
                        callback_data="switch_confirm:keep",
                    ),
                ]])
                await update.message.reply_text(text, parse_mode=MD2, reply_markup=keyboard)
                return

        prefix = f"{day}_" if day else ""
        s.set_setting(f"{prefix}daily_limit_minutes", str(minutes))
        self._auto_clear_mode("simple", day=day, store=s)

        if day:
            label = self._DAY_LABELS[day]
            await update.message.reply_text(f"{label} limit set to {minutes} minutes. Playback stops when time runs out.")
        else:
            await update.message.reply_text(f"Daily limit set to {minutes} minutes. Playback stops when time runs out.")

    # --- Category limits ---

    async def _time_set_category_limit(self, update: Update, args: list[str],
                                       category: str, day: str = "", store=None) -> None:
        """Handle /time [<day>] edu|fun <minutes|off>."""
        s = store or self.video_store
        cat_label = "Educational" if category == "edu" else "Entertainment"
        prefix = f"{day}_" if day else ""
        setting_key = f"{prefix}{category}_limit_minutes"

        if not args:
            current = s.get_setting(setting_key, "")
            limit = int(current) if current else 0
            if day:
                label = self._DAY_LABELS[day]
                if limit == 0:
                    # Day override: check if it's explicitly set or just empty
                    if current:
                        await update.message.reply_text(f"{label} {cat_label}: OFF (override)")
                    else:
                        effective = s.get_setting(f"{category}_limit_minutes", "")
                        eff_val = int(effective) if effective else 0
                        if eff_val:
                            await update.message.reply_text(f"{label} {cat_label}: {eff_val} min (from default)")
                        else:
                            await update.message.reply_text(f"{label} {cat_label}: OFF")
                else:
                    await update.message.reply_text(f"{label} {cat_label}: {limit} min (override)")
            else:
                if limit == 0:
                    await update.message.reply_text(f"{cat_label} limit: OFF (unlimited)")
                else:
                    await update.message.reply_text(f"{cat_label} limit: {limit} minutes/day")
            return

        value = args[0].lower()

        if value in ("off", "0"):
            if day:
                # Day override: "off" clears the override (falls back to default)
                s.set_setting(setting_key, "")
                label = self._DAY_LABELS[day]
                await update.message.reply_text(f"{label} {cat_label} override cleared — default settings will apply.")
            else:
                s.set_setting(setting_key, "0")
                await update.message.reply_text(f"{cat_label} limit disabled — no daily cap.")
            return

        if not value.isdigit():
            await update.message.reply_text(f"Usage: /time {category} <minutes|off>")
            return

        minutes = int(value)

        # Mode switch check (only for default, not per-day)
        if not day:
            mode = self._get_limit_mode(store=s)
            if mode == "simple":
                flat = s.get_setting("daily_limit_minutes", "")
                flat_val = int(flat) if flat else 0
                text = _md(
                    f"\u26a0\ufe0f You have a simple limit of {flat_val} min.\n\n"
                    f"Switching to category mode replaces this with separate "
                    f"edu and fun budgets."
                )
                pid = s.profile_id if hasattr(s, 'profile_id') else "default"
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "Set up categories",
                        callback_data=f"switch_confirm:{pid}:category:{category}:{minutes}",
                    ),
                    InlineKeyboardButton(
                        "Keep simple limit",
                        callback_data="switch_confirm:keep",
                    ),
                ]])
                await update.message.reply_text(text, parse_mode=MD2, reply_markup=keyboard)
                return

        s.set_setting(setting_key, str(minutes))
        self._auto_clear_mode("category", day=day, store=s)

        if day:
            label = self._DAY_LABELS[day]
            await update.message.reply_text(f"{label} {cat_label} limit set to {minutes} min. Playback stops when budget runs out.")
        else:
            await update.message.reply_text(f"{cat_label} limit set to {minutes} min/day. Playback stops when budget runs out.")

    # --- Schedule ---

    async def _time_schedule(self, update: Update, args: list[str],
                             setting_key: str, day: str = "", store=None) -> None:
        """Handle /time [<day>] start|stop subcommands."""
        s = store or self.video_store
        is_start = setting_key.endswith("schedule_start")
        label = "Start" if is_start else "Stop"
        day_label = f"{self._DAY_LABELS[day]} " if day else ""

        if not args:
            current = s.get_setting(setting_key, "")
            if current:
                await update.message.reply_text(f"{day_label}{label} time: {format_time_12h(current)}")
            elif day:
                # Show effective (default fallback)
                base = "schedule_start" if is_start else "schedule_end"
                default = s.get_setting(base, "")
                if default:
                    await update.message.reply_text(
                        f"{day_label}{label} time: {format_time_12h(default)} (from default)"
                    )
                else:
                    await update.message.reply_text(f"{day_label}{label} time: not set")
            else:
                await update.message.reply_text(f"{label} time: not set")
            return

        value = args[0].lower()
        if value == "off":
            s.set_setting(setting_key, "")
            if day:
                await update.message.reply_text(f"{day_label}{label} time override cleared.")
            else:
                await update.message.reply_text(f"{label} time cleared.")
            return

        parsed = parse_time_input(args[0])
        if not parsed:
            await update.message.reply_text(
                "Invalid time. Examples: 800am, 8:00, 2000, 8:00PM"
            )
            return

        s.set_setting(setting_key, parsed)
        await update.message.reply_text(
            f"{day_label}{label} time set to {format_time_12h(parsed)}"
        )

    # --- Bonus ---

    async def _time_add_bonus(self, update: Update, args: list[str], store=None) -> None:
        """Handle /time add <minutes> — grant bonus screen time for today only."""
        s = store or self.video_store
        if not args or not args[0].isdigit():
            await update.message.reply_text("Usage: /time add <minutes>")
            return
        add_min = int(args[0])
        if add_min <= 0:
            await update.message.reply_text("Bonus minutes must be a positive number.")
            return
        today = get_today_str(self._get_tz())
        bonus_date = s.get_setting("daily_bonus_date", "")
        if bonus_date == today:
            existing = int(s.get_setting("daily_bonus_minutes", "0") or "0")
        else:
            existing = 0
        new_bonus = existing + add_min
        s.set_setting("daily_bonus_minutes", str(new_bonus))
        s.set_setting("daily_bonus_date", today)
        await update.message.reply_text(
            f"Added {add_min} bonus minutes for today ({new_bonus} total). Expires at midnight."
        )

    # --- Guided limit setup wizard ---

    async def _time_setup_start(self, update: Update, store=None) -> None:
        """Send top-level setup menu with Limits / Schedule choices."""
        # Store profile_id for wizard callbacks
        chat_id = update.effective_chat.id
        pid = store.profile_id if store and hasattr(store, 'profile_id') else "default"
        self._pending_wizard[chat_id] = {"step": "setup_top", "profile_id": pid}
        text = _md(
            "\u23f0 **Time Setup**\n\n"
            "What would you like to configure?\n\n"
            "**Limits** — daily screen time budgets\n"
            "**Schedule** — when videos are available"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Limits", callback_data="setup_top:limits"),
            InlineKeyboardButton("Schedule", callback_data="setup_top:schedule"),
        ]])
        await update.message.reply_text(text, parse_mode=MD2, reply_markup=keyboard)

    async def _cb_setup_top(self, query, choice: str) -> None:
        """Route top-level setup choice to limits or schedule wizard."""
        if choice == "limits":
            text = _md(
                "\u23f0 **Time Limit Setup**\n\n"
                "How would you like to manage screen time?\n\n"
                "**Simple** \u2014 one daily cap for all videos.\n"
                "**Category** \u2014 separate edu + fun budgets (total = edu + fun)."
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Simple Limit", callback_data="setup_mode:simple"),
                InlineKeyboardButton("Category Limits", callback_data="setup_mode:category"),
            ]])
            await _edit_msg(query, text, keyboard)
        elif choice == "schedule":
            text = _md(
                "Same schedule every day, or different times for specific days?"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Same for all days", callback_data="setup_sched_apply:all"),
                InlineKeyboardButton("Customize by day", callback_data="setup_sched_apply:custom"),
            ]])
            await _edit_msg(query, text, keyboard)

    # --- Schedule wizard helpers ---

    async def _setup_sched_start_menu(self, query, prefix: str = "setup_sched_start") -> None:
        """Show start-time presets."""
        text = _md("Set when watching is allowed to begin:")
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("7 AM", callback_data=f"{prefix}:07:00"),
            InlineKeyboardButton("8 AM", callback_data=f"{prefix}:08:00"),
            InlineKeyboardButton("9 AM", callback_data=f"{prefix}:09:00"),
            InlineKeyboardButton("Custom", callback_data=f"{prefix}:custom"),
        ]])
        await _edit_msg(query, text, keyboard)

    async def _setup_sched_stop_menu(self, query, start_display: str,
                                     prefix: str = "setup_sched_stop") -> None:
        """Show stop-time presets."""
        text = _md(
            f"Start: {start_display} \u2713\n"
            f"Now set when watching must stop:"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("7 PM", callback_data=f"{prefix}:19:00"),
            InlineKeyboardButton("8 PM", callback_data=f"{prefix}:20:00"),
            InlineKeyboardButton("9 PM", callback_data=f"{prefix}:21:00"),
            InlineKeyboardButton("Custom", callback_data=f"{prefix}:custom"),
        ]])
        await _edit_msg(query, text, keyboard)

    def _setup_sched_day_grid(self, store=None) -> tuple[str, InlineKeyboardMarkup]:
        """Build day-grid text and keyboard."""
        s = store or self.video_store
        # Show default schedule if set
        start = s.get_setting("schedule_start", "")
        end = s.get_setting("schedule_end", "")
        if start or end:
            start_disp = format_time_12h(start) if start else "not set"
            end_disp = format_time_12h(end) if end else "not set"
            header = f"Default: {start_disp} \u2013 {end_disp}\n\n"
        else:
            header = "Days without a schedule are open (no restrictions).\n\n"
        text = _md(
            f"{header}"
            f"Tap a day to set its schedule, or Done to finish."
        )
        # Build day buttons, mark overrides with bullet
        row1, row2 = [], []
        for day in DAY_NAMES:
            has_override = (
                s.get_setting(f"{day}_schedule_start", "") or
                s.get_setting(f"{day}_schedule_end", "")
            )
            label = self._DAY_LABELS[day][:3]
            if has_override:
                label += " \u2022"
            btn = InlineKeyboardButton(label, callback_data=f"setup_sched_day:{day}")
            if day in ("mon", "tue", "wed", "thu"):
                row1.append(btn)
            else:
                row2.append(btn)
        done_row = [InlineKeyboardButton("Done \u2713", callback_data="setup_sched_done")]
        keyboard = InlineKeyboardMarkup([row1, row2, done_row])
        return text, keyboard

    async def _cb_setup_sched_start(self, query, value: str) -> None:
        """Handle default start-time selection."""
        chat_id = query.message.chat_id
        ws = self._wizard_store(chat_id)
        if value == "custom":
            await _edit_msg(query, _md("Reply with the start time (e.g. 8am, 08:00):"))
            pid = self._pending_wizard.get(chat_id, {}).get("profile_id", "default")
            self._pending_wizard[chat_id] = {"step": "setup_sched_start", "profile_id": pid}
            return
        ws.set_setting("schedule_start", value)
        await self._setup_sched_stop_menu(query, format_time_12h(value))

    async def _cb_setup_sched_stop(self, query, value: str) -> None:
        """Handle default stop-time selection — goes to done summary."""
        chat_id = query.message.chat_id
        ws = self._wizard_store(chat_id)
        if value == "custom":
            await _edit_msg(query, _md("Reply with the stop time (e.g. 8pm, 20:00):"))
            pid = self._pending_wizard.get(chat_id, {}).get("profile_id", "default")
            self._pending_wizard[chat_id] = {"step": "setup_sched_stop", "profile_id": pid}
            return
        ws.set_setting("schedule_end", value)
        await self._cb_setup_sched_done(query)

    async def _cb_setup_sched_apply(self, query, choice: str) -> None:
        """Route same-for-all (start picker) vs customize-by-day (day grid)."""
        if choice == "all":
            await self._setup_sched_start_menu(query)
        elif choice == "custom":
            ws = self._wizard_store(query.message.chat_id)
            text, keyboard = self._setup_sched_day_grid(store=ws)
            await _edit_msg(query, text, keyboard)

    async def _cb_setup_sched_day(self, query, day: str) -> None:
        """Show per-day start-time picker."""
        if day not in DAY_NAMES:
            return
        ws = self._wizard_store(query.message.chat_id)
        label = self._DAY_LABELS[day]
        start = self._effective_setting(day, "schedule_start", store=ws)
        end = self._effective_setting(day, "schedule_end", store=ws)
        start_disp = format_time_12h(start) if start else "not set"
        end_disp = format_time_12h(end) if end else "not set"
        # Check if this day has its own overrides
        has_own = (
            ws.get_setting(f"{day}_schedule_start", "") or
            ws.get_setting(f"{day}_schedule_end", "")
        )
        source = "" if has_own else " (default)"
        text = _md(
            f"**{label}** \u2014 currently {start_disp} \u2013 {end_disp}{source}\n\n"
            f"Set start time for {label}:"
        )
        # Offer presets near the current default
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("8 AM", callback_data=f"setup_daystart:{day}:08:00"),
            InlineKeyboardButton("9 AM", callback_data=f"setup_daystart:{day}:09:00"),
            InlineKeyboardButton("10 AM", callback_data=f"setup_daystart:{day}:10:00"),
            InlineKeyboardButton("Custom", callback_data=f"setup_daystart:{day}:custom"),
        ]])
        await _edit_msg(query, text, keyboard)

    async def _cb_setup_daystart(self, query, day: str, value: str) -> None:
        """Handle per-day start-time selection."""
        if day not in DAY_NAMES:
            return
        chat_id = query.message.chat_id
        ws = self._wizard_store(chat_id)
        if value == "custom":
            label = self._DAY_LABELS[day]
            await _edit_msg(query, _md(f"Reply with start time for {label} (e.g. 9am, 09:00):"))
            pid = self._pending_wizard.get(chat_id, {}).get("profile_id", "default")
            self._pending_wizard[chat_id] = {"step": f"setup_daystart:{day}", "profile_id": pid}
            return
        ws.set_setting(f"{day}_schedule_start", value)
        label = self._DAY_LABELS[day]
        text = _md(
            f"{label} start: {format_time_12h(value)} \u2713\n"
            f"Set stop time for {label}:"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("8 PM", callback_data=f"setup_daystop:{day}:20:00"),
            InlineKeyboardButton("9 PM", callback_data=f"setup_daystop:{day}:21:00"),
            InlineKeyboardButton("10 PM", callback_data=f"setup_daystop:{day}:22:00"),
            InlineKeyboardButton("Custom", callback_data=f"setup_daystop:{day}:custom"),
        ]])
        await _edit_msg(query, text, keyboard)

    async def _cb_setup_daystop(self, query, day: str, value: str) -> None:
        """Handle per-day stop-time selection."""
        if day not in DAY_NAMES:
            return
        chat_id = query.message.chat_id
        ws = self._wizard_store(chat_id)
        if value == "custom":
            label = self._DAY_LABELS[day]
            await _edit_msg(query, _md(f"Reply with stop time for {label} (e.g. 9pm, 21:00):"))
            pid = self._pending_wizard.get(chat_id, {}).get("profile_id", "default")
            self._pending_wizard[chat_id] = {"step": f"setup_daystop:{day}", "profile_id": pid}
            return
        ws.set_setting(f"{day}_schedule_end", value)
        text, keyboard = self._setup_sched_day_grid(store=ws)
        await _edit_msg(query, text, keyboard)

    async def _cb_setup_sched_done(self, query) -> None:
        """Final summary when schedule wizard completes."""
        ws = self._wizard_store(query.message.chat_id)
        start = ws.get_setting("schedule_start", "")
        end = ws.get_setting("schedule_end", "")
        start_disp = format_time_12h(start) if start else "not set"
        end_disp = format_time_12h(end) if end else "not set"
        lines = [
            f"\u2713 **Schedule configured**\n",
            f"Default: {start_disp} \u2013 {end_disp}",
        ]
        # List per-day overrides
        for day in DAY_NAMES:
            ds = ws.get_setting(f"{day}_schedule_start", "")
            de = ws.get_setting(f"{day}_schedule_end", "")
            if ds or de:
                label = self._DAY_LABELS[day][:3]
                ds_disp = format_time_12h(ds) if ds else start_disp
                de_disp = format_time_12h(de) if de else end_disp
                lines.append(f"{label}: {ds_disp} \u2013 {de_disp}")
        lines.append(f"\nUse `/time <day> start|stop` to adjust later.")
        await _edit_msg(query, _md("\n".join(lines)))

    async def _cb_setup_mode(self, query, mode: str) -> None:
        """Handle mode choice from wizard."""
        if mode == "simple":
            text = _md(
                "Set a daily screen time limit. All videos share one pool.\n\n"
                "Pick a preset or reply with a custom number:"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("60 min", callback_data="setup_simple:60"),
                InlineKeyboardButton("90 min", callback_data="setup_simple:90"),
                InlineKeyboardButton("120 min", callback_data="setup_simple:120"),
                InlineKeyboardButton("Custom", callback_data="setup_simple:custom"),
            ]])
            await _edit_msg(query, text, keyboard)
        elif mode == "category":
            text = _md(
                "Category mode gives separate budgets for educational and "
                "entertainment videos. Total screen time = edu + fun.\n\n"
                "Set **educational** limit:"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("60 min", callback_data="setup_edu:60"),
                InlineKeyboardButton("90 min", callback_data="setup_edu:90"),
                InlineKeyboardButton("120 min", callback_data="setup_edu:120"),
                InlineKeyboardButton("Custom", callback_data="setup_edu:custom"),
            ]])
            await _edit_msg(query, text, keyboard)

    async def _cb_setup_simple(self, query, value: str) -> None:
        """Handle simple limit selection."""
        chat_id = query.message.chat_id
        ws = self._wizard_store(chat_id)
        if value == "custom":
            await _edit_msg(query, "Reply with the number of minutes:")
            pid = self._pending_wizard.get(chat_id, {}).get("profile_id", "default")
            self._pending_wizard[chat_id] = {"step": "setup_simple", "profile_id": pid}
            return
        minutes = int(value)
        ws.set_setting("daily_limit_minutes", str(minutes))
        self._auto_clear_mode("simple", store=ws)
        text = _md(
            f"\u2713 **Simple limit set**\n"
            f"  Daily cap: {minutes} min/day\n\n"
            f"These apply to all days. Use `/time <day> limit <min>` to "
            f"customize specific days."
        )
        await _edit_msg(query, text)

    async def _cb_setup_edu(self, query, value: str) -> None:
        """Handle edu limit selection in wizard."""
        chat_id = query.message.chat_id
        ws = self._wizard_store(chat_id)
        if value == "custom":
            await _edit_msg(query, "Reply with the number of minutes for **educational** limit:")
            pid = self._pending_wizard.get(chat_id, {}).get("profile_id", "default")
            self._pending_wizard[chat_id] = {"step": "setup_edu", "profile_id": pid}
            return
        minutes = int(value)
        ws.set_setting("edu_limit_minutes", str(minutes))
        self._auto_clear_mode("category", store=ws)
        text = _md(
            f"Educational: {minutes} min \u2713\n"
            f"Now set **entertainment** limit:"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("30 min", callback_data="setup_fun:30"),
            InlineKeyboardButton("60 min", callback_data="setup_fun:60"),
            InlineKeyboardButton("90 min", callback_data="setup_fun:90"),
            InlineKeyboardButton("Custom", callback_data="setup_fun:custom"),
        ]])
        await _edit_msg(query, text, keyboard)

    async def _cb_setup_fun(self, query, value: str) -> None:
        """Handle fun limit selection in wizard."""
        chat_id = query.message.chat_id
        ws = self._wizard_store(chat_id)
        if value == "custom":
            await _edit_msg(query, "Reply with the number of minutes for **entertainment** limit:")
            pid = self._pending_wizard.get(chat_id, {}).get("profile_id", "default")
            self._pending_wizard[chat_id] = {"step": "setup_fun", "profile_id": pid}
            return
        minutes = int(value)
        ws.set_setting("fun_limit_minutes", str(minutes))
        self._auto_clear_mode("category", store=ws)
        edu = int(ws.get_setting("edu_limit_minutes", "0") or "0")
        total = edu + minutes
        text = _md(
            f"\u2713 **Category limits set**\n"
            f"  Educational: {edu} min/day\n"
            f"  Entertainment: {minutes} min/day\n"
            f"  Total: {total} min/day\n\n"
            f"These apply to all days. Use `/time <day> edu|fun <min>` to "
            f"customize specific days."
        )
        await _edit_msg(query, text)

    async def _cb_switch_confirm(self, query, choice: str) -> None:
        """Handle mode switch confirmation callback."""
        if choice == "keep":
            await _edit_msg(query, "Keeping current settings.")
            return

        parts = choice.split(":")
        # Format: {pid}:simple:{minutes} or {pid}:category:{cat}:{minutes}
        if len(parts) >= 3 and parts[1] == "simple" and parts[2].isdigit():
            pid = parts[0]
            ws = self._child_store(pid)
            minutes = int(parts[2])
            ws.set_setting("daily_limit_minutes", str(minutes))
            self._auto_clear_mode("simple", store=ws)
            text = _md(f"\u2713 Switched to simple limit: {minutes} min/day")
            await _edit_msg(query, text)
        elif len(parts) >= 4 and parts[1] == "category" and parts[3].isdigit():
            pid = parts[0]
            ws = self._child_store(pid)
            category = parts[2]
            minutes = int(parts[3])
            ws.set_setting(f"{category}_limit_minutes", str(minutes))
            self._auto_clear_mode("category", store=ws)
            cat_label = "Educational" if category == "edu" else "Entertainment"
            other = "fun" if category == "edu" else "edu"
            other_label = "Entertainment" if category == "edu" else "Educational"
            text = _md(
                f"\u2713 Switched to category mode.\n"
                f"  {cat_label}: {minutes} min/day\n\n"
                f"Set the {other_label} limit with `/time {other} <minutes>`."
            )
            await _edit_msg(query, text)

    # --- Wizard custom reply handler ---

    async def _handle_wizard_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle text replies during wizard custom input."""
        if not self._check_admin(update):
            return
        chat_id = update.effective_chat.id
        state = self._pending_wizard.get(chat_id)
        if not state:
            return  # No wizard active
        text = update.message.text.strip()
        step = state["step"]
        ws = self._wizard_store(chat_id)

        # Schedule wizard steps expect time input, not minutes
        if step.startswith("setup_sched_") or step.startswith("setup_daystart:") or step.startswith("setup_daystop:"):
            parsed = parse_time_input(text)
            if not parsed:
                await update.message.reply_text(
                    "Invalid time. Examples: 8am, 08:00, 2000, 8:00PM"
                )
                return
            del self._pending_wizard[chat_id]

            if step == "setup_sched_start":
                ws.set_setting("schedule_start", parsed)
                # Show stop-time picker (as new message since we can't edit)
                stop_text = _md(
                    f"Start: {format_time_12h(parsed)} \u2713\n"
                    f"Now set when watching must stop:"
                )
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("7 PM", callback_data="setup_sched_stop:19:00"),
                    InlineKeyboardButton("8 PM", callback_data="setup_sched_stop:20:00"),
                    InlineKeyboardButton("9 PM", callback_data="setup_sched_stop:21:00"),
                    InlineKeyboardButton("Custom", callback_data="setup_sched_stop:custom"),
                ]])
                await update.message.reply_text(stop_text, parse_mode=MD2, reply_markup=keyboard)
            elif step == "setup_sched_stop":
                ws.set_setting("schedule_end", parsed)
                start = ws.get_setting("schedule_start", "")
                start_disp = format_time_12h(start) if start else "not set"
                end_disp = format_time_12h(parsed)
                lines = [
                    f"\u2713 **Schedule configured**\n",
                    f"Default: {start_disp} \u2013 {end_disp}",
                    f"\nUse `/time <day> start|stop` to adjust later.",
                ]
                await update.message.reply_text(_md("\n".join(lines)), parse_mode=MD2)
            elif step.startswith("setup_daystart:"):
                day = step.split(":", 1)[1]
                ws.set_setting(f"{day}_schedule_start", parsed)
                label = self._DAY_LABELS[day]
                stop_text = _md(
                    f"{label} start: {format_time_12h(parsed)} \u2713\n"
                    f"Set stop time for {label}:"
                )
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("8 PM", callback_data=f"setup_daystop:{day}:20:00"),
                    InlineKeyboardButton("9 PM", callback_data=f"setup_daystop:{day}:21:00"),
                    InlineKeyboardButton("10 PM", callback_data=f"setup_daystop:{day}:22:00"),
                    InlineKeyboardButton("Custom", callback_data=f"setup_daystop:{day}:custom"),
                ]])
                await update.message.reply_text(stop_text, parse_mode=MD2, reply_markup=keyboard)
            elif step.startswith("setup_daystop:"):
                day = step.split(":", 1)[1]
                ws.set_setting(f"{day}_schedule_end", parsed)
                grid_text, keyboard = self._setup_sched_day_grid(store=ws)
                await update.message.reply_text(grid_text, parse_mode=MD2, reply_markup=keyboard)
            return

        # Limit wizard steps expect positive integer minutes
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text("Please reply with a positive number of minutes.")
            return
        minutes = int(text)
        del self._pending_wizard[chat_id]

        if step == "setup_simple":
            ws.set_setting("daily_limit_minutes", str(minutes))
            self._auto_clear_mode("simple", store=ws)
            await update.message.reply_text(_md(
                f"\u2713 **Simple limit set**\n"
                f"  Daily cap: {minutes} min/day\n\n"
                f"Use `/time <day> limit <min>` to customize specific days."
            ), parse_mode=MD2)
        elif step == "setup_edu":
            ws.set_setting("edu_limit_minutes", str(minutes))
            self._auto_clear_mode("category", store=ws)
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("30 min", callback_data="setup_fun:30"),
                InlineKeyboardButton("60 min", callback_data="setup_fun:60"),
                InlineKeyboardButton("90 min", callback_data="setup_fun:90"),
                InlineKeyboardButton("Custom", callback_data="setup_fun:custom"),
            ]])
            await update.message.reply_text(_md(
                f"Educational: {minutes} min \u2713\n"
                f"Now set **entertainment** limit:"
            ), parse_mode=MD2, reply_markup=keyboard)
        elif step == "setup_fun":
            ws.set_setting("fun_limit_minutes", str(minutes))
            self._auto_clear_mode("category", store=ws)
            edu = int(ws.get_setting("edu_limit_minutes", "0") or "0")
            total = edu + minutes
            await update.message.reply_text(_md(
                f"\u2713 **Category limits set**\n"
                f"  Educational: {edu} min/day\n"
                f"  Entertainment: {minutes} min/day\n"
                f"  Total: {total} min/day\n\n"
                f"Use `/time <day> edu|fun <min>` to customize specific days."
            ), parse_mode=MD2)
