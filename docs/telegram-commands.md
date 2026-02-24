# Telegram Commands

Once BrainRotGuard is running, these commands are available in your Telegram chat with the bot.

## General

| Command | What It Does |
|---------|-------------|
| `/help` | Show all available commands |
| `/pending` | List videos waiting for your approval |
| `/approved` | List all approved videos with view counts |
| `/approved <search>` | Search approved videos by title |
| `/stats` | Summary: total approved, denied, pending, and views |
| `/watch` | Today's watch activity grouped by edu/fun with per-category progress bars |
| `/watch yesterday` | Yesterday's watch activity |
| `/watch <N>` | Watch activity for N days ago |
| `/logs [days\|today]` | Activity report for a given period |
| `/changelog` | Show what's new in the latest version |

## Channel Management

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

## Time Limits & Schedule

| Command | What It Does |
|---------|-------------|
| `/time` | Show today's status + weekly schedule overview |
| `/time setup` | Guided wizard to configure limit mode (simple or category) |
| `/time <min\|off>` | Set a simple daily limit (shared pool for all videos) |
| `/time edu <min\|off>` | Set daily limit for educational content (0 or off = unlimited) |
| `/time fun <min\|off>` | Set daily limit for entertainment content (0 or off = unlimited) |
| `/time start <time>` | Set when watching is allowed to begin |
| `/time stop <time>` | Set when watching must stop |
| `/time add <min>` | Grant bonus minutes for today (applies to both categories, stacks, resets tomorrow) |
| `/time <day> start\|stop <time>` | Set schedule for a specific day (e.g. `/time mon start 8am`) |
| `/time <day> edu\|fun <min>` | Set category limit for a specific day |
| `/time <day> limit <min>` | Set simple limit for a specific day |
| `/time <day>` | Show effective settings for a specific day |
| `/time <day> off` | Clear all overrides for a day (falls back to defaults) |
| `/time <day> copy <targets>` | Copy day overrides to other days (e.g. `weekdays`, `weekend`, `all`) |

## Other

| Command | What It Does |
|---------|-------------|
| `/shorts [on\|off]` | Enable or disable YouTube Shorts playback |
| `/start` | Welcome message explaining bot purpose and starter channels |

## Approval Flow

When a child requests a video, the parent receives a Telegram notification with these buttons:

- **Approve (Edu)** / **Approve (Fun)** — approve the video and tag it as educational or entertainment
- **Deny** — reject the video
- **Allow Ch (Edu)** / **Allow Ch (Fun)** — allowlist the entire channel with a category + approve the video
- **Block Channel** — blocklist the channel + deny the video

After approval, two buttons remain:
- **Revoke** — revoke approval (video becomes denied)
- **→ Edu** / **→ Fun** — toggle the video's category without revoking
