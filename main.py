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
from data.child_store import ChildStore
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

    def _bootstrap_profiles(self) -> None:
        """Ensure at least one profile exists. Auto-creates 'default' on first run."""
        profiles = self.video_store.get_profiles()
        if profiles:
            return  # Profiles already exist

        pin = self.config.web.pin if self.config.web else ""
        self.video_store.create_profile("default", "Default", pin=pin)
        logger.info("Created default profile (PIN: %s)", "set" if pin else "none")

    async def setup(self) -> None:
        """Initialize all components."""
        db_path = self.config.database.path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.video_store = VideoStore(db_path=db_path)
        logger.info("Database initialized")

        # Bootstrap default profile from config on first run
        self._bootstrap_profiles()

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
        async def notify_callback(video: dict, profile_id: str = "default"):
            if self.bot:
                await self.bot.notify_new_request(video, profile_id=profile_id)

        async def time_limit_cb(used_min: float, limit_min: int,
                                category: str = "", profile_id: str = "default"):
            if self.bot:
                await self.bot.notify_time_limit_reached(
                    used_min, limit_min, category, profile_id=profile_id)

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

        # Periodic backfill of missing channel_id / handle on channels + videos
        asyncio.create_task(self._backfill_loop())

        stats = self.video_store.get_stats()
        logger.info(
            f"BrainRotGuard started - {stats['approved']} approved videos, "
            f"{stats['pending']} pending"
        )

        try:
            await server.serve()
        except asyncio.CancelledError:
            logger.info("Server cancelled")

    async def _backfill_loop(self) -> None:
        """Periodically backfill missing channel_id and handle on channels + videos."""
        _INTERVAL = 3600  # re-check every hour
        while self.running:
            try:
                await self._backfill_identifiers()
            except Exception as e:
                logger.error(f"Backfill error: {e}")
            await asyncio.sleep(_INTERVAL)

    async def _backfill_identifiers(self) -> None:
        """One-shot backfill of all missing unique identifiers across all profiles."""
        from youtube.extractor import (
            resolve_channel_handle,
            resolve_handle_from_channel_id,
            extract_metadata,
        )

        profiles = self.video_store.get_profiles()
        if not profiles:
            profiles = [{"id": "default"}]

        for profile in profiles:
            pid = profile["id"]
            cs = ChildStore(self.video_store, pid)

            # 1) Channels missing channel_id
            missing_cid = cs.get_channels_missing_ids()
            if missing_cid:
                logger.info(f"Backfilling channel_id for {len(missing_cid)} channels (profile={pid})")
            for name, handle in missing_cid:
                try:
                    lookup = handle or f"@{name}"
                    info = await resolve_channel_handle(lookup)
                    if info and info.get("channel_id"):
                        cs.update_channel_id(name, info["channel_id"])
                        if info.get("handle") and not handle:
                            cs.update_channel_handle(name, info["handle"])
                        logger.info(f"Backfilled channel_id: {name} → {info['channel_id']}")
                except Exception as e:
                    logger.debug(f"Failed to backfill channel_id for {name}: {e}")

            # 2) Channels missing @handle (have channel_id)
            missing_handles = cs.get_channels_missing_handles()
            if missing_handles:
                logger.info(f"Backfilling @handles for {len(missing_handles)} channels (profile={pid})")
            for name, channel_id in missing_handles:
                try:
                    handle = await resolve_handle_from_channel_id(channel_id)
                    if handle:
                        cs.update_channel_handle(name, handle)
                        logger.info(f"Backfilled handle: {name} → {handle}")
                except Exception as e:
                    logger.debug(f"Failed to resolve handle for {name}: {e}")

            # 3) Videos missing channel_id
            missing_vid_cid = cs.get_videos_missing_channel_id()
            if missing_vid_cid:
                logger.info(f"Backfilling channel_id for {len(missing_vid_cid)} videos (profile={pid})")
            for v in missing_vid_cid:
                try:
                    metadata = await extract_metadata(v["video_id"])
                    if metadata and metadata.get("channel_id"):
                        cs.update_video_channel_id(v["video_id"], metadata["channel_id"])
                        logger.info(
                            f"Backfilled video channel_id: {v['video_id']} → {metadata['channel_id']}"
                        )
                except Exception as e:
                    logger.debug(f"Failed to backfill channel_id for video {v['video_id']}: {e}")

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
