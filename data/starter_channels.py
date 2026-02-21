"""Loader for the starter channels YAML file."""

import logging
import re
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_HANDLE_RE = re.compile(r"^@[\w.-]+$")
_VALID_CATEGORIES = {"edu", "fun"}


def load_starter_channels(path: Optional[Path] = None) -> list[dict]:
    """Load and validate starter channels from a YAML file.

    Returns a list of dicts with keys: handle, name, category, description.
    Returns [] if the file is missing or invalid.
    """
    if path is None or not path.exists():
        logger.debug(f"Starter channels file not found: {path}")
        return []

    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except Exception as e:
        logger.warning(f"Failed to load starter channels: {e}")
        return []

    if not isinstance(data, dict) or "channels" not in data:
        logger.warning("Starter channels file missing 'channels' key")
        return []

    result = []
    for entry in data["channels"]:
        if not isinstance(entry, dict):
            continue
        handle = entry.get("handle", "").strip()
        name = entry.get("name", "").strip()
        if not handle or not name:
            logger.warning(f"Skipping starter channel missing handle/name: {entry}")
            continue
        if not _HANDLE_RE.match(handle):
            logger.warning(f"Skipping invalid handle: {handle}")
            continue
        category = entry.get("category", "").strip().lower() or None
        if category and category not in _VALID_CATEGORIES:
            logger.warning(f"Skipping invalid category '{category}' for {handle}")
            category = None
        result.append({
            "handle": handle.lower(),
            "name": name,
            "category": category,
            "description": entry.get("description", "").strip(),
        })

    logger.info(f"Loaded {len(result)} starter channels")
    return result
