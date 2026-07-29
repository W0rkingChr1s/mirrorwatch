"""Test suite. Runs entirely offline against a local HTTP server."""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socketserver
import tempfile
import threading
import unittest
from email.utils import formatdate

from mirrorwatch.config import ConfigError, load
from mirrorwatch.detect import KIND_DIR, KIND_FILE, KIND_MISSING, classify, validate_rules
from mirrorwatch.fetch import HttpClient, Response
from mirrorwatch.sources import IndexSource, ProbeSource, dedupe
from mirrorwatch.util import resolve_secret, safe_relpath

# ---------------------------------------------------------------------------
# A tiny configurable server so the tests need no network.
# ---------------------------------------------------------------------------

ROUTES: dict = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _route(self):
        return ROUTES.get(self.path)

    def _emit(self, include_body: bool):
        route = self._route()
        if route is None:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            if include_body:
                self.wfile.write(b"not found")
            return

        body = route["body"]
        self.send_response(route.get("status", 200))
        self.send_header("Content-Type", route.get("content_type", "text/plain"))
        self.send_header("Content-Length", str(len(body)))
        if route.get("last_modified"):
            self.send_header("Last-Modified", route["last_modified"])
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def do_GET(self):       # noqa: N802
        self._emit(True)

    def do_HEAD(self):      # noqa: N802
        self._emit(False)


class ServerFixture:
    def __init__(self):
        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def route(path: str, body: bytes, content_type: str = "application/pdf",
          last_modified: str | None = None, status: int = 200):
    ROUTES[path] = {"body": body, "content_type": content_type,
                    "status": status,
                    "last_modified": last_modified or formatdate(usegmt=True)}


# ---------------------------------------------------------------------------


class TestDetect(unittest.TestCase):
    def test_broken_php_404_is_recognised_as_missing(self):
        """The proWIN case: HTTP 200 with a tiny HTML body means 'not there'."""
        rules = {
            "missing": [{"content_type": "text/html"}],
            "directory": [{"content_type": "directory"}],
            "default": "file",
        }
        html = Response("u", 200, {"content-type": "text/html; charset=UTF-8"})
        self.assertEqual(classify(html, rules), KIND_MISSING)

        directory = Response("u", 200, {"content-type": "directory"})
        self.assertEqual(classify(directory, rules), KIND_DIR)

        pdf = Response("u", 200, {"content-type": "application/pdf"})
        self.assertEqual(classify(pdf, rules), KIND_FILE)

    def test_default_rules_use_status_codes(self):
        self.assertEqual(classify(Response("u", 404, {})), KIND_MISSING)
        self.assertEqual(
            classify(Response("u", 200, {"content-type": "application/pdf"})),
            KIND_FILE)

    def test_non_2xx_never_counts_as_a_file(self):
        self.assertEqual(classify(Response("u", 500, {})), KIND_MISSING)

    def test_network_error_is_its_own_kind(self):
        self.assertEqual(classify(Response("u", 0, {}, None, "timed out")), "error")

    def test_size_criteria_ignored_when_length_unknown(self):
        rules = {"missing": [{"content_type": "text/html", "max_size": 64}]}
        no_length = Response("u", 200, {"content-type": "text/html"})
        self.assertEqual(classify(no_length, rules), KIND_MISSING)
        too_big = Response("u", 200, {"content-type": "text/html",
                                      "content-length": "9000"})
        self.assertNotEqual(classify(too_big, rules), KIND_MISSING)

    def test_validate_flags_empty_rule(self):
        problems = validate_rules({"missing": [{}]}, "sources[0]")
        self.assertTrue(any("match everything" in p for p in problems))

    def test_validate_flags_unknown_criterion(self):
        problems = validate_rules({"missing": [{"colour": "red"}]}, "sources[0]")
        self.assertTrue(any("unknown criterion" in p for p in problems))


class TestUtil(unittest.TestCase):
    def test_safe_relpath_blocks_traversal(self):
        self.assertEqual(safe_relpath("../../etc/passwd"), os.path.join("etc", "passwd"))
        # ".." segments are dropped, not resolved: the result can never escape
        self.assertEqual(safe_relpath("/a/../../b"), os.path.join("a", "b"))
        self.assertNotIn("..", safe_relpath("a/../../../../x"))

    def test_safe_relpath_strips_query(self):
        self.assertEqual(safe_relpath("a/b.pdf?x=1"), os.path.join("a", "b.pdf"))

    def test_resolve_secret_from_env(self):
        os.environ["MIRRORWATCH_TEST_SECRET"] = "  hunter2  "
        self.assertEqual(resolve_secret("env:MIRRORWATCH_TEST_SECRET"), "hunter2")
        self.assertEqual(resolve_secret("plain"), "plain")

    def test_resolve_secret_from_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
            handle.write("from-file\n")
            path = handle.name
        try:
            self.assertEqual(resolve_secret(f"file:{path}"), "from-file")
        finally:
            os.unlink(path)


