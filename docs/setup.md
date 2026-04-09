# Setup Guide

Full walkthrough for getting BrainRotGuard running. If you already know your way around Docker and Telegram bots, the [Quick Start in the README](../README.md#quick-start) may be all you need.

## Contents
- [Step 1: Create a Telegram Bot](#step-1-create-a-telegram-bot)
- [Step 2: Get Your Chat ID](#step-2-get-your-chat-id)
- [Step 3: Install BrainRotGuard](#step-3-install-brainrotguard)
- [Step 4: Start It Up](#step-4-start-it-up)
- [Step 5: Lock Down the Kid's Device](#step-5-lock-down-the-kids-device)
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

## Step 5: Lock Down the Kid's Device

Without this step, your kid can just open youtube.com in a browser or use the YouTube app and bypass BrainRotGuard entirely. The recommended approach is to install BrainRotGuard as a PWA and restrict browser access on the device.

### Android (Google Family Link)

1. **Install BrainRotGuard as a PWA**: Open Chrome on the kid's tablet, go to `http://<your-server-ip>:8080`, tap the menu (three dots) > **Add to Home screen**. This creates a standalone app.
2. **Set up Family Link**: Install [Google Family Link](https://families.google.com/familylink/) on your phone and the kid's tablet.
3. **Block browser apps**: In Family Link, go to **Controls** > **App limits** and block Chrome, Samsung Internet, and any other browser apps. The BrainRotGuard PWA will continue to work — it runs in its own app container.
4. **Block the YouTube app**: Also block the YouTube and YouTube Kids apps if installed.

### iOS / iPad (Screen Time)

1. **Install BrainRotGuard as a PWA**: Open Safari, go to `http://<your-server-ip>:8080`, tap **Share** > **Add to Home Screen**.
2. **Enable Screen Time**: Go to **Settings** > **Screen Time** > **Content & Privacy Restrictions** > **Content Restrictions** > **Web Content**.
3. **Limit websites**: Choose **Allowed Websites Only** and add your BrainRotGuard URL (e.g., `http://10.71.1.27:8080`). This blocks all other web browsing while keeping the PWA functional.

### Why not DNS blocking?

Previously, this guide recommended blocking `youtube.com` via DNS (AdGuard Home, Pi-hole, etc.) while allowing `youtube-nocookie.com` for the embedded player. **This no longer works** — YouTube's bot detection now blocks anonymous embedded playback with a "Sign in to confirm you're not a bot" wall. As of v1.x, BrainRotGuard uses standard `youtube.com` embeds, so the tablet needs a signed-in Google session. See [#36](https://github.com/GHJJ123/brainrotguard/issues/36) and [#38](https://github.com/GHJJ123/brainrotguard/issues/38) for details.

DNS blocking is still fine as an additional layer, but you must allow `www.youtube.com` through:
```
@@||www.youtube.com^
@@||googlevideo.com^
```

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
