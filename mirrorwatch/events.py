"""The change events mirrorwatch emits, and their vocabulary.

An :class:`Event` is what a run produces and a notifier consumes. It is a plain
data record: the runner fills it in, the notifiers render it. ``payload`` (the
downloaded bytes) is deliberately kept out of :meth:`Event.to_dict` so the JSON
a webhook receives stays small and serialisable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

NEW = "new"
CHANGED = "changed"
GONE = "gone"


@dataclass
class Event:
    type: str                       # NEW, CHANGED or GONE
    kind: str                       # "file" or "dir"
    source: str                     # name of the source that produced it
    path: str                       # stable key / path within the source
    url: str                        # absolute URL the change was seen at
    last_modified: str | None = None
    previous_modified: str | None = None
    size: int | None = None
    sha256: str | None = None
    filename: str | None = None
    local_path: str | None = None
    # Directory events only: how many names were tried inside, and how many
    # of them turned out to exist. None means no probing ran.
    probed: int | None = None
    found: int | None = None
    payload: bytes | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        """A JSON-serialisable view. The raw ``payload`` is intentionally omitted."""
        return {
            "type": self.type,
            "kind": self.kind,
            "source": self.source,
            "path": self.path,
            "url": self.url,
            "last_modified": self.last_modified,
            "previous_modified": self.previous_modified,
            "size": self.size,
            "sha256": self.sha256,
            "filename": self.filename,
            "local_path": self.local_path,
            "probed": self.probed,
            "found": self.found,
        }
