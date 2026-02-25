"""Shared bot helpers: markdown formatting, callback utilities, pagination."""

import asyncio
from typing import Optional
from urllib.parse import quote

import telegramify_markdown
from telegram import InlineKeyboardButton

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
