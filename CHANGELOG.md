# Changelog
## v1.14.1 - 2026-02-21

**Security**
- Validate `video_id` against regex on `/api/status/` endpoint (prevents DB probing with arbitrary strings)
- Bind watch heartbeat to session — only the video loaded on `/watch` can send heartbeats (prevents cross-video time inflation)
- Validate callback data: `chan_filter`/`chan_page` status checked against allowlist, `logs_page`/`search_page` days clamped to 1-365
- Validate `video_id` in thumbnail URL fallback construction (defense-in-depth for yt-dlp output)
- Separate empty-PIN logic from HMAC check for clarity and correctness
- Fix misleading status labels when `allowchan`/`blockchan` pressed on already-resolved videos

## v1.14.0 - 2026-02-21

**Changed**
- `/channel` now shows Allowed/Blocked menu with summary stats and side-by-side buttons
- Filtered channel views with pagination and 📋 Channels home button
- All pagination uses consistent ◀ Back / Next ▶ buttons with disabled placeholders
- Internal: extracted `_nav_row`, `_edit_msg`, `_channel_resolve_and_add`, `_channel_remove` helpers (-68 lines)

## v1.13.1 - 2026-02-21

**Changed**
- Welcome message now prompts with inline Yes/No buttons instead of auto-sending starter channels
- Starter channels list paginated (10 per page) with Show more/Back navigation

## v1.13.0 - 2026-02-21

**Added**
- Starter channels: ~15 curated kid-friendly YouTube channels available on first boot and via `/channel starter` (closes #9)
- Per-channel Import buttons with check mark feedback for already-imported channels
- Welcome message on `/start` and first-run (empty DB) explaining the bot's purpose
- `/channel starter` command always available for browsing and importing starter channels

## v1.12.5 - 2026-02-20

**Added**
- `/watch N` now trims to available data range and shows a hint when fewer days exist (e.g. "Only 3 days of data available — try `/watch 3`")

## v1.12.4 - 2026-02-20

**Fixed**
- Fix `/watch yesterday` and `/watch N` commands crashing due to passing timezone string instead of `ZoneInfo` object to `datetime.now()`

**Added**
