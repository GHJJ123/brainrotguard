# Troubleshooting

**The bot doesn't respond to commands**
- Make sure `BRG_BOT_TOKEN` is correct. You can verify by opening `https://api.telegram.org/botYOUR_TOKEN/getMe` in a browser — it should return your bot's info.
- Make sure `BRG_ADMIN_CHAT_ID` matches your account. The bot only responds to the configured admin.

**Videos won't play on the tablet**
- If you set up DNS blocking, make sure `www.youtube-nocookie.com` and `*.googlevideo.com` are allowlisted. These domains serve the actual video content.
- Check the browser console for errors — some ad blockers interfere with embedded video playback.

**"Connection refused" when opening the web UI**
- Confirm the container is running: `docker compose ps`
- Check logs for errors: `docker compose logs`
- Make sure port 8080 isn't blocked by a firewall on the host machine.

**Search returns no results**
- yt-dlp may need updating. Rebuild the container: `docker compose up -d --build`
- YouTube occasionally changes their site, which temporarily breaks yt-dlp until they release an update.

**The kid's tablet can still reach youtube.com**
- DNS changes can take a few minutes to propagate. Try clearing the tablet's DNS cache or restarting its WiFi.
- Make sure the DNS blocker is applied to the right device/network.
