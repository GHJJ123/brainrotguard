from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional, Protocol, runtime_checkable
from urllib.parse import urlparse
import yt_dlp

logger = logging.getLogger(__name__)

# Allowlisted YouTube thumbnail CDN hostnames (single source of truth)
THUMB_ALLOWED_HOSTS = frozenset({
    "i.ytimg.com", "i1.ytimg.com", "i2.ytimg.com", "i3.ytimg.com",
    "i4.ytimg.com", "i9.ytimg.com", "img.youtube.com",
})

# Regex to extract video ID from various YouTube URL formats
YOUTUBE_URL_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})'
)

_VIDEO_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{11}$')

_SHORTS_PATH_RE = re.compile(r'/shorts/')


def _is_short_url(url: Optional[str]) -> bool:
    """Check if a YouTube URL indicates a Short (contains /shorts/ in path)."""
    return bool(url and _SHORTS_PATH_RE.search(url))


def _safe_thumbnail(url: Optional[str], video_id: str) -> str:
    """Return the thumbnail URL if it's from an allowlisted host, else use ytimg fallback."""
    if url:
        try:
            parsed = urlparse(url)
            if parsed.scheme == "https" and parsed.hostname in THUMB_ALLOWED_HOSTS:
                return url
        except Exception:
            pass
    if video_id and _VIDEO_ID_RE.match(video_id):
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return ""


def extract_video_id(url_or_id: str) -> Optional[str]:
    """Extract YouTube video ID from URL or return as-is if already an ID."""
    url_or_id = url_or_id.strip()
    match = YOUTUBE_URL_PATTERN.search(url_or_id)
    if match:
        return match.group(1)
    # Check if it's already a valid video ID (11 chars, alphanumeric + _ -)
    if re.match(r'^[a-zA-Z0-9_-]{11}$', url_or_id):
        return url_or_id
    return None

_YDL_TIMEOUT = 30  # default; overridden by configure_timeout()


def configure_timeout(seconds: int):
    """Set yt-dlp timeout from config."""
    global _YDL_TIMEOUT
    _YDL_TIMEOUT = seconds


def _ydl_opts() -> dict:
    """Common yt-dlp options - no download, just metadata."""
    return {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'skip_download': True,
        'ignore_no_formats_error': True,
        'socket_timeout': _YDL_TIMEOUT,
    }


async def extract_metadata(video_id: str) -> Optional[dict]:
    """Extract metadata for a single YouTube video."""
    def _extract():
        try:
            with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                if not info:
                    return None
                return {
                    'video_id': video_id,
                    'title': info.get('title', 'Unknown'),
                    'channel_name': info.get('channel', info.get('uploader', 'Unknown')),
                    'channel_id': info.get('channel_id'),
                    'thumbnail_url': _safe_thumbnail(info.get('thumbnail'), video_id),
                    'duration': info.get('duration'),
                    'view_count': info.get('view_count'),
                    'is_short': _is_short_url(info.get('webpage_url')),
                }
        except Exception as e:
            logger.error(f"Failed to extract metadata for {video_id}: {e}")
            return None
    try:
        return await asyncio.wait_for(asyncio.to_thread(_extract), timeout=_YDL_TIMEOUT)
    except asyncio.TimeoutError:
        logger.error(f"Metadata extraction timed out for {video_id}")
        return None

async def search(query: str, max_results: int = 10) -> list[dict]:
    """Search YouTube via yt-dlp ytsearch."""
    def _search():
        try:
            opts = _ydl_opts()
            opts['extract_flat'] = True
            with yt_dlp.YoutubeDL(opts) as ydl:
                results = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
                if not results or 'entries' not in results:
                    return []
                videos = []
                for entry in results['entries']:
                    if not entry:
                        continue
                    vid_id = entry.get('id')
                    if not vid_id or not _VIDEO_ID_RE.match(vid_id):
                        continue  # skip channels/playlists mixed into search results
                    videos.append({
                        'video_id': vid_id,
                        'title': entry.get('title', 'Unknown'),
                        'channel_name': entry.get('channel', entry.get('uploader', 'Unknown')),
                        'thumbnail_url': _safe_thumbnail(entry.get('thumbnail'), vid_id),
                        'duration': entry.get('duration'),
                        'view_count': entry.get('view_count'),
                        'is_short': _is_short_url(entry.get('url')),
                    })
                return videos
        except Exception as e:
            logger.error(f"Search failed for '{query}': {e}")
            return []
    try:
        return await asyncio.wait_for(asyncio.to_thread(_search), timeout=_YDL_TIMEOUT)
    except asyncio.TimeoutError:
        logger.error(f"Search timed out for '{query}'")
        return []

