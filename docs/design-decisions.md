# Design Decisions

| Decision | Why |
|----------|-----|
| **yt-dlp instead of YouTube API** | No API key signup, no quotas, no billing. yt-dlp is a well-maintained open source tool that extracts video info directly. |
| **Telegram instead of a custom app** | You already have Telegram on your phone. Inline buttons make approve/deny a one-tap action. No separate parent app to install. |
| **SQLite instead of a database server** | Zero setup. The entire database is one file. No Postgres, no Redis, nothing else to install or maintain. |
| **youtube-nocookie.com embeds** | Google's privacy-enhanced embed domain. No cookies, no tracking, no recommendations sidebar. The video just plays. |
| **Server-side rendering (not a SPA)** | Simpler to run, simpler to debug. Works on any browser, including older tablets. No JavaScript framework to maintain. |
| **DNS blocking for enforcement** | The app itself can't prevent a kid from opening youtube.com directly. Blocking YouTube at the DNS level (your router or DNS server) closes that gap completely. |
