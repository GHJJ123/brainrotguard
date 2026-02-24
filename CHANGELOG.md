# Changelog
## v1.22.0 - 2026-02-24

**Added**
- `/filter` top-level command — manage word filters that hide matching video titles everywhere (catalog, Shorts, Your Requests, search results)
- Word filters now apply globally, not just to search results

**Changed**
- `/search` simplified to show search history directly (was `/search history`); supports `/search [days|today|all]`
- Removed `/search filter` subcommand — use `/filter add|remove <word>` instead

## v1.21.2 - 2026-02-24

**Fixed**
- Channel matching throughout backend now uses `channel_id` (YouTube's stable unique identifier) instead of `channel_name` (mutable display name) — fixes "Your Requests" showing videos from allowlisted channels when YouTube changes the channel's display name
- SQL JOINs for watch-time-by-category and watch breakdown use `channel_id`
- `is_channel_allowed` / `is_channel_blocked` prefer `channel_id` lookup with name fallback
- Bulk operations (`set_channel_videos_category`, `delete_channel_videos`) match on `channel_id` with name fallback for legacy rows
- Channel cache and catalog builder keyed by `channel_id`
- Backfill loop periodically resolves missing `channel_id` and `@handle` on channels and videos

## v1.21.1 - 2026-02-23

**Changed**
- `/help` command now links to GitHub docs instead of the self-hosted help page (always works regardless of `base_url` config)

**Docs**
- Rewrote `docs/telegram-commands.md` — organized into sections, removed `/denied` (not implemented), added all missing commands (`/approved <search>`, `/channel unallow|unblock`, `/search`, `/stats`, `/logs`, `/shorts`)

## v1.21.0 - 2026-02-23

**Added**
- "Your Requests" grid section on homepage — shows recently-approved videos the kid explicitly searched for (excludes auto-approved channel videos), limited to 5 with "Show More" pagination
- `/approved <search>` — fuzzy search approved videos by title or channel name; without args lists all approved videos as before
**Changed**
- `/channel unallow` now deletes all DB videos from that channel (cleanup on removal)
- Renamed "Your Videos" → "Channel Videos" in the main grid section to distinguish passive channel feed from explicit requests
- Channel Videos initial load reduced from 24 to 12 (with Show More for pagination)
- Category filter pills show/hide cards in both Your Requests and Channel Videos sections
- Schedule banner phrasing: "Videos available tomorrow at 9:00 AM" (was doubling "at at")

## v1.20.1 - 2026-02-23

**Fixed**
- Include `starter-channels.yaml` in Docker image — `/channel starter` was showing "No starter channels configured" because the file was excluded by `.dockerignore`

**Docs**
- Refreshed README Features section to cover all features through v1.20 (Shorts, thumbnail previews, starter channels, per-day schedules, setup wizard, update notifications, help page)
- Added `utils.py` and `starter-channels.yaml` to README Project Structure

## v1.20.0 - 2026-02-22

**Added**
- GitHub release check: background task checks for new releases every 12 hours and sends a one-time Telegram notification to the admin with release notes and upgrade link
- Notification is sent once per installation — loop stops permanently after notifying

**Fixed**
- Outside-hours unlock time now shows tomorrow's actual start time instead of incorrect value

## v1.19.1 - 2026-02-22

**Improved**
- Polished feedback messages across bot and web UI for clearer, more actionable communication
- Bot: "Unauthorized" → "This bot is for the parent/admin only." via new `_require_admin()` helper
- Bot: Empty states (pending, approved), revoke flow, channel resolution, category management, search filters, time limits, and setup wizard now include context and next steps
- Web: Warmer child-facing copy on denied, outside-hours, time's-up, and pending pages
- Web: More specific error messages for invalid video links and fetch failures

## v1.19.0 - 2026-02-22

**Added**
- YouTube Shorts support: detect Shorts via `/shorts/` URL pattern in yt-dlp results
- Dedicated Shorts row on homepage — horizontal scroll with portrait 9:16 thumbnail cards
- Channel cache now fetches `/shorts` tab alongside `/videos` tab for allowlisted channels
- Portrait 9:16 player on watch page for Shorts (centered, max-width 480px)
- "Short" badge on search results and homepage Shorts cards
- `/shorts [on|off]` Telegram command to toggle Shorts row visibility (persisted in DB)
- `shorts_enabled` config key under `youtube:` (default: true)
- `/api/catalog?shorts=true` endpoint for Shorts catalog
- `[SHORT]` label in Telegram approval notifications with `youtube.com/shorts/` link
- `is_short` column in videos table (auto-migrated, existing videos default to 0)
- `get_approved_shorts()` DB method for querying approved Shorts

**Behavioral**
- Shorts never appear in the main video grid — they only appear in the dedicated Shorts row when enabled
- When Shorts are disabled (`/shorts off`), Shorts are hidden everywhere: catalog, search results, and channel filters

## v1.18.0 - 2026-02-22

**Added**
- `/help` web page at `http://<host>:8080/help` — standalone dark-mode command reference for all Telegram bot commands (no PIN required)
- `/help` bot command includes a clickable "Full command reference" link when `BRG_BASE_URL` is set
- `BRG_BASE_URL` env var for LAN links in Telegram messages; `deploy.sh` auto-detects host IP

**Fixed**
- Callback handler: added video_id regex validation in catch-all branch (defense-in-depth)
- `_cb_switch_confirm`: guarded `int()` calls with `.isdigit()` checks to prevent unhandled ValueError

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
