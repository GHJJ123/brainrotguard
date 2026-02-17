# Telegram Commands

Once BrainRotGuard is running, these commands are available in your Telegram chat with the bot:

| Command | What It Does |
|---------|-------------|
| `/help` | Show all available commands |
| `/pending` | List videos waiting for your approval |
| `/approved` | List all approved videos with view counts |
| `/denied` | List denied videos |
| `/stats` | Summary: total approved, denied, pending, and views |
| `/channel` | Browse allowlisted channels with management buttons |
| `/channel allow @handle [edu\|fun]` | Auto-approve all videos from a channel, optionally tagged as edu or fun |
| `/channel cat <name> edu\|fun` | Change an existing channel's category |
| `/channel block @handle` | Auto-deny all videos from a channel |
| `/time` | Show current schedule, screen time, and per-category usage |
| `/time edu <min\|off>` | Set daily limit for educational content (0 or off = unlimited) |
| `/time fun <min\|off>` | Set daily limit for entertainment content (0 or off = unlimited) |
| `/time start 8am` | Set when watching is allowed to begin |
| `/time stop 7pm` | Set when watching must stop |
| `/time add 30` | Grant 30 bonus minutes for today (applies to both categories, stacks, resets tomorrow) |
| `/watch` | Today's watch activity grouped by edu/fun with per-category progress bars |
| `/watch yesterday` | Yesterday's watch activity |
| `/search history` | See everything your child has searched for |
| `/changelog` | Show what's new in the latest version |

## Approval Flow

When a child requests a video, the parent receives a Telegram notification with these buttons:

- **Approve (Edu)** / **Approve (Fun)** — approve the video and tag it as educational or entertainment
- **Deny** — reject the video
- **Allow Ch (Edu)** / **Allow Ch (Fun)** — allowlist the entire channel with a category + approve the video
- **Block Channel** — blocklist the channel + deny the video

After approval, two buttons remain:
- **Revoke** — revoke approval (video becomes denied)
- **→ Edu** / **→ Fun** — toggle the video's category without revoking
