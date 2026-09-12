"""Finding the files behind a directory that offers no listing.

Some servers answer a directory request with a ``Last-Modified`` header and an
empty body. That is enough to know *that* something inside changed, and useless
for knowing *what*. No index page, no API, no autoindex — the only lever left is
that the server will happily confirm or deny any path you name.

So mirrorwatch asks by name. The whole art is in the guest list, and three
generators feed it, most-likely-to-hit first:

``names`` / ``templates``  What the config states outright. If you know the
                           house style, say so here; nothing beats it.
learned names              Every basename this source has ever seen, plus the
                           same names with the year shifted. This is what turns
                           last year's ``flyer2025.pdf`` into a hit on
                           ``flyer2026.pdf`` without a config edit.
derived names              Names built from the directory's own name, because a
                           folder called ``yellow-weeks`` holding
                           ``yellowweeks2026.pdf`` is a convention, not a
                           coincidence.

Guessing is honest work but it is still guessing: a name nobody has ever seen
and that follows no visible pattern will not be found, and the notification
says so rather than pretending otherwise.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

YEAR_RE = re.compile(r"(?:19|20)\d{2}")

ON_VALUES = ("change", "always", "never")

DEFAULTS = {
    "enabled": True,
    "on": "change",
    "depth": 1,
    "max_probes": 150,
    "names": [],
    "templates": [],
    "learn": True,
    "derive": True,
    "year_window": 1,
}


class Budget:
    """A shared allowance of probe requests, so one run cannot run away.

    The limit is per source and per run, not per directory: ten changed
    directories share one allowance rather than multiplying it.
    """

    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self.spent = 0

    @property
    def left(self) -> int:
        return max(0, self.limit - self.spent)

    def take(self) -> bool:
        if self.spent >= self.limit:
            return False
        self.spent += 1
        return True


class DirProbe:
    """A source's plan for probing inside its directories.

    ``dir_probe`` accepts ``true`` as shorthand for "on with the defaults", and
    an object to tune it. Absent means off: probing costs requests, so it is
    never switched on behind the operator's back.
    """

    def __init__(self, spec=None):
        if spec is True:
            spec = {}
        elif not isinstance(spec, dict):
            spec = None

        self.configured = spec is not None
        merged = {**DEFAULTS, **(spec or {})}

        self.on = str(merged["on"])
        self.enabled = (self.configured and bool(merged["enabled"])
                        and self.on != "never")
        self.depth = max(0, int(merged["depth"]))
        self.max_probes = max(0, int(merged["max_probes"]))
        self.names = [str(n) for n in (merged["names"] or [])]
        self.templates = list(merged["templates"] or [])
        self.learn = bool(merged["learn"])
        self.derive = bool(merged["derive"])
        self.year_window = max(0, int(merged["year_window"]))

    def probes_at(self, depth: int) -> bool:
        return self.enabled and depth < self.depth


def leaf(path: str) -> str:
    """The last path segment (``a/b/c`` -> ``c``)."""
    return (path or "").rstrip("/").rsplit("/", 1)[-1]


def _years(window: int) -> list[int]:
    """The current year first, then outwards — nearest guesses go first."""
    now = datetime.now(timezone.utc).year
    out = [now]
    for offset in range(1, window + 1):
        out.append(now + offset)
        out.append(now - offset)
    return out


def year_variants(name: str, window: int) -> list[str]:
    """``flyer2025.pdf`` -> the same name carrying the other years in range."""
    match = YEAR_RE.search(name)
    if not match:
        return []
    return [f"{name[:match.start()]}{year}{name[match.end():]}"
            for year in _years(window)
            if str(year) != match.group(0)]


def _stems(dir_name: str) -> list[str]:
    """Spellings of a directory name that a file inside it might reuse."""
    seen: list[str] = []
    for candidate in (dir_name,
                      dir_name.replace("-", ""),
                      dir_name.replace("_", ""),
                      dir_name.replace("-", "_"),
                      dir_name.replace("_", "-")):
        if candidate and candidate not in seen:
            seen.append(candidate)
    return seen


def derived_names(dir_name: str, extensions: list[str],
                  window: int) -> list[str]:
    """Candidate filenames built from the directory's own name."""
    out: list[str] = []
    years = _years(window)
    for stem in _stems(dir_name):
        for ext in extensions:
            for year in years:
                out.append(f"{stem}{year}{ext}")
            out.append(f"{stem}{ext}")
            for year in years:
                out.append(f"{stem}-{year}{ext}")
                out.append(f"{stem}_{year}{ext}")
    return out


def _dedupe(names) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        cleaned = str(name).strip().strip("/")
        if not cleaned or cleaned in seen or "/" in cleaned:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def candidates(plan: DirProbe, dir_name: str, learned: list[str],
               extensions: list[str], limit: int | None = None) -> list[str]:
    """The ordered list of names to try inside ``dir_name``.

    Order is priority: an explicit config entry is tried before a guess, and a
    guess grounded in something already seen before one spun out of thin air.
    The caller's budget cuts the tail, which is why the ordering matters more
    than the length.
    """
    from .sources import ProbeSource          # local: sources imports us back

    pool: list[str] = list(plan.names)
    pool += ProbeSource.expand(plan.templates)

    if plan.learn:
        for name in learned:
            pool += year_variants(name, plan.year_window)
    if plan.derive and dir_name:
        pool += derived_names(dir_name, extensions or [".pdf"],
                              plan.year_window)
    if plan.learn:
        pool += learned

    out = _dedupe(pool)
    if limit is not None:
        out = out[:max(0, limit)]
    return out


def validate_dir_probe(spec, where: str) -> list[str]:
    """Human readable problems with a ``dir_probe`` block. Empty means valid."""
    if spec is None or isinstance(spec, bool):
        return []                       # true means defaults, false means off
    if not isinstance(spec, dict):
        return [f"{where}: dir_probe must be an object, true or false"]

    problems: list[str] = []
    known = set(DEFAULTS)
    for key in spec:
        if key not in known:
            problems.append(f"{where}: unknown dir_probe key {key!r} "
                            f"(known: {', '.join(sorted(known))})")

    if "on" in spec and spec["on"] not in ON_VALUES:
        problems.append(f"{where}: dir_probe.on must be one of "
                        f"{', '.join(ON_VALUES)}, got {spec['on']!r}")

    for key in ("depth", "max_probes", "year_window"):
        if key in spec:
            try:
                if int(spec[key]) < 0:
                    problems.append(f"{where}: dir_probe.{key} cannot be negative")
            except (TypeError, ValueError):
                problems.append(f"{where}: dir_probe.{key} must be a whole number")

    for key in ("enabled", "learn", "derive"):
        if key in spec and not isinstance(spec[key], bool):
            problems.append(f"{where}: dir_probe.{key} must be true or false")

    for key in ("names", "templates"):
        if key in spec and not isinstance(spec[key], list):
            problems.append(f"{where}: dir_probe.{key} must be a list")

    for name in (spec.get("names") or []) if isinstance(spec.get("names"), list) else []:
        if not isinstance(name, str) or "/" in name:
            problems.append(f"{where}: dir_probe.names entries must be plain "
                            f"file names without a slash, got {name!r}")
    return problems
