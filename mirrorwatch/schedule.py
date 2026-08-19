"""Wall-clock scheduling for ``mirrorwatch run``.

By default runs are spaced out by ``interval_seconds``. Configure
``check_times`` instead and mirrorwatch runs at those times of day — 06:00 and
18:00, say — in ``timezone`` when one is given, and in the machine's local time
otherwise.

Standard library only, in keeping with mirrorwatch's zero-dependency promise.
On slim base images (Alpine among them) ``timezone`` additionally needs the
system tzdata, which the shipped Dockerfile installs.
"""

from __future__ import annotations

from datetime import datetime, time as clock_time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ScheduleError(Exception):
    pass


def parse_times(value) -> list[tuple[int, int]]:
    """Normalise ``check_times`` into sorted, unique ``(hour, minute)`` pairs.

    Accepts a list of ``"HH:MM"`` strings, or one comma-separated string, which
    is all an environment variable can carry.
    """
    if not value:
        return []
    if isinstance(value, str):
        items: list = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        problem = "check_times must be a list of \"HH:MM\" strings"
        raise ScheduleError(problem)

    times: set[tuple[int, int]] = set()
    for item in items:
        if not isinstance(item, str):
            raise ScheduleError(f"check_times entry {item!r} must be a string "
                                f"like \"06:00\"")
        text = item.strip()
        if not text:
            continue
        parts = text.split(":")
        if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
            raise ScheduleError(f"check_times entry {item!r} is not a "
                                f"\"HH:MM\" time")
        hour, minute = (int(part) for part in parts)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ScheduleError(f"check_times entry {item!r} is outside "
                                f"00:00-23:59")
        times.add((hour, minute))
    return sorted(times)


def format_times(times) -> str:
    """``[(6, 0), (18, 30)]`` -> ``"06:00, 18:30"``."""
    return ", ".join(f"{hour:02d}:{minute:02d}" for hour, minute in times)


def resolve_timezone(name):
    """Return a tzinfo for ``name``, or None for the machine's local time."""
    if not name:
        return None
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ScheduleError(
            f"unknown timezone {name!r}: {exc}. Use an IANA name such as "
            f"\"Europe/Berlin\"; slim container images need the tzdata "
            f"package for these to resolve") from None


def _reference(now, tz):
    """Put ``now`` in the frame the candidates are built in.

    With a timezone it is that zone, so daylight saving is handled by zoneinfo.
    Without one it is naive local time, which is what ``datetime.now()`` gives.
    """
    if now is None:
        return datetime.now(tz)
    if tz is not None:
        return now.astimezone(tz) if now.tzinfo else now.astimezone().astimezone(tz)
    return now.astimezone().replace(tzinfo=None) if now.tzinfo else now


def next_run(times, tz=None, now=None):
    """The next moment matching ``times``, strictly after ``now``.

    Returns None when no times are configured, i.e. in interval mode.
    """
    if not times:
        return None
    current = _reference(now, tz)
    for offset in (0, 1):
        day = (current + timedelta(days=offset)).date()
        for hour, minute in times:
            candidate = datetime.combine(day, clock_time(hour, minute),
                                         tzinfo=current.tzinfo)
            if candidate > current:
                return candidate
    # Unreachable: tomorrow's first slot is always ahead of now.
    raise ScheduleError("could not determine the next check time")


def seconds_until(target, tz=None, now=None) -> float:
    """Real seconds from now until ``target``, never negative.

    Both ends go through UTC first. Subtracting two datetimes that share a
    zoneinfo tzinfo is wall-clock arithmetic, which would lose the hour a
    daylight-saving switch adds or removes; naive values are read as local
    time, where the same applies.
    """
    current = _reference(now, tz)
    return max(0.0, (target.astimezone(timezone.utc)
                     - current.astimezone(timezone.utc)).total_seconds())
