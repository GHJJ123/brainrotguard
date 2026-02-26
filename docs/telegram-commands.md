# Telegram Commands

Once BrainRotGuard is running, these commands are available in your Telegram chat with the bot.

## Getting Started

**New to BrainRotGuard? Start here.** Run `/setup` in Telegram and the bot walks you through everything — adding children, setting time limits, importing channels, and configuring Shorts. No need to memorize commands; the wizard uses inline buttons for each step.

| Command | What It Does |
|---------|-------------|
| `/setup` | **Recommended** — interactive setup wizard (children, time limits, channels, shorts) |
| `/start` | Same as `/setup` |
| `/time setup` | Focused wizard for schedule and time limit configuration only |

Most of the commands below are for quick manual adjustments after initial setup. Everything they do can also be done through `/setup` or `/time setup`.

---

## Approvals

When a child requests a video from a non-allowlisted channel, you get a Telegram notification with Approve/Deny buttons. These commands let you review the queue.

| Command | What It Does |
|---------|-------------|
| `/pending` | List videos waiting for your approval |
| `/approved` | List all approved videos with view counts |
| `/approved <search>` | Search approved videos by title |
| `/stats` | Summary: total approved, denied, pending, and views |

### Approval Buttons

When a child requests a video, the parent receives a Telegram notification with these buttons:

- **Approve (Edu)** / **Approve (Fun)** — approve the video and tag it as educational or entertainment
- **Deny** — reject the video
- **Allow Ch (Edu)** / **Allow Ch (Fun)** — allowlist the entire channel with a category + approve the video
- **Block Channel** — blocklist the channel + deny the video

After approval, two buttons remain:
- **Revoke** — revoke approval (video becomes denied)
- **→ Edu** / **→ Fun** — toggle the video's category without revoking

## Channel Management

Allowed channels auto-approve all future videos from that channel. Blocked channels auto-deny. Each allowed channel has a category (edu or fun) that determines which time budget it counts against.

| Command | What It Does |
|---------|-------------|
| `/channel` | Browse allowlisted channels with management buttons |
| `/channel starter` | Browse and import kid-friendly starter channels |
| `/channel allow @handle [edu\|fun]` | Auto-approve all videos from a channel, optionally tagged as edu or fun |
| `/channel cat <name> edu\|fun` | Change an existing channel's category |
| `/channel unallow <name>` | Remove a channel from the allowlist |
| `/channel block @handle` | Auto-deny all videos from a channel |
| `/channel unblock <name>` | Remove a channel from the blocklist |

## Filters & Search

| Command | What It Does |
|---------|-------------|
| `/filter` | List active word filters |
| `/filter add <word>` | Hide videos with this word in the title (everywhere: catalog, Shorts, requests, search) |
| `/filter remove <word>` | Remove a word filter |
| `/search` | Search history (last 7 days) |
| `/search [days\|today\|all]` | See everything your child has searched for |

## Schedule

Playback is blocked outside the start/stop window. The child can still browse and search, just not watch.

| Command | What It Does |
|---------|-------------|
| `/time start <time>` | Set when watching is allowed to begin (e.g. `8am`, `9:30am`) |
| `/time stop <time>` | Set when watching must stop (e.g. `7pm`, `8:30pm`) |

## Time Limits

You can either set separate limits per category (edu vs fun) or a single combined limit — not both. Setting a category limit auto-clears the global limit, and vice versa.

| Command | What It Does |
|---------|-------------|
| `/time <min\|off>` | Set a simple daily limit (shared pool for all videos) |
| `/time edu <min\|off>` | Set daily limit for educational content (0 or off = unlimited) |
| `/time fun <min\|off>` | Set daily limit for entertainment content (0 or off = unlimited) |

## Bonus & Activity

| Command | What It Does |
|---------|-------------|
| `/time add <min>` | Grant bonus minutes for today (applies to both categories, stacks, resets tomorrow) |
| `/time` | Show today's status + weekly schedule overview |
| `/watch` | Today's watch activity grouped by edu/fun with per-category progress bars |
| `/watch yesterday` | Yesterday's watch activity |
| `/watch <N>` | Watch activity for N days ago |
| `/logs [days\|today]` | Activity report for a given period |

## Day Overrides

Override schedule or limits for specific days of the week (e.g. longer screen time on weekends). You can also set these up through `/time setup`.

| Command | What It Does |
|---------|-------------|
| `/time <day> start\|stop <time>` | Set schedule for a specific day (e.g. `/time mon start 8am`) |
| `/time <day> edu\|fun <min\|off>` | Set category limit for a specific day |
| `/time <day> limit <min\|off>` | Set simple limit for a specific day |
| `/time <day>` | Show effective settings for a specific day |
| `/time <day> off` | Clear all overrides for a day (falls back to defaults) |
| `/time <day> copy <targets>` | Copy day overrides to other days (e.g. `weekdays`, `weekend`, `all`) |

Day names: mon, tue, wed, thu, fri, sat, sun. Copy targets: day names, weekdays, weekend, all.

## Shorts

YouTube Shorts are short vertical videos (under 60s). When enabled, they appear in a dedicated row on the homepage. When disabled, they're hidden from the catalog, search results, and channel filters. Shorts count toward edu/fun time budgets like regular videos.

| Command | What It Does |
|---------|-------------|
| `/shorts` | Show current Shorts status (enabled or disabled) |
| `/shorts on` | Enable Shorts — shows dedicated row on homepage, fetches from allowlisted channels |
| `/shorts off` | Disable Shorts — hides from homepage, catalog, and search results |

Shorts are disabled by default. Approved Shorts stay in the database when disabled — nothing is deleted.

## Profiles

Manage child profiles. Each profile has its own PIN, watch history, and time budgets.

| Command | What It Does |
|---------|-------------|
| `/child` | List child profiles |
| `/child add <name> [pin]` | Add a new child profile with optional PIN |
| `/child remove <name>` | Remove a child profile |
| `/child rename <name>` | Rename a child profile |
| `/child pin <name>` | Change a child profile's PIN |

## Other

| Command | What It Does |
|---------|-------------|
| `/help` | Show all available commands |
| `/changelog` | Show what's new in the latest version |
