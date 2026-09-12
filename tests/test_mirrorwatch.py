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
from datetime import date, datetime
from email.utils import formatdate

from mirrorwatch.config import ConfigError, load
from mirrorwatch.detect import KIND_DIR, KIND_FILE, KIND_MISSING, classify, validate_rules
from mirrorwatch.discover import (DirProbe, candidates, derived_names,
                                  validate_dir_probe, year_variants)
from mirrorwatch.fetch import HttpClient, Response
from mirrorwatch.schedule import (ScheduleError, format_times, next_run,
                                  parse_times, resolve_timezone, seconds_until)
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



class TestDirProbePlan(unittest.TestCase):
    def test_absent_means_off(self):
        """Probing costs requests, so it is never switched on by itself."""
        self.assertFalse(DirProbe(None).enabled)
        self.assertTrue(DirProbe({}).enabled)      # an empty object still opts in
        self.assertTrue(DirProbe(True).enabled)

    def test_never_overrides_enabled(self):
        self.assertFalse(DirProbe({"on": "never"}).enabled)
        self.assertFalse(DirProbe({"enabled": False}).enabled)

    def test_depth_gates_recursion(self):
        plan = DirProbe({"depth": 2})
        self.assertTrue(plan.probes_at(0))
        self.assertTrue(plan.probes_at(1))
        self.assertFalse(plan.probes_at(2))

    def test_year_variants_shift_the_year(self):
        this_year = date.today().year
        variants = year_variants(f"yellowweeks{this_year}.pdf", 1)
        self.assertIn(f"yellowweeks{this_year + 1}.pdf", variants)
        self.assertIn(f"yellowweeks{this_year - 1}.pdf", variants)
        # The name we already hold is not worth a request.
        self.assertNotIn(f"yellowweeks{this_year}.pdf", variants)
        self.assertEqual(year_variants("katalog.pdf", 2), [])

    def test_derived_names_follow_the_folder(self):
        """yellow-weeks/yellowweeks2026.pdf is a house style, not an accident."""
        names = derived_names("yellow-weeks", [".pdf"], 1)
        this_year = date.today().year
        self.assertIn(f"yellowweeks{this_year}.pdf", names)
        self.assertIn(f"yellow-weeks{this_year}.pdf", names)
        self.assertIn("yellowweeks.pdf", names)

    def test_configured_names_are_tried_first(self):
        plan = DirProbe({"names": ["preisliste.pdf"], "derive": True})
        order = candidates(plan, "flyer", ["alt.pdf"], [".pdf"])
        self.assertEqual(order[0], "preisliste.pdf")

    def test_candidates_are_unique_and_capped(self):
        plan = DirProbe({"names": ["a.pdf", "a.pdf", "b.pdf"]})
        self.assertEqual(candidates(plan, "d", [], [], limit=2), ["a.pdf", "b.pdf"])

    def test_nested_names_are_refused(self):
        plan = DirProbe({"names": ["a.pdf"]})
        self.assertNotIn("x/y.pdf", candidates(plan, "d", ["x/y.pdf"], []))

    def test_validation_catches_typos(self):
        self.assertEqual(validate_dir_probe({"on": "change"}, "s"), [])
        self.assertEqual(validate_dir_probe(True, "s"), [])
        for bad in ({"on": "sometimes"}, {"depth": -1}, {"learn": "yes"},
                    {"nmaes": []}, {"names": ["a/b.pdf"]}, {"names": "a.pdf"}):
            self.assertTrue(validate_dir_probe(bad, "s"),
                            f"{bad} should have been rejected")


