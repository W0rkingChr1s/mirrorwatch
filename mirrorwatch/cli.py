"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

from . import __version__
from .config import ConfigError, load, validate
from .core import Runner
from .schedule import (format_times, next_run, parse_times,
                       resolve_timezone, seconds_until)
from .sources import build_source, dedupe
from .util import LOG, setup_logging

_stop = False


def _handle_signal(signum, _frame):
    global _stop                                            # noqa: PLW0603
    _stop = True
    LOG.info("received signal %s, finishing current run then exiting", signum)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mirrorwatch",
        description="Watch HTTP endpoints for new and changed files, "
                    "mirror them locally, and get notified.")
    parser.add_argument("-c", "--config",
                        default=os.environ.get("MIRRORWATCH_CONFIG", "config.json"),
                        help="path to the config file (default: config.json)")
    parser.add_argument("-l", "--log-level",
                        default=os.environ.get("MIRRORWATCH_LOG_LEVEL", "INFO"),
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("-V", "--version", action="version",
                        version=f"mirrorwatch {__version__}")

    # The same options are accepted after the subcommand. SUPPRESS keeps the
    # subparser from overwriting a value already given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS)
    common.add_argument("-l", "--log-level", default=argparse.SUPPRESS,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", parents=[common],
                   help="run forever on the configured schedule: check_times "
                        "if set, otherwise interval_seconds")

    once = sub.add_parser("once", parents=[common],
                          help="run a single pass and exit")
    once.add_argument("--dry-run", action="store_true",
                      help="report what would happen; write no state, "
                           "no mirror, no notifications")

    sub.add_parser("check", parents=[common],
                   help="validate the config and exit")
    sub.add_parser("targets", parents=[common],
                   help="list the targets each source resolves to")

    status = sub.add_parser("status", parents=[common],
                            help="summarise the current state file")
    status.add_argument("--json", action="store_true", help="raw JSON output")
    status.add_argument("--max-age", type=int, default=None, metavar="SECONDS",
                        help="exit non-zero if the last run is older than this; "
                             "use it as a container healthcheck")

    return parser


def cmd_check(config_path: str) -> int:
    try:
        load(config_path)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"{config_path}: OK")
    return 0


def cmd_targets(config: dict) -> int:
    from .fetch import HttpClient
    client = HttpClient(user_agent=config["user_agent"],
                        timeout=config["timeout"],
                        retries=config["retries"])
    total = 0
    for spec in config["sources"]:
        source = build_source(spec)
        targets = dedupe(source.targets(client))
        total += len(targets)
        print(f"\n{source.name} ({source.type}) -> {len(targets)} target(s)")
        for target in targets:
            marker = "d" if target.hint == "dir" else "-"
            print(f"  [{marker}] {target.url}")
    print(f"\n{total} target(s) total")
    return 0


def cmd_status(config: dict, as_json: bool, max_age: int | None = None) -> int:
    path = config["state_file"]
    if not os.path.exists(path):
        print(f"no state file at {path}; mirrorwatch has not run yet")
        return 1
    with open(path, "r", encoding="utf-8") as handle:
        state = json.load(handle)

    if as_json:
        json.dump(state, sys.stdout, indent=2, ensure_ascii=False)
        print()
        return 0

    last_run = state.get("last_run")
    print(f"state file : {path}")
    print(f"last run   : {last_run or 'never'}")

    times = parse_times(config.get("check_times"))
    if times:
        tz = resolve_timezone(config.get("timezone"))
        upcoming = next_run(times, tz)
        zone = config.get("timezone") or "local time"
        print(f"schedule   : {format_times(times)} ({zone})")
        print(f"next run   : {upcoming:%Y-%m-%d %H:%M}")
    else:
        print(f"schedule   : every {config['interval_seconds']}s")

    stale = False
    if max_age is not None:
        if not last_run:
            stale = True
        else:
            from datetime import datetime, timezone
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(last_run)).total_seconds()
            print(f"age        : {int(age)}s (limit {max_age}s)")
            stale = age > max_age
    for name, entries in sorted(state.get("sources", {}).items()):
        kinds: dict[str, int] = {}
        for record in entries.values():
            kinds[record.get("kind", "?")] = kinds.get(record.get("kind", "?"), 0) + 1
        breakdown = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
        print(f"  {name}: {len(entries)} entries ({breakdown})")

    if stale:
        print("last run is too old", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_level)

    if args.command == "check":
        return cmd_check(args.config)

    try:
        config = load(args.config)
    except ConfigError as exc:
        LOG.error("%s", exc)
        return 2

    if args.command == "targets":
        return cmd_targets(config)
    if args.command == "status":
        return cmd_status(config, args.json, args.max_age)

    if args.command == "once":
        runner = Runner(config, dry_run=args.dry_run)
        summary = runner.run_once()
        return 1 if summary["errors"] else 0

    # default: run forever
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    interval = int(config["interval_seconds"])
    times = parse_times(config.get("check_times"))
    tz = resolve_timezone(config.get("timezone"))

    # Unset means "whatever suits the mode": an interval starts counting from
    # now, while fixed times are a promise about the clock, not about restarts.
    run_now = config.get("run_on_start")
    if run_now is None:
        run_now = not times

    if times:
        LOG.info("mirrorwatch %s starting, checks at %s (%s)", __version__,
                 format_times(times), config.get("timezone") or "local time")
    else:
        LOG.info("mirrorwatch %s starting, interval %ss", __version__, interval)

    while not _stop:
        if run_now:
            try:
                Runner(config).run_once()
            except Exception as exc:                        # noqa: BLE001
                LOG.exception("run failed: %s", exc)
            if _stop:
                break
        run_now = True
        _wait(times, tz, interval)
    LOG.info("stopped")
    return 0


def _wait(times, tz, interval: int) -> None:
    """Sleep until the next run is due, waking often enough to notice signals."""
    if times:
        target = next_run(times, tz)
        LOG.info("next check at %s", f"{target:%Y-%m-%d %H:%M}")
        # Recomputed every tick, so a clock or DST change is picked up.
        while not _stop:
            remaining = seconds_until(target, tz)
            if remaining <= 0:
                return
            time.sleep(min(5.0, remaining))
        return
    slept = 0
    while slept < interval and not _stop:
        time.sleep(min(5, interval - slept))
        slept += 5


if __name__ == "__main__":
    sys.exit(main())
