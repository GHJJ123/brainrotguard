# Changelog
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
- Text-based summary chart in multi-day `/watch` output — shows daily totals with progress bars scaled to busiest day


## Upgrading

Upgrades are seamless — rebuild and restart with no manual steps:
```
docker compose down && docker compose build && docker compose up -d
```
Database schema changes (if any) are applied automatically on startup. Existing data is preserved.

### v1.11.3–v1.11.7 behavioral notes

These won't cause errors on upgrade, but you may notice different behavior:

- **`/channel block` syntax changed** (v1.11.3): Now requires `@handle` format (`/channel block @SomeChannel`) instead of free-text names. Existing blocked channels in the DB are unaffected. You'll get a usage hint if you use the old format.
- **`/api/catalog` now requires PIN session** (v1.11.7): The web UI handles this transparently (already authenticated via login). Only affects external scripts hitting the endpoint directly — they'll get a 401.
- **Search word filters are stricter** (v1.11.3): Queries containing filtered words now return zero results immediately, instead of only filtering matching titles from results.
- **CSP headers added** (v1.11.7): May need attention if you run behind a reverse proxy that sets its own CSP.

---

## v1.12.3 - Security Hardening

- Add video_id format validation at entry of `/pending/` and `/watch/` routes
- Fix unallow/unblock buttons failing on channel names containing colons
- Require HTTPS scheme for thumbnail URLs (reject HTTP)
- Cap `days` parameter in `/watch`, `/logs`, `/search history` commands to 365

## v1.12.2 - Fix Video Extraction Failures

- Fix yt-dlp failing on videos where playable formats aren't available but metadata is (fixes #4 — thanks @EricReiche)
- Search page now shows error banner when a pasted URL can't be loaded

## v1.12.1 - Config & Catalog Fixes

