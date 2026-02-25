"""Shared web infrastructure: Jinja2 templates + slowapi rate limiter.

Neutral module with no imports from web.* — safe for all web modules to import.
"""

from fastapi.templating import Jinja2Templates
from pathlib import Path
from slowapi import Limiter
from slowapi.util import get_remote_address

from version import __version__
from youtube.extractor import format_duration

templates_dir = Path(__file__).parent / "templates"
static_dir = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(templates_dir))
limiter = Limiter(key_func=get_remote_address)

# Template globals
templates.env.globals["format_duration"] = format_duration
templates.env.globals["app_version"] = __version__


def register_filters():
    """Register custom Jinja2 filters. Called once after helpers are importable."""
    from web.helpers import format_views
    templates.env.filters["format_views"] = format_views
