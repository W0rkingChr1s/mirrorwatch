"""Configuration loading and validation.

JSON is the native format so mirrorwatch keeps its zero-dependency promise. YAML is
accepted too, but only when PyYAML happens to be installed.
"""

from __future__ import annotations

import json
import os

from .detect import validate_rules
from .discover import validate_dir_probe
from .notify import BACKENDS
from .schedule import ScheduleError, parse_times, resolve_timezone
from .sources import SOURCE_TYPES

DEFAULTS = {
    "user_agent": "mirrorwatch/0.1 (+https://github.com/yourname/mirrorwatch)",
    "timeout": 60,
    "retries": 2,
    "request_delay_ms": 250,
    "max_download_mb": 200,
    "interval_seconds": 21600,
    # Wall-clock times of day ("06:00", "18:00") for `run` mode. When set,
    # they replace interval_seconds. Empty means interval mode.
    "check_times": [],
    # IANA zone the check times are read in; null means local time.
    "timezone": None,
    # Run once immediately on start? null picks the sensible default per
    # mode: yes on an interval, no when check times are configured.
    "run_on_start": None,
    "bootstrap_notify": "summary",
    "state_file": "./data/state.json",
    "mirror": {
        "enabled": True,
        "dir": "./data/mirror",
        "archive_dir": "./data/archive",
        "keep_versions": True,
    },
    "notifiers": {},
    "sources": [],
}


class ConfigError(Exception):
    pass


def boolean(raw: str) -> bool:
    """Parse the usual truthy/falsy spellings an env var arrives in."""
    lowered = str(raw).strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ValueError(raw)


def _load_raw(path: str) -> dict:
    # The whole config can travel in an environment variable instead of a file.
    # Handy for one-paste container stacks (e.g. Portainer) where mounting a
    # file is awkward. When set, it wins over the file at `path`.
    inline = os.environ.get("MIRRORWATCH_CONFIG_JSON")
    if inline and inline.strip():
        try:
            return json.loads(inline)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"MIRRORWATCH_CONFIG_JSON is not valid JSON: {exc}") from exc

    if not os.path.exists(path):
        raise ConfigError(f"config file not found: {path}")

    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()

    if path.endswith((".yaml", ".yml")):
        try:
            import yaml                                   # type: ignore
        except ImportError as exc:
            raise ConfigError(
                "YAML config requires PyYAML (pip install pyyaml), or use JSON"
            ) from exc
        return yaml.safe_load(text) or {}

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _apply_env_overrides(config: dict) -> dict:
    """Environment beats file, which matters for containers."""
    mapping = {
        "MIRRORWATCH_STATE_FILE": ("state_file", str),
        "MIRRORWATCH_MIRROR_DIR": ("mirror.dir", str),
        "MIRRORWATCH_ARCHIVE_DIR": ("mirror.archive_dir", str),
        "MIRRORWATCH_INTERVAL": ("interval_seconds", int),
        "MIRRORWATCH_CHECK_TIMES": ("check_times", str),
        "MIRRORWATCH_TIMEZONE": ("timezone", str),
        "MIRRORWATCH_RUN_ON_START": ("run_on_start", boolean),
        "MIRRORWATCH_USER_AGENT": ("user_agent", str),
        "MIRRORWATCH_REQUEST_DELAY_MS": ("request_delay_ms", int),
        "MIRRORWATCH_BOOTSTRAP_NOTIFY": ("bootstrap_notify", str),
    }
    for env_name, (dotted, caster) in mapping.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        node = config
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        try:
            node[parts[-1]] = caster(raw)
        except ValueError:
            raise ConfigError(f"{env_name}={raw!r} is not a valid "
                              f"{caster.__name__}") from None
    return config


def validate(config: dict) -> list[str]:
    problems: list[str] = []

    if config.get("bootstrap_notify") not in ("summary", "full", "none"):
        problems.append("bootstrap_notify must be one of: summary, full, none")

    times: list[tuple[int, int]] = []
    try:
        times = parse_times(config.get("check_times"))
    except ScheduleError as exc:
        problems.append(str(exc))
    try:
        resolve_timezone(config.get("timezone"))
    except ScheduleError as exc:
        problems.append(str(exc))
    if config.get("run_on_start") not in (None, True, False):
        problems.append("run_on_start must be true, false, or null (automatic)")
    if not times:
        try:
            if int(config.get("interval_seconds")) < 1:
                problems.append("interval_seconds must be at least 1, or set "
                                "check_times instead")
        except (TypeError, ValueError):
            problems.append("interval_seconds must be a whole number of seconds")

    for name, spec in (config.get("notifiers") or {}).items():
        if not isinstance(spec, dict):
            problems.append(f"notifier {name!r} must be an object")
            continue
        backend = spec.get("type", name)
        if backend not in BACKENDS:
            problems.append(f"notifier {name!r}: unknown type {backend!r} "
                            f"(known: {', '.join(sorted(BACKENDS))})")
        if backend == "telegram":
            for required in ("token", "chat_id"):
                if not spec.get(required):
                    problems.append(f"notifier {name!r}: {required} is required")
        if backend in ("webhook", "ntfy") and not spec.get("url"):
            problems.append(f"notifier {name!r}: url is required")

    sources = config.get("sources") or []
    if not sources:
        problems.append("no sources configured, mirrorwatch would do nothing")

    seen_names: set[str] = set()
    for index, spec in enumerate(sources):
        where = f"sources[{index}]"
        if not isinstance(spec, dict):
            problems.append(f"{where} must be an object")
            continue
        name = spec.get("name")
        if not name:
            problems.append(f"{where}: name is required")
        elif name in seen_names:
            problems.append(f"{where}: duplicate source name {name!r}")
        else:
            seen_names.add(name)

        source_type = spec.get("type", "urls")
        if source_type not in SOURCE_TYPES:
            problems.append(f"{where}: unknown type {source_type!r} "
                            f"(known: {', '.join(sorted(SOURCE_TYPES))})")
        elif source_type == "probe":
            if not (spec.get("dirs") or spec.get("files") or spec.get("probes")):
                problems.append(f"{where}: probe source needs at least one of "
                                f"dirs, files, probes")
        elif source_type == "index":
            if not (spec.get("url") or spec.get("urls")):
                problems.append(f"{where}: index source needs url or urls")
        elif source_type == "urls":
            if not spec.get("urls"):
                problems.append(f"{where}: urls source needs urls")

        if spec.get("detect"):
            problems.extend(validate_rules(spec["detect"], where))

        if "dir_probe" in spec:
            problems.extend(validate_dir_probe(spec["dir_probe"], where))
            if spec["dir_probe"] and source_type == "urls":
                problems.append(f"{where}: dir_probe needs a source that can "
                                f"have directories; a urls source has none")

        for target in spec.get("notify") or []:
            if target not in (config.get("notifiers") or {}):
                problems.append(f"{where}: notify references unknown "
                                f"notifier {target!r}")
    return problems


def load(path: str) -> dict:
    config = _merge(DEFAULTS, _load_raw(path))
    config = _apply_env_overrides(config)
    problems = validate(config)
    if problems:
        raise ConfigError("invalid configuration:\n  - "
                          + "\n  - ".join(problems))
    # Keep the parsed form canonical: a list of "HH:MM", however it arrived.
    config["check_times"] = [f"{hour:02d}:{minute:02d}"
                             for hour, minute in parse_times(config["check_times"])]
    return config
