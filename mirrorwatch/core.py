"""The observation run itself."""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field

from .detect import KIND_DIR, KIND_ERROR, KIND_FILE, KIND_MISSING, classify
from .discover import Budget, candidates, leaf
from .events import CHANGED, GONE, NEW, Event
from .fetch import HttpClient
from .mirror import Mirror
from .notify import build_notifiers
from .sources import Target, build_source, dedupe
from .state import State
from .util import LOG, http_date_to_iso, human_size, now_iso

# A generated candidate list is only ever walked until the budget runs out, so
# this cap exists purely to stop a large state file producing a huge one.
MAX_CANDIDATES = 5000


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


def vocabulary(entries: dict) -> tuple[list[str], list[str]]:
    """Every name this source has seen, and the file extensions among them.

    File names come before directory names: when probing inside a directory,
    a file is the thing worth finding, so it should be guessed first.
    """
    files: list[str] = []
    dirs: list[str] = []
    extensions: list[str] = []
    for key, record in entries.items():
        if record.get("kind") == KIND_MISSING:
            continue
        name = record.get("filename") or leaf(record.get("rel_path") or key)
        if not name:
            continue
        if record.get("kind") == KIND_DIR:
            dirs.append(name)
            continue
        files.append(name)
        stem, dot, extension = name.rpartition(".")
        if stem and dot and 1 <= len(extension) <= 5:
            extensions.append(f".{extension.lower()}")
    return (list(dict.fromkeys(files + dirs)),
            list(dict.fromkeys(extensions)))


