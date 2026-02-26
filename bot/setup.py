"""Setup hub mixin: /start and /setup interactive setup wizard with section sub-menus."""

import logging
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot.helpers import _md, _answer_bg, _edit_msg, MD2

logger = logging.getLogger(__name__)


class SetupMixin:
    """Interactive setup hub for first-run and returning configuration."""

    # --- Hub ---

    def _build_setup_hub(self, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
        """Build the setup hub message text + 4 category buttons with current status."""
        from version import __version__
        profiles = self._get_profiles()

        # Children status
        if len(profiles) == 1 and profiles[0]["display_name"] == "default" and not profiles[0]["pin"]:
            children_status = "not configured"
        elif profiles:
            parts = []
            for p in profiles:
                pin = " (PIN set)" if p["pin"] else ""
                parts.append(f"{p['display_name']}{pin}")
            children_status = ", ".join(parts)
        else:
            children_status = "not configured"

        # Time limits status
        time_parts = []
        for p in profiles:
            cs = self._child_store(p["id"])
            simple = cs.get_setting("daily_limit_minutes", "")
            edu = cs.get_setting("edu_limit_minutes", "")
            fun = cs.get_setting("fun_limit_minutes", "")
            sched_start = cs.get_setting("schedule_start", "")
            if simple:
                time_parts.append(f"{p['display_name']}: {simple}m/day")
            elif edu or fun:
                e = f"{edu}m edu" if edu else ""
                f_ = f"{fun}m fun" if fun else ""
                time_parts.append(f"{p['display_name']}: {' / '.join(x for x in [e, f_] if x)}")
            elif sched_start:
                time_parts.append(f"{p['display_name']}: schedule only")
        if time_parts:
            time_status = "; ".join(time_parts) if len(profiles) > 1 else time_parts[0].split(": ", 1)[1]
        else:
            time_status = "not configured"

        # Channels status
        total_allowed = 0
        for p in profiles:
            cs = self._child_store(p["id"])
            total_allowed += len(cs.get_channels_with_ids("allowed"))
        channels_status = f"{total_allowed} allowed" if total_allowed else "0 channels"

        # Shorts status
        shorts_parts = []
        for p in profiles:
            cs = self._child_store(p["id"])
            db_val = cs.get_setting("shorts_enabled", "")
            if db_val:
                enabled = db_val.lower() == "true"
            elif p["id"] == "default" and self.config and hasattr(self.config.youtube, 'shorts_enabled'):
                enabled = self.config.youtube.shorts_enabled
            else:
                enabled = False
            shorts_parts.append((p["display_name"], enabled))
        if not shorts_parts:
            shorts_status = "disabled"
        elif len(profiles) > 1:
            shorts_status = "; ".join(f"{name}: {'enabled' if e else 'disabled'}" for name, e in shorts_parts)
        else:
            shorts_status = "enabled" if shorts_parts[0][1] else "disabled"

        text = (
            f"**BrainRotGuard v{__version__}**\n\n"
            "YouTube approval system for kids. Tap a section "
            "below to set things up.\n\n"
            f"  Children — {children_status}\n"
            f"  Time Limits — {time_status}\n"
            f"  Channels — {channels_status}\n"
            f"  Shorts — {shorts_status}"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("\U0001f9d2 Children", callback_data="onboard_children"),
                InlineKeyboardButton("\u23f0 Time Limits", callback_data="onboard_time"),
            ],
            [
                InlineKeyboardButton("\U0001f4fa Channels", callback_data="onboard_channels"),
                InlineKeyboardButton("\U0001f3ac Shorts", callback_data="onboard_shorts"),
            ],
        ])
        return _md(text), keyboard

    async def _cmd_setup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send the setup hub (alias for /start)."""
        if not await self._require_admin(update):
            return
        await self._send_setup_hub(update)

    async def _send_setup_hub(self, update: Update) -> None:
        """Send the hub message and track its message_id."""
        chat_id = update.effective_chat.id
        text, markup = self._build_setup_hub(chat_id)
        msg = await update.effective_message.reply_text(text, parse_mode=MD2, reply_markup=markup)
        self._pending_wizard[chat_id] = {
            "step": "onboard_hub",
            "hub_message_id": msg.message_id,
        }

    async def _edit_hub(self, query) -> None:
        """Re-render the hub in place and restore wizard state."""
        chat_id = query.message.chat_id
        text, markup = self._build_setup_hub(chat_id)
        await _edit_msg(query, text, markup)
        # Restore hub state so _is_onboard_active works for channel browsing
        self._pending_wizard[chat_id] = {
            "step": "onboard_hub",
            "hub_message_id": query.message.message_id,
        }

    # --- Children section ---

    def _build_children_submenu(self) -> tuple[str, InlineKeyboardMarkup]:
        """Render children sub-menu showing current profiles."""
        profiles = self._get_profiles()
        lines = ["**Children Setup**\n"]
        if not profiles or (len(profiles) == 1 and profiles[0]["display_name"] == "default"):
            lines.append("Current: default (no name)")
        else:
            for p in profiles:
                pin = " (PIN set)" if p["pin"] else " (no PIN)"
                lines.append(f"  {p['display_name']}{pin}")

        buttons = []
        # Show rename button if default profile exists (unnamed)
        has_default = any(p["display_name"] == "default" for p in profiles)
        if has_default:
            buttons.append([InlineKeyboardButton("Rename Default", callback_data="onboard_child_rename")])
        buttons.append([InlineKeyboardButton("Add Child", callback_data="onboard_child_add")])
        buttons.append([InlineKeyboardButton("\u2190 Back", callback_data="onboard_child_back")])

        return _md("\n".join(lines)), InlineKeyboardMarkup(buttons)

    async def _cb_onboard_children(self, query) -> None:
        """Enter children sub-menu."""
        _answer_bg(query)
        text, markup = self._build_children_submenu()
        await _edit_msg(query, text, markup)

    async def _cb_onboard_child_rename(self, query) -> None:
        """Prompt for new name to rename default profile."""
        _answer_bg(query)
        chat_id = query.message.chat_id
        hub_mid = self._pending_wizard.get(chat_id, {}).get("hub_message_id")
        self._pending_wizard[chat_id] = {
            "step": "onboard_child_name:rename",
            "hub_message_id": hub_mid,
            "target_profile": "default",
        }
        await _edit_msg(query, _md("Reply with the child's name:"))

    async def _cb_onboard_child_add(self, query) -> None:
        """Prompt for new child name."""
        _answer_bg(query)
        chat_id = query.message.chat_id
        hub_mid = self._pending_wizard.get(chat_id, {}).get("hub_message_id")
        self._pending_wizard[chat_id] = {
            "step": "onboard_child_name:add",
            "hub_message_id": hub_mid,
        }
        await _edit_msg(query, _md("Reply with the child's name:"))

    async def _cb_onboard_child_pin(self, query, choice: str) -> None:
        """Handle PIN yes/no choice."""
        _answer_bg(query)
        chat_id = query.message.chat_id
        state = self._pending_wizard.get(chat_id, {})
        if not state.get("last_profile_id"):
            await query.answer("Session expired — run /setup to restart.")
            return
        if choice == "yes":
            state["step"] = "onboard_child_pin"
            self._pending_wizard[chat_id] = state
            await _edit_msg(query, _md("Reply with a PIN:"))
        else:
            # Skip PIN, return to children sub-menu
            state["step"] = "onboard_hub"
            self._pending_wizard[chat_id] = state
            text, markup = self._build_children_submenu()
            await _edit_msg(query, text, markup)

    async def _cb_onboard_child_back(self, query) -> None:
        """Return to hub from children sub-menu."""
        _answer_bg(query)
        await self._edit_hub(query)

    # --- Channels section ---

    def _build_channels_submenu(self) -> tuple[str, InlineKeyboardMarkup]:
        """Render channels sub-menu with per-profile stats."""
        profiles = self._get_profiles()
        lines = ["**Channels**\n"]
        for p in profiles:
            cs = self._child_store(p["id"])
            allowed = len(cs.get_channels_with_ids("allowed"))
            blocked = len(cs.get_channels_with_ids("blocked"))
            lines.append(f"  {p['display_name']}: {allowed} allowed, {blocked} blocked")

        buttons = []
        if len(profiles) == 1:
            buttons.append([InlineKeyboardButton(
                "Browse Starters", callback_data=f"onboard_chan_sel:{profiles[0]['id']}",
            )])
        else:
            row = []
            for p in profiles:
                row.append(InlineKeyboardButton(
                    p["display_name"], callback_data=f"onboard_chan_sel:{p['id']}",
                ))
                if len(row) == 3:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
        buttons.append([InlineKeyboardButton("\u2190 Back", callback_data="onboard_chan_back")])
        return _md("\n".join(lines)), InlineKeyboardMarkup(buttons)

    async def _cb_onboard_channels(self, query) -> None:
        """Enter channels sub-menu."""
        _answer_bg(query)
        text, markup = self._build_channels_submenu()
        await _edit_msg(query, text, markup)

    async def _cb_onboard_channels_sel(self, query, profile_id: str) -> None:
        """Select profile for channels — render starter browser."""
        _answer_bg(query)
        cs = self._child_store(profile_id)
        text, markup = self._render_starter_message(store=cs, profile_id=profile_id, onboard=True)
        await _edit_msg(query, text, markup, disable_preview=True)

    async def _cb_onboard_channels_back(self, query) -> None:
        """Return to hub from channels sub-menu."""
        _answer_bg(query)
        await self._edit_hub(query)

    # --- Time section ---

    def _build_time_submenu(self) -> tuple[str, InlineKeyboardMarkup]:
        """Render time limits sub-menu with per-profile status."""
        profiles = self._get_profiles()
        lines = ["**Time Limits**\n"]
        for p in profiles:
            cs = self._child_store(p["id"])
            simple = cs.get_setting("daily_limit_minutes", "")
            edu = cs.get_setting("edu_limit_minutes", "")
            fun = cs.get_setting("fun_limit_minutes", "")
            sched_start = cs.get_setting("schedule_start", "")
            sched_end = cs.get_setting("schedule_end", "")
            parts = []
            if simple:
                parts.append(f"{simple}m/day")
            elif edu or fun:
                if edu:
                    parts.append(f"{edu}m edu")
                if fun:
                    parts.append(f"{fun}m fun")
            if sched_start or sched_end:
                from utils import format_time_12h
                s_disp = format_time_12h(sched_start) if sched_start else "?"
                e_disp = format_time_12h(sched_end) if sched_end else "?"
                parts.append(f"{s_disp}\u2013{e_disp}")
            if not parts:
                parts.append("no limits set")
            lines.append(f"  {p['display_name']}: {' / '.join(parts)}")

        buttons = []
        if len(profiles) == 1:
            buttons.append([InlineKeyboardButton(
                "Set Limits", callback_data=f"onboard_time_sel:{profiles[0]['id']}",
            )])
        else:
            row = []
            for p in profiles:
                row.append(InlineKeyboardButton(
                    p["display_name"], callback_data=f"onboard_time_sel:{p['id']}",
                ))
                if len(row) == 3:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
        buttons.append([InlineKeyboardButton("\u2190 Back", callback_data="onboard_time_back")])
        return _md("\n".join(lines)), InlineKeyboardMarkup(buttons)

    async def _cb_onboard_time(self, query) -> None:
        """Enter time limits sub-menu."""
        _answer_bg(query)
        text, markup = self._build_time_submenu()
        await _edit_msg(query, text, markup)

    async def _cb_onboard_time_sel(self, query, profile_id: str) -> None:
        """Select profile and chain to /time setup wizard."""
        _answer_bg(query)
        chat_id = query.message.chat_id
        hub_mid = self._pending_wizard.get(chat_id, {}).get("hub_message_id")
        cs = self._child_store(profile_id)
        self._pending_wizard[chat_id] = {
            "step": "setup_top",
            "profile_id": profile_id,
            "onboard_return": True,
            "hub_message_id": hub_mid,
        }
        text = _md(
            "\u23f0 **Time Setup**\n\n"
            "What would you like to configure?\n\n"
            "**Limits** \u2014 daily screen time budgets\n"
            "**Schedule** \u2014 when videos are available"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Limits", callback_data="setup_top:limits"),
            InlineKeyboardButton("Schedule", callback_data="setup_top:schedule"),
        ]])
        await _edit_msg(query, text, keyboard)

    async def _cb_onboard_time_back(self, query) -> None:
        """Return to hub from time sub-menu."""
        _answer_bg(query)
        await self._edit_hub(query)

    async def _send_onboard_time_return(self, chat_id: int) -> None:
        """After time setup wizard completes, send fresh time sub-menu."""
        text, markup = self._build_time_submenu()
        try:
            await self._app.bot.send_message(
                chat_id=chat_id, text=text, parse_mode=MD2, reply_markup=markup,
            )
        except Exception as e:
            logger.error(f"Failed to send onboard time return: {e}")

    # --- Shorts section ---

    def _build_shorts_submenu(self, selected_profile_id: str = "") -> tuple[str, InlineKeyboardMarkup]:
        """Render shorts sub-menu with per-profile status."""
        profiles = self._get_profiles()
        lines = ["**YouTube Shorts** (under 60s)\n"]
        for p in profiles:
            cs = self._child_store(p["id"])
            db_val = cs.get_setting("shorts_enabled", "")
            if db_val:
                enabled = db_val.lower() == "true"
            elif p["id"] == "default" and self.config and hasattr(self.config.youtube, 'shorts_enabled'):
                enabled = self.config.youtube.shorts_enabled
            else:
                enabled = False
            lines.append(f"  {p['display_name']}: {'enabled' if enabled else 'disabled'}")

        buttons = []
        if len(profiles) == 1:
            # Direct enable/disable for single child
            pid = profiles[0]["id"]
            buttons.append([
                InlineKeyboardButton("Enable", callback_data=f"onboard_shorts_tog:{pid}:on"),
                InlineKeyboardButton("Disable", callback_data=f"onboard_shorts_tog:{pid}:off"),
            ])
        elif selected_profile_id:
            # Show toggle for selected profile
            buttons.append([
                InlineKeyboardButton("Enable", callback_data=f"onboard_shorts_tog:{selected_profile_id}:on"),
                InlineKeyboardButton("Disable", callback_data=f"onboard_shorts_tog:{selected_profile_id}:off"),
            ])
        else:
            # Profile selector
            row = []
            for p in profiles:
                row.append(InlineKeyboardButton(
                    p["display_name"], callback_data=f"onboard_shorts_sel:{p['id']}",
                ))
                if len(row) == 3:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
        buttons.append([InlineKeyboardButton("\u2190 Back", callback_data="onboard_shorts_back")])
        return _md("\n".join(lines)), InlineKeyboardMarkup(buttons)

    async def _cb_onboard_shorts(self, query) -> None:
        """Enter shorts sub-menu."""
        _answer_bg(query)
        text, markup = self._build_shorts_submenu()
        await _edit_msg(query, text, markup)

    async def _cb_onboard_shorts_select(self, query, profile_id: str) -> None:
        """Select profile for shorts toggle (multi-child)."""
        _answer_bg(query)
        text, markup = self._build_shorts_submenu(selected_profile_id=profile_id)
        await _edit_msg(query, text, markup)

    async def _cb_onboard_shorts_toggle(self, query, profile_id: str, choice: str) -> None:
        """Toggle shorts for a profile."""
        _answer_bg(query, f"Shorts {'enabled' if choice == 'on' else 'disabled'}")
        cs = self._child_store(profile_id)
        cs.set_setting("shorts_enabled", str(choice == "on").lower())
        if self.on_channel_change:
            self.on_channel_change()
        # Return to shorts sub-menu with updated status
        text, markup = self._build_shorts_submenu()
        await _edit_msg(query, text, markup)

    async def _cb_onboard_shorts_back(self, query) -> None:
        """Return to hub from shorts sub-menu."""
        _answer_bg(query)
        await self._edit_hub(query)

    # --- Onboard return from time wizard ---

    async def _maybe_onboard_return(self, chat_id: int) -> None:
        """If the time wizard was launched from the setup hub, send time sub-menu."""
        state = self._pending_wizard.get(chat_id, {})
        if state.get("onboard_return"):
            await self._send_onboard_time_return(chat_id)
            self._pending_wizard.pop(chat_id, None)

    # --- Onboard text reply handler ---

    async def _handle_onboard_reply(self, update: Update, state: dict) -> bool:
        """Handle text replies for onboard wizard steps.

        Returns True if the reply was handled, False otherwise.
        """
        chat_id = update.effective_chat.id
        text = update.message.text.strip()
        step = state["step"]

        if step.startswith("onboard_child_name:"):
            action = step.split(":")[1]  # "rename" or "add"
            name = text[:30].strip()
            if not name:
                await update.effective_message.reply_text("Name can't be empty. Try again:")
                return True
            # Validate name
            pid = re.sub(r'[^a-z0-9]', '', name.lower())[:20]
            if not pid:
                await update.effective_message.reply_text("Name must contain at least one alphanumeric character. Try again:")
                return True

            if action == "rename":
                target_pid = state.get("target_profile", "default")
                target = self.video_store.get_profile(target_pid)
                if target:
                    self.video_store.update_profile(target_pid, display_name=name)
                state["step"] = "onboard_child_pin_prompt"
                state["last_profile_id"] = target_pid
                state["last_profile_name"] = name
                self._pending_wizard[chat_id] = state
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("Set PIN", callback_data="onboard_child_pin:yes"),
                    InlineKeyboardButton("No PIN", callback_data="onboard_child_pin:no"),
                ]])
                await update.effective_message.reply_text(
                    _md(f"Set a PIN for {name}?"), parse_mode=MD2, reply_markup=keyboard,
                )
            elif action == "add":
                # Check for conflict
                existing = self.video_store.get_profile(pid)
                if existing:
                    await update.effective_message.reply_text(
                        f"A profile with that name already exists. Try a different name:"
                    )
                    return True
                self.video_store.create_profile(pid, name)
                state["step"] = "onboard_child_pin_prompt"
                state["last_profile_id"] = pid
                state["last_profile_name"] = name
                self._pending_wizard[chat_id] = state
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("Set PIN", callback_data="onboard_child_pin:yes"),
                    InlineKeyboardButton("No PIN", callback_data="onboard_child_pin:no"),
                ]])
                await update.effective_message.reply_text(
                    _md(f"Set a PIN for {name}?"), parse_mode=MD2, reply_markup=keyboard,
                )
            return True

        if step == "onboard_child_pin":
            pin = text.strip()
            pid = state.get("last_profile_id", "default")
            self.video_store.update_profile(pid, pin=pin)
            # Return to children sub-menu
            state["step"] = "onboard_hub"
            self._pending_wizard[chat_id] = state
            text_msg, markup = self._build_children_submenu()
            await update.effective_message.reply_text(text_msg, parse_mode=MD2, reply_markup=markup)
            return True

        return False