async def resolve_channel_handle(handle: str) -> Optional[dict]:
    """Resolve a @handle to channel name, ID, and handle. Returns dict or None."""
    clean = handle.lstrip("@")
    url = f"https://www.youtube.com/@{clean}"
    def _resolve():
        try:
            opts = _ydl_opts()
            opts['extract_flat'] = True
            opts['playlistend'] = 1
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None
                return {
                    'channel_name': info.get('channel', info.get('uploader', clean)),
                    'channel_id': info.get('channel_id') or info.get('id'),
                    'handle': f"@{clean}",
                }
        except Exception as e:
            logger.debug(f"Handle resolve failed for '@{clean}': {e}")
            return None
    try:
        return await asyncio.wait_for(asyncio.to_thread(_resolve), timeout=_YDL_TIMEOUT)
    except asyncio.TimeoutError:
        logger.error(f"Handle resolve timed out for '@{clean}'")
        return None


async def resolve_handle_from_channel_id(channel_id: str) -> Optional[str]:
    """Resolve a channel_id to its @handle. Returns '@handle' string or None."""
    def _resolve():
        try:
            opts = _ydl_opts()
            opts['extract_flat'] = True
            opts['playlistend'] = 1
            url = f"https://www.youtube.com/channel/{channel_id}/videos"
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None
                uploader_id = info.get('uploader_id', '')
                if uploader_id and uploader_id.startswith('@'):
                    return uploader_id
                channel_url = info.get('channel_url', '') or info.get('uploader_url', '')
                if '/@' in channel_url:
                    return '@' + channel_url.split('/@', 1)[1].split('/')[0]
                return None
        except Exception as e:
            logger.debug(f"Handle resolve failed for channel {channel_id}: {e}")
            return None
    try:
        return await asyncio.wait_for(asyncio.to_thread(_resolve), timeout=_YDL_TIMEOUT)
    except asyncio.TimeoutError:
        logger.error(f"Handle resolve timed out for channel {channel_id}")
        return None


def _resolve_channel_id(channel_name: str) -> Optional[str]:
    """Resolve a channel display name to a YouTube channel ID via search."""
    from urllib.parse import quote
    try:
        opts = _ydl_opts()
        opts['extract_flat'] = 'in_playlist'
        opts['playlistend'] = 5
        url = f"https://www.youtube.com/results?search_query={quote(channel_name)}&sp=EgIQAg%3D%3D"
        with yt_dlp.YoutubeDL(opts) as ydl:
            results = ydl.extract_info(url, download=False)
            for entry in (results or {}).get('entries', []):
                if not entry:
                    continue
                entry_name = entry.get('channel', entry.get('title', ''))
                if entry_name.lower() == channel_name.lower():
                    return entry.get('id') or entry.get('channel_id')
    except Exception as e:
        logger.debug(f"Channel ID resolve failed for '{channel_name}': {e}")
    return None


def _fetch_from_channel_page(channel_id: str, channel_name: str, max_results: int) -> list[dict]:
    """Fetch videos directly from a channel's uploads tab."""
    try:
        opts = _ydl_opts()
        opts['extract_flat'] = True
        opts['playlistend'] = max_results
        url = f"https://www.youtube.com/channel/{channel_id}/videos"
        with yt_dlp.YoutubeDL(opts) as ydl:
            results = ydl.extract_info(url, download=False)
            if not results or 'entries' not in results:
                return []
            # Channel name from playlist metadata (entries lack it in flat mode)
            resolved_name = results.get('channel', results.get('uploader', channel_name))
            videos = []
            for entry in results['entries']:
                if not entry:
                    continue
                vid_id = entry.get('id')
                if not vid_id or not _VIDEO_ID_RE.match(vid_id):
                    continue
                videos.append({
                    'video_id': vid_id,
                    'title': entry.get('title', 'Unknown'),
                    'channel_name': resolved_name,
                    'thumbnail_url': _safe_thumbnail(entry.get('thumbnail'), vid_id),
                    'duration': entry.get('duration'),
                    'timestamp': entry.get('timestamp'),
                    'view_count': entry.get('view_count'),
                    'is_short': _is_short_url(entry.get('url')),
                })
            return videos
    except Exception as e:
        logger.debug(f"Channel page fetch failed for '{channel_id}': {e}")
        return []


async def fetch_channel_videos(channel_name: str, max_results: int = 10, channel_id: Optional[str] = None) -> list[dict]:
    """Fetch recent videos from a YouTube channel.

    If channel_id is provided, fetches directly from the uploads tab.
    Otherwise resolves via search first. Falls back to ytsearch with name filtering.
    """
    def _fetch():
        # Try direct channel page approach first
        cid = channel_id or _resolve_channel_id(channel_name)
        if cid:
            videos = _fetch_from_channel_page(cid, channel_name, max_results)
            if videos:
                return videos

        # Fallback: search and filter by exact channel name
        try:
            fetch_count = max_results * 3
            opts = _ydl_opts()
            opts['extract_flat'] = True
            with yt_dlp.YoutubeDL(opts) as ydl:
                results = ydl.extract_info(f"ytsearch{fetch_count}:{channel_name}", download=False)
                if not results or 'entries' not in results:
                    return []
                videos = []
                for entry in results['entries']:
                    if not entry:
                        continue
                    vid_id = entry.get('id')
                    if not vid_id:
                        continue
                    entry_channel = entry.get('channel', entry.get('uploader', ''))
                    if entry_channel.lower() != channel_name.lower():
                        continue
                    videos.append({
                        'video_id': vid_id,
                        'title': entry.get('title', 'Unknown'),
                        'channel_name': entry_channel,
                        'thumbnail_url': _safe_thumbnail(entry.get('thumbnail'), vid_id),
                        'duration': entry.get('duration'),
                        'timestamp': entry.get('timestamp'),
                        })
                    if len(videos) >= max_results:
                        break
                return videos
        except Exception as e:
            logger.error(f"Channel fetch failed for '{channel_name}': {e}")
            return []
    try:
        return await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=_YDL_TIMEOUT * 2)
    except asyncio.TimeoutError:
        logger.error(f"Channel fetch timed out for '{channel_name}'")
        return []