- Fix `daily_limit_minutes` in config.yaml being ignored after first boot (fixes #7 — thanks @TheDogg)
- Revoked videos no longer appear in the homepage catalog from allowlisted channels (fixes #8 — thanks @EricReiche)

## v1.12.0 - Group Chat Support & Error Handling

- Group chat admin authorization: bot now works in Telegram group chats, allowing multiple parents to approve/deny videos (fixes #1 — thanks @EricReiche)
- Error banner on homepage when video metadata extraction fails instead of silent redirect (addresses #4 — thanks @EricReiche)
- Session cookie max_age reduced from 14 days to 24 hours (security hardening)
- README: added config file setup step (PR #2 — thanks @EricReiche)

## v1.11.8 - Version Tag & Ko-fi Link
- Version badge below logo in web header, links to GitHub changelog
- Ko-fi "Buy me a coffee" link in web footer and Telegram `/help`
- Ko-fi support section in README

## v1.11.7 - Security Headers & Efficiency Fixes
- Security headers middleware: CSP, X-Frame-Options DENY, X-Content-Type-Options nosniff, Referrer-Policy
- PIN auth tightened: `/api/catalog` and `/api/watch-heartbeat` now require session auth (only status polling and YT script proxies exempt)
- Thumbnail URL validation at both extractor and storage layers against YouTube CDN hostname allowlist (SSRF prevention)
- Fix `_last_heartbeat` dict unbounded memory growth with periodic eviction of stale entries
- SQL pagination for `/approved` bot command (no longer loads full table into memory)
- Fix `_build_catalog` mutating shared channel cache dicts (copy on use)
- Extract `resolve_handle_from_channel_id()` to `extractor.py` (eliminates duplicate code in bot + main)

## v1.11.6 - Fix Mobile Horizontal Scroll on Category Cards
- Added `overflow-x: hidden` to body and main to prevent horizontal scroll on mobile
- Category card labels truncate with ellipsis on narrow screens
- Tightened category card padding and progress bar width for mobile fit
- Clock icon (activity link) sits inline without `margin-left: auto` push
- Added `docker image prune -f` to deploy.sh for post-deploy cleanup

## v1.11.5 - Auto-resolve @handles for Inline Channel Actions
- Inline Allow/Block Channel buttons now resolve @handle in background via yt-dlp
- Startup backfill resolves missing @handles for legacy channels
- Both paths ensure /channel list always shows @handles

## v1.11.4 - Channel Handle Support in Commands
- `/channel cat` now accepts @handle (resolves to display name for lookup)
- `/channel` list shows @handle next to each channel name for easy reference
- Added `resolve_channel_name()` for handle-to-name resolution in VideoStore

## v1.11.3 - Channel Block Fix + Search Query Filter
- Fixed `/channel block @handle` not resolving handle to display name (blocked channels weren't filtered from search results)
- `/channel block` now requires `@handle` format (was free-text channel name), resolves via yt-dlp to match `/channel allow` behavior
- Search word filters now block the query itself (returns zero results immediately instead of only filtering titles)

## v1.11.2 - Timezone Fix + Category Cache + Activity Link

- Fixed watch time queries using wrong timezone boundaries (UTC timestamps queried with local date, causing time to reset on redeploy)
- Added `get_day_utc_bounds()` to convert local dates to UTC ranges for all watch time queries
- Fixed channel category changes not persisting in video cache (always re-apply current channel category)
- Added clock icon linking to activity page from category cards row
- Category cards: progress bar visibility fix, desktop width cap, layout refinements

## v1.11.1 - Category Filter Cards + Desktop Scrollbar Fix

- Redesigned category filter from pills to compact horizontal cards
- Cards show category name, time remaining (large), and progress bar
- Unlimited categories display "Unlimited" label without progress bar
- Exhausted categories show "time's up" with dimmed state
- Fixed channel pill scrollbar hidden on desktop (now shows thin scrollbar on mouse devices, hidden on touch)

## v1.11.0 - Edu/Fun Category Time Limits

- Two fixed categories: edu (educational) and fun (entertainment)
- Per-category daily time limits via `/time edu|fun <min|off>`
- Approve buttons split into "Approve (Edu)" / "Approve (Fun)"
- Allow Channel buttons split into "Allow Ch (Edu)" / "Allow Ch (Fun)"
- `/channel allow @handle edu|fun` sets category on allowlist
- Homepage: category filter pills with remaining-time badges
- Video thumbnails: edu (green) / fun (orange) category badges
- Watch page: category-specific countdown ("Entertainment: 12 min left")
- Time's up page: shows which category is exceeded + links to browse other category
- Activity page: per-category grouped breakdown with progress bars
- `/watch` and `/time` commands show per-category usage
- Bonus minutes (`/time add`) apply to both categories equally
- Uncategorized videos default to "fun"

## v1.10.3 - Fix Homepage Channel Variety

- Restored round-robin interleaving for homepage catalog
- yt-dlp flat extraction returns no timestamps, so pure date sort was a no-op
- First channel in dict iteration (BBC Earth) dominated the entire homepage
- Round-robin picks one video from each channel in turn for balanced variety

## v1.10.2 - CSS Efficiency Improvements

- Added CSS custom properties (`:root` block) with 12 design tokens for colors, borders, and radii
- Replaced ~50 hardcoded color/radius values with `var()` references throughout `style.css`
- Extracted shared `.btn-primary` class from 4 duplicate gradient button selectors (search, request, login, show more)
- Fixed `.empty-hint` specificity: changed selector to `.empty-state .empty-hint`, removed `!important`
- Net reduction of 16 lines in `style.css`

## v1.10.1 - Screenshot & Demo Refresh

- Updated all device screenshots for new UI (swipe hero, search in header)
- Re-recorded and compressed demo video for GitHub inline playback (18MB → 1.4MB)
- New combined showcase: landscape iPad (top) + 3 iPhone bezels (bottom) + full-page strip (right)
- Added landscape tablet screenshot for showcase layout

## v1.9.7 - Screenshot Refresh & Search Results

- Retook phone screenshots at iPhone 17 Pro Max resolution (440x956 @2x) for proper tall aspect ratio
- Rebuilt combined screenshot image: tablet + 3 bezeled phones (left column), full-page scroll (right column)
- Increased `search_max_results` default from 10 to 50

## v1.9.6 - Network Diagram

- Added ASCII network diagram to README "How It Works" section showing kid device, BrainRotGuard server, router, DNS blocking, Telegram cloud, and parent device

## v1.9.5 - README Restructure

- Moved Configuration Reference, Telegram Commands, Troubleshooting, Architecture, and Design Decisions into separate `docs/*.md` files
- Added table of contents to README
- Added Documentation links section for quick access to reference material
- README reduced from 368 to 269 lines, focused on pitch + setup flow

## v1.9.4 - Architecture Diagrams

- Added Mermaid architecture diagram to README: all page routes, API routes, Telegram bot commands/callbacks, external services, and data flows
- Added Mermaid sequence diagram showing the video request/approval flow (auto-approve, auto-deny, and manual approval paths)

## v1.9.3 - Search Show More Pagination

- Client-side "Show More" pagination on search results: shows first 5 results, reveals next batch on tap
- Reuses existing `.show-more-btn` styling from homepage catalog
- Button auto-hides when all results are visible; not rendered if ≤5 results

## v1.9.2 - README Overhaul & Video End Overlay

- Rewrote README for non-technical audience: plain-language feature descriptions, design decisions table, step-by-step setup guide, troubleshooting section
- Added production screenshots (homepage, search, playback, activity, mobile views)
- Video end overlay: covers YouTube's suggested video overlay when a video finishes, shows "Video finished — Back to Library" instead
- Emphasized DNS blocking as effectively required (not just recommended)

## v1.9.1 - Channel Link Fix

- `/channel` list now uses @handle URL (`youtube.com/@handle`) when channel_id is unavailable, instead of falling back to YouTube search query

## v1.9.0 - Unified Curated Catalog & Channel Management

- Merged channel cache videos and individually approved videos into single browsable catalog
- Round-robin interleave across channels for variety at the top of the grid
- Client-side "Show More" progressive loading via `/api/catalog` endpoint (replaces server-side pagination)
- Channel pills visible on all screen sizes (not just mobile), filter catalog by channel with "All" default pill
- Removed desktop sidebar navigation (channel tree with collapsible nodes)
- Added `_build_catalog()` helper with deduplication by video_id and LRU caching (invalidated on channel refresh or video status change)
- Added `GET /api/catalog` endpoint with offset/limit pagination and optional channel filter (DB query, not Python filtering)
- Client-side `buildCard()` and `formatDuration()` JS helpers for dynamic card rendering
- Show More button now full-width with red gradient styling, always in DOM (hidden when no more results)
- Channel cache default increased to 200 videos per channel (configurable via `youtube.channel_cache_results`)
- New config parameters: `youtube.channel_cache_results`, `youtube.channel_cache_ttl`, `youtube.ydl_timeout` (all with env var support)
- `/channel allow @handle` now requires @handle format; resolves via yt-dlp to get exact channel name + channel_id
- `/channel` command uses paginated inline buttons (10 channels per page, "Show more"/"Back" nav) with in-place message refresh
- Each channel in list has inline "Unallow"/"Unblock" button for quick management
- Channel list links use channel_id URLs (prevents Telegram chat link behavior from @handle)
- `youtube/extractor.py` added `resolve_channel_handle()` to convert @handle to channel name + ID
- `data/video_store.py` updated: `get_by_status()` accepts optional `channel_name` filter (SQL WHERE clause); channels table now stores `handle`; `remove_channel()` matches by name or handle
- `main.py` wired `on_video_change` callback from bot to `_invalidate_catalog_cache()` for live updates on approve/deny

## v1.8.0 - Channel Links & /watch Command

- `/watch [yesterday|N]` Telegram command: daily watch activity with time budget progress bar and per-video breakdown (watched vs duration with percentage)
- Channel names in all Telegram messages now link to YouTube channel pages when channel_id is available (notifications, approve/deny, /pending, /approved, /channel list)
- `_channel_md_link()` helper builds `/channel/{id}` URLs with search fallback
- `get_daily_watch_breakdown()` now returns video `duration` and `channel_id`
- `/channel` list uses `get_channels_with_ids()` for proper channel page URLs

## v1.7.1 - Security Hardening

- Remove yt-dlp `js_runtimes` and `remote_components` (RCE prevention), remove nodejs from Docker image
- Require PIN auth session for `/api/watch-heartbeat` (was bypassing auth via `/api/` exemption)
- Strengthen login rate limit from 5/min to 5/hour (brute-force mitigation)
- Validate YouTube widget API proxy URL against domain allowlist before fetching (SSRF prevention)
- Add 30s `asyncio.wait_for` timeout on all yt-dlp calls (DoS prevention)
- Fix XSS: use `|tojson` for video_id in watch.html JavaScript context

## v1.7.0 - Watch Activity Log

- `/activity` page: per-video watch time breakdown for today with thumbnails, titles, and minutes
- Summary stats bar showing watched / allowed / remaining minutes with progress bar
- Time budget bar on homepage now links to activity page for quick access
- DB: `get_daily_watch_breakdown()` joins watch_log with videos for per-video daily stats

## v1.6.0 - Bonus Minutes & Library Pagination

- `/time add <min>`: grant bonus screen time for today only (stacks, auto-expires next day)
- Paginated video library on homepage (24 per page, Newer/Older navigation)
- DB: `get_approved_page()` for server-side pagination, `get_batch_watch_minutes()` for batch stats
- DB: status index on videos table, optimized daily watch query with range scan
- Startup data pruning: watch_log (180 days) and search_log (90 days) auto-cleaned
- Dockerfile: add nodejs for yt-dlp, fix `useradd -m` flag for home directory

## v1.5.0 - Silent Auto-Approve & Telegram UX Cleanup

- Allowlisted channel videos auto-approve silently (no Telegram notification)
- `/approved` list cleaned up: bullet links, compact stats, `/revoke_VID` commands
- Inline button pagination (Show more / Back) on `/approved`, `/logs`, and `/search history`
- `/search history` shows chronological log with fixed-width `MM-DD HH:MM` timestamps
- `/revoke` handles video IDs with hyphens (encoded as underscores for Telegram compat)
- Channel sidebar fix: resolves channel ID via YouTube search for generic names (e.g. LEGO)
- `channel_id` stored in DB from video metadata, used for direct channel page fetches
- `extract_metadata()` now returns `channel_id`; `channels` and `videos` tables auto-migrated

## v1.4.0 - Allowlisted Channel Discovery

- Sidebar now shows only allowlisted channels (from `/channel allow`) with fresh YouTube content
- Background task fetches latest videos for each allowlisted channel every 30 minutes
- Clicking a sidebar video from a trusted channel auto-approves and plays immediately
- `fetch_channel_videos()` in extractor searches YouTube and filters to exact channel match
- Mobile pills reflect allowlisted channels; tapping filters approved grid by channel
- Approved video grid unchanged as main content

## v1.3.1 - Channel Sidebar Navigation

- Collapsible channel tree sidebar on homepage groups approved videos by channel
- Each channel node shows mini-thumbnail previews (max 10 per channel)
- Click channel header to expand/collapse with +/− indicator
- Mobile (≤768px): sidebar replaced by horizontal scrollable channel pills
- Tapping a pill filters the video grid to that channel; tap again to clear
- No backend changes — uses existing approved video data with Jinja2 groupby

## v1.3.0 - Scheduled Access Window

- Configurable start/stop times to restrict when videos can be watched
- `/time start <time>` and `/time stop <time>` bot commands (flexible input: 800am, 20:00, etc.)
- `/time` status now shows schedule window and OPEN/CLOSED state
- Playback blocked outside schedule; search and browsing remain available
- Schedule banner on homepage shows unlock time when closed
- Heartbeat returns 403 during off-hours, triggering client-side video pause
- New `outsidehours.html` blocking page with schedule details
- `parse_time_input()`, `format_time_12h()`, `is_within_schedule()` utilities
- Overnight schedule wrap support (e.g. 22:00–06:00)

## v1.2.1 - Security Hardening

- Thread-safe SQLite access via threading.Lock on all VideoStore methods
- Session secret persisted in DB settings (survives container restarts)
- Rate limit (5/min) on PIN login endpoint
- CSRF token regenerated on failed login attempts
- Heartbeat rejects unapproved/nonexistent video IDs
- Thumbnail SSRF: exact hostname allowlist replaces suffix matching
- Admin check defense-in-depth (rejects falsy admin_chat_id)
- Config validation warns on empty/non-numeric admin_chat_id
- SHA-256 hash logging of cached YouTube API scripts
- Heartbeat interval tightened (30s→15s client, 20→10s server floor)

## v1.2.0 - Parental Controls & Content Filtering

- Daily watch time limits with configurable timezone and parent notification
- Channel allow/block lists with auto-approve and auto-deny
- Word filters with word-boundary matching to block videos by title
- Search logging and `/search history` command for parents
- `/timelimit`, `/channel`, `/search` Telegram commands
- Activity report via `/logs` command
- YouTube IFrame API proxy for playback on DNS-blocked networks
- MarkdownV2 formatting for all Telegram messages
- Inline "Allow Channel" / "Block Channel" buttons on approval notifications
- Shared timezone utility with startup validation and UTC fallback
- YT script cache with 24h TTL and graceful error handling
- Heartbeat interval clamping to prevent inflated watch time
- Telegram message truncation (4096 char limit, newline-aware)
- Time-limit notification race guard (once per day)
- Watch log index for faster per-video queries
- Search query truncation (200 char limit in DB)

## v1.1.0 - Public Release & Security Hardening

- Optional PIN auth gate for web UI (session-based, configurable via `web.pin`)
- CSRF protection on all POST routes
- Rate limiting via slowapi (10/min search+request, 30/min status poll)
- Video ID regex validation (`^[a-zA-Z0-9_-]{11}$`) and input length limits
- Thumbnail URL domain allowlist to prevent SSRF
- SQLite WAL mode for safer concurrent access
- Docker container runs as non-root user (appuser)
- Database path and poll interval moved from hardcoded to config
- Dependency versions pinned with upper bounds
- Added README.md with setup guide, config reference, AdGuard DNS instructions
- Added MIT LICENSE
- Added login page with styled PIN input

## v1.0.0 - Initial Release

- Web UI: search YouTube, request videos, watch approved videos
- Telegram bot: parent receives photo notifications with Approve/Deny inline buttons
- yt-dlp integration for metadata extraction and search (no API key needed)
- SQLite storage for video approval tracking and view counts
- youtube-nocookie.com iframe embeds for playback
- Dark theme, tablet-friendly UI with large touch targets
- Docker Compose deployment
- Admin commands: /help, /pending, /approved, /stats, /changelog
