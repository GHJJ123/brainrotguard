"""Commands mixin: child profiles, start/help/shorts, pending/approved/revoke, stats/changelog."""

import re
import logging
from typing import Optional
from urllib.parse import quote

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot.helpers import _md, _answer_bg, _nav_row, _edit_msg, _channel_md_link, MD2
from utils import CAT_LABELS
from youtube.extractor import format_duration

logger = logging.getLogger(__name__)


class CommandsMixin:
    """General command methods extracted from BrainRotGuardBot."""

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
        existing = self.video_store.get_profile(pid)
        if existing:
            await update.effective_message.reply_text(f"Profile ID conflict with '{existing['display_name']}' — try a different name.")
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
        # Check for display_name uniqueness
        conflict = self._find_profile(new_name)
        if conflict and conflict["id"] != target["id"]:
            await update.effective_message.reply_text(f"A profile named '{new_name}' already exists.")
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
        """Send the setup hub."""
        if not await self._require_admin(update):
            return
        await self._send_setup_hub(update)

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
            "`/watch [yesterday|N]` - Watch activity & time budget\n"
            "`/logs [days|today]` - Activity report\n"
            "`/changelog` - Latest changes\n\n"
            "**Channel:**\n"
            "`/channel` - List all channels\n"
            "`/channel starter` - Kid-friendly starter list\n"
            "`/channel allow|unallow @handle [edu|fun]`\n"
            "`/channel block|unblock @handle`\n"
            "`/channel cat <name> edu|fun`\n\n"
            "**Filters & Search:**\n"
            "`/filter` - List word filters\n"
            "`/filter add|remove <word>`\n"
            "`/search [days|today|all]` - Search history\n\n"
            "**Time & Schedule:**\n"
            "`/time` - Show status & weekly view\n"
            "`/time setup` - Guided limit wizard\n"
            "`/time <min|off>` - Simple watch limit\n"
            "`/time edu|fun <min|off>` - Category limits\n"
            "`/time start|stop [time|off]` - Schedule\n"
            "`/time add <min>` - Bonus for today\n"
            "`/time <day> [start|stop|edu|fun|limit|off]`\n"
            "`/time <day> copy <days|weekdays|weekend|all>`\n"
            "`/shorts [on|off]` - Toggle Shorts row\n\n"
            "**Profiles:**\n"
            "`/child` - List child profiles\n"
            "`/child add <name> [pin]`\n"
            "`/child remove|rename|pin <name>`\n\n"
            "**Setup:**\n"
            "`/setup` - Interactive setup hub\n\n"
            f"{help_link}"
            "☕ [Buy me a coffee](https://ko-fi.com/coffee4jj)"
        ), parse_mode=MD2, disable_web_page_preview=True)

    async def _cmd_shorts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Toggle Shorts display on/off or show status."""
        if not await self._require_admin(update):
            return

        async def _inner(update, context, cs, profile):
            args = context.args
            ctx = self._ctx_label(profile)
            is_default = cs.profile_id == "default"
            if args and args[0].lower() in ("on", "off"):
                enabled = args[0].lower() == "on"
                cs.set_setting("shorts_enabled", str(enabled).lower())
                if self.on_channel_change:
                    self.on_channel_change()
                if enabled:
                    await update.effective_message.reply_text(_md(
                        f"**Shorts enabled{ctx}**\n\n"
                        "- Shorts row appears on the homepage below videos\n"
                        "- Shorts from allowlisted channels are fetched on next cache refresh\n"
                        "- Shorts still count toward category time budgets (edu/fun)\n"
                        "- Shorts hidden from search results remain hidden"
                    ), parse_mode=MD2)
                else:
                    await update.effective_message.reply_text(_md(
                        f"**Shorts disabled{ctx}**\n\n"
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
                        f"**Shorts: enabled{ctx}**\n\n"
                        "Shorts appear in a dedicated row on the homepage and are "
                        "fetched from allowlisted channels. They count toward "
                        "edu/fun time budgets like regular videos.\n\n"
                        "`/shorts off` — hide Shorts everywhere"
                    ), parse_mode=MD2)
                else:
                    await update.effective_message.reply_text(_md(
                        f"**Shorts: disabled{ctx}**\n\n"
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

        ctx = self._ctx_label({"display_name": self._profile_name(profile_id)}) if len(self._get_profiles()) > 1 else ""
        header = f"**Pending Requests{ctx} ({total})**"
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

        ctx = self._ctx_label({"display_name": self._profile_name(profile_id)}) if len(self._get_profiles()) > 1 else ""
        if search:
            header = f"\U0001f50d **\"{search}\"{ctx} ({total} result{'s' if total != 1 else ''})**"
        else:
            header = f"\U0001f4cb **Approved{ctx} ({total})**"
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

    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return

        async def _inner(update, context, cs, profile):
            ctx = self._ctx_label(profile)
            stats = cs.get_stats()
            await update.effective_message.reply_text(_md(
                f"**Stats{ctx}**\n\n"
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

