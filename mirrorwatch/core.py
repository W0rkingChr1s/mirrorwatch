"""The observation run itself."""

from __future__ import annotations

import hashlib
import os
import time

from .detect import KIND_DIR, KIND_ERROR, KIND_FILE, KIND_MISSING, classify
from .events import CHANGED, GONE, NEW, Event
from .fetch import HttpClient
from .mirror import Mirror
from .notify import build_notifiers
from .sources import Target, build_source, dedupe
from .state import State
from .util import LOG, http_date_to_iso, human_size, now_iso


def _fingerprint(response) -> dict:
    return {
        "lm": http_date_to_iso(response.header("last-modified")),
        "etag": response.header("etag"),
        "len": response.size_hint,
    }


def _filename_from(response, rel_path: str) -> str:
    disposition = response.header("content-disposition") or ""
    if "filename=" in disposition:
        candidate = disposition.split("filename=", 1)[1]
        candidate = candidate.split(";")[0].strip().strip('"').strip("'")
        candidate = os.path.basename(candidate)
        if candidate:
            return candidate
    return os.path.basename(rel_path) or "download.bin"


class Runner:
    def __init__(self, config: dict, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run

        self.client = HttpClient(
            user_agent=config["user_agent"],
            timeout=config["timeout"],
            retries=config["retries"],
            max_bytes=int(config["max_download_mb"]) * 1024 * 1024,
        )
        mirror_cfg = config["mirror"]
        self.mirror = Mirror(
            root=mirror_cfg["dir"],
            archive_root=mirror_cfg.get("archive_dir"),
            keep_versions=mirror_cfg.get("keep_versions", True),
            enabled=mirror_cfg.get("enabled", True) and not dry_run,
        )
        self.state = State(config["state_file"]).load()
        self.notifiers = build_notifiers(config.get("notifiers"), dry_run=dry_run)
        self.delay = config["request_delay_ms"] / 1000.0

    # ------------------------------------------------------------------
    def run_once(self) -> dict:
        bootstrap = self.state.fresh
        if bootstrap:
            LOG.info("no previous state: establishing baseline")

        events: list[Event] = []
        checked = 0
        errors = 0

        for spec in self.config["sources"]:
            source = build_source(spec)
            LOG.info("[%s] type=%s", source.name, source.type)
            try:
                targets = source.targets(self.client)
            except Exception as exc:                        # noqa: BLE001
                LOG.error("[%s] target discovery failed: %s", source.name, exc)
                errors += 1
                continue

            entries = self.state.entries(source.name)
            # Keep watching directories discovered in earlier runs.
            known_dirs = {key for key, rec in entries.items()
                          if rec.get("kind") == KIND_DIR}
            seen_keys = {t.key for t in targets}
            for key in sorted(known_dirs - seen_keys):
                record = entries[key]
                targets.append(Target(key=key,
                                      url=record.get("url", key),
                                      rel_path=record.get("rel_path", key),
                                      hint=KIND_DIR))

            for target in dedupe(targets):
                checked += 1
                event, failed = self._check(source, target, entries)
                if failed:
                    errors += 1
                if event:
                    events.append(event)
                time.sleep(self.delay)

        summary = {"checked": checked, "events": len(events), "errors": errors,
                   "bootstrap": bootstrap}
        LOG.info("checked %s target(s), %s change(s), %s error(s)",
                 checked, len(events), errors)

        self._notify(events, summary, bootstrap)

        if not self.dry_run:
            self.state.record_run(summary)
            self.state.save()
        else:
            LOG.info("dry run: state not written")

        return summary

    # ------------------------------------------------------------------
    def _check(self, source, target, entries: dict):
        response = self.client.head(target.url, source.headers)
        kind = classify(response, source.detect_rules)
        previous = entries.get(target.key)
        was_known = bool(previous) and previous.get("kind") != KIND_MISSING

        if kind == KIND_ERROR:
            LOG.error("[%s] network error on %s: %s",
                      source.name, target.key, response.error)
            return None, True                # never report "gone" on a network fault

        if kind == KIND_MISSING:
            if was_known:
                LOG.info("[%s] GONE %s", source.name, target.key)
                entries[target.key] = {**previous, "kind": KIND_MISSING,
                                       "last_change": now_iso()}
                return Event(GONE, previous.get("kind", KIND_FILE), source.name,
                             target.key, target.url), False
            return None, False

        fingerprint = _fingerprint(response)

        if kind == KIND_DIR:
            return self._check_dir(source, target, entries, previous,
                                   was_known, fingerprint), False

        return self._check_file(source, target, entries, previous,
                                was_known, fingerprint)

    # ------------------------------------------------------------------
    def _check_dir(self, source, target, entries, previous, was_known, fingerprint):
        record = {"kind": KIND_DIR, "url": target.url, "rel_path": target.rel_path,
                  **fingerprint}
        if not was_known:
            entries[target.key] = {**record, "first_seen": now_iso(),
                                   "last_change": now_iso()}
            LOG.info("[%s] NEW dir %s (%s)", source.name, target.key,
                     fingerprint["lm"])
            return Event(NEW, KIND_DIR, source.name, target.key, target.url,
                         last_modified=fingerprint["lm"])

        changed = (previous.get("lm") != fingerprint["lm"]
                   or previous.get("etag") != fingerprint["etag"])
        entries[target.key] = {**record,
                               "first_seen": previous.get("first_seen"),
                               "last_change": now_iso() if changed
                               else previous.get("last_change")}
        if not changed:
            return None
        LOG.info("[%s] CHANGED dir %s (%s -> %s)", source.name, target.key,
                 previous.get("lm"), fingerprint["lm"])
        return Event(CHANGED, KIND_DIR, source.name, target.key, target.url,
                     last_modified=fingerprint["lm"],
                     previous_modified=previous.get("lm"))

    # ------------------------------------------------------------------
    def _check_file(self, source, target, entries, previous, was_known, fingerprint):
        unchanged_headers = (
            was_known
            and previous.get("lm") == fingerprint["lm"]
            and previous.get("etag") == fingerprint["etag"]
            and previous.get("len") == fingerprint["len"]
        )
        wants_download = source.spec.get("download", True)

        if unchanged_headers and (previous.get("sha256") or not wants_download) \
                and not source.always_download:
            entries[target.key] = {**previous, **fingerprint}
            return None, False

        if not wants_download:
            record = {"kind": KIND_FILE, "url": target.url,
                      "rel_path": target.rel_path, **fingerprint}
            if not was_known:
                entries[target.key] = {**record, "first_seen": now_iso(),
                                       "last_change": now_iso()}
                return Event(NEW, KIND_FILE, source.name, target.key, target.url,
                             last_modified=fingerprint["lm"],
                             size=fingerprint["len"],
                             filename=os.path.basename(target.rel_path)), False
            entries[target.key] = {**record,
                                   "first_seen": previous.get("first_seen"),
                                   "last_change": now_iso()}
            return Event(CHANGED, KIND_FILE, source.name, target.key, target.url,
                         last_modified=fingerprint["lm"],
                         previous_modified=previous.get("lm"),
                         size=fingerprint["len"],
                         filename=os.path.basename(target.rel_path)), False

        response = self.client.get(target.url, source.headers)
        if not response.ok or response.body is None:
            LOG.error("[%s] download failed for %s: %s", source.name,
                      target.key, response.error or f"HTTP {response.status}")
            return None, True

        data = response.body
        digest = hashlib.sha256(data).hexdigest()
        filename = _filename_from(response, target.rel_path)

        if was_known and previous.get("sha256") == digest:
            LOG.info("[%s] %s: headers moved but content is identical, staying quiet",
                     source.name, target.key)
            entries[target.key] = {**previous, **fingerprint}
            return None, False

        local_path = None
        if source.mirror:
            try:
                local_path = self.mirror.write(source.name, target.rel_path,
                                               data, fingerprint["lm"])
            except (OSError, ValueError) as exc:
                LOG.error("[%s] mirror write failed for %s: %s",
                          source.name, target.key, exc)
                return None, True

        entries[target.key] = {
            "kind": KIND_FILE, "url": target.url, "rel_path": target.rel_path,
            "sha256": digest, "filename": filename, **fingerprint,
            "first_seen": (previous or {}).get("first_seen", now_iso()),
            "last_change": now_iso(),
        }

        event_type = CHANGED if was_known else NEW
        LOG.info("[%s] %s file %s (%s, sha=%s)", source.name, event_type.upper(),
                 target.key, human_size(len(data)), digest[:12])

        return Event(event_type, KIND_FILE, source.name, target.key, target.url,
                     last_modified=fingerprint["lm"],
                     previous_modified=(previous or {}).get("lm"),
                     size=len(data), sha256=digest, filename=filename,
                     local_path=local_path, payload=data), False

    # ------------------------------------------------------------------
    def _notify(self, events: list[Event], summary: dict, bootstrap: bool) -> None:
        mode = self.config["bootstrap_notify"]

        if bootstrap and mode == "none":
            LOG.info("bootstrap: notifications suppressed")
            return

        if bootstrap and mode == "summary":
            files = sum(1 for e in events if e.kind == KIND_FILE)
            dirs = sum(1 for e in events if e.kind == KIND_DIR)
            text = (f"\U0001f7e2 <b>mirrorwatch: Baseline erstellt</b>\n"
                    f"{summary['checked']} Ziel(e) geprüft\n"
                    f"{files} Datei(en) gespiegelt, {dirs} Verzeichnis(se) verfolgt\n"
                    f"<i>Ab jetzt bekommst du nur noch echte Änderungen "
                    f"gemeldet.</i>")
            for notifier in self.notifiers.values():
                notifier.send_summary(text, summary)
            return

        if not events:
            return

        for name, notifier in self.notifiers.items():
            selected = [e for e in events
                        if self._routes_to(e, name)]
            if selected:
                notifier.send(selected, summary)

    def _routes_to(self, event: Event, notifier_name: str) -> bool:
        for spec in self.config["sources"]:
            if spec.get("name") == event.source:
                wanted = spec.get("notify")
                return wanted is None or notifier_name in wanted
        return True
