"""Time limits mixin: /time command, schedule, category limits, setup wizard, wizard reply handler."""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot.helpers import _md, _answer_bg, _edit_msg, MD2
from data.child_store import ChildStore
from utils import (
    get_today_str, get_day_utc_bounds, get_weekday, parse_time_input,
    format_time_12h, is_within_schedule, resolve_setting, get_bonus_minutes,
    DAY_NAMES, DAY_GROUPS, CAT_LABELS,
)

logger = logging.getLogger(__name__)


def _progress_bar(fraction: float, width: int = 20) -> str:
    filled = min(width, int(fraction * width))
    return "\u2593" * filled + "\u2591" * (width - filled)


class TimeLimitMixin:
    """Time limit methods extracted from BrainRotGuardBot."""

    _DAY_LABELS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday",
                   "thu": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}
    _OVERRIDE_KEYS = ("schedule_start", "schedule_end", "edu_limit_minutes",
                      "fun_limit_minutes", "daily_limit_minutes")


    def _wizard_store(self, chat_id: int) -> 'ChildStore':
        """Get the ChildStore for an active wizard, based on stored profile_id."""
        state = self._pending_wizard.get(chat_id, {})
        pid = state.get("profile_id", "default")
        return self._child_store(pid)

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

    def _resolve_setting(self, base_key: str, default: str = "", store=None) -> str:
        """Resolve a setting with per-day override support."""
        s = store or self.video_store
        return resolve_setting(base_key, s, tz_name=self._get_tz(), default=default)

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
                    cs.set_setting("edu_limit_minutes", "0")
                    cs.set_setting("fun_limit_minutes", "0")
                    await update.effective_message.reply_text("All watch time limits disabled. Videos can be watched without a daily cap.")
                    return
                elif arg.isdigit():
                    await self._time_set_flat_limit(update, [arg], store=cs)
                    return
                else:
                    await update.effective_message.reply_text(
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
        pid = getattr(s, 'profile_id', 'default')
        ctx = self._ctx_label({"display_name": self._profile_name(pid)}) if len(self._get_profiles()) > 1 else ""
        lines = [f"\u23f0 **Today ({day_label[:3]}){ctx}** \u2014 {status}\n"]

        if s_display:
            lines.append(f"Schedule: {s_display} \u2013 {e_display}")

        # Bonus
        bonus = get_bonus_minutes(s, today)

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
            lines.append(f"`{_progress_bar(pct)}` {int(total_used)}/{effective_total} min ({int(pct * 100)}%)")

            # Per-category bars
            if edu_limit > 0:
                eff_edu = edu_limit + bonus
                epct = min(1.0, edu_used / eff_edu) if eff_edu > 0 else 0
                lines.append(f"  Edu `{_progress_bar(epct, 10)}` {int(edu_used)}/{eff_edu}")
            if fun_limit > 0:
                eff_fun = fun_limit + bonus
                fpct = min(1.0, fun_used / eff_fun) if eff_fun > 0 else 0
                lines.append(f"  Fun `{_progress_bar(fpct, 10)}` {int(fun_used)}/{eff_fun}")
        elif flat_limit > 0:
            effective = flat_limit + bonus
            remaining = max(0, effective - used)
            pct = min(1.0, used / effective) if effective > 0 else 0
            lines.append(f"Limit: {flat_limit} min")
            if bonus > 0:
                lines.append(f"Bonus today: +{bonus} min")
            lines.append("")
            lines.append(f"`{_progress_bar(pct)}` {int(used)}/{effective} min ({int(pct * 100)}%)")
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

        await update.effective_message.reply_text(_md("\n".join(lines)), parse_mode=MD2)

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
            await update.effective_message.reply_text(f"{label} overrides cleared — default settings will apply.")
        elif sub == "copy":
            await self._time_day_copy(update, day, args[1:], store=s)
        elif sub.isdigit():
            await self._time_set_flat_limit(update, [sub], day=day, store=s)
        else:
            label = self._DAY_LABELS[day]
            await update.effective_message.reply_text(
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

        await update.effective_message.reply_text(_md("\n".join(lines)), parse_mode=MD2)

    async def _time_day_copy(self, update: Update, src_day: str, args: list[str], store=None) -> None:
        """Handle /time <day> copy <targets>."""
        s = store or self.video_store
        if not args:
            await update.effective_message.reply_text(
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
                await update.effective_message.reply_text(f"Unknown day: {arg}. Use day names (mon, tue...), weekdays, weekend, or all.")
                return

        # Remove source from targets and deduplicate
        targets = list(dict.fromkeys(t for t in targets if t != src_day))
        if not targets:
            await update.effective_message.reply_text("No valid days. Use day names (mon, tue...), weekdays, weekend, or all.")
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
        await update.effective_message.reply_text(
            f"Copied {count} override(s) from {src_label} \u2192 {target_labels}."
        )

    # --- Flat limit (simple mode) ---

    async def _time_set_flat_limit(self, update: Update, args: list[str], day: str = "", store=None) -> None:
        """Handle /time [<day>] limit|<N> with mode switch warning."""
        s = store or self.video_store
        if not args or not args[0].isdigit():
            await update.effective_message.reply_text("Usage: /time [<day>] limit <minutes>")
            return
        minutes = int(args[0])
        if minutes == 0:
            await update.effective_message.reply_text("Use `/time off` to disable the time limit.", parse_mode="MarkdownV2")
            return

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
                await update.effective_message.reply_text(text, parse_mode=MD2, reply_markup=keyboard)
                return

        prefix = f"{day}_" if day else ""
        s.set_setting(f"{prefix}daily_limit_minutes", str(minutes))
        self._auto_clear_mode("simple", day=day, store=s)

        if day:
            label = self._DAY_LABELS[day]
            await update.effective_message.reply_text(f"{label} limit set to {minutes} minutes. Playback stops when time runs out.")
        else:
            await update.effective_message.reply_text(f"Daily limit set to {minutes} minutes. Playback stops when time runs out.")

    # --- Category limits ---

    async def _time_set_category_limit(self, update: Update, args: list[str],
                                       category: str, day: str = "", store=None) -> None:
        """Handle /time [<day>] edu|fun <minutes|off>."""
        s = store or self.video_store
        cat_label = CAT_LABELS.get(category, "Entertainment")
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
                        await update.effective_message.reply_text(f"{label} {cat_label}: OFF (override)")
                    else:
                        effective = s.get_setting(f"{category}_limit_minutes", "")
                        eff_val = int(effective) if effective else 0
                        if eff_val:
                            await update.effective_message.reply_text(f"{label} {cat_label}: {eff_val} min (from default)")
                        else:
                            await update.effective_message.reply_text(f"{label} {cat_label}: OFF")
                else:
                    await update.effective_message.reply_text(f"{label} {cat_label}: {limit} min (override)")
            else:
                if limit == 0:
                    await update.effective_message.reply_text(f"{cat_label} limit: OFF (unlimited)")
                else:
                    await update.effective_message.reply_text(f"{cat_label} limit: {limit} minutes/day")
            return

        value = args[0].lower()

        if value in ("off", "0"):
            if day:
                # Day override: "off" clears the override (falls back to default)
                s.set_setting(setting_key, "")
                label = self._DAY_LABELS[day]
                await update.effective_message.reply_text(f"{label} {cat_label} override cleared — default settings will apply.")
            else:
                s.set_setting(setting_key, "0")
                await update.effective_message.reply_text(f"{cat_label} limit disabled — no daily cap.")
            return

        if not value.isdigit():
            await update.effective_message.reply_text(f"Usage: /time {category} <minutes|off>")
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
                await update.effective_message.reply_text(text, parse_mode=MD2, reply_markup=keyboard)
                return

        s.set_setting(setting_key, str(minutes))
        self._auto_clear_mode("category", day=day, store=s)

        if day:
            label = self._DAY_LABELS[day]
            await update.effective_message.reply_text(f"{label} {cat_label} limit set to {minutes} min. Playback stops when budget runs out.")
        else:
            await update.effective_message.reply_text(f"{cat_label} limit set to {minutes} min/day. Playback stops when budget runs out.")

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
                await update.effective_message.reply_text(f"{day_label}{label} time: {format_time_12h(current)}")
            elif day:
                # Show effective (default fallback)
                base = "schedule_start" if is_start else "schedule_end"
                default = s.get_setting(base, "")
                if default:
                    await update.effective_message.reply_text(
                        f"{day_label}{label} time: {format_time_12h(default)} (from default)"
                    )
                else:
                    await update.effective_message.reply_text(f"{day_label}{label} time: not set")
            else:
                await update.effective_message.reply_text(f"{label} time: not set")
            return

        value = args[0].lower()
        if value == "off":
            s.set_setting(setting_key, "")
            if day:
                await update.effective_message.reply_text(f"{day_label}{label} time override cleared.")
            else:
                await update.effective_message.reply_text(f"{label} time cleared.")
            return

        parsed = parse_time_input(args[0])
        if not parsed:
            await update.effective_message.reply_text(
                "Invalid time. Examples: 800am, 8:00, 2000, 8:00PM"
            )
            return

        s.set_setting(setting_key, parsed)
        await update.effective_message.reply_text(
            f"{day_label}{label} time set to {format_time_12h(parsed)}"
        )

    # --- Bonus ---

    async def _time_add_bonus(self, update: Update, args: list[str], store=None) -> None:
        """Handle /time add <minutes> — grant bonus screen time for today only."""
        s = store or self.video_store
        if not args or not args[0].isdigit():
            await update.effective_message.reply_text("Usage: /time add <minutes>")
            return
        add_min = int(args[0])
        if add_min <= 0:
            await update.effective_message.reply_text("Bonus minutes must be a positive number.")
            return
        if add_min > 480:
            await update.effective_message.reply_text("Bonus must be 480 minutes (8 hours) or less.")
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
        await update.effective_message.reply_text(
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
        await update.effective_message.reply_text(text, parse_mode=MD2, reply_markup=keyboard)

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
            state = self._pending_wizard.get(chat_id, {})
            pid = state.get("profile_id", "default")
            new_state = {"step": "setup_sched_start", "profile_id": pid}
            if state.get("onboard_return"):
                new_state["onboard_return"] = True
            self._pending_wizard[chat_id] = new_state
            return
        ws.set_setting("schedule_start", value)
        await self._setup_sched_stop_menu(query, format_time_12h(value))

    async def _cb_setup_sched_stop(self, query, value: str) -> None:
        """Handle default stop-time selection — goes to done summary."""
        chat_id = query.message.chat_id
        ws = self._wizard_store(chat_id)
        if value == "custom":
            await _edit_msg(query, _md("Reply with the stop time (e.g. 8pm, 20:00):"))
            state = self._pending_wizard.get(chat_id, {})
            pid = state.get("profile_id", "default")
            new_state = {"step": "setup_sched_stop", "profile_id": pid}
            if state.get("onboard_return"):
                new_state["onboard_return"] = True
            self._pending_wizard[chat_id] = new_state
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
            state = self._pending_wizard.get(chat_id, {})
            pid = state.get("profile_id", "default")
            new_state = {"step": f"setup_daystart:{day}", "profile_id": pid}
            if state.get("onboard_return"):
                new_state["onboard_return"] = True
            self._pending_wizard[chat_id] = new_state
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
            state = self._pending_wizard.get(chat_id, {})
            pid = state.get("profile_id", "default")
            new_state = {"step": f"setup_daystop:{day}", "profile_id": pid}
            if state.get("onboard_return"):
                new_state["onboard_return"] = True
            self._pending_wizard[chat_id] = new_state
            return
        ws.set_setting(f"{day}_schedule_end", value)
        text, keyboard = self._setup_sched_day_grid(store=ws)
        await _edit_msg(query, text, keyboard)

    async def _cb_setup_sched_done(self, query) -> None:
        """Final summary when schedule wizard completes."""
        chat_id = query.message.chat_id
        ws = self._wizard_store(chat_id)
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
        await self._maybe_onboard_return(chat_id)

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
            state = self._pending_wizard.get(chat_id, {})
            pid = state.get("profile_id", "default")
            onboard = state.get("onboard_return", False)
            new_state = {"step": "setup_simple", "profile_id": pid}
            if onboard:
                new_state["onboard_return"] = True
            self._pending_wizard[chat_id] = new_state
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
        await self._maybe_onboard_return(chat_id)

    async def _cb_setup_edu(self, query, value: str) -> None:
        """Handle edu limit selection in wizard."""
        chat_id = query.message.chat_id
        ws = self._wizard_store(chat_id)
        if value == "custom":
            await _edit_msg(query, "Reply with the number of minutes for **educational** limit:")
            state = self._pending_wizard.get(chat_id, {})
            pid = state.get("profile_id", "default")
            onboard = state.get("onboard_return", False)
            new_state = {"step": "setup_edu", "profile_id": pid}
            if onboard:
                new_state["onboard_return"] = True
            self._pending_wizard[chat_id] = new_state
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
            state = self._pending_wizard.get(chat_id, {})
            pid = state.get("profile_id", "default")
            onboard = state.get("onboard_return", False)
            new_state = {"step": "setup_fun", "profile_id": pid}
            if onboard:
                new_state["onboard_return"] = True
            self._pending_wizard[chat_id] = new_state
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
        await self._maybe_onboard_return(chat_id)

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
            cat_label = CAT_LABELS.get(category, "Entertainment")
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

        # Route onboard_* steps to SetupMixin
        step = state["step"]
        if step.startswith("onboard_"):
            if await self._handle_onboard_reply(update, state):
                return
            # Unhandled onboard step (e.g. onboard_hub, onboard_child_pin_prompt)
            # — ignore text input, these steps expect button presses only
            return

        text = update.message.text.strip()
        ws = self._wizard_store(chat_id)

        # Schedule wizard steps expect time input, not minutes
        if step.startswith("setup_sched_") or step.startswith("setup_daystart:") or step.startswith("setup_daystop:"):
            parsed = parse_time_input(text)
            if not parsed:
                await update.effective_message.reply_text(
                    "Invalid time. Examples: 8am, 08:00, 2000, 8:00PM"
                )
                return
            onboard = state.get("onboard_return", False)
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
                await update.effective_message.reply_text(stop_text, parse_mode=MD2, reply_markup=keyboard)
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
                await update.effective_message.reply_text(_md("\n".join(lines)), parse_mode=MD2)
                if onboard:
                    await self._send_onboard_time_return(chat_id)
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
                await update.effective_message.reply_text(stop_text, parse_mode=MD2, reply_markup=keyboard)
            elif step.startswith("setup_daystop:"):
                day = step.split(":", 1)[1]
                ws.set_setting(f"{day}_schedule_end", parsed)
                grid_text, keyboard = self._setup_sched_day_grid(store=ws)
                await update.effective_message.reply_text(grid_text, parse_mode=MD2, reply_markup=keyboard)
            return

        # Limit wizard steps expect positive integer minutes
        if not text.isdigit() or int(text) <= 0:
            await update.effective_message.reply_text("Please reply with a positive number of minutes.")
            return
        onboard = state.get("onboard_return", False)
        minutes = int(text)
        del self._pending_wizard[chat_id]

        if step == "setup_simple":
            ws.set_setting("daily_limit_minutes", str(minutes))
            self._auto_clear_mode("simple", store=ws)
            await update.effective_message.reply_text(_md(
                f"\u2713 **Simple limit set**\n"
                f"  Daily cap: {minutes} min/day\n\n"
                f"Use `/time <day> limit <min>` to customize specific days."
            ), parse_mode=MD2)
            if onboard:
                await self._send_onboard_time_return(chat_id)
        elif step == "setup_edu":
            ws.set_setting("edu_limit_minutes", str(minutes))
            self._auto_clear_mode("category", store=ws)
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("30 min", callback_data="setup_fun:30"),
                InlineKeyboardButton("60 min", callback_data="setup_fun:60"),
                InlineKeyboardButton("90 min", callback_data="setup_fun:90"),
                InlineKeyboardButton("Custom", callback_data="setup_fun:custom"),
            ]])
            await update.effective_message.reply_text(_md(
                f"Educational: {minutes} min \u2713\n"
                f"Now set **entertainment** limit:"
            ), parse_mode=MD2, reply_markup=keyboard)
        elif step == "setup_fun":
            ws.set_setting("fun_limit_minutes", str(minutes))
            self._auto_clear_mode("category", store=ws)
            edu = int(ws.get_setting("edu_limit_minutes", "0") or "0")
            total = edu + minutes
            await update.effective_message.reply_text(_md(
                f"\u2713 **Category limits set**\n"
                f"  Educational: {edu} min/day\n"
                f"  Entertainment: {minutes} min/day\n"
                f"  Total: {total} min/day\n\n"
                f"Use `/time <day> edu|fun <min>` to customize specific days."
            ), parse_mode=MD2)
            if onboard:
                await self._send_onboard_time_return(chat_id)
