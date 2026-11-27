import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fc2_monitor as m


class DiscoveryFix12Tests(unittest.TestCase):
    def setUp(self):
        self.now = 1800000000
        self.sources = ("missav", "javdb", "supjav")
        self.cursors = dict(missav_page=29, javdb_page=41, supjav_page=57,
                            recovery_until={"missav_page": 60},
                            last_discovery={"checked_at": "previous"})
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.github_output = root / "github-output"
        self.github_output.touch()
        for name in ("CRAWL_FILE", "SOURCE_COOLDOWN_FILE", "FETCH_STATE_FILE", "DATA_FILE",
                     "HISTORY_FILE", "VIEWS_HISTORY_FILE", "NOTIFY_QUEUE_FILE"):
            self.enterContext(patch.object(m, name, root / getattr(m, name).name))
        self.enterContext(patch.dict(m.os.environ, {
            "MISSAV_GUARD_STATE_FILE": str(root / "guard.json"),
            "GITHUB_OUTPUT": str(self.github_output),
        }))
        self.enterContext(patch.dict(m.SOURCE_RETRY_AT, {}, clear=True))
        self.enterContext(patch.dict(m.CHANNEL_RETRY_AT, {}, clear=True))
        self.enterContext(patch.object(m, "now_ts", return_value=self.now))
        self.enterContext(patch.object(m, "initialize_run_deadline"))
        self.enterContext(patch.object(m, "run_deadline_reached", return_value=False))
        self.enterContext(patch.object(m, "run_optional_step"))
        self.http = self.enterContext(patch.object(m, "guarded_get", side_effect=AssertionError("Mock HTTP")))
        self.stdout = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        m.save_json(m.CRAWL_FILE, self.cursors)

    def reply(self, statuses):
        def response(url, purpose, **kwargs):
            return SimpleNamespace(status_code=statuses[purpose], text="", url=url, headers={})
        self.http.side_effect = response

    def assert_saved_deadlines(self, sources=None, *, channels=None):
        self.assertEqual(json.loads(m.SOURCE_COOLDOWN_FILE.read_text()),
                         {"source_retry_at": sources or {}, "channel_retry_at": channels or {}})

    def denied_channels(self, *sources):
        deadline = self.now + m.DISCOVERY_RETRY_SEC
        routes = {"missav": ("search", "catalog"), "javdb": ("search",), "supjav": ("category",)}
        return {source: dict.fromkeys(routes[source], deadline) for source in sources}

    def test_all_denied_returns_zero_without_writing_crawl(self):
        self.reply(dict.fromkeys(self.sources, 403))
        before = m.CRAWL_FILE.read_bytes()
        self.assertEqual(m.main(discovery_only=True), 0)
        self.assertEqual(self.github_output.read_text(), "all_sources_denied=true\n")
        self.assertEqual(m.CRAWL_FILE.read_bytes(), before)
        self.assert_saved_deadlines(channels=self.denied_channels(*self.sources))
        self.assertIn("[main discovery] all sources access_limited; no items fetched (exit 0)",
                      self.stdout.getvalue().splitlines())
        self.assertTrue(any(line.startswith("::warning::") for line in self.stdout.getvalue().splitlines()))
        self.assertEqual(m.DISCOVERY_FETCH_OUTCOMES["denied"], 4)
        self.http.reset_mock()
        self.assertEqual(m.main(discovery_only=True), 0)
        self.http.assert_not_called()
        self.assertEqual(m.DISCOVERY_FETCH_OUTCOMES, {"cooldown_skip": 4})

    def test_two_denials_and_checked_source_return_one(self):
        self.reply({"missav": 403, "javdb": 429, "supjav": 200})
        before = m.CRAWL_FILE.read_bytes()
        self.assertEqual(m.main(discovery_only=True), 1)
        self.assertEqual(self.github_output.read_bytes(), b"")
        deadlines = self.denied_channels("missav", "javdb")
        self.assert_saved_deadlines(channels=deadlines)
        self.assertEqual(m.load_json(m.CRAWL_FILE, {}), {
            **self.cursors, "source_retry_at": {}, "channel_retry_at": deadlines,
        })
        self.assertNotIn("::warning::", self.stdout.getvalue())

        # Preserve publishes the cooldown file while crawl state reverts to the run's start.
        m.CRAWL_FILE.write_bytes(before)
        m.SOURCE_RETRY_AT.clear()
        m.CHANNEL_RETRY_AT.clear()
        self.http.reset_mock()
        _, crawl = m.get_latest_videos()
        self.assertEqual({call.args[1] for call in self.http.call_args_list}, {"supjav"})
        for source in self.sources:
            key = f"{source}_page"
            self.assertEqual(crawl[key], self.cursors[key])
        self.assertEqual(m.CRAWL_FILE.read_bytes(), before)

    def test_github_output_write_failure_logs_error_and_returns_one(self):
        self.reply(dict.fromkeys(self.sources, 403))
        before = m.CRAWL_FILE.read_bytes()
        with patch.dict(m.os.environ, {"GITHUB_OUTPUT": str(self.github_output.parent)}):
            self.assertEqual(m.main(discovery_only=True), 1)
        self.assertIn("::error::[main discovery] could not write GITHUB_OUTPUT", self.stderr.getvalue())
        self.assertEqual(m.CRAWL_FILE.read_bytes(), before)
        self.assert_saved_deadlines(channels=self.denied_channels(*self.sources))

    def test_all_denied_without_github_output_returns_zero(self):
        self.reply(dict.fromkeys(self.sources, 403))
        with patch.dict(m.os.environ):
            m.os.environ.pop("GITHUB_OUTPUT")
            self.assertEqual(m.main(discovery_only=True), 0)

    def test_mixed_fetch_outcomes_return_one(self):
        for source, first_status in (("missav", None), ("javdb", None), ("missav", 200)):
            with self.subTest(source=source, first_status=first_status):
                self.http.reset_mock()
                first = m.MISSAV_URL if source == "missav" else m.JAVDB_URL

                def response(url, purpose, **kwargs):
                    if url == first:
                        if first_status is None:
                            raise m.requests.ConnectionError("connection failed")
                        return SimpleNamespace(status_code=first_status, url=url, headers={},
                                               text="<html>" + " " * 2100 + "</html>")
                    return SimpleNamespace(status_code=403, url=url, headers={}, text="")

                with patch.object(m, "SOURCE_COOLDOWN_FILE",
                                  m.SOURCE_COOLDOWN_FILE.with_name(f"{source}-{first_status}.json")):
                    m.save_json(m.CRAWL_FILE, self.cursors)
                    self.http.side_effect = response
                    self.assertEqual(m.main(discovery_only=True), 1)
                    channels = self.denied_channels(*self.sources)
                    if source == "missav":
                        del channels["missav"]["search"]
                    self.assert_saved_deadlines(channels=channels)
                    self.assertEqual(m.DISCOVERY_FETCH_OUTCOMES["other"], 1)
                    self.assertEqual(m.DISCOVERY_FETCH_OUTCOMES["denied"], 3 if source == "missav" else 4)
                    self.assertEqual(self.github_output.read_bytes(), b"")

    def test_error_report_returns_one_even_with_three_deadlines(self):
        self.reply(dict.fromkeys(self.sources, 403))

        def fail_after_denial(pages, report):
            m.record_source_denial(m.SUPJAV_URL, 403)
            raise RuntimeError("supjav parser error")

        with patch.object(m, "scrape_supjav", side_effect=fail_after_denial):
            self.assertEqual(m.main(discovery_only=True), 1)
        self.assert_saved_deadlines(channels=self.denied_channels(*self.sources))
        self.assertIn('"status": "error"', self.stdout.getvalue())
        self.assertNotIn("::warning::", self.stdout.getvalue())

    def test_discovery_exception_saves_only_deadlines_and_returns_one(self):
        def fail():
            m.SOURCE_RETRY_AT.update(missav=self.now + 60, javdb=self.now)
            raise RuntimeError("collection failed")

        with patch.object(m, "get_latest_videos", side_effect=fail):
            self.assertEqual(m.main(discovery_only=True), 1)
        self.assert_saved_deadlines({"missav": self.now + 60})
        crawl = m.load_json(m.CRAWL_FILE, {})
        crawl.pop("source_retry_at")
        self.assertEqual(crawl.pop("channel_retry_at"), {})
        self.assertEqual(crawl, self.cursors)

    def test_empty_discovery_saves_empty_deadlines_and_returns_one(self):
        self.reply(dict.fromkeys(self.sources, 200))
        self.assertEqual(m.main(discovery_only=True), 1)
        self.assert_saved_deadlines({})
        self.assertEqual(m.load_json(m.CRAWL_FILE, {}), self.cursors)

    def test_discovery_exception_before_loading_preserves_active_deadlines(self):
        m.save_json(m.SOURCE_COOLDOWN_FILE, {
            "source_retry_at": {"missav": self.now + 60, "javdb": self.now, "supjav": "invalid"},
        })
        with patch.object(m, "get_latest_videos", side_effect=RuntimeError("before cooldown load")):
            self.assertEqual(m.main(discovery_only=True), 1)
        self.assert_saved_deadlines({"missav": self.now + 60})
        self.assertEqual(m.SOURCE_RETRY_AT, {})

    def test_saving_uses_maximum_active_deadline_per_source(self):
        m.save_json(m.SOURCE_COOLDOWN_FILE, {
            "source_retry_at": {"missav": self.now + 60, "javdb": self.now + 10},
        })
        m.SOURCE_RETRY_AT.update(missav=self.now + 30, javdb=self.now + 20)
        m.save_source_cooldown_file()
        self.assert_saved_deadlines({"missav": self.now + 60, "javdb": self.now + 20})

    def test_cooldown_read_oserror_still_attempts_save_and_propagates_write_error(self):
        m.SOURCE_COOLDOWN_FILE.mkdir()
        error = PermissionError("cannot save cooldowns")
        with patch.object(m, "save_json", side_effect=error) as save:
            with self.assertRaises(PermissionError) as caught:
                m.save_source_cooldown_file()
        self.assertIs(caught.exception, error)
        save.assert_called_once_with(m.SOURCE_COOLDOWN_FILE, {"source_retry_at": {}, "channel_retry_at": {}})

    def test_state_validation_failure_returns_one_before_discovery(self):
        m.CRAWL_FILE.write_text("[]", encoding="utf-8")
        with patch.object(m, "get_latest_videos") as discovery:
            self.assertEqual(m.main(discovery_only=True), 1)
        discovery.assert_not_called()
        self.assertFalse(m.SOURCE_COOLDOWN_FILE.exists())

    def test_discovery_structure_error_saves_deadlines_without_writing_crawl(self):
        before = m.CRAWL_FILE.read_bytes()

        def fail():
            m.SOURCE_RETRY_AT["missav"] = self.now + 60
            raise m.StateStructureError("invalid discovery state")

        with patch.object(m, "get_latest_videos", side_effect=fail):
            self.assertEqual(m.main(discovery_only=True), 1)
        self.assert_saved_deadlines({"missav": self.now + 60})
        self.assertEqual(m.CRAWL_FILE.read_bytes(), before)
        self.assertIn("状態読込失敗", self.stderr.getvalue())

    def test_full_mode_all_denied_returns_one(self):
        self.reply(dict.fromkeys(self.sources, 403))
        self.assertEqual(m.main(discovery_only=False), 1)
        self.assert_saved_deadlines(channels=self.denied_channels(*self.sources))
        self.assertNotIn("::warning::", self.stdout.getvalue())

    def test_file_only_deadlines_skip_requests_and_expired_deadlines_do_not(self):
        self.reply(dict.fromkeys(self.sources, 200))
        deadlines = {"missav": self.now + 100, "javdb": self.now, "supjav": self.now - 1}
        m.save_json(m.SOURCE_COOLDOWN_FILE, {"source_retry_at": deadlines})
        _, crawl = m.get_latest_videos()
        self.assertEqual(crawl["source_retry_at"], {"missav": deadlines["missav"]})
        self.assertEqual(crawl["last_discovery"]["sources"]["missav"]["status"], "access_limited")
        self.assertEqual({call.args[1] for call in self.http.call_args_list}, {"javdb", "supjav"})

    def test_legacy_deadlines_and_per_source_max(self):
        self.reply(dict.fromkeys(self.sources, 200))
        legacy = {"missav": self.now + 90, "javdb": self.now + 80}
        m.save_json(m.CRAWL_FILE, {**self.cursors, "source_retry_at": legacy})
        _, crawl = m.get_latest_videos()
        self.assertEqual(crawl["source_retry_at"], legacy)
        self.assertEqual({call.args[1] for call in self.http.call_args_list}, {"supjav"})

        m.save_json(m.SOURCE_COOLDOWN_FILE,
                    {"source_retry_at": {"missav": self.now + 10, "javdb": self.now + 100}})
        _, crawl = m.get_latest_videos()
        self.assertEqual(crawl["source_retry_at"], {"missav": self.now + 90, "javdb": self.now + 100})

    def test_update_status_uses_max_deadline_including_pending_changes(self):
        m.save_json(m.SOURCE_COOLDOWN_FILE, {"source_retry_at": {"missav": self.now + 90,
                                                               "supjav": self.now}})
        crawl = {"source_retry_at": {"missav": self.now + 10, "javdb": self.now + 20},
                 "last_discovery": {"sources": {"supjav": {"status": "access_limited"}}}}
        m.SOURCE_RETRY_AT.update(missav=self.now + 60, javdb=self.now + 30)
        sources = m.build_update_status([], crawl, {})["sources"]
        self.assertEqual(sources["missav"]["next_retry_at"], self.now + 90)
        self.assertEqual(sources["javdb"]["next_retry_at"], self.now + 30)
        self.assertEqual(sources["supjav"]["next_retry_at"], 0)
        self.assertEqual(sources["supjav"]["status"], "retry_due")

    def test_success_saves_deadlines_recorded_after_discovery(self):
        item = m.enrich("8000001", "title", "JavDB", "https://javdb.com/v/test", None, None)

        def optional_step(label, action, *args, **kwargs):
            if label == "MissAV rankings":
                m.record_source_denial(m.MISSAV_URL, 403)

        with patch.object(m, "get_latest_videos", return_value=([item], dict(self.cursors))), \
             patch.object(m, "run_optional_step", side_effect=optional_step), \
             patch.object(m, "write_site"):
            self.assertEqual(m.main(), 0)
        deadlines = {"missav": {"search": self.now + m.DISCOVERY_RETRY_SEC}}
        self.assert_saved_deadlines(channels=deadlines)
        self.assertEqual(m.load_json(m.CRAWL_FILE, {})["channel_retry_at"], deadlines)

    def test_refresh_saves_denial_when_fill_missing_views_raises(self):
        item = m.enrich("8000001", "title", "JavDB", "https://javdb.com/v/test", None, None)
        m.save_json(m.DATA_FILE, {"items": [item]})
        self.reply(dict.fromkeys(self.sources, 403))
        error = RuntimeError("views failed")
        with patch.object(m, "fill_missing_views", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                m.refresh_update_metadata()
        self.assertIs(caught.exception, error)
        self.assert_saved_deadlines(channels={"missav": {"ranking": self.now + m.DISCOVERY_RETRY_SEC}})

    def test_main_saves_post_discovery_denial_when_write_site_raises(self):
        def response(url, purpose, **kwargs):
            if url == m.JAVDB_URL:
                return SimpleNamespace(status_code=200, url=url, headers={},
                                       text='<a href="/v/test">FC2-PPV-8000001 title</a>' + " " * 2100)
            status = 403 if purpose == "missav" and "?sort=" in url else 404
            return SimpleNamespace(status_code=status, url=url, headers={}, text="")

        self.http.side_effect = response

        def optional_step(label, action, *args, **kwargs):
            if label == "MissAV rankings":
                action()

        error = RuntimeError("site failed")
        with patch.object(m, "run_optional_step", side_effect=optional_step), \
             patch.object(m, "write_site", side_effect=error) as write_site:
            with self.assertRaises(RuntimeError) as caught:
                m.main()
        self.assertIs(caught.exception, error)
        self.assertEqual([item["code"] for item in write_site.call_args.args[0]], ["FC2-PPV-8000001"])
        self.assert_saved_deadlines(channels={"missav": {"ranking": self.now + m.DISCOVERY_RETRY_SEC}})

    def test_final_save_failure_preserves_in_flight_exception(self):
        for error in (None, RuntimeError("processing failed")):
            with self.subTest(error=error):
                save_error = OSError("save failed")
                with patch.object(m, "save_source_cooldown_file", side_effect=save_error):
                    with self.assertRaises(type(error or save_error)) as caught:
                        with m.persist_source_cooldowns():
                            if error is not None:
                                raise error
                self.assertIs(caught.exception, error or save_error)
        self.assertIn("[source cooldowns] warning: save failed", self.stderr.getvalue())

    def test_missing_corrupt_and_invalid_file_values_warn_and_keep_usable_deadlines(self):
        self.reply(dict.fromkeys(self.sources, 200))
        valid = {"missav": self.now + 60}
        cases = ((None, {}), ("{", {}), ("[]", {}), ('{"source_retry_at":[]}', {}),
                 (json.dumps({"source_retry_at": {**valid, "javdb": "invalid", "supjav": True}}), valid))
        for raw, expected in cases:
            with self.subTest(raw=raw):
                if raw is not None:
                    m.SOURCE_COOLDOWN_FILE.write_text(raw, encoding="utf-8")
                offset = self.stdout.tell()
                _, crawl = m.get_latest_videos()
                warning_lines = [line for line in self.stdout.getvalue()[offset:].splitlines()
                                 if line.startswith("[source cooldowns] warning:")]
                self.assertEqual(len(warning_lines), 1)
                self.assertEqual(crawl["source_retry_at"], expected)

    def test_saving_unchanged_deadlines_avoids_atomic_rewrite(self):
        m.SOURCE_RETRY_AT["missav"] = self.now + 60
        m.save_discovery_cooldowns()
        with patch.object(m, "save_json", wraps=m.save_json) as save:
            m.save_discovery_cooldowns()
        save.assert_not_called()
