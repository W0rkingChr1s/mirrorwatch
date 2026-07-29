"""The persistent state file.

One JSON document records what mirrorwatch has seen, so a run can tell a change
from a repeat. Its shape::

    {
      "version": 1,
      "last_run": "2026-01-01T00:00:00+00:00",
      "last_summary": {"checked": 12, "events": 1, "errors": 0},
      "sources": {
        "<source name>": {
          "<target key>": { "kind": "file", "sha256": "...", ... }
        }
      }
    }

Writes are atomic: a temporary file is renamed into place, so an interrupted
run never corrupts the state that earlier runs depend on.
"""

from __future__ import annotations

import json
import os

from .util import LOG, now_iso


class State:
    def __init__(self, path: str):
        self.path = path
        self.data: dict = {"version": 1, "last_run": None, "sources": {}}
        self.fresh = True

    # ------------------------------------------------------------------
    def load(self) -> "State":
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                LOG.error("could not read state %s: %s; starting from scratch",
                          self.path, exc)
                return self
            if isinstance(loaded, dict):
                self.data.update(loaded)
                self.data.setdefault("sources", {})
                self.fresh = not self.data.get("sources")
        return self

    # ------------------------------------------------------------------
    def entries(self, source_name: str) -> dict:
        """The mutable record dict for a source, created on first access."""
        return self.data["sources"].setdefault(source_name, {})

    def record_run(self, summary: dict) -> None:
        self.data["last_run"] = now_iso()
        self.data["last_summary"] = summary

    def save(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)