class TestDirProbeRun(unittest.TestCase):
    """The proWIN case end to end: a directory that will not say what is in it."""

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
                "name": "blind",
                "type": "probe",
                "base_url": f"{self.server.base}/",
                "detect": {"missing": [{"content_type": "text/plain"}],
                           "directory": [{"content_type": "directory"}],
                           "default": "file"},
                "dirs": ["flyer"],
                "files": ["flyer/yellowweeks2026.pdf"],
                "dir_probe": {"max_probes": 400, "depth": 2},
            }],
        }
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)

    def tearDown(self):
        self.server.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        ROUTES.clear()

    def _dir(self, path: str, stamp: int):
        ROUTES[path] = {"body": b"", "content_type": "directory", "status": 200,
                        "last_modified": formatdate(stamp, usegmt=True)}

    def _run(self):
        from mirrorwatch.core import Runner
        return Runner(load(self.config_path)).run_once()

    def test_probing_finds_the_file_the_listing_will_not_name(self):
        year = date.today().year
        self._dir("/flyer", 1_700_000_000)
        route("/flyer/yellowweeks2026.pdf", b"the 2026 flyer",
              last_modified=formatdate(1_700_000_000, usegmt=True))

        # 1. baseline: the directory and the one configured file
        self._run()

        # 2. a new flyer appears under a name nobody configured, and the
        #    directory's mtime moves because of it
        self._dir("/flyer", 1_800_000_000)
        route(f"/flyer/yellowweeks{year + 1}.pdf", b"next year already",
              last_modified=formatdate(1_800_000_000, usegmt=True))

        summary = self._run()
        self.assertGreater(summary["probed"], 0)

        mirrored = []
        for root, _dirs, files in os.walk(os.path.join(self.tmp, "mirror")):
            mirrored += files
        self.assertIn(f"yellowweeks{year + 1}.pdf", mirrored)

    def test_a_discovered_file_keeps_being_watched(self):
        year = date.today().year
        found = f"/flyer/yellowweeks{year}.pdf"
        self._dir("/flyer", 1_700_000_000)
        route("/flyer/yellowweeks2026.pdf", b"seed",
              last_modified=formatdate(1_700_000_000, usegmt=True))
        self._run()

        self._dir("/flyer", 1_800_000_000)
        route(found, b"version one", last_modified=formatdate(1_800_000_000, usegmt=True))
        self._run()

        # The config never named it, so only the "discovered" flag can bring it
        # back — and it must, otherwise a found file is checked once and forgotten.
        route(found, b"version two", last_modified=formatdate(1_900_000_000, usegmt=True))
        summary = self._run()
        self.assertEqual(summary["events"], 1)

    def test_an_unchanged_directory_is_not_probed(self):
        self._dir("/flyer", 1_700_000_000)
        route("/flyer/yellowweeks2026.pdf", b"seed",
              last_modified=formatdate(1_700_000_000, usegmt=True))
        self._run()
        self.assertEqual(self._run()["probed"], 0)

    def test_the_budget_is_respected(self):
        self._dir("/flyer", 1_700_000_000)
        self._run()
        self._dir("/flyer", 1_800_000_000)

        with open(self.config_path, encoding="utf-8") as handle:
            config = json.load(handle)
        config["sources"][0]["dir_probe"]["max_probes"] = 3
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)

        self.assertEqual(self._run()["probed"], 3)

    def test_probing_reports_what_it_tried(self):
        """A dir event carries the probe counts, so the message can be honest."""
        from mirrorwatch.core import Runner
        self._dir("/flyer", 1_700_000_000)
        self._run()
        self._dir("/flyer", 1_800_000_000)

        captured = []
        runner = Runner(load(self.config_path))
        runner.notifiers["log"].send = lambda events, ctx: captured.extend(events)
        runner.run_once()

        dir_events = [e for e in captured if e.kind == KIND_DIR]
        self.assertEqual(len(dir_events), 1)
        self.assertGreater(dir_events[0].probed, 0)
        self.assertEqual(dir_events[0].found, 0)

    def test_an_unfinished_sweep_resumes_where_it_stopped(self):
        """A budget that ran out must not strand the rest of the list.

        Without a memo of what was already asked about, every run would spend
        its whole budget re-probing the same opening names and the tail would
        never be reached at all.
        """
        with open(self.config_path, encoding="utf-8") as handle:
            config = json.load(handle)
        config["sources"][0]["dir_probe"].update(
            {"max_probes": 2, "learn": False, "derive": False,
             "names": ["a.pdf", "b.pdf", "c.pdf", "d.pdf"]})
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)

        self._dir("/flyer", 1_700_000_000)
        route("/flyer/d.pdf", b"last on the list",
              last_modified=formatdate(1_700_000_000, usegmt=True))

        # Run one gets through a.pdf and b.pdf and runs out.
        self.assertEqual(self._run()["probed"], 2)
        mirrored = []
        for root, _dirs, files in os.walk(os.path.join(self.tmp, "mirror")):
            mirrored += files
        self.assertNotIn("d.pdf", mirrored)

        # Run two starts at c.pdf, so the file at the end of the list is found.
        self.assertEqual(self._run()["probed"], 2)
        mirrored = []
        for root, _dirs, files in os.walk(os.path.join(self.tmp, "mirror")):
            mirrored += files
        self.assertIn("d.pdf", mirrored)

    def test_a_change_makes_every_name_worth_asking_about_again(self):
        """The memo is a resume marker, not a permanent blacklist."""
        with open(self.config_path, encoding="utf-8") as handle:
            config = json.load(handle)
        config["sources"][0]["dir_probe"].update(
            {"max_probes": 50, "learn": False, "derive": False,
             "names": ["late.pdf"]})
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)

        self._dir("/flyer", 1_700_000_000)
        self._run()                        # late.pdf is asked about and absent

        # It shows up later, and the directory's timestamp moves with it.
        self._dir("/flyer", 1_800_000_000)
        route("/flyer/late.pdf", b"here now",
              last_modified=formatdate(1_800_000_000, usegmt=True))

        self._run()
        mirrored = []
        for root, _dirs, files in os.walk(os.path.join(self.tmp, "mirror")):
            mirrored += files
        self.assertIn("late.pdf", mirrored)

    def test_a_finished_sweep_is_not_repeated(self):
        self._dir("/flyer", 1_700_000_000)
        self._run()                       # budget is 400 here: the sweep finishes
        self.assertEqual(self._run()["probed"], 0)

    def test_probing_descends_into_directories_it_discovers(self):
        """Recursion is what makes a whole subtree reachable without a listing."""
        with open(self.config_path, encoding="utf-8") as handle:
            config = json.load(handle)
        config["sources"][0]["dir_probe"].update(
            {"depth": 3, "names": ["sub", "deep.pdf"],
             "learn": False, "derive": False})
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)

        self._dir("/flyer", 1_700_000_000)
        self._dir("/flyer/sub", 1_700_000_000)
        route("/flyer/sub/deep.pdf", b"two levels down",
              last_modified=formatdate(1_700_000_000, usegmt=True))

        self._run()
        mirrored = []
        for root, _dirs, files in os.walk(os.path.join(self.tmp, "mirror")):
            mirrored += files
        self.assertIn("deep.pdf", mirrored)

    def test_probing_stays_off_unless_configured(self):
        with open(self.config_path, encoding="utf-8") as handle:
            config = json.load(handle)
        del config["sources"][0]["dir_probe"]
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)

        self._dir("/flyer", 1_700_000_000)
        self._run()
        self._dir("/flyer", 1_800_000_000)
        self.assertEqual(self._run()["probed"], 0)


