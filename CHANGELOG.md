# Changelog
## v1.17.0 - 2026-02-22

**Added**
- `/time setup` now shows top-level [Limits] [Schedule] menu
- Schedule wizard with two paths:
  - "Same for all days": start presets → stop presets → done summary
  - "Customize by day": 7-day grid → per-day start/stop pickers → back to grid (configured days marked)
- Custom time input in wizard via text reply with `parse_time_input()` validation

**Fixed**
- `parse_time_input()` now accepts hour-only formats (8am, 12pm, 9pm) — previously required minutes
- Schedule wizard Custom buttons now work (wrapped prompts in `_md()` for MarkdownV2 escaping)

## v1.16.0 - 2026-02-22

**Added**
- Per-day schedule overrides: set different time windows and limits for each day of the week (e.g. `/time mon start 8am`, `/time sat edu 120`)
- Day override copy command: `/time mon copy weekdays` copies Monday's settings to Tue-Fri
- `/time setup` guided wizard with inline buttons for choosing between simple (one daily cap) and category (edu + fun) limit modes
- Mode switch warnings: switching from category to simple (or vice versa) prompts with inline confirmation buttons before changing
- `/time` now shows today's status with progress bars plus a 7-day weekly overview
- `/time <day>` shows effective settings for that specific day

**Behavioral**
- Setting a flat limit now auto-clears category limits (and vice versa) to prevent conflicts
- Per-day override "off" clears the override (falls back to default), unlike default "off" which disables the limit
- Web enforcement (`_get_time_limit_info`, `_get_category_time_info`, `_get_schedule_info`) now resolves per-day overrides automatically
- `/watch` command uses per-day resolved limits for progress display

## v1.15.0 - 2026-02-21

**Added**
- Search cards now show view count below channel name (e.g. "2.3M views")
- Thumbnail preview cycling: hover (desktop) or scroll-into-view (tablet) cycles through YouTube auto-generated thumbnails with crossfade and progress dots
- Preview engine supports dynamically loaded cards (catalog pagination, channel/category filters)

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
