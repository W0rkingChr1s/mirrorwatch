"""Notification backends.

Every backend implements ``send(events, context)``. Adding one means writing a
class with that method and registering it in ``BACKENDS``.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime

from .events import CHANGED, GONE, NEW, Event
from .util import LOG, html_escape, human_size, now_iso, resolve_secret

MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".zip": "application/zip",
    ".mp4": "video/mp4",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def guess_mime(filename: str) -> str:
    lowered = (filename or "").lower()
    for ext, mime in MIME_BY_EXT.items():
        if lowered.endswith(ext):
            return mime
    return "application/octet-stream"


def de_datetime(iso: str | None, with_time: bool = False) -> str:
    """Render an ISO 8601 timestamp as a friendly German date.

    ``2021-11-15T12:01:15+00:00`` -> ``15.11.2021`` (or ``15.11.2021 12:01``
    with ``with_time``). Falls back to a trimmed raw value if parsing fails,
    so a malformed timestamp never breaks a notification.
    """
    if not iso:
        return ""
    try:
        parsed = datetime.fromisoformat(iso.strip().replace("Z", "+00:00"))
    except ValueError:
        return iso[:16].replace("T", " ")
    return parsed.strftime("%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y")


def de_size(num_bytes: int | None) -> str:
    """Human-friendly size with a German decimal comma (``453,5 KB``)."""
    return human_size(num_bytes).replace(".", ",")


def leaf(path: str) -> str:
    """The last path segment (``a/b/c`` -> ``c``), for a compact heading."""
    return (path or "").rstrip("/").rsplit("/", 1)[-1] or (path or "")


def multipart(fields: dict, files: list) -> tuple[bytes, str]:
    """Build a multipart/form-data body. files: (name, filename, data, mime)."""
    boundary = uuid.uuid4().hex
    out = bytearray()
    for key, value in fields.items():
        out += (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n").encode("utf-8")
    for name, filename, data, mime in files:
        safe_name = filename.replace('"', "_")
        out += (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{safe_name}"\r\n'
                f"Content-Type: {mime}\r\n\r\n").encode("utf-8")
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode("utf-8")
    return bytes(out), f"multipart/form-data; boundary={boundary}"


class Notifier:
    name = "base"

    def __init__(self, config: dict, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run

    def send(self, events: list[Event], context: dict) -> None:
        raise NotImplementedError

    def send_summary(self, text: str, context: dict) -> None:
        """Called instead of send() on a bootstrap run."""
        LOG.info("[%s] summary: %s", self.name, text.replace("\n", " | "))


class StdoutNotifier(Notifier):
    name = "stdout"

    def send(self, events: list[Event], context: dict) -> None:
        for event in events:
            LOG.info("[stdout] %s %s %s (%s)", event.type.upper(), event.kind,
                     event.path, event.last_modified)

    def send_summary(self, text: str, context: dict) -> None:
        LOG.info("[stdout] %s", text.replace("\n", " | "))


class WebhookNotifier(Notifier):
    name = "webhook"

    def send(self, events: list[Event], context: dict) -> None:
        url = resolve_secret(self.config.get("url", ""))
        if not url:
            LOG.error("webhook notifier has no url")
            return

        body = json.dumps({
            "source": "mirrorwatch",
            "ts": now_iso(),
            "run": context,
            "count": len(events),
            "events": [event.to_dict() for event in events],
        }, ensure_ascii=False).encode("utf-8")

        if self.dry_run:
            LOG.info("[dry-run] POST %s (%s events)", url, len(events))
            return

        headers = {"Content-Type": "application/json"}
        for key, value in (self.config.get("headers") or {}).items():
            headers[key] = resolve_secret(value)

        request = urllib.request.Request(url, data=body, method="POST",
                                         headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                LOG.info("webhook -> HTTP %s", response.status)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            LOG.error("webhook failed: %s", exc)

    def send_summary(self, text: str, context: dict) -> None:
        self.send([], {**context, "summary": text})


class NtfyNotifier(Notifier):
    name = "ntfy"

    def send(self, events: list[Event], context: dict) -> None:
        url = resolve_secret(self.config.get("url", ""))
        if not url:
            LOG.error("ntfy notifier has no url")
            return

        lines = [f"{event.type.upper()} {event.kind}: {event.path}"
                 for event in events[:25]]
        self._post(url, "\n".join(lines),
                   f"mirrorwatch: {len(events)} change(s)")

    def send_summary(self, text: str, context: dict) -> None:
        url = resolve_secret(self.config.get("url", ""))
        if url:
            self._post(url, text, "mirrorwatch: baseline established")

    def _post(self, url: str, body: str, title: str) -> None:
        if self.dry_run:
            LOG.info("[dry-run] ntfy %s: %s", title, body.replace("\n", " | "))
            return
        headers = {"Title": title,
                   "Priority": str(self.config.get("priority", 3)),
                   "Tags": ",".join(self.config.get("tags", ["page_facing_up"]))}
        token = resolve_secret(self.config.get("token", ""))
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=body.encode("utf-8"),
                                         method="POST", headers=headers)
        try:
            urllib.request.urlopen(request, timeout=30)
            LOG.info("ntfy sent")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            LOG.error("ntfy failed: %s", exc)


class TelegramNotifier(Notifier):
    name = "telegram"
    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, config: dict, dry_run: bool = False):
        super().__init__(config, dry_run)
        self.token = resolve_secret(config.get("token", ""))
        self.chat_id = str(resolve_secret(config.get("chat_id", "")))
        self.thread_id = str(resolve_secret(config.get("thread_id", "")) or "")
        self.send_files = config.get("send_files", True)
        self.max_bytes = int(float(config.get("max_mb", 45)) * 1024 * 1024)
        self.silent = bool(config.get("silent", False))
        self.pause = float(config.get("pause_seconds", 1.2))

    # -- transport -------------------------------------------------------
    def _post(self, method: str, body: bytes, content_type: str) -> dict | None:
        if not self.token or not self.chat_id:
            LOG.error("telegram notifier misconfigured (token/chat_id missing)")
            return None

        url = self.API.format(token=self.token, method=method)
        for attempt in range(4):
            request = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": content_type})
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                if exc.code == 429:
                    try:
                        wait = int(json.loads(raw)["parameters"]["retry_after"])
                    except Exception:                       # noqa: BLE001
                        wait = 5
                    LOG.warning("telegram rate limited, waiting %ss", wait)
                    time.sleep(wait + 1)
                    continue
                LOG.error("telegram %s HTTP %s: %s", method, exc.code, raw[:300])
                return None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                LOG.warning("telegram %s network error (%s/4): %s",
                            method, attempt + 1, exc)
                time.sleep(3 * (attempt + 1))
        return None

    def _fields(self) -> dict:
        fields = {"chat_id": self.chat_id, "parse_mode": "HTML"}
        if self.thread_id:
            fields["message_thread_id"] = self.thread_id
        if self.silent:
            fields["disable_notification"] = "true"
        return fields

    def _message(self, text: str) -> None:
        if self.dry_run:
            LOG.info("[dry-run] telegram message:\n%s", text)
            return
        fields = self._fields()
        fields["text"] = text[:4096]
        fields["link_preview_options"] = json.dumps({"is_disabled": True})
        result = self._post("sendMessage",
                            json.dumps(fields).encode("utf-8"),
                            "application/json")
        if result and not result.get("ok"):
            LOG.error("telegram rejected message: %s", result)

    def _document(self, data: bytes, filename: str, caption: str) -> bool:
        if self.dry_run:
            LOG.info("[dry-run] telegram document %s (%s):\n%s",
                     filename, human_size(len(data)), caption)
            return True
        fields = self._fields()
        fields["caption"] = caption[:1024]
        body, content_type = multipart(
            fields, [("document", filename, data, guess_mime(filename))])
        result = self._post("sendDocument", body, content_type)
        if result and result.get("ok"):
            return True
        LOG.error("telegram sendDocument failed for %s: %s", filename, result)
        return False

    # -- rendering -------------------------------------------------------
    @staticmethod
    def _caption(event: Event) -> str:
        heading = {
            NEW: "\U0001f195 <b>Neue Datei</b>",
            CHANGED: "\U0001f504 <b>Datei aktualisiert</b>",
        }.get(event.type, f"<b>{html_escape(event.type)}</b>")

        name = html_escape(event.filename or leaf(event.path))
        parts = [heading, f"<b>{name}</b>"]

        meta = []
        if event.size is not None:
            meta.append(de_size(event.size))
        if event.last_modified:
            meta.append(de_datetime(event.last_modified))
        if meta:
            parts.append("  \u00b7  ".join(meta))

        footer = f"\U0001f4c2 {html_escape(event.source)}"
        if event.url:
            footer += (f"  \u00b7  \U0001f517 "
                       f'<a href="{html_escape(event.url)}">Quelle</a>')
        parts.append(footer)

        if event.type == CHANGED and event.previous_modified:
            parts.append(f"<i>zuvor: {de_datetime(event.previous_modified)}</i>")
        return "\n".join(parts)

    def send(self, events: list[Event], context: dict) -> None:
        for event in events:
            if event.kind == "file" and event.type in (NEW, CHANGED):
                self._send_file_event(event)
            elif event.kind == "dir":
                self._send_dir_event(event)
            elif event.type == GONE:
                art = "Datei" if event.kind == "file" else "Verzeichnis"
                self._message(
                    f"\U0001f5d1 <b>Nicht mehr verfügbar</b>\n"
                    f"<b>{html_escape(leaf(event.path))}</b>\n"
                    f"<code>{html_escape(event.path)}</code>\n"
                    f"\U0001f4c2 {html_escape(event.source)} · {art}")
            time.sleep(self.pause)

    def _send_file_event(self, event: Event) -> None:
        caption = self._caption(event)
        too_big = event.size is not None and event.size > self.max_bytes
        sent = False
        if self.send_files and event.payload and not too_big:
            sent = self._document(event.payload,
                                  event.filename or "download.bin", caption)
        if not sent:
            note = ""
            if too_big:
                note = (f"\n<i>{human_size(event.size)} exceeds the Telegram "
                        f"upload limit; the mirror has it.</i>")
            self._message(caption + note)

    @staticmethod
    def _probe_note(event: Event) -> str:
        """What the name probing inside this directory turned up.

        Saying "unknown" and stopping there is a dead end for the reader. Say
        how hard mirrorwatch looked instead, so "nothing found" reads as a
        result rather than a shrug — and so it is obvious when the fix is to
        add the name to ``dir_probe.names``.
        """
        if event.probed is None:
            return ("<i>ℹ️ Kein Listing verfügbar – welche Datei "
                    "sich geändert hat, ist unbekannt.</i>")
        if event.found == 1:
            return (f"<i>🔍 {event.probed} Namen geprüft · "
                    f"1 neuer Eintrag gefunden – siehe unten.</i>")
        if event.found:
            return (f"<i>🔍 {event.probed} Namen geprüft · "
                    f"{event.found} neue Einträge gefunden – "
                    f"siehe unten.</i>")
        return (f"<i>🔍 {event.probed} Namen geprüft, kein Treffer. "
                f"Der Server bietet kein Listing – der geänderte "
                f"Eintrag heißt anders als alles, was mirrorwatch kennt. "
                f"Bekannter Name? Ab damit in "
                f"<code>dir_probe.names</code>.</i>")

    def _send_dir_event(self, event: Event) -> None:
        new = event.type == NEW
        heading = ("\U0001f4c1 <b>Neues Verzeichnis</b>" if new
                   else "\U0001f504 <b>Verzeichnis geändert</b>")
        parts = [heading,
                 f"<b>{html_escape(leaf(event.path))}</b>",
                 f"<code>{html_escape(event.path)}</code>"]

        when = de_datetime(event.last_modified, with_time=not new)
        source = f"\U0001f4c2 {html_escape(event.source)}"
        parts.append(f"\U0001f5d3 {when}  ·  {source}" if when else source)

        if not new and event.previous_modified:
            parts.append("<i>zuvor: "
                         f"{de_datetime(event.previous_modified, with_time=True)}</i>")
        if not new or event.probed is not None:
            parts.append(self._probe_note(event))
        self._message("\n".join(parts))

    def send_summary(self, text: str, context: dict) -> None:
        self._message(text)


BACKENDS = {
    "telegram": TelegramNotifier,
    "webhook": WebhookNotifier,
    "ntfy": NtfyNotifier,
    "stdout": StdoutNotifier,
}


def build_notifiers(config: dict, dry_run: bool = False) -> dict:
    notifiers = {}
    for name, spec in (config or {}).items():
        if spec.get("enabled") is False:
            continue
        backend_type = spec.get("type", name)
        backend = BACKENDS.get(backend_type)
        if backend is None:
            LOG.error("unknown notifier type %r for %r (known: %s)",
                      backend_type, name, ", ".join(sorted(BACKENDS)))
            continue
        notifiers[name] = backend(spec, dry_run=dry_run)
    return notifiers
