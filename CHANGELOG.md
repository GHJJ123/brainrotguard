# Changelog
## v1.31.1 - 2026-04-09

**Fixed**
- Switch embeds from `youtube-nocookie.com` to `youtube.com` — YouTube's bot detection blocks anonymous nocookie embeds with a "Sign in to confirm you're not a bot" wall; standard embeds share the tablet's Google session (closes #38)
- Updated CSP `frame-src` header to allow `youtube.com`
- Updated docs: README, setup guide, troubleshooting, design decisions

## v1.31.0 - 2026-03-25

**Added**
- Watch history page: `/history` accessible from avatar dropdown, grouped by day with progress bars and infinite scroll (PR #33 — thanks @NoLooseEnds)
- Activity row: homepage "Your Requests" reworked to "Activity" showing 6 most recent unfinished videos with progress bars, dismiss controls, and collapse/expand toggles (PR #34 — thanks @NoLooseEnds)
- Progress bars on channel video catalog cards showing playback progress (PR #34)
- Section collapse/expand toggles for all homepage sections with localStorage persistence (PR #34)
- Requests page: `/requests` for one-off video approvals (pending + approved outside allowed channels) (PR #35 — thanks @NoLooseEnds)
- Channel pills alphabetized on homepage (PR #34)
- Rate limit handler returns JSON for `/api/` routes instead of HTML (PR #33)

**Fixed**
- `position_seconds` heartbeat field bounded to [0, 86400] — prevents arbitrary integers in database
- `dismissed` catalog param validated against video ID regex for consistency with security model
- `_annotate_progress` replaced full approved-videos table scan with targeted batch query — single JOIN instead of two separate queries
- `get_active_videos` SQL includes `resume_seconds` in completion check — prevents phantom gap where SQL LIMIT applied before Python filter could return fewer results than available
- `HAVING` clause includes `OR v.duration IS NULL` — partially-watched videos without stored duration no longer silently dropped
- History infinite scroll retries on all fetch errors (not just HTTP 429)

## v1.30.0 - 2026-03-17

**Added**
- i18n/localization system with English and Norwegian translations (PR #26 -- thanks @NoLooseEnds)
- PWA support: installable as tablet app with manifest, service worker, standalone mode, safe-area insets (PR #29 -- thanks @NoLooseEnds)
- Watch autoplay/resume: remembers playback position via localStorage, more reliable auto-start (PR #30 -- thanks @NoLooseEnds)
- Persistent playback progress: watch progress stored to database and restored on return, survives app restarts (PR #32 -- thanks @NoLooseEnds)
- Localized app name: "BrainRotGuard" renders as "HjerneVakt" in Norwegian (PR #31 -- thanks @NoLooseEnds)
- `/autoload [on|off]` toggle: switch homepage between Show More buttons (default) and infinite scroll mode (PR #27 -- thanks @NoLooseEnds)
- New `/api/catalog/status` endpoint for lightweight cache polling (PR #27)
- Re-request flow: re-requesting a pending video resends the Telegram notification to parent (PR #27)
- IntersectionObserver-based infinite scroll replaces "Show More" button on search results (PR #27)
- Circular profile avatars with username in header (PR #28)
- CSS fullscreen fallback for mobile/PWA standalone mode where native Fullscreen API is unavailable (PR #29)
- Back navigation tracking via sessionStorage for smarter back links (PR #30)

**Changed**
- Simplified web header, navigation, and time budget UI (PR #28 -- thanks @NoLooseEnds)
- Channel filter fix: `build_requests_row` now correctly filters allowlisted channels even when video has a channel_id but channel was added by name only (PR #27)

**Fixed**
- Norwegian revoke confirmation toast: "Fjernet!" (past tense) instead of "Fjern!" (imperative) or "Avslått!" (which duplicated "Denied")
- Playback position cleared on time expiry instead of saved (prevents resuming from expired position)
- Heartbeat dedup guard: lowered `_HEARTBEAT_MIN_INTERVAL` from 10s to 8s to maintain safety margin (must be < client heartbeat interval; PR #32 reduced client interval to 10s)

## v1.28.0 - 2026-03-01

**Changed**
- Shorts now shuffle daily — different selection each day instead of the same popularity-ordered shorts repeating indefinitely

## v1.27.7 - 2026-03-01

**Fixed**
- Video requests from new/unknown channels failed with internal server error since v1.27.5 — wrong keyword argument (`view_count` instead of `yt_view_count`) in the pending-approval code path

## v1.27.6 - 2026-02-26

**Changed**
- Removed standalone `/help` web page — command reference consolidated into [docs/telegram-commands.md](docs/telegram-commands.md)
- Reorganized telegram-commands.md: `/setup` wizard promoted to top as recommended entry point, added contextual notes throughout

## v1.27.5 - 2026-02-26

**Added**
- YouTube view count (`yt_view_count`) stored at request time — "Your Requests" section now shows real YouTube views instead of internal watch count

**Changed**
- Separated YouTube views (`yt_view_count`) from internal watch counter (`view_count`) in database — parent-facing bot commands (`/approved`, `/stats`) show internal counts, web UI shows YouTube views

## v1.27.4 - 2026-02-26

**Added**
- YouTube link sandbox: iframe `sandbox` attribute blocks all navigation to youtube.com (logo, title, channel, "Watch on YouTube" links)
- Pause overlay: full-coverage overlay with play button when video is paused, hiding YouTube's recommended video strip
- Early end-of-video overlay: covers the player with 15 seconds remaining to hide YouTube's recommended videos grid
- Replay icon on end overlay (circular arrow button matching pause overlay style)
- Custom fullscreen: our fullscreen wraps the video + overlays together, so pause/end overlays work in fullscreen too (YouTube's native fullscreen is disabled by sandbox)

**Changed**
- Removed `autofocus` from homepage search bar to prevent tablet keyboard from popping up on load

## v1.27.3 - 2026-02-25

**Changed**
- `/time setup` wizard: context-aware navigation — back goes to setup hub when launched from onboarding, shows Done button when standalone

## v1.27.2 - 2026-02-25

**Added**
- Back buttons on every step of the `/time setup` wizard for easy navigation

## v1.27.1 - 2026-02-25

**Changed**
- Child name context now shown in all command headers (`/pending`, `/approved`, `/stats`, `/watch`, `/logs`, `/search`, `/time`, `/shorts`)
- Multi-child context labels added to setup hub sub-menus (Time, Channels, Shorts) to show which child is being configured
- Starter channels setup now displays child name context for clarity in multi-child environments

## v1.27.0 - 2026-02-25

**Added**
- Interactive setup hub: `/start` and `/setup` now show a persistent message with 4 configurable sections (Children, Time Limits, Channels, Shorts)
- Each section has its own sub-menu with inline buttons — tap to configure, tap Back to return to the hub
- Children section: rename default profile, add children, set PINs — all via inline buttons and text replies
- Channels section: browse and import starter channels per profile with Back to Setup navigation
- Time Limits section: chains to existing `/time setup` wizard, returns to hub on completion
- Shorts section: per-profile enable/disable toggle
- Hub shows live status for all sections (child names, limit values, channel counts, shorts state)
- First-run auto-message now sends the setup hub instead of a static welcome

## v1.26.6 - 2026-02-25

**Changed**
- Emoji icons on all Telegram inline buttons (approve, deny, allow/block channel, revoke, category toggle)
- Default daily time limit changed from 120 to 0 (unlimited)

**Fixed**
- Mobile search dropdown CSS cascade bug causing horizontal viewport overflow

## v1.26.5 - 2026-02-25

**Fixed**
- Mobile search input no longer triggers iOS Safari auto-zoom (font-size 14px → 16px)

## v1.26.4 - 2026-02-25

**Fixed**
- Container volume permissions on Unraid/NAS (entrypoint chown + gosu for mounted `/app/db`)

**Added**
- Unraid install instructions in README and setup guide (CA search + manual template download)
- `TemplateURL` in Unraid template for Community Apps discovery

## v1.26.3 - 2026-02-25

**Changed**
- Redesigned logo and favicon (new shield+brain icon, PNG favicons replace SVG)
- Search bar hidden on login screen
- Dockerfile supports env-only configuration (no config.yaml required)

**Added**
- Unraid Community Apps template (`unraid-template.xml`)
- 512x512 square icon for Unraid/app stores
- Updated demo video and screenshots

## v1.26.2 - 2026-02-25

**Fixed**
- Replaced false-positive protocol mock test with structural method check
- Strengthened wrong-PIN and invalid-video-ID integration tests (real assertions instead of conditionals)
- Backfill task now properly cancelled on shutdown (faster stop)

**Removed**
- Dead `sample_config` fixture, unused config imports, dead `templates` import

## v1.26.1 - 2026-02-25

**Fixed**
- Mobile search dropdown caused horizontal overflow/resize when opened (dropdown used absolute positioning with fixed 280px width; now uses viewport-relative fixed positioning on mobile)

## v1.26.0 - 2026-02-25

**Modularity refactor** — decomposed two monolithic files into 20 focused modules with dependency injection, declarative callback routing, and 241 automated tests.

**Changed**
- `web/app.py` (1,433 → 50 lines): split into 7 domain routers + deps/helpers/middleware/cache modules
- `bot/telegram_bot.py` (3,151 → 394 lines): split into 6 mixins (approval, channels, timelimits, commands, activity, helpers) + declarative callback router
- `youtube/extractor.py`: wrapped in `YouTubeExtractor` class with `ExtractorProtocol` for DI and mocking
- Catalog cache now per-profile (multi-child setups no longer rebuild on every request)
- Bonus-minutes logic deduplicated into `get_bonus_minutes()` helper

**Fixed**
- Missing `_channel_md_link` import in activity mixin (crash on `/watch` command)
- Heartbeat dedup now keyed by `(video_id, profile_id)` — prevents cross-profile time tracking suppression
- PIN comparison guards against `None` stored PIN (previously raised `TypeError`)
- Channel unallow/unblock callback_data truncated to Telegram's 64-byte limit with prefix-match fallback
- Session cookie set to `SameSite=Strict`
- Removed dead imports and unused `YouTubeExtractor.timeout` parameter

**Added**
- 241 automated tests (pytest): utils, config, video_store, child_store, extractor, callback router, web deps, web integration
- `REFACTOR.md` documenting the full refactor with metrics and confidence score

## v1.25.0 - 2026-02-25

**Added**
- Avatar customization: tap the header avatar badge to open a dropdown with 16 emoji icons and 8 background colors
- Selections persist per-profile in the database and update instantly without page reload
- Login profile picker shows each child's custom avatar icon and color
- "Switch Profile" link in avatar dropdown (multi-profile setups)
- `POST /api/avatar` endpoint with allowlist validation (rate limited 10/min)

## v1.24.2 - 2026-02-25

**Changed**
- Mobile header: search bar replaced with Search button that opens a floating overlay
- Profile avatar shown as rounded-square initial badge in header (all screen sizes)
- Desktop header: search bar centered between logo and profile avatar
- Search results no longer include YouTube channel/playlist entries (fixes broken thumbnails)

## v1.24.1 - 2026-02-24

**Fixed**
- Login redirect loop when only one profile exists and it has a PIN
- `/pending`, `/approved`, `/stats`, `/logs`, `/search`, `/channel`, `/revoke` now scoped per-child profile (previously always operated on default profile)
- All inline button callbacks (unallow/unblock, resend, channel menu/filter/page, starter import, pagination) now carry profile_id for correct routing

## v1.24.0 - 2026-02-24

**Added**
- `/time`, `/shorts`, `/watch` commands now scoped per-child profile — each child gets independent time limits, schedules, bonus minutes, and Shorts settings
- `/time setup` wizard preserves child context through all steps (button and custom text flows)

**Fixed**
- `/time off` no longer overridden by `config.yaml` defaults (string vs integer fallback guard)

**Changed**
- Config-level time limit fallbacks (`time_limits.*` in YAML) only apply to the "default" profile — new profiles start with no restrictions
- Mode switch confirmation callbacks (`switch_confirm`) include profile_id for correct scoping

## v1.23.0 - 2026-02-24

**Added**
- Multi-child profile support — each child gets isolated videos, channels, watch history, and time limits
- `/child` command for profile management (`add`, `remove`, `rename`, `pin` subcommands)
- Profile picker login flow ("Who's Watching?" → optional PIN entry per child)
- Cross-child auto-approve: when a video is already approved for one child, the notification for another shows an "Auto-approve" button
- `ChildStore` wrapper that scopes all `VideoStore` operations to a single profile
- Profile badge in web UI header with "Switch" link when multiple profiles exist
- `profiles` table with full CRUD and cascade delete

**Changed**
- All database tables (`videos`, `channels`, `watch_log`, `search_log`) now include a `profile_id` column — existing databases auto-migrate on startup
- Unique constraints updated: `videos(video_id)` → `videos(video_id, profile_id)`, `channels(channel_name)` → `channels(channel_name, profile_id)`
- Callback data format: `action:video_id` → `action:profile_id:video_id` (legacy 2-part format still accepted)
- Channel cache is now per-profile
- Time limits, schedule windows, and category budgets resolve per-profile
- Telegram notifications include child name when multiple profiles exist
- "Time's Up" page shows child name (e.g., "Alex has used..." instead of "You've used...")
- PIN auth middleware rewritten for profile-based sessions (`child_id` / `child_name` in session)
- Auto-creates "default" profile on first startup (inherits PIN from config)

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
