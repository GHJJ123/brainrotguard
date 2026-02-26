<p align="center">
  <img src="web/static/brg-logo.png" alt="BrainRotGuard" width="300">
</p>

<p align="center">
  <strong>YouTube approval system for kids.</strong><br>
  Your child searches for videos on a tablet. You approve or deny from your phone via Telegram.
</p>

---

## Contents
- [What Is This?](#what-is-this)
- [Features](#features)
- [Quick Start](#quick-start)
- [What You'll Need](#what-youll-need)
- [Documentation](#documentation)
- [License](#license)

## What Is This?

BrainRotGuard puts you in control of what your kids watch on YouTube — without standing over their shoulder.

Your child gets a simple web page on their tablet where they can search YouTube and request videos. Every request sends you a Telegram message with the video thumbnail, title, channel, and duration. You tap **Approve** or **Deny** right in the chat. If approved, the video starts playing on their tablet automatically.

No YouTube account needed. No ads. No algorithmic rabbit holes. No "up next" autoplay.

### How It Works

```
Kid's Tablet ----> BrainRotGuard Server ----> Router ----> Telegram Cloud ----> Parent's Phone
                                                |
                                          DNS (AdGuard/Pi-hole)
                                          X  youtube.com (blocked)
                                          OK youtube-nocookie.com (allowed)
```

1. Kid opens BrainRotGuard on their tablet and searches for a video
2. They tap **Request** on the one they want to watch
3. You get a Telegram notification with all the details
4. You tap **Approve** or **Deny**
5. Their screen updates automatically — approved videos play right away

### Screenshots

Works on any screen size — desktop, tablet, and phone:

<img src="docs/screenshots/screenshots-combined.png" alt="BrainRotGuard on desktop, tablet, and phone — homepage, search, watch, and activity views" width="900">

### Demo

Kid searches, requests a video, parent approves via Telegram, video plays:

https://github.com/user-attachments/assets/e36eeb48-bb28-4edd-8d8e-10d2fd30d002

### Why I Built This

<details>
<summary>The short version: YouTube's algorithm was winning, and every parental control was either too strict or too loose.</summary>

I'm a father of a preteen son. I didn't want to block YouTube completely — YouTube is genuinely how I learn things myself, and I wanted my son to have that same ability to research topics, explore educational content, and develop the problem-solving habit of "let me figure this out." That's a skill I want him to have.

The problem was his feed. It was overrun with gamers screaming into microphones and brainrot content. I'd tell him to change the channel every time I walked by and heard one of those obnoxious gaming videos. He'd switch, but YouTube's algorithm would pull him right back within minutes. The algorithm is designed to keep kids glued — and it's very good at its job.

Every parental control I tried was either too restrictive (block YouTube entirely) or too permissive (YouTube Kids still recommends garbage). I needed the middle ground: let him explore and search freely, but give me the final say on what actually plays.

I'm not a developer — just a dad who wanted to protect his kid from YouTube's brainrot algorithms. I am a homelab enthusiast though, so I'm comfortable tinkering with self-hosted tools. I built this entirely with Claude (Anthropic's AI) — I described what I wanted, Claude wrote the code. Every release went through security reviews and code reviews via Claude before I pushed it.

BrainRotGuard removes the algorithm entirely. There's no autoplay, no "up next" sidebar, no recommendation engine pulling him deeper. He searches for what he wants, I approve or deny, and the video plays and stops. Done. No rabbit holes.

Don't want them watching gaming content? Block those channels. Tired of a specific creator? One tap. You can allow the channels you trust (educational, science, building, nature) and block the ones you don't — and it sticks. He picks what to ask for, I have the final say.

Now I curate his content and I can see the difference. He's not parroting gamer lingo back at me anymore. The stuff he watches is actually interesting — things he's curious about, things he's learning from. I pair this with Google Family Link on his device for general screen time, but BrainRotGuard is what controls YouTube specifically: daily time limits, scheduled access windows, and per-channel approval. Family Link says "the tablet turns off at 8pm." BrainRotGuard says "you can watch 2 hours of entertainment YouTube today, but educational content is unlimited — and only from channels I've approved."
</details>

## Features

### For Kids
- **Works on any device** — it's just a web page. Android tablet, iPad, laptop, phone, Kindle Fire — if it has a browser, it works
- **Simple search** — type what you want, see results, tap Request
- **Instant playback** — approved videos play immediately, no waiting
- **Video library** — browse everything that's been approved before
- **Category browsing** — filter by educational or entertainment content with one tap
- **Channel browsing** — see latest videos from pre-approved channels without needing to request each one
- **YouTube Shorts** — dedicated Shorts row with portrait thumbnails and a 9:16 player
- **Thumbnail previews** — hover or scroll to cycle through multiple thumbnails before requesting
- **Dark theme** — easy on the eyes, designed for tablets

### For Parents
- **Telegram approval** — approve/deny from anywhere with one tap
- **Channel allow/block lists** — trust a channel once, and new videos from it are auto-approved
- **Multi-child profiles** — separate PINs, watch history, and time budgets per child
- **Edu/Fun categories** — label channels and videos as educational or entertainment, each with its own daily time limit
- **Daily screen time limits** — set separate limits for educational and entertainment content, or a single global limit
- **Scheduled hours** — define when watching is allowed (e.g., 8am–7pm, not during school)
- **Per-day schedules** — different time windows and limits for each day of the week (e.g., relaxed weekends, tighter school days)
- **Guided setup wizard** — `/time setup` walks through limit mode and schedule configuration with inline buttons
- **Bonus time** — grant extra minutes for today only (`/time add 30`)
- **Shorts control** — toggle YouTube Shorts visibility across the entire app
- **Watch activity log** — see what was watched, for how long, grouped by category
- **Word filters** — block videos whose titles contain specific words
- **Search history** — see everything your child has searched for
- **Starter channels** — curated list of kid-friendly channels (edu + fun) to import on first boot
- **Update notifications** — automatic Telegram alert when a new version is available on GitHub
- **PIN lock** — optional PIN gate so only your kid can access the web UI on the right device

### Privacy & Security
- **100% self-hosted** — runs entirely on your own hardware inside your home network. No cloud service, no third-party accounts, no subscriptions
- **No YouTube account required** — no sign-in, no tracking, no personalized ads
- **No API key needed** — uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) for search and metadata
- **Privacy-respecting embeds** — videos play via `youtube-nocookie.com` (Google's reduced-tracking embed domain)
- **Single-file database** — all data is one SQLite file on your machine. Nothing phones home.
- **Container runs as non-root** — Docker security best practice

## Quick Start

> **Prerequisites:** [Docker](https://docs.docker.com/get-docker/), a [Telegram bot token](https://core.telegram.org/bots#how-do-i-create-a-bot), and your [chat ID](docs/setup.md#step-2-get-your-chat-id). New to this? The **[full setup guide](docs/setup.md)** walks through everything step by step.

```bash
git clone https://github.com/GHJJ123/brainrotguard.git
cd brainrotguard
cp .env.example .env
cp config.example.yaml config.yaml
# Edit .env with your bot token and chat ID
docker compose up -d
```

Open `http://<your-server-ip>:8080` on the kid's tablet.

**Pre-built image** (no build step, supports amd64 + arm64):
```bash
docker pull ghcr.io/ghjj123/brainrotguard:latest
```
See the [full setup guide](docs/setup.md#using-the-pre-built-docker-image) for compose file details.

**Unraid:** Add the [template repository](docs/setup.md#installing-on-unraid) in Unraid's Docker tab for a guided install — no command line needed.

**Important:** You'll also want to [block YouTube at the DNS level](docs/setup.md#step-5-block-youtube-on-the-kids-devices) on the kid's devices — otherwise they can just open youtube.com directly.

## What You'll Need

| Requirement | What It Is |
|-------------|-----------|
| **A computer that stays on** | Raspberry Pi, old laptop, or home server — anything running Docker |
| **Docker** | [Install Docker](https://docs.docker.com/get-docker/) |
| **Telegram account** | The messaging app where you'll receive approval requests |
| **Telegram bot token** | Created in 5 minutes via [@BotFather](https://core.telegram.org/bots#how-do-i-create-a-bot) |
| **DNS-level blocking** | [AdGuard Home](https://adguard.com/en/adguard-home/overview.html), [Pi-hole](https://pi-hole.net/), or router-level blocking |

> **Why Telegram?** It was the easiest way to build instant notifications with approve/deny buttons that work from your phone. No custom app to develop, no push notification infrastructure to maintain — Telegram handles all of that.

> **Network note:** BrainRotGuard runs on your home network. Your child's device needs to be on the same network (that's where DNS blocking and the web UI live). You can approve/deny from anywhere via Telegram — you don't need to be home for that part.

## Documentation

- **[Setup Guide](docs/setup.md)** — full walkthrough from Telegram bot creation to DNS blocking
- [Configuration Reference](docs/configuration.md) — config.yaml options, environment variables, defaults
- [Telegram Commands](docs/telegram-commands.md) — full command list for the parent bot
- [Troubleshooting](docs/troubleshooting.md) — common issues and fixes
- [Architecture](docs/architecture.md) — system diagrams and request flow
- [Design Decisions](docs/design-decisions.md) — why yt-dlp, Telegram, SQLite, etc.
- [Running Without Docker](docs/running-without-docker.md) — plain Python setup (no containers)

## Support

If you find BrainRotGuard useful:

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/coffee4jj)

## License

[MIT](LICENSE) — use it however you want.