class TestProbeExpansion(unittest.TestCase):
    def test_year_template(self):
        out = ProbeSource.expand([
            {"template": "flyer{yyyy}.pdf", "years": {"from": 2024, "to": 2026}}])
        self.assertEqual(out, ["flyer2024.pdf", "flyer2025.pdf", "flyer2026.pdf"])

    def test_value_template(self):
        out = ProbeSource.expand([
            {"template": "{v}/index.pdf", "values": ["a", "b"]}])
        self.assertEqual(out, ["a/index.pdf", "b/index.pdf"])

    def test_plain_strings_pass_through(self):
        self.assertEqual(ProbeSource.expand(["a.pdf"]), ["a.pdf"])

    def test_query_style_base_url_is_not_slash_joined(self):
        source = ProbeSource({"name": "x", "type": "probe",
                              "base_url": "https://h/p.php?file="})
        self.assertEqual(source._url("a/b.pdf"), "https://h/p.php?file=a/b.pdf")

    def test_path_style_base_url_is_slash_joined(self):
        source = ProbeSource({"name": "x", "type": "probe",
                              "base_url": "https://h/files/"})
        self.assertEqual(source._url("a/b.pdf"), "https://h/files/a/b.pdf")

    def test_dedupe_keeps_first(self):
        from mirrorwatch.sources import Target
        targets = [Target("a", "u1", "a"), Target("a", "u2", "a"),
                   Target("b", "u3", "b")]
        self.assertEqual([t.url for t in dedupe(targets)], ["u1", "u3"])


class TestIndexSource(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ROUTES.clear()
        cls.server = ServerFixture()
        base = cls.server.base
        route("/docs/", (
            f'<a href="a.pdf">a</a>'
            f'<a href="{base}/docs/b.pdf">b</a>'
            f'<a href="draft-c.pdf">c</a>'
            f'<a href="sub/">sub</a>'
            f'<a href="notes.txt">txt</a>'
            f'<a href="mailto:x@y.z">mail</a>'
            f'<a href="https://elsewhere.invalid/d.pdf">other host</a>'
        ).encode(), content_type="text/html")
        route("/docs/sub/", b'<a href="deep.pdf">deep</a>', content_type="text/html")

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        ROUTES.clear()

    def _source(self, **overrides):
        spec = {"name": "idx", "type": "index",
                "url": f"{self.server.base}/docs/",
                "match": r"\.pdf$", "exclude": "draft"}
        spec.update(overrides)
        return IndexSource(spec)

    def test_matches_filters_and_resolves_relative_links(self):
        targets = self._source().targets(HttpClient(retries=0))
        urls = sorted(t.url for t in targets)
        self.assertEqual(urls, [f"{self.server.base}/docs/a.pdf",
                                f"{self.server.base}/docs/b.pdf"])

    def test_recursion_finds_nested_files(self):
        source = self._source(recursive={"depth": 1, "match": r"/sub/$"})
        urls = sorted(t.url for t in source.targets(HttpClient(retries=0)))
        self.assertIn(f"{self.server.base}/docs/sub/deep.pdf", urls)

    def test_other_hosts_are_skipped_by_default(self):
        targets = self._source().targets(HttpClient(retries=0))
        self.assertFalse(any("elsewhere.invalid" in t.url for t in targets))


class TestFullRun(unittest.TestCase):
    def setUp(self):
        ROUTES.clear()
        self.server = ServerFixture()
        self.tmp = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmp, "config.json")
        config = {
            "request_delay_ms": 0,
            "bootstrap_notify": "none",
            "state_file": os.path.join(self.tmp, "state.json"),
            "mirror": {"enabled": True,
                       "dir": os.path.join(self.tmp, "mirror"),
                       "archive_dir": os.path.join(self.tmp, "archive"),
                       "keep_versions": True},
            "notifiers": {"log": {"type": "stdout"}},
            "sources": [{
                "name": "local",
                "type": "urls",
                "urls": [f"{self.server.base}/a.pdf"],
            }],
        }
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)

        route("/a.pdf", b"version one",
              last_modified=formatdate(1_600_000_000, usegmt=True))

    def tearDown(self):
        self.server.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        ROUTES.clear()

    def _run(self):
        from mirrorwatch.core import Runner
        return Runner(load(self.config_path)).run_once()

    def test_lifecycle(self):
        # 1. first run establishes the baseline and mirrors the file
        summary = self._run()
        self.assertEqual(summary["events"], 1)
        found = []
        for root, _dirs, files in os.walk(os.path.join(self.tmp, "mirror")):
            found += [os.path.join(root, f) for f in files]
        self.assertEqual(len(found), 1)
        with open(found[0], "rb") as handle:
            self.assertEqual(handle.read(), b"version one")

        # 2. nothing moved -> silence
        self.assertEqual(self._run()["events"], 0)

        # 3. Last-Modified bumped but bytes identical -> still silence
        route("/a.pdf", b"version one",
              last_modified=formatdate(1_700_000_000, usegmt=True))
        self.assertEqual(self._run()["events"], 0)

        # 4. content actually changes -> one event, old version archived
        route("/a.pdf", b"version two is longer",
              last_modified=formatdate(1_700_000_001, usegmt=True))
        self.assertEqual(self._run()["events"], 1)
        archived = []
        for root, _dirs, files in os.walk(os.path.join(self.tmp, "archive")):
            archived += [os.path.join(root, f) for f in files]
        self.assertEqual(len(archived), 1)
        with open(archived[0], "rb") as handle:
            self.assertEqual(handle.read(), b"version one")

        # 5. file disappears -> gone event
        ROUTES.clear()
        self.assertEqual(self._run()["events"], 1)

    def test_network_failure_never_reports_gone(self):
        self._run()
        self.server.stop()
        summary = self._run()
        self.assertEqual(summary["events"], 0)
        self.assertGreaterEqual(summary["errors"], 1)


