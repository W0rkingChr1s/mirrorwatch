"""Small shared helpers: logging, time, sizes, secrets, path safety.

Everything here is standard library only, in keeping with mirrorwatch's
zero-dependency promise.
"""

from __future__ import annotations

import html as _html
import logging
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

LOG = logging.getLogger("mirrorwatch")


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger once, honouring the requested level."""
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    LOG.setLevel(numeric)


def now_iso() -> str:
    """Current time as a timezone-aware ISO 8601 string in UTC."""
    return datetime.now(timezone.utc).isoformat()


def http_date_to_iso(value: str | None) -> str | None:
    """Convert an HTTP-date (RFC 7231) to ISO 8601 UTC, or None."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def human_size(num_bytes: int | None) -> str:
    """A short, human-friendly rendering of a byte count."""
    if num_bytes is None:
        return "unknown size"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def html_escape(value) -> str:
    """Escape text for Telegram's HTML parse mode."""
    return _html.escape("" if value is None else str(value), quote=True)


def resolve_secret(value):
    """Resolve ``env:NAME`` and ``file:/path`` indirections.

    Anything else is returned unchanged, so plain values still work. Secrets
    are stripped of surrounding whitespace, which is almost always a trailing
    newline in a mounted file.
    """
    if not isinstance(value, str):
        return value
    if value.startswith("env:"):
        return (os.environ.get(value[4:], "") or "").strip()
    if value.startswith("file:"):
        try:
            with open(value[5:], "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError as exc:
            LOG.error("cannot read secret from %s: %s", value[5:], exc)
            return ""
    return value


def safe_relpath(path: str) -> str:
    """Turn an arbitrary URL path into a relative filesystem path that can
    never escape its root.

    Query strings and fragments are dropped, and ``.`` / ``..`` segments are
    removed outright rather than resolved, so no combination of input can point
    outside the mirror directory.
    """
    cleaned = (path or "").split("?", 1)[0].split("#", 1)[0]
    parts = [
        segment
        for segment in cleaned.replace("\\", "/").split("/")
        if segment not in ("", ".", "..")
    ]
    return os.path.join(*parts) if parts else ""
