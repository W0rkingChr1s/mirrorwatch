"""Writing mirrored files, and archiving the version they replace.

The layout is ``<mirror>/<source>/<path>``. When a file's content changes and
``keep_versions`` is on, the copy currently on disk is moved into
``<archive>/<source>/<path>.<stamp>`` before the new bytes are written, so every
version stays recoverable. Writes go through a temporary file and an atomic
rename, so a crash never leaves a half-written mirror.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone

from .util import LOG, safe_relpath


class Mirror:
    def __init__(self, root: str, archive_root: str | None = None,
                 keep_versions: bool = True, enabled: bool = True):
        self.root = root
        self.archive_root = archive_root
        self.keep_versions = keep_versions
        self.enabled = enabled

    # ------------------------------------------------------------------
    def write(self, source: str, rel_path: str, data: bytes,
              last_modified: str | None = None) -> str | None:
        """Store ``data`` for ``source``/``rel_path``; return the path written.

        Returns None when mirroring is disabled (including dry runs).
        """
        if not self.enabled:
            return None

        rel = safe_relpath(rel_path)
        if not rel:
            raise ValueError(f"refusing to mirror empty path from {rel_path!r}")

        dest = os.path.join(self.root, safe_relpath(source), rel)

        if os.path.exists(dest) and self.keep_versions and self.archive_root:
            self._archive(source, rel, dest)

        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        tmp = f"{dest}.part"
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, dest)
        self._set_mtime(dest, last_modified)
        return dest

    # ------------------------------------------------------------------
    def _archive(self, source: str, rel: str, dest: str) -> None:
        stamp = self._stamp(dest)
        target = os.path.join(self.archive_root, safe_relpath(source),
                              f"{rel}.{stamp}")
        # Guard against two versions sharing a Last-Modified second.
        counter = 1
        base = target
        while os.path.exists(target):
            target = f"{base}.{counter}"
            counter += 1
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        try:
            shutil.copy2(dest, target)
            LOG.info("archived previous version to %s", target)
        except OSError as exc:
            LOG.error("could not archive %s: %s", dest, exc)

    @staticmethod
    def _stamp(dest: str) -> str:
        try:
            mtime = os.path.getmtime(dest)
        except OSError:
            mtime = 0
        return datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    @staticmethod
    def _set_mtime(dest: str, last_modified: str | None) -> None:
        """Stamp the mirror copy with the server's Last-Modified when we have it,
        so an archived version later carries a meaningful timestamp.

        ``last_modified`` is the ISO 8601 string mirrorwatch stores in state.
        """
        if not last_modified:
            return
        try:
            parsed = datetime.fromisoformat(last_modified)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            epoch = parsed.timestamp()
            os.utime(dest, (epoch, epoch))
        except (TypeError, ValueError, OverflowError, OSError):
            pass
