# Configuration Reference

All configuration lives in two files:

**`.env`** — your secrets and server settings (never share this file):
```
BRG_BOT_TOKEN=123456789:ABCdefGhIjKlMnOpQrStUvWxYz
BRG_ADMIN_CHAT_ID=987654321
BRG_PIN=1234
BRG_BASE_URL=http://192.168.1.100:8080
```

**`config.yaml`** — app behavior (references `.env` variables via `${VAR}` syntax):
```yaml
app:
  locale: en             # UI/bot language: en or nb
  time_format: locale    # locale default, or force 12h / 24h time display

web:
  host: 0.0.0.0         # listen on all network interfaces
  port: 8080             # web UI port
  poll_interval: 3000    # how often pending page checks for updates (ms)
  pin: ${BRG_PIN}        # optional — remove this line to disable PIN

telegram:
  bot_token: ${BRG_BOT_TOKEN}
  admin_chat_id: ${BRG_ADMIN_CHAT_ID}

youtube:
  search_max_results: 50         # max results per search
  channel_cache_results: 200     # videos to cache per allowed channel
  channel_cache_ttl: 1800        # seconds between channel refreshes (default 30 min)
  ydl_timeout: 30                # seconds — max time for a single yt-dlp operation

database:
  path: db/videos.db

watch_limits:
  daily_limit_minutes: 120       # 0 = unlimited (global fallback when no category limits set)
  timezone: America/New_York     # your local timezone
  notify_on_limit: true          # notify parent when limit is hit
```

### Language and Time Format

Language and time display are configured under the `app:` section:

- `locale` sets the language used by both the web UI and Telegram bot
- `time_format` controls how times are rendered in schedules, status messages, and other time-related labels

Supported canonical locales:

- `en` — English
- `nb` — Norwegian Bokmal

`locale` is normalized on load, so common variants such as `en-US`, `en_GB`, `nb-NO`, and `no` resolve to the supported internal locale automatically.

Supported time format values:

- `locale` — use the locale default
- `12h` — force 12-hour time
- `24h` — force 24-hour time

When `time_format` is set to `locale`, English defaults to 12-hour time and Norwegian defaults to 24-hour time.

For contributors adding another language, see the locale guide in [`i18n/locales/README.md`](../i18n/locales/README.md).

### Category Time Limits

Category limits are managed via Telegram commands, not config files. They're stored in the SQLite database:

- `/time edu 120` — 120 minutes/day for educational content
- `/time fun 60` — 60 minutes/day for entertainment content
- `/time edu off` — unlimited educational content
- `/time fun off` — unlimited entertainment content

When category limits are set, they replace the global `daily_limit_minutes`. When neither category limit is set, the global limit applies as a fallback.

Channels are tagged when allowlisted (`/channel allow @handle edu`) or recategorized later (`/channel cat <name> edu`). Individual videos are tagged during approval (Approve Edu / Approve Fun buttons) or toggled after approval.

If you skip `config.yaml` entirely, everything falls back to environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `BRG_BOT_TOKEN` | Telegram bot token | *required* |
| `BRG_ADMIN_CHAT_ID` | Parent's Telegram chat ID | *required* |
| `BRG_WEB_HOST` | Web server bind address | `0.0.0.0` |
| `BRG_WEB_PORT` | Web server port | `8080` |
| `BRG_PIN` | Web UI access PIN (empty = no auth) | — |
| `BRG_POLL_INTERVAL` | Pending page poll interval (ms) | `3000` |
| `BRG_LOCALE` | UI/bot language (`en` or `nb`) | `en` |
| `BRG_TIME_FORMAT` | Time display format (`locale`, `12h`, `24h`) | `locale` |
| `BRG_YOUTUBE_MAX_RESULTS` | Max search results | `10` |
| `BRG_BASE_URL` | LAN URL for Telegram links (e.g. `http://192.168.1.100:8080`) | — |
| `BRG_DB_PATH` | SQLite database path | `db/videos.db` |
