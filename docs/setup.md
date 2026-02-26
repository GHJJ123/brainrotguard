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

### Option A: Community Apps (Recommended)

If BrainRotGuard is available in Community Applications, search for **BrainRotGuard** in the **Apps** tab and click **Install**. The template will pre-fill all the fields — just enter your Telegram bot token and chat ID.

### Option B: Template File

Download the template XML to your Unraid flash drive, then use it from the Add Container dropdown:

1. Open an Unraid terminal (or SSH in) and run:
   ```bash
   wget -O /boot/config/plugins/dockerMan/templates-user/my-brainrotguard.xml \
     https://raw.githubusercontent.com/GHJJ123/brainrotguard/main/unraid-template.xml
   ```
2. Go to **Docker** > **Add Container**
3. In the **Template** dropdown, select **BrainRotGuard**
4. Fill in your **Telegram Bot Token** and **Admin Chat ID**
5. Click **Apply**

### Option C: Manual Install

If you prefer to set up each field yourself:

1. Go to **Docker** > **Add Container**
2. Fill in the top-level fields:

   | Field | Value |
   |-------|-------|
   | **Name** | `BrainRotGuard` |
   | **Repository** | `ghcr.io/ghjj123/brainrotguard:latest` |
   | **Icon URL** | `https://raw.githubusercontent.com/GHJJ123/brainrotguard/main/web/static/brg-icon-512.png` |
   | **WebUI** | `http://[IP]:[PORT:8080]` |
   | **Network Type** | Bridge |

3. Click **Add another Path, Port, Variable, Label or Device** to add each of the following:

   **Port:**

   | Field | Value |
   |-------|-------|
   | Config Type | Port |
   | Name | `Web UI Port` |
   | Container Port | `8080` |
   | Host Port | `8080` |

   **Path (database volume):**

   | Field | Value |
   |-------|-------|
   | Config Type | Path |
   | Name | `Database` |
   | Container Path | `/app/db` |
   | Host Path | `/mnt/user/appdata/brainrotguard/db` |
   | Access Mode | Read/Write |

   **Variables (add each one separately):**

   | Name | Key | Value | Required |
   |------|-----|-------|----------|
   | Telegram Bot Token | `BRG_BOT_TOKEN` | Your token from [@BotFather](https://core.telegram.org/bots#how-do-i-create-a-bot) | Yes |
   | Telegram Admin Chat ID | `BRG_ADMIN_CHAT_ID` | Your numeric chat ID ([how to find it](#step-2-get-your-chat-id)) | Yes |
   | PIN Code | `BRG_PIN` | PIN to protect the web UI (leave empty to skip) | No |
   | Base URL | `BRG_BASE_URL` | LAN address for Telegram links (e.g. `http://192.168.1.50:8080`) | No |
   | Daily Watch Limit | `BRG_DAILY_LIMIT_MINUTES` | Minutes per day, 0 = unlimited (default: 120) | No |
   | Timezone | `BRG_TIMEZONE` | e.g. `America/New_York`, `Europe/London` | No |

4. Click **Apply**

The container will pull the image and start. Open `http://<your-unraid-ip>:8080` on the kid's tablet.

### Updating

When a new image is pushed, Unraid shows an update notification in the Docker tab. Click the container icon and select **Update** to pull the latest version.

## Running Without Docker

See [Running Without Docker](running-without-docker.md) for a plain Python setup.
