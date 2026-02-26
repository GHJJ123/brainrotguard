"""Approval mixin: video request notifications, auto-approve, child selector, profile deletion."""

import logging
import re
from io import BytesIO
from urllib.parse import urlparse

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup

from bot.helpers import _md, _channel_md_link, _answer_bg, _edit_msg, MD2
from utils import CAT_LABELS
from youtube.extractor import format_duration, THUMB_ALLOWED_HOSTS

logger = logging.getLogger(__name__)


class ApprovalMixin:
    """Approval-related methods extracted from BrainRotGuardBot."""

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
            child_name = p["display_name"] if p else ""

        # Check if already approved for another child
        other = self.video_store.find_video_approved_for_others(video_id, profile_id)
        cross_child_note = ""
        if other and len(profiles) > 1:
            other_profile = self.video_store.get_profile(other["profile_id"])
            other_name = other_profile["display_name"] if other_profile else "another child"
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
            [InlineKeyboardButton("▶️ Watch on YouTube", url=yt_link)],
        ]
        # If cross-child approved, show auto-approve button
        if other and len(profiles) > 1:
            buttons.append([
                InlineKeyboardButton("⚡ Auto-approve", callback_data=f"autoapprove:{pid}:{video_id}"),
            ])
        buttons.extend([
            [
                InlineKeyboardButton("📚 Approve Edu", callback_data=f"approve_edu:{pid}:{video_id}"),
                InlineKeyboardButton("🎮 Approve Fun", callback_data=f"approve_fun:{pid}:{video_id}"),
            ],
            [
                InlineKeyboardButton("🚫 Deny", callback_data=f"deny:{pid}:{video_id}"),
            ],
            [
                InlineKeyboardButton("📚 Allow Ch Edu", callback_data=f"allowchan_edu:{pid}:{video_id}"),
                InlineKeyboardButton("🎮 Allow Ch Fun", callback_data=f"allowchan_fun:{pid}:{video_id}"),
            ],
            [
                InlineKeyboardButton("🔒 Block Channel", callback_data=f"blockchan:{pid}:{video_id}"),
            ],
        ])
        keyboard = InlineKeyboardMarkup(buttons)

        try:
            # Try to send with thumbnail (only fetch from known YouTube CDN domains)
            thumbnail_url = video.get('thumbnail_url')
            if thumbnail_url:
                parsed = urlparse(thumbnail_url)
                if not parsed.hostname or parsed.hostname not in THUMB_ALLOWED_HOSTS:
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
        cat_label = CAT_LABELS.get(cat, "Entertainment")
        result_text = _md(
            f"**AUTO-APPROVED ({cat_label})**\n\n"
            f"**Title:** {video['title']}\n"
            f"**Channel:** {channel_link}\n"
            f"[Watch on YouTube]({yt_link})"
        )
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ Revoke", callback_data=f"revoke:{profile_id}:{video_id}"),
        ]])
        try:
            await query.edit_message_caption(caption=result_text, reply_markup=reply_markup, parse_mode=MD2)
        except Exception:
            await query.edit_message_text(text=result_text, reply_markup=reply_markup, parse_mode=MD2)

    async def _cb_resend(self, query, profile_id: str, video_id: str) -> None:
        """Resend notification for a pending video from /pending list."""
        cs = self._child_store(profile_id)
        video = cs.get_video(video_id)
        if not video or video['status'] != 'pending':
            await query.answer("No longer pending.")
            return
        _answer_bg(query, "Resending...")
        await self.notify_new_request(video, profile_id=profile_id)

    async def _cb_child_delete_confirm(self, query, profile_id: str) -> None:
        """Handle profile deletion confirmation."""
        p = self.video_store.get_profile(profile_id)
        if not p:
            await query.answer("Profile not found.")
            return
        if self.video_store.delete_profile(profile_id):
            if self.on_channel_change:
                self.on_channel_change()
            await _edit_msg(query, _md(f"Deleted profile: **{p['display_name']}** and all associated data."))
        else:
            await query.answer("Failed to delete profile.")

    async def _cb_video_action(self, query, action: str, profile_id: str, video_id: str) -> None:
        """Handle approve/deny/revoke/allowchan/blockchan/setcat actions on a video."""
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
            _answer_bg(query, f"\u2192 {cat_label}")
            toggle_cat = "edu" if cat == "fun" else "fun"
            toggle_label = "📚 \u2192 Edu" if toggle_cat == "edu" else "🎮 \u2192 Fun"
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Revoke", callback_data=f"revoke:{profile_id}:{video_id}"),
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
            toggle_label = "📚 \u2192 Edu" if toggle_cat == "edu" else "🎮 \u2192 Fun"
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Revoke", callback_data=f"revoke:{profile_id}:{video_id}"),
                InlineKeyboardButton(toggle_label, callback_data=f"setcat_{toggle_cat}:{profile_id}:{video_id}"),
            ]])
        else:
            reply_markup = None

        try:
            await query.edit_message_caption(caption=result_text, reply_markup=reply_markup, parse_mode=MD2)
        except Exception:
            await query.edit_message_text(text=result_text, reply_markup=reply_markup, parse_mode=MD2)
