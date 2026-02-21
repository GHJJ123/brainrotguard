#!/usr/bin/env python3
"""BrainRotGuard - YouTube approval system for kids."""

import argparse
import asyncio
import logging
import signal
import os
from pathlib import Path

import uvicorn

from config import load_config, Config
from data.video_store import VideoStore
from bot.telegram_bot import BrainRotGuardBot
from web.app import app as fastapi_app, setup as web_setup, invalidate_channel_cache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("brainrotguard")


class BrainRotGuard:
    """Main orchestrator - runs FastAPI + Telegram bot."""

    def __init__(self, config: Config):
        self.config = config
        self.video_store = None
        self.bot = None
        self.running = False

    async def setup(self) -> None:
        """Initialize all components."""
        db_path = self.config.database.path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.video_store = VideoStore(db_path=db_path)
        logger.info("Database initialized")

        if self.config.telegram.bot_token and self.config.telegram.admin_chat_id:
            self.bot = BrainRotGuardBot(
                bot_token=self.config.telegram.bot_token,
                admin_chat_id=self.config.telegram.admin_chat_id,
                video_store=self.video_store,
                config=self.config,
                starter_channels_path=Path(__file__).parent / "starter-channels.yaml",
            )
            self.bot.on_channel_change = invalidate_channel_cache
            from web.app import _invalidate_catalog_cache
            self.bot.on_video_change = _invalidate_catalog_cache
            logger.info("Telegram bot initialized")

        # Wire up web app with video store and notification callbacks
        async def notify_callback(video: dict):
            if self.bot:
                await self.bot.notify_new_request(video)

        async def time_limit_cb(used_min: float, limit_min: int, category: str = ""):
            if self.bot:
                await self.bot.notify_time_limit_reached(used_min, limit_min, category)

        web_setup(
            self.video_store, notify_callback, self.config.youtube, self.config.web,
            wl_cfg=self.config.watch_limits,
            time_limit_cb=time_limit_cb,
        )
        logger.info("Web app initialized")

    async def run(self) -> None:
        """Start everything."""
        self.running = True
        await self.setup()

        # Start Telegram bot
        if self.bot:
            await self.bot.start()

        # Start FastAPI via uvicorn
        config = uvicorn.Config(
            fastapi_app,
            host=self.config.web.host,
            port=self.config.web.port,
            log_level="info",
        )
        server = uvicorn.Server(config)

        # Prune old log data on startup
        w_pruned, s_pruned = self.video_store.prune_old_data()
        if w_pruned or s_pruned:
            logger.info(f"Pruned {w_pruned} watch_log and {s_pruned} search_log entries")

        # Backfill missing @handles in background
        asyncio.create_task(self._backfill_handles())

        stats = self.video_store.get_stats()
        logger.info(
            f"BrainRotGuard started - {stats['approved']} approved videos, "
            f"{stats['pending']} pending"
        )

        try:
            await server.serve()
        except asyncio.CancelledError:
            logger.info("Server cancelled")

    async def _backfill_handles(self) -> None:
        """Resolve missing @handles for channels that have a channel_id."""
        missing = self.video_store.get_channels_missing_handles()
        if not missing:
            return
        logger.info(f"Backfilling @handles for {len(missing)} channels")
        from youtube.extractor import resolve_handle_from_channel_id
        for name, channel_id in missing:
            try:
                handle = await resolve_handle_from_channel_id(channel_id)
                if handle:
                    self.video_store.update_channel_handle(name, handle)
                    logger.info(f"Backfilled handle: {name} → {handle}")
            except Exception as e:
                logger.debug(f"Failed to resolve handle for {name}: {e}")

    async def stop(self) -> None:
        """Stop all components."""
        self.running = False
        if self.bot:
            await self.bot.stop()
        if self.video_store:
            self.video_store.close()
        logger.info("BrainRotGuard stopped")


async def main() -> None:
    parser = argparse.ArgumentParser(description="BrainRotGuard")
    parser.add_argument("-c", "--config", help="Path to config file", default=None)
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = load_config(args.config)
    app = BrainRotGuard(config)

    loop = asyncio.get_event_loop()

    def signal_handler():
        asyncio.create_task(app.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            signal.signal(sig, lambda s, f: signal_handler())

    try:
        await app.run()
    except KeyboardInterrupt:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
