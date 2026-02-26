"""Activity mixin: /watch, /logs, /search, /filter commands."""

import logging

from telegram import Update, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot.helpers import _md, _answer_bg, _nav_row, _edit_msg, _channel_md_link, MD2
from bot.timelimits import _progress_bar
from utils import get_today_str, get_day_utc_bounds, get_bonus_minutes

logger = logging.getLogger(__name__)


class ActivityMixin:
    """Activity/reporting methods extracted from BrainRotGuardBot."""

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

            ctx = self._ctx_label(profile)
            if days == 0:
                today = get_today_str(tz)
                dates = [today]
                header = f"Today's Watch Activity{ctx}"
            elif days == 1:
                yesterday = (_dt.datetime.now(tz_info) - timedelta(days=1)).strftime("%Y-%m-%d")
                dates = [yesterday]
                header = f"Yesterday's Watch Activity{ctx}"
            else:
                dates = [
                    (_dt.datetime.now(tz_info) - timedelta(days=i)).strftime("%Y-%m-%d")
                    for i in range(days)
                ]
                header = f"Watch Activity (last {days} days){ctx}"

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

                bonus = get_bonus_minutes(cs, today)

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
        ctx = self._ctx_label({"display_name": self._profile_name(profile_id)}) if len(self._get_profiles()) > 1 else ""
        header = f"\U0001f4cb **Activity ({period}){ctx} \u2014 {total} videos**"
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
        ctx = self._ctx_label({"display_name": self._profile_name(profile_id)}) if len(self._get_profiles()) > 1 else ""
        header = f"\U0001f50d **Search History ({period}){ctx}**"
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


