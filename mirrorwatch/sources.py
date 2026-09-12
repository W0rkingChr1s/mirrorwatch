"""Source types: how mirrorwatch discovers what to look at.

``probe``  Build candidate paths from an explicit list plus templates, then ask
           the server whether each exists. Needed for servers that expose no
           listing at all.

``index``  Fetch an HTML page, extract links, keep the ones that match. This is
           the right choice whenever the site has any kind of overview page,
           because it discovers files you did not know about.

``urls``   A plain list of absolute URLs. The degenerate, always-works case.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from .discover import DirProbe
from .util import LOG


@dataclass
class Target:
    key: str            # stable identity used in the state file
    url: str            # absolute URL to request
    rel_path: str       # where it lands inside the mirror
    hint: str | None = None   # "dir" when the source knows it is a directory


class _LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.links.append(value.strip())


def _rel_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/index"
    if parsed.query:
        # Keep query-addressed resources distinguishable in the mirror.
        path = f"{path}__{abs(hash(parsed.query)) % 10**8}"
    return f"{parsed.netloc}{path}"


class BaseSource:
    type = "base"

    def __init__(self, spec: dict):
        self.spec = spec
        self.name = spec["name"]
        self.detect_rules = spec.get("detect") or {}
        self.headers = spec.get("headers") or {}
        self.mirror = spec.get("mirror", True)
        self.notify_to = spec.get("notify")        # None = all notifiers
        self.always_download = spec.get("always_download", False)
        self.dir_probe = DirProbe(spec.get("dir_probe"))

    def targets(self, client) -> list[Target]:
        raise NotImplementedError

    def child(self, parent: Target, name: str) -> Target:
        """A target for ``name`` inside the directory ``parent``.

        The default appends to the parent URL, which is right for anything
        addressed by a real path. Sources that build URLs some other way
        override it.
        """
        return Target(key=f"{parent.key.rstrip('/')}/{name}",
                      url=f"{parent.url.rstrip('/')}/{name}",
                      rel_path=f"{parent.rel_path.rstrip('/')}/{name}")


class UrlsSource(BaseSource):
    type = "urls"

    def targets(self, client) -> list[Target]:
        out = []
        for url in self.spec.get("urls", []):
            out.append(Target(key=url, url=url, rel_path=_rel_from_url(url)))
        return out


class ProbeSource(BaseSource):
    type = "probe"

    def __init__(self, spec: dict):
        super().__init__(spec)
        self.base_url = spec.get("base_url", "")

    def _url(self, path: str) -> str:
        if self.base_url.endswith(("=", "?", "&")):
            return self.base_url + path          # query-parameter style endpoint
        if not self.base_url:
            return path
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")

    @staticmethod
    def expand(templates: list) -> list[str]:
        """Expand ``{yyyy}`` / ``{yy}`` / ``{mm}`` / ``{v}`` placeholders."""
        out: list[str] = []
        year_now = datetime.now(timezone.utc).year
        for entry in templates or []:
            if isinstance(entry, str):
                out.append(entry)
                continue
            template = entry.get("template")
            if not template:
                continue
            years = entry.get("years")
            values = entry.get("values")
            months = entry.get("months")

            candidates = [template]
            if years:
                start = int(years.get("from", year_now))
                end = int(years.get("to", year_now + 1))
                candidates = [c.replace("{yyyy}", str(y)).replace("{yy}", f"{y % 100:02d}")
                              for c in candidates for y in range(start, end + 1)]
            if months:
                candidates = [c.replace("{mm}", f"{m:02d}")
                              for c in candidates for m in range(1, 13)]
            if values:
                candidates = [c.replace("{v}", str(v))
                              for c in candidates for v in values]
            out.extend(candidates)
        return out

    def child(self, parent: Target, name: str) -> Target:
        path = f"{parent.key.rstrip('/')}/{name}"
        return Target(key=path, url=self._url(path), rel_path=path)

    def targets(self, client) -> list[Target]:
        out: list[Target] = []
        for path in self.spec.get("dirs", []):
            out.append(Target(path, self._url(path), path, hint="dir"))
        for path in self.spec.get("files", []):
            out.append(Target(path, self._url(path), path))
        for path in self.expand(self.spec.get("probes", [])):
            out.append(Target(path, self._url(path), path))
        return out


class IndexSource(BaseSource):
    type = "index"

    def __init__(self, spec: dict):
        super().__init__(spec)
        self.start_urls = spec.get("urls") or ([spec["url"]] if spec.get("url") else [])
        self.match = re.compile(spec["match"]) if spec.get("match") else None
        self.exclude = re.compile(spec["exclude"]) if spec.get("exclude") else None
        recursive = spec.get("recursive") or {}
        self.depth = int(recursive.get("depth", 0))
        self.follow = re.compile(recursive["match"]) if recursive.get("match") else None
        self.same_host_only = spec.get("same_host_only", True)
        self.max_links = int(spec.get("max_links", 1000))

    def _allowed_host(self, url: str, origin: str) -> bool:
        if not self.same_host_only:
            return True
        return urlparse(url).netloc == urlparse(origin).netloc

    def targets(self, client) -> list[Target]:
        found: dict[str, Target] = {}
        visited: set[str] = set()
        queue = [(url, 0) for url in self.start_urls]

        while queue:
            page_url, depth = queue.pop(0)
            if page_url in visited:
                continue
            visited.add(page_url)

            response = client.get(page_url, self.headers)
            if not response.ok or response.body is None:
                LOG.error("[%s] cannot read index %s: %s",
                          self.name, page_url, response.error or response.status)
                continue
            if not 200 <= response.status < 300:
                LOG.error("[%s] index %s returned HTTP %s",
                          self.name, page_url, response.status)
                continue

            collector = _LinkCollector()
            try:
                collector.feed(response.body.decode("utf-8", "replace"))
            except Exception as exc:                        # noqa: BLE001
                LOG.error("[%s] cannot parse %s: %s", self.name, page_url, exc)
                continue

            for href in collector.links:
                if href.startswith(("mailto:", "javascript:", "tel:", "#")):
                    continue
                absolute = urljoin(page_url, href)
                if not absolute.startswith(("http://", "https://")):
                    continue
                if not self._allowed_host(absolute, page_url):
                    continue
                if self.exclude and self.exclude.search(absolute):
                    continue

                if self.match and self.match.search(absolute):
                    if absolute not in found and len(found) < self.max_links:
                        found[absolute] = Target(absolute, absolute,
                                                 _rel_from_url(absolute))
                elif (depth < self.depth and self.follow
                      and self.follow.search(absolute)
                      and absolute not in visited):
                    queue.append((absolute, depth + 1))

            LOG.debug("[%s] %s -> %s links, %s matched so far",
                      self.name, page_url, len(collector.links), len(found))

        if len(found) >= self.max_links:
            LOG.warning("[%s] hit max_links=%s, results are truncated",
                        self.name, self.max_links)
        LOG.info("[%s] index discovered %s matching file(s)", self.name, len(found))
        return list(found.values())


SOURCE_TYPES = {
    "probe": ProbeSource,
    "index": IndexSource,
    "urls": UrlsSource,
}


def build_source(spec: dict) -> BaseSource:
    source_type = spec.get("type", "urls")
    factory = SOURCE_TYPES.get(source_type)
    if factory is None:
        raise SystemExit(f"unknown source type {source_type!r} in source "
                         f"{spec.get('name')!r}; known: {', '.join(SOURCE_TYPES)}")
    return factory(spec)


def dedupe(targets: list[Target]) -> list[Target]:
    """Drop repeated keys, keeping the first occurrence.

    A path can legitimately appear in both ``files`` and an expanded ``probes``
    template; without this every run would spend two requests on it.
    """
    seen: set[str] = set()
    out: list[Target] = []
    for target in targets:
        if target.key in seen:
            continue
        seen.add(target.key)
        out.append(target)
    return out