class TestConfigValidation(unittest.TestCase):
    def _write(self, payload) -> str:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(payload, handle)
        handle.close()
        return handle.name

    def test_rejects_config_without_sources(self):
        path = self._write({"sources": []})
        with self.assertRaises(ConfigError) as ctx:
            load(path)
        self.assertIn("no sources", str(ctx.exception))
        os.unlink(path)

    def test_rejects_unknown_notifier_reference(self):
        path = self._write({
            "notifiers": {"log": {"type": "stdout"}},
            "sources": [{"name": "s", "type": "urls",
                         "urls": ["http://x/y"], "notify": ["nope"]}]})
        with self.assertRaises(ConfigError) as ctx:
            load(path)
        self.assertIn("unknown notifier", str(ctx.exception))
        os.unlink(path)

    def test_rejects_probe_source_without_targets(self):
        path = self._write({"sources": [{"name": "s", "type": "probe",
                                         "base_url": "http://x/"}]})
        with self.assertRaises(ConfigError):
            load(path)
        os.unlink(path)

    def test_inline_config_json_env(self):
        os.environ["MIRRORWATCH_CONFIG_JSON"] = json.dumps({
            "sources": [{"name": "s", "type": "urls", "urls": ["http://x/y"]}],
            "interval_seconds": 4242,
        })
        try:
            # No file needed: the path does not exist and is ignored.
            config = load("/nonexistent/config.json")
            self.assertEqual(config["interval_seconds"], 4242)
            self.assertEqual(config["sources"][0]["name"], "s")
        finally:
            del os.environ["MIRRORWATCH_CONFIG_JSON"]

    def test_inline_config_json_rejects_garbage(self):
        os.environ["MIRRORWATCH_CONFIG_JSON"] = "{not json"
        try:
            with self.assertRaises(ConfigError) as ctx:
                load("/nonexistent/config.json")
            self.assertIn("MIRRORWATCH_CONFIG_JSON", str(ctx.exception))
        finally:
            del os.environ["MIRRORWATCH_CONFIG_JSON"]

    def test_env_override_wins(self):
        path = self._write({"sources": [{"name": "s", "type": "urls",
                                         "urls": ["http://x/y"]}],
                            "interval_seconds": 100})
        os.environ["MIRRORWATCH_INTERVAL"] = "555"
        try:
            self.assertEqual(load(path)["interval_seconds"], 555)
        finally:
            del os.environ["MIRRORWATCH_INTERVAL"]
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
