"""A minimal HTTP client built on ``urllib``.

Two ideas only: a :class:`Response` that answers the handful of questions the
rest of mirrorwatch asks (status, a header, a size hint, whether the request
even reached the server), and an :class:`HttpClient` that produces one for a
``HEAD`` or ``GET``.

A network failure is represented as a ``Response`` with ``error`` set and
``ok`` False. An HTTP error status (404, 500, ...) is *not* a failure: the
server answered, so the response is ``ok`` and the status carries the meaning.
That distinction is what lets mirrorwatch refuse to report an unreachable
server as a deletion.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request

from .util import LOG

DEFAULT_USER_AGENT = "mirrorwatch/0.1 (+https://github.com/yourname/mirrorwatch)"


class Response:
    def __init__(self, url: str, status: int, headers: dict | None = None,
                 body: bytes | None = None, error: str | None = None):
        self.url = url
        self.status = status
        self._headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        self.body = body
        self.error = error

    @property
    def ok(self) -> bool:
        """True when the request reached the server, whatever it answered."""
        return self.error is None

    def header(self, name: str) -> str | None:
        return self._headers.get(name.lower())

    @property
    def content_type(self) -> str:
        return (self.header("content-type") or "").lower()

    @property
    def size_hint(self) -> int | None:
        raw = self.header("content-length")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def __repr__(self) -> str:            # pragma: no cover - debugging aid
        if self.error:
            return f"<Response {self.url} error={self.error!r}>"
        return f"<Response {self.url} status={self.status}>"


class HttpClient:
    def __init__(self, user_agent: str = DEFAULT_USER_AGENT, timeout: int = 60,
                 retries: int = 2, max_bytes: int = 200 * 1024 * 1024):
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.max_bytes = int(max_bytes)

    # ------------------------------------------------------------------
    def head(self, url: str, headers: dict | None = None) -> Response:
        return self._request(url, headers, method="HEAD", want_body=False)

    def get(self, url: str, headers: dict | None = None) -> Response:
        return self._request(url, headers, method="GET", want_body=True)

    # ------------------------------------------------------------------
    def _request(self, url: str, headers: dict | None, method: str,
                 want_body: bool) -> Response:
        request_headers = {"User-Agent": self.user_agent}
        request_headers.update(headers or {})

        last_error = None
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(url, method=method,
                                             headers=request_headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    status = getattr(resp, "status", None) or resp.getcode()
                    hdrs = {k.lower(): v for k, v in resp.headers.items()}
                    body = self._read_limited(resp, hdrs, url) if want_body else None
                    return Response(url, status, hdrs, body=body)
            except urllib.error.HTTPError as exc:
                # A real answer with an error status. Not a network failure.
                hdrs = ({k.lower(): v for k, v in exc.headers.items()}
                        if exc.headers else {})
                body = None
                if want_body:
                    try:
                        body = exc.read()
                    except OSError:
                        body = None
                return Response(url, exc.code, hdrs, body=body)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = getattr(exc, "reason", None) or exc
                if attempt < self.retries:
                    time.sleep(0.5 * (attempt + 1))     # linear backoff
                    continue

        return Response(url, 0, {}, error=str(last_error) if last_error
                        else "request failed")

    # ------------------------------------------------------------------
    def _read_limited(self, resp, headers: dict, url: str) -> bytes | None:
        """Read a body, refusing anything larger than ``max_bytes``."""
        declared = headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    LOG.error("refusing %s: %s bytes exceeds max_download of %s",
                              url, declared, self.max_bytes)
                    return None
            except ValueError:
                pass

        data = resp.read(self.max_bytes + 1)
        if len(data) > self.max_bytes:
            LOG.error("refusing %s: body exceeds max_download of %s bytes",
                      url, self.max_bytes)
            return None
        return data
