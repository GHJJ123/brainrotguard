# Setup Guide

Full walkthrough for getting BrainRotGuard running. If you already know your way around Docker and Telegram bots, the [Quick Start in the README](../README.md#quick-start) may be all you need.

## Contents
- [Step 1: Create a Telegram Bot](#step-1-create-a-telegram-bot)
- [Step 2: Get Your Chat ID](#step-2-get-your-chat-id)
- [Step 3: Install BrainRotGuard](#step-3-install-brainrotguard)
- [Step 4: Start It Up](#step-4-start-it-up)
- [Step 5: Block YouTube on the Kid's Devices](#step-5-block-youtube-on-the-kids-devices)
- [Using the Pre-Built Docker Image](#using-the-pre-built-docker-image)
- [Installing on Unraid](#installing-on-unraid)
- [Running Without Docker](#running-without-docker)

## Step 1: Create a Telegram Bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Choose a name (e.g., "BrainRotGuard") and a username (e.g., "mybrainrotguard_bot")
4. BotFather gives you a **token** — it looks like `123456789:ABCdefGhIjKlMnOpQrStUvWxYz`. Copy it, you'll need it in Step 3.

## Step 2: Get Your Chat ID

1. Open a new chat with your bot — search for `@yourbotname_bot` in Telegram, or go to `https://t.me/yourbotname_bot` in your browser. Press **Start**, then send any message (e.g., "hello")
2. Open this URL in your browser (replace `YOUR_TOKEN` with the token from Step 1):
   ```
   https://api.telegram.org/botYOUR_TOKEN/getUpdates
   ```
3. Look for `"chat":{"id":123456789}` in the response — that number is your **chat ID**

## Step 3: Install BrainRotGuard

```bash
# Download the project
git clone https://github.com/GHJJ123/brainrotguard.git
cd brainrotguard

# Create your secrets file
cp .env.example .env
# Create your config file
cp config.example.yaml config.yaml
```

Edit the `.env` file and fill in your values:
```
BRG_BOT_TOKEN=123456789:ABCdefGhIjKlMnOpQrStUvWxYz
BRG_ADMIN_CHAT_ID=987654321
BRG_PIN=1234
BRG_BASE_URL=http://192.168.1.100:8080
```
You can optionally edit the defaults in the config.yaml.

| Setting | What to put | Required? |
|---------|------------|-----------|
| `BRG_BOT_TOKEN` | The token from Step 1 | Yes |
| `BRG_ADMIN_CHAT_ID` | The chat ID from Step 2 | Yes |
| `BRG_PIN` | A PIN code kids enter to use the web UI. Leave empty to skip. | No |
| `BRG_BASE_URL` | Your server's LAN address (e.g. `http://192.168.1.100:8080`). Enables clickable links in Telegram bot messages. Use an IP address, not a hostname. | No |

## Step 4: Start It Up

```bash
docker compose up -d
```

That's it. Open `http://<your-server-ip>:8080` on the kid's tablet.

To check that it's running:
```bash
docker compose logs -f
```

To stop it:
```bash
docker compose down
```

To update to a new version:
```bash
git pull
docker compose up -d --build
```

## Step 5: Block YouTube on the Kid's Devices

Without this step, your kid can just open youtube.com in a browser or use the YouTube app and bypass BrainRotGuard entirely. DNS-level blocking prevents that — it makes YouTube unreachable on their devices while keeping BrainRotGuard's embedded playback working.

**Block these domains** (prevents direct YouTube access):
- `youtube.com`
- `www.youtube.com`
- `m.youtube.com`
- `youtubei.googleapis.com`

**Allow these domains** (required for embedded playback through BrainRotGuard):
- `www.youtube-nocookie.com`
- `*.googlevideo.com`

### AdGuard Home

1. Go to **Filters** > **Custom filtering rules**
2. Add blocking rules:
   ```
   ||youtube.com^
   ||m.youtube.com^
   ||youtubei.googleapis.com^
   ```
3. Add allowlist rules (under **DNS allowlists** or prefix with `@@`):
   ```
   @@||www.youtube-nocookie.com^
   @@||googlevideo.com^
   ```
4. Under **Client settings**, apply these rules only to the kid's device if you don't want to block YouTube for everyone on your network

### Pi-hole

Add the block domains to your blocklist and the allow domains to your allowlist. Same domains, just entered through the Pi-hole admin UI.

### Other Options

Any DNS filtering tool that lets you block/allow specific domains will work — pfBlockerNG, NextDNS, router-level parental controls, etc.

## Using the Pre-Built Docker Image

If you don't want to build from source, you can pull the pre-built image from GitHub Container Registry. It supports both `amd64` and `arm64` (Raspberry Pi, Unraid, etc.).

```bash
docker pull ghcr.io/ghjj123/brainrotguard:latest
```

Then use the example compose file instead of building locally:

```bash
# Download config and env templates
curl -O https://raw.githubusercontent.com/GHJJ123/brainrotguard/main/config.example.yaml
curl -O https://raw.githubusercontent.com/GHJJ123/brainrotguard/main/.env.example
curl -O https://raw.githubusercontent.com/GHJJ123/brainrotguard/main/docker-compose.example.yml

# Set up your config
cp config.example.yaml config.yaml
cp .env.example .env
# Edit .env with your bot token and chat ID

# Start it
docker compose -f docker-compose.example.yml up -d
```

To update to a new version:
```bash
docker compose -f docker-compose.example.yml pull
docker compose -f docker-compose.example.yml up -d
```

## Installing on Unraid

BrainRotGuard has an Unraid template for guided installation through the Docker tab.

### Add the Template Repository

1. In the Unraid web UI, go to **Docker** > **Add Container** > **Template Repositories**
2. Add this URL:
   ```
   https://raw.githubusercontent.com/GHJJ123/brainrotguard/main/unraid-template.xml
   ```
3. Click **Save**

### Create the Container

1. Go to **Docker** > **Add Container**
2. Select **BrainRotGuard** from the template dropdown
3. Fill in the required fields:

   | Field | What to put |
   |-------|------------|
   | **Telegram Bot Token** | The token from [@BotFather](https://core.telegram.org/bots#how-do-i-create-a-bot) |
   | **Telegram Admin Chat ID** | Your numeric chat ID ([how to find it](#step-2-get-your-chat-id)) |

4. Optional fields (click **Show more settings** if needed):

   | Field | What it does | Default |
   |-------|-------------|---------|
   | **PIN Code** | Protect the web UI with a PIN | No PIN |
   | **Base URL** | LAN address for Telegram deep links (e.g. `http://192.168.1.50:8080`) | Auto-detected |
   | **Daily Watch Limit** | Daily screen time limit in minutes (0 = unlimited) | 120 |
   | **Timezone** | For daily resets and scheduled access windows | America/New_York |
   | **Config File** | Mount a custom `config.yaml` for advanced settings | Not needed |

5. Click **Apply**

The container will pull the image and start. Open `http://<your-unraid-ip>:8080` on the kid's tablet.

### Updating

Unraid handles updates automatically when a new image is pushed. You'll see an update notification in the Docker tab — click **Apply Update** to pull the latest version.

## Running Without Docker

See [Running Without Docker](running-without-docker.md) for a plain Python setup.
