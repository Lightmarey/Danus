"""Danus' local research console.

The console reads the shared SQLite research model and serves a one-page
browser view. It never parses fact Markdown for graph or overview requests.

    python -m danus.observability --project <dir> [--port 8099]

See ``app.py`` for the server + parsers.
"""

from __future__ import annotations

from .app import (
    CHANNELS,
    app,
    build_channel,
    build_channels,
    build_control,
    build_overview,
    main,
)

__all__ = [
    "app",
    "main",
    "CHANNELS",
    "build_overview",
    "build_control",
    "build_channels",
    "build_channel",
]
