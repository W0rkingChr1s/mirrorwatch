"""Rule based classification of an HTTP response.

Servers disagree wildly about how they signal "this does not exist". Some return
404, some return 200 with an error page, some invent content types. Rather than
hardcoding one server's quirks, mirrorwatch lets the config describe them.

Rule example::

    "detect": {
      "missing":   [{"status": [404, 410]},
                    {"content_type": "text/html", "max_size": 64}],
      "directory": [{"content_type": "directory"}],
      "default":   "file"
    }

A rule matches when every criterion it specifies matches. Criteria:

==================  =========================================================
``status``          int or list of ints, exact match
``status_range``    ``[min, max]`` inclusive
``content_type``    string or list; matched as a prefix, case insensitive
``max_size``        Content-Length at most this. Ignored when the server does
                    not send Content-Length.
``min_size``        Content-Length at least this. Same caveat.
``url_suffix``      string or list; the URL ends with one of them
==================  =========================================================
"""

from __future__ import annotations

from .fetch import Response

KIND_FILE = "file"
KIND_DIR = "dir"
KIND_MISSING = "missing"
KIND_ERROR = "error"

VALID_KINDS = (KIND_FILE, KIND_DIR, KIND_MISSING)

DEFAULT_RULES = {
    "missing": [{"status": [404, 403, 410, 451]}],
    "directory": [],
    "default": KIND_FILE,
}


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, (list, tuple)) else [value]


def _rule_matches(rule: dict, response: Response) -> bool:
    if "status" in rule:
        if response.status not in _as_list(rule["status"]):
            return False

    if "status_range" in rule:
        low, high = rule["status_range"]
        if not low <= response.status <= high:
            return False

    if "content_type" in rule:
        wanted = [str(c).lower() for c in _as_list(rule["content_type"])]
        actual = response.content_type
        if not any(actual.startswith(w) for w in wanted):
            return False

    size = response.size_hint
    if "max_size" in rule and size is not None and size > rule["max_size"]:
        return False
    if "min_size" in rule and size is not None and size < rule["min_size"]:
        return False

    if "url_suffix" in rule:
        suffixes = _as_list(rule["url_suffix"])
        if not any(response.url.endswith(str(s)) for s in suffixes):
            return False

    return True


def classify(response: Response, rules: dict | None = None) -> str:
    """Return one of KIND_FILE, KIND_DIR, KIND_MISSING, KIND_ERROR."""
    if not response.ok:
        return KIND_ERROR

    rules = {**DEFAULT_RULES, **(rules or {})}

    for rule in rules.get("missing", []):
        if _rule_matches(rule, response):
            return KIND_MISSING

    for rule in rules.get("directory", []):
        if _rule_matches(rule, response):
            return KIND_DIR

    for rule in rules.get("file", []):
        if _rule_matches(rule, response):
            return KIND_FILE

    default = rules.get("default", KIND_FILE)
    if default not in VALID_KINDS:
        return KIND_FILE
    # A non-2xx response that matched nothing is not a usable file.
    if default == KIND_FILE and not 200 <= response.status < 300:
        return KIND_MISSING
    return default


def validate_rules(rules: dict, where: str) -> list[str]:
    """Return a list of human readable problems. Empty list means valid."""
    problems: list[str] = []
    if not isinstance(rules, dict):
        return [f"{where}: detect must be an object"]

    known_buckets = {"missing", "directory", "file", "default"}
    for key in rules:
        if key not in known_buckets:
            problems.append(f"{where}: unknown detect key {key!r}")

    if "default" in rules and rules["default"] not in VALID_KINDS:
        problems.append(
            f"{where}: detect.default must be one of {VALID_KINDS}, "
            f"got {rules['default']!r}")

    known_criteria = {"status", "status_range", "content_type",
                      "max_size", "min_size", "url_suffix"}
    for bucket in ("missing", "directory", "file"):
        for index, rule in enumerate(rules.get(bucket, []) or []):
            if not isinstance(rule, dict):
                problems.append(f"{where}: detect.{bucket}[{index}] must be an object")
                continue
            if not rule:
                problems.append(f"{where}: detect.{bucket}[{index}] is empty "
                                f"and would match everything")
            for key in rule:
                if key not in known_criteria:
                    problems.append(
                        f"{where}: detect.{bucket}[{index}] unknown criterion {key!r}")
    return problems
