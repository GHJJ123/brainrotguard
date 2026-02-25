"""Channel management mixin: /channel command, starter channels, inline callbacks."""

import logging
from urllib.parse import quote

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot.helpers import _md, _answer_bg, _nav_row, _edit_msg, MD2
from utils import CAT_LABELS

logger = logging.getLogger(__name__)


class ChannelMixin:
    """Channel management methods extracted from BrainRotGuardBot."""

    _CHANNEL_PAGE_SIZE = 10
    _STARTER_PAGE_SIZE = 10

    async def _cmd_channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return

        async def _inner(update, context, cs, profile):
            args = context.args or []
            if not args:
                await self._channel_list(update, store=cs)
                return
            sub = args[0].lower()
            rest = args[1:]

            if sub == "allow":
                await self._channel_allow(update, rest, store=cs)
            elif sub == "unallow":
                await self._channel_unallow(update, rest, store=cs)
            elif sub == "block":
                await self._channel_block(update, rest, store=cs)
            elif sub == "unblock":
                await self._channel_unblock(update, rest, store=cs)
            elif sub == "cat":
                await self._channel_set_cat(update, rest, store=cs)
            elif sub == "starter":
                await self._channel_starter(update, store=cs)
            else:
                await update.effective_message.reply_text(
                    "Usage: /channel allow|unallow|block|unblock|cat|starter <name>"
                )

        await self._with_child_context(update, context, _inner, allow_all=True)

    # --- Starter channels ---

    async def _channel_starter(self, update: Update, store=None) -> None:
        """Handle /channel starter — show importable starter channels."""
        if not self._starter_channels:
            await update.effective_message.reply_text("No starter channels configured.")
            return
        s = store or self.video_store
        pid = getattr(s, 'profile_id', 'default')
        text, markup = self._render_starter_message(store=s, profile_id=pid)
        await update.effective_message.reply_text(
            text, parse_mode=MD2, reply_markup=markup, disable_web_page_preview=True,
        )

    def _render_starter_message(self, page: int = 0, store=None, profile_id: str = "default") -> tuple[str, InlineKeyboardMarkup | None]:
        """Build starter channels message with per-channel Import buttons and pagination."""
        s = store or self.video_store
        existing = s.get_channel_handles_set()
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
                    f"Import: {name}", callback_data=f"starter_import:{profile_id}:{idx}",
                )])

        nav = _nav_row(page, total, ps, f"starter_page:{profile_id}")
        if nav:
            buttons.append(nav)
        markup = InlineKeyboardMarkup(buttons) if buttons else None
        return _md("\n".join(lines)), markup

    async def _cb_starter_page(self, query, profile_id: str, page: int) -> None:
        """Handle starter channels pagination."""
        _answer_bg(query)
        cs = self._child_store(profile_id)
        text, markup = self._render_starter_message(page, store=cs, profile_id=profile_id)
        await _edit_msg(query, text, markup, disable_preview=True)

    async def _cb_starter_import(self, query, profile_id: str, idx: int) -> None:
        """Handle Import button press from starter channels message."""
        if idx < 0 or idx >= len(self._starter_channels):
            await query.answer("Invalid channel.")
            return
        cs = self._child_store(profile_id)
        ch = self._starter_channels[idx]
        handle = ch["handle"]
        name = ch["name"]
        cat = ch.get("category")

        # Idempotency: already imported?
        existing = cs.get_channel_handles_set()
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
            cs.add_channel(name, "allowed", channel_id=cid, handle=handle, category=cat)
            if self.on_channel_change:
                self.on_channel_change(profile_id)

        # Acknowledge callback in background (non-blocking)
        msg = f"Already imported: {name}" if already else f"Imported: {name}"
        _answer_bg(query, msg)

        # Re-render the message immediately
        page = idx // self._STARTER_PAGE_SIZE
        text, markup = self._render_starter_message(page, store=cs, profile_id=profile_id)
        await _edit_msg(query, text, markup, disable_preview=True)

    # --- Allow / block / remove ---

    async def _channel_allow(self, update: Update, args: list[str], store=None) -> None:
        await self._channel_resolve_and_add(update, args, "allowed", store=store)

    async def _channel_unallow(self, update: Update, args: list[str], store=None) -> None:
        await self._channel_remove(update, args, "unallow", store=store)

    async def _channel_block(self, update: Update, args: list[str], store=None) -> None:
        await self._channel_resolve_and_add(update, args, "blocked", store=store)

    async def _channel_resolve_and_add(self, update: Update, args: list[str], status: str, store=None) -> None:
        """Resolve a @handle via yt-dlp and add to channel list."""
        s = store or self.video_store
        pid = getattr(s, 'profile_id', 'default')
        verb = "allow" if status == "allowed" else "block"
        example = "@LEGO" if status == "allowed" else "@Slurry"
        if not args:
            await update.effective_message.reply_text(f"Usage: /channel {verb} @handle\nExample: /channel {verb} {example}")
            return
        raw = args[0]
        if not raw.startswith("@"):
            await update.effective_message.reply_text(
                f"Please use the channel's @handle (e.g. {example}).\n"
                "You can find it on the channel's YouTube page."
            )
            return
        await update.effective_message.reply_text(f"Looking up {raw} on YouTube...")
        from youtube.extractor import resolve_channel_handle
        info = await resolve_channel_handle(raw)
        if not info or not info.get("channel_name"):
            await update.effective_message.reply_text(f"Couldn't find a channel for {raw}. Check the spelling or try the full @handle from YouTube.")
            return
        channel_name = info["channel_name"]
        channel_id = info.get("channel_id")
        handle = info.get("handle")
        cat = None
        if status == "allowed" and len(args) > 1 and args[1].lower() in ("edu", "fun"):
            cat = args[1].lower()
        s.add_channel(channel_name, status, channel_id=channel_id, handle=handle, category=cat)
        if self.on_channel_change:
            self.on_channel_change(pid)
        if status == "allowed":
            cat_label = {"edu": "Educational", "fun": "Entertainment"}.get(cat, "No category")
            await update.effective_message.reply_text(
                f"Added to allowlist: {channel_name} ({raw})\nCategory: {cat_label}"
            )
        else:
            await update.effective_message.reply_text(f"Blocked: {channel_name}\nVideos from this channel will be auto-denied.")

    async def _channel_unblock(self, update: Update, args: list[str], store=None) -> None:
        await self._channel_remove(update, args, "unblock", store=store)

    async def _channel_remove(self, update: Update, args: list[str], verb: str, store=None) -> None:
        """Remove a channel from allow/block list."""
        s = store or self.video_store
        pid = getattr(s, 'profile_id', 'default')
        if not args:
            await update.effective_message.reply_text(f"Usage: /channel {verb} <channel name>")
            return
        channel = " ".join(args)
        # Look up channel_id before removing (remove_channel deletes the row)
        ch_id = ""
        status = "allowed" if verb == "unallow" else "blocked"
        for name, cid, _h, _c in s.get_channels_with_ids(status):
            if name.lower() == channel.lower():
                ch_id = cid or ""
                break
        if s.remove_channel(channel):
            if verb == "unallow":
                deleted = s.delete_channel_videos(channel, channel_id=ch_id)
            else:
                deleted = 0
            if self.on_channel_change:
                self.on_channel_change(pid)
            label = "Removed from allowlist" if verb == "unallow" else "Unblocked"
            extra = f" Deleted {deleted} video{'s' if deleted != 1 else ''} from catalog." if deleted else ""
            await update.effective_message.reply_text(f"{label}: {channel}.{extra}")
        else:
            await update.effective_message.reply_text(f"Channel not in list: {channel}. Use /channel to see all channels.")

    async def _channel_set_cat(self, update: Update, args: list[str], store=None) -> None:
        """Handle /channel cat <name> edu|fun."""
        s = store or self.video_store
        pid = getattr(s, 'profile_id', 'default')
        if len(args) < 2:
            await update.effective_message.reply_text("Usage: /channel cat <name> edu|fun\n\nThis sets which time budget the channel's videos count against.")
            return
        cat = args[-1].lower()
        if cat not in ("edu", "fun"):
            await update.effective_message.reply_text("Category must be edu (Educational) or fun (Entertainment).")
            return
        raw = " ".join(args[:-1])
        channel = s.resolve_channel_name(raw) or raw
        if s.set_channel_category(channel, cat):
            # Look up channel_id for stable matching
            ch_id = ""
            for name, cid, _h, _c in s.get_channels_with_ids("allowed"):
                if name.lower() == channel.lower():
                    ch_id = cid or ""
                    break
            s.set_channel_videos_category(channel, cat, channel_id=ch_id)
            cat_label = CAT_LABELS.get(cat, "Entertainment")
            if self.on_channel_change:
                self.on_channel_change(pid)
            await update.effective_message.reply_text(f"**{channel}** → {cat_label}\nExisting videos from this channel updated too.", parse_mode=MD2)
        else:
            await update.effective_message.reply_text(f"Channel not in list: {raw}. Use /channel to see all channels.")

    # --- Channel list rendering + callbacks ---

    def _render_channel_menu(self, store=None, profile_id: str = "default") -> tuple[str, InlineKeyboardMarkup | None]:
        """Build the channel menu with Allowed/Blocked buttons and summary stats."""
        s = store or self.video_store
        allowed = s.get_channels_with_ids("allowed")
        blocked = s.get_channels_with_ids("blocked")
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
                f"Allowed ({len(allowed)})", callback_data=f"chan_filter:{profile_id}:allowed",
            ))
        if blocked:
            row.append(InlineKeyboardButton(
                f"Blocked ({len(blocked)})", callback_data=f"chan_filter:{profile_id}:blocked",
            ))
        return text, InlineKeyboardMarkup([row]) if row else None

    def _render_channel_page(self, status: str, page: int = 0, store=None, profile_id: str = "default") -> tuple[str, InlineKeyboardMarkup | None]:
        """Build text + inline buttons for a page of the channel list filtered by status."""
        s = store or self.video_store
        entries = s.get_channels_with_ids(status)
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
                btn_label, callback_data=f"{btn_action}:{profile_id}:{ch}"
            )])

        nav = _nav_row(page, total, page_size, f"chan_page:{profile_id}:{status}")
        if nav:
            buttons.append(nav)
        # Back to menu
        buttons.append([InlineKeyboardButton("\U0001f4cb Channels", callback_data=f"chan_menu:{profile_id}")])

        text = _md("\n".join(lines))
        markup = InlineKeyboardMarkup(buttons) if buttons else None
        return text, markup

    async def _channel_list(self, update: Update, store=None) -> None:
        s = store or self.video_store
        pid = getattr(s, 'profile_id', 'default')
        text, markup = self._render_channel_menu(store=s, profile_id=pid)
        await update.effective_message.reply_text(
            text, parse_mode=MD2, disable_web_page_preview=True,
            reply_markup=markup,
        )

    async def _cb_channel_filter(self, query, profile_id: str, status: str) -> None:
        """Handle Allowed/Blocked button press from channel menu."""
        _answer_bg(query)
        cs = self._child_store(profile_id)
        text, markup = self._render_channel_page(status, 0, store=cs, profile_id=profile_id)
        await _edit_msg(query, text, markup, disable_preview=True)

    async def _cb_channel_menu(self, query, profile_id: str = "default") -> None:
        """Handle back-to-menu button press."""
        _answer_bg(query)
        cs = self._child_store(profile_id)
        text, markup = self._render_channel_menu(store=cs, profile_id=profile_id)
        await _edit_msg(query, text, markup, disable_preview=True)

    async def _cb_channel_page(self, query, profile_id: str, status: str, page: int) -> None:
        """Handle channel list pagination."""
        _answer_bg(query)
        cs = self._child_store(profile_id)
        text, markup = self._render_channel_page(status, page, store=cs, profile_id=profile_id)
        await _edit_msg(query, text, markup, disable_preview=True)

    async def _update_channel_list_message(self, query, profile_id: str = "default") -> None:
        """Refresh back to channel menu after unallow/unblock."""
        cs = self._child_store(profile_id)
        text, markup = self._render_channel_menu(store=cs, profile_id=profile_id)
        await _edit_msg(query, text, markup, disable_preview=True)