@dataclass
class _Scan:
    """State shared by every check within one source's pass over its targets."""

    budget: Budget
    planned: set = field(default_factory=set)
    probed: int = 0
    errors: int = 0


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
        probed = 0
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
            # Keep watching what earlier runs established: directories the
            # config named, and whatever probing discovered inside them.
            # Without this a discovered file would be checked once, on the run
            # that found it, and then never again.
            carried = {key for key, record in entries.items()
                       if record.get("kind") != KIND_MISSING
                       and (record.get("kind") == KIND_DIR
                            or record.get("discovered"))}
            seen_keys = {target.key for target in targets}
            for key in sorted(carried - seen_keys):
                record = entries[key]
                targets.append(Target(key=key,
                                      url=record.get("url", key),
                                      rel_path=record.get("rel_path", key),
                                      hint=record.get("kind")))

            targets = dedupe(targets)
            scan = _Scan(budget=Budget(source.dir_probe.max_probes
                                       if source.dir_probe.enabled else 0),
                         planned={target.key for target in targets})

            for target in targets:
                checked += 1
                found, failed = self._check(source, target, entries, scan)
                if failed:
                    errors += 1
                events.extend(found)
                time.sleep(self.delay)

            if scan.probed:
                LOG.info("[%s] probed %s name(s) inside its directories",
                         source.name, scan.probed)
            probed += scan.probed
            errors += scan.errors

        summary = {"checked": checked, "probed": probed, "events": len(events),
                   "errors": errors, "bootstrap": bootstrap}
        LOG.info("checked %s target(s), probed %s name(s), %s change(s), "
                 "%s error(s)", checked, probed, len(events), errors)

        self._notify(events, summary, bootstrap)

        if not self.dry_run:
            self.state.record_run(summary)
            self.state.save()
        else:
            LOG.info("dry run: state not written")

        return summary

    # ------------------------------------------------------------------
    def _check(self, source, target, entries: dict, scan: _Scan,
               depth: int = 0) -> tuple[list[Event], bool]:
        response = self.client.head(target.url, source.headers)
        kind = classify(response, source.detect_rules)
        previous = entries.get(target.key)
        was_known = bool(previous) and previous.get("kind") != KIND_MISSING

        if kind == KIND_ERROR:
            LOG.error("[%s] network error on %s: %s",
                      source.name, target.key, response.error)
            return [], True                  # never report "gone" on a network fault

        if kind == KIND_MISSING:
            if was_known:
                LOG.info("[%s] GONE %s", source.name, target.key)
                entries[target.key] = {**previous, "kind": KIND_MISSING,
                                       "last_change": now_iso()}
                return [Event(GONE, previous.get("kind", KIND_FILE), source.name,
                              target.key, target.url)], False
            return [], False

        fingerprint = _fingerprint(response)

        if kind == KIND_DIR:
            return self._check_dir(source, target, entries, previous,
                                   was_known, fingerprint, scan, depth), False

        event, failed = self._check_file(source, target, entries, previous,
                                         was_known, fingerprint)
        return ([event] if event else []), failed

    # ------------------------------------------------------------------
    def _check_dir(self, source, target, entries, previous, was_known,
                   fingerprint, scan, depth) -> list[Event]:
        record = {"kind": KIND_DIR, "url": target.url, "rel_path": target.rel_path,
                  **fingerprint}
        if not was_known:
            entries[target.key] = {**record, "first_seen": now_iso(),
                                   "last_change": now_iso()}
            LOG.info("[%s] NEW dir %s (%s)", source.name, target.key,
                     fingerprint["lm"])
            event = Event(NEW, KIND_DIR, source.name, target.key, target.url,
                          last_modified=fingerprint["lm"])
        else:
            changed = (previous.get("lm") != fingerprint["lm"]
                       or previous.get("etag") != fingerprint["etag"])
            entries[target.key] = {**record,
                                   "first_seen": previous.get("first_seen"),
                                   "explored": previous.get("explored"),
                                   "last_change": now_iso() if changed
                                   else previous.get("last_change")}
            if changed:
                LOG.info("[%s] CHANGED dir %s (%s -> %s)", source.name,
                         target.key, previous.get("lm"), fingerprint["lm"])
                event = Event(CHANGED, KIND_DIR, source.name, target.key,
                              target.url, last_modified=fingerprint["lm"],
                              previous_modified=previous.get("lm"))
            else:
                event = None

        # A directory whose mtime moved is the server admitting that something
        # inside it appeared, vanished or was renamed — and then refusing to
        # say what. Probing is the only way to turn that into a filename.
        #
        # A directory nobody has finished looking inside is worth a sweep too,
        # even with its timestamp untouched: it was discovered mid-run, or the
        # budget ran out partway through it. That is what lets a deep tree
        # converge over successive runs instead of stalling wherever the first
        # run happened to stop.
        plan = source.dir_probe
        explored = (previous or {}).get("explored")
        if plan.probes_at(depth) and (plan.on == "always" or event is not None
                                      or not explored):
            # Names already tried and found absent are remembered, but only
            # until the sweep finishes: that is what makes an interrupted one
            # resume at the point it stopped instead of burning the next run's
            # budget on the same opening names. A change wipes the memo — the
            # contents moved, so every name is worth asking about again.
            tried = set() if event is not None else set(
                (previous or {}).get("tried") or [])
            record = entries[target.key]
            record.pop("explored", None)

            children, probed, complete = self._probe_children(
                source, target, entries, scan, depth, tried)

            if complete:
                record["explored"] = now_iso()
                record.pop("tried", None)
            else:
                record["tried"] = sorted(tried)
            if event is not None:
                event.probed = probed
                event.found = sum(1 for child in children
                                  if child.type in (NEW, CHANGED))
            return ([event] if event else []) + children

        return [event] if event else []

    # ------------------------------------------------------------------
    def _probe_children(self, source, target, entries: dict, scan: _Scan,
                        depth: int, tried: set) -> tuple[list[Event], int, bool]:
        """Ask the server, name by name, what lives inside ``target``.

        ``tried`` is the caller's memo of names already asked about and not
        found; it is read to skip them and written as the sweep goes. The third
        return value says whether the whole candidate list was walked, because
        a sweep the budget cut short must not be recorded as a finished one.
        """
        if scan.budget.left <= 0:
            return [], 0, False

        learned, extensions = vocabulary(entries)
        names = candidates(source.dir_probe, leaf(target.key), learned,
                           extensions, limit=MAX_CANDIDATES)

        events: list[Event] = []
        probed = 0
        complete = True
        for name in names:
            if name in tried:
                continue          # asked about on an earlier, unfinished sweep
            child = source.child(target, name)
            if child.key in scan.planned:
                continue          # already checked, or queued, by this same run
            known = entries.get(child.key)
            if known and known.get("kind") != KIND_MISSING:
                continue
            if not scan.budget.take():
                LOG.info("[%s] probe budget of %s spent, stopping inside %s; "
                         "the next run picks up where this one left off",
                         source.name, scan.budget.limit, target.key)
                complete = False
                break

            scan.planned.add(child.key)
            tried.add(name)
            probed += 1
            scan.probed += 1
            found, failed = self._check(source, child, entries, scan, depth + 1)
            if failed:
                scan.errors += 1
            for event in found:
                record = entries.get(event.path)
                if record is not None:
                    # Remembered so later runs keep checking it; the config
                    # never named it, so nothing else would bring it back.
                    record["discovered"] = True
                LOG.info("[%s] probe found %s %s", source.name,
                         event.kind, event.path)
            events.extend(found)
            time.sleep(self.delay)

        return events, probed, complete

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