def _fetch_from_channel_shorts(channel_id: str, channel_name: str, max_results: int) -> list[dict]:
    """Fetch Shorts directly from a channel's /shorts tab."""
    try:
        opts = _ydl_opts()
        opts['extract_flat'] = True
        opts['playlistend'] = max_results
        url = f"https://www.youtube.com/channel/{channel_id}/shorts"
        with yt_dlp.YoutubeDL(opts) as ydl:
            results = ydl.extract_info(url, download=False)
            if not results or 'entries' not in results:
                return []
            resolved_name = results.get('channel', results.get('uploader', channel_name))
            videos = []
            for entry in results['entries']:
                if not entry:
                    continue
                vid_id = entry.get('id')
                if not vid_id or not _VIDEO_ID_RE.match(vid_id):
                    continue
                videos.append({
                    'video_id': vid_id,
                    'title': entry.get('title', 'Unknown'),
                    'channel_name': resolved_name,
                    'thumbnail_url': _safe_thumbnail(entry.get('thumbnail'), vid_id),
                    'duration': entry.get('duration'),
                    'timestamp': entry.get('timestamp'),
                    'view_count': entry.get('view_count'),
                    'is_short': True,
                })
            return videos
    except Exception as e:
        logger.debug(f"Channel shorts fetch failed for '{channel_id}': {e}")
        return []


async def fetch_channel_shorts(channel_name: str, max_results: int = 50, channel_id: Optional[str] = None) -> list[dict]:
    """Fetch recent Shorts from a YouTube channel's /shorts tab."""
    if not channel_id:
        return []
    def _fetch():
        return _fetch_from_channel_shorts(channel_id, channel_name, max_results)
    try:
        return await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=_YDL_TIMEOUT * 2)
    except asyncio.TimeoutError:
        logger.error(f"Channel shorts fetch timed out for '{channel_name}'")
        return []


def format_duration(seconds) -> str:
    """Format seconds into human readable duration like '5:23' or '1:02:15'."""
    if not seconds:
        return "?"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# ---------------------------------------------------------------------------
# Class wrapper + Protocol for dependency injection / mocking
# ---------------------------------------------------------------------------

@runtime_checkable
class YouTubeExtractorProtocol(Protocol):
    """Protocol for YouTube metadata extraction — use for type hints and test mocks."""

    async def extract_metadata(self, video_id: str) -> Optional[dict]: ...
    async def search(self, query: str, max_results: int = 10) -> list[dict]: ...
    async def fetch_channel_videos(self, channel_name: str, max_results: int = 10,
                                    channel_id: Optional[str] = None) -> list[dict]: ...
    async def fetch_channel_shorts(self, channel_name: str, max_results: int = 50,
                                    channel_id: Optional[str] = None) -> list[dict]: ...
    async def resolve_channel_handle(self, handle: str) -> Optional[dict]: ...
    async def resolve_handle_from_channel_id(self, channel_id: str) -> Optional[str]: ...


class YouTubeExtractor:
    """Concrete implementation wrapping yt-dlp — satisfies YouTubeExtractorProtocol.

    Thin namespace wrapper that delegates to the module-level functions.
    Timeout is configured globally via configure_timeout().
    """

    async def extract_metadata(self, video_id: str) -> Optional[dict]:
        return await extract_metadata(video_id)

    async def search(self, query: str, max_results: int = 10) -> list[dict]:
        return await search(query, max_results=max_results)

    async def fetch_channel_videos(self, channel_name: str, max_results: int = 10,
                                    channel_id: Optional[str] = None) -> list[dict]:
        return await fetch_channel_videos(channel_name, max_results=max_results,
                                           channel_id=channel_id)

    async def fetch_channel_shorts(self, channel_name: str, max_results: int = 50,
                                    channel_id: Optional[str] = None) -> list[dict]:
        return await fetch_channel_shorts(channel_name, max_results=max_results,
                                           channel_id=channel_id)

    async def resolve_channel_handle(self, handle: str) -> Optional[dict]:
        return await resolve_channel_handle(handle)

    async def resolve_handle_from_channel_id(self, channel_id: str) -> Optional[str]:
        return await resolve_handle_from_channel_id(channel_id)