class TestSchedule(unittest.TestCase):
    def test_parses_list_and_comma_string(self):
        self.assertEqual(parse_times(["18:30", "06:00"]), [(6, 0), (18, 30)])
        self.assertEqual(parse_times("06:00, 18:30"), [(6, 0), (18, 30)])
        self.assertEqual(parse_times("07:00,07:00"), [(7, 0)])
        self.assertEqual(parse_times([]), [])
        self.assertEqual(parse_times(None), [])

    def test_rejects_nonsense_times(self):
        for bad in ["6", "06:60", "24:00", "noon", ["06.00"], 600]:
            with self.assertRaises(ScheduleError):
                parse_times(bad)

    def test_formats_times(self):
        self.assertEqual(format_times([(6, 0), (18, 30)]), "06:00, 18:30")

    def test_next_run_picks_the_next_slot_today(self):
        times = parse_times(["06:00", "18:00"])
        target = next_run(times, None, datetime(2026, 8, 19, 7, 0))
        self.assertEqual(target, datetime(2026, 8, 19, 18, 0))

    def test_next_run_rolls_over_to_tomorrow(self):
        times = parse_times(["06:00", "18:00"])
        target = next_run(times, None, datetime(2026, 8, 19, 19, 0))
        self.assertEqual(target, datetime(2026, 8, 20, 6, 0))

    def test_next_run_is_strictly_after_now(self):
        # Exactly on the slot means the run just happened; take the next one.
        times = parse_times(["06:00", "18:00"])
        target = next_run(times, None, datetime(2026, 8, 19, 6, 0))
        self.assertEqual(target, datetime(2026, 8, 19, 18, 0))

    def test_next_run_without_times_is_none(self):
        self.assertIsNone(next_run([], None, datetime(2026, 8, 19, 6, 0)))

    def test_seconds_until(self):
        now = datetime(2026, 8, 19, 17, 0)
        target = next_run(parse_times(["18:00"]), None, now)
        self.assertEqual(seconds_until(target, None, now), 3600.0)
        self.assertEqual(seconds_until(now, None, target), 0.0)

    def test_timezone_survives_the_dst_switch(self):
        zone = resolve_timezone("Europe/Berlin")
        # 2026-10-25 is the European autumn switch: the day is 25 hours long,
        # but 06:00 stays 06:00 and the wait grows accordingly.
        now = datetime(2026, 10, 24, 7, 0, tzinfo=zone)
        target = next_run(parse_times(["06:00"]), zone, now)
        self.assertEqual((target.hour, target.minute), (6, 0))
        self.assertEqual(target.date(), date(2026, 10, 25))
        self.assertEqual(seconds_until(target, zone, now), 24 * 3600.0)

    def test_unknown_timezone_is_reported(self):
        self.assertIsNone(resolve_timezone(None))
        with self.assertRaises(ScheduleError) as ctx:
            resolve_timezone("Mars/Olympus_Mons")
        self.assertIn("Mars/Olympus_Mons", str(ctx.exception))


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


    def test_check_times_are_normalised(self):
        path = self._write({"sources": [{"name": "s", "type": "urls",
                                         "urls": ["http://x/y"]}],
                            "check_times": ["18:30", "6:00"],
                            "timezone": "Europe/Berlin"})
        try:
            self.assertEqual(load(path)["check_times"], ["06:00", "18:30"])
        finally:
            os.unlink(path)

    def test_rejects_bad_check_times_and_timezone(self):
        path = self._write({"sources": [{"name": "s", "type": "urls",
                                         "urls": ["http://x/y"]}],
                            "check_times": ["25:00"],
                            "timezone": "Nowhere/Land"})
        try:
            with self.assertRaises(ConfigError) as ctx:
                load(path)
            self.assertIn("25:00", str(ctx.exception))
            self.assertIn("Nowhere/Land", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_schedule_env_overrides(self):
        path = self._write({"sources": [{"name": "s", "type": "urls",
                                         "urls": ["http://x/y"]}]})
        os.environ["MIRRORWATCH_CHECK_TIMES"] = "07:15,19:45"
        os.environ["MIRRORWATCH_TIMEZONE"] = "Europe/Berlin"
        os.environ["MIRRORWATCH_RUN_ON_START"] = "true"
        try:
            config = load(path)
            self.assertEqual(config["check_times"], ["07:15", "19:45"])
            self.assertEqual(config["timezone"], "Europe/Berlin")
            self.assertIs(config["run_on_start"], True)
        finally:
            for name in ("MIRRORWATCH_CHECK_TIMES", "MIRRORWATCH_TIMEZONE",
                         "MIRRORWATCH_RUN_ON_START"):
                del os.environ[name]
            os.unlink(path)

    def test_rejects_bad_run_on_start_env(self):
        path = self._write({"sources": [{"name": "s", "type": "urls",
                                         "urls": ["http://x/y"]}]})
        os.environ["MIRRORWATCH_RUN_ON_START"] = "perhaps"
        try:
            with self.assertRaises(ConfigError) as ctx:
                load(path)
            self.assertIn("MIRRORWATCH_RUN_ON_START", str(ctx.exception))
        finally:
            del os.environ["MIRRORWATCH_RUN_ON_START"]
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
