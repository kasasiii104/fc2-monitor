import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fc2_monitor as m


class StateValidationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(m.os.environ, {"RUNNER_TEMP": "", "GITHUB_RUN_ID": "", "RUN_DEADLINE_SEC": "2700"}))
        self.enterContext(patch.object(m, "_RUN_STARTED", None))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ("CRAWL_FILE", "FETCH_STATE_FILE", "DATA_FILE", "HISTORY_FILE",
                     "VIEWS_HISTORY_FILE", "NOTIFY_QUEUE_FILE"):
            self.enterContext(patch.object(m, name, self.root / getattr(m, name).name))

    def load_crawl(self, payload):
        m.CRAWL_FILE.write_text(json.dumps(payload), encoding="utf-8")
        return m.load_json(m.CRAWL_FILE, {})

    def test_real_source_shapes_without_warnings(self):
        page = dict(page=1, current_page=1, items=30, fingerprint="0123456789abcdef", added_to_batch=2)
        payload = dict(missav_page=61, supjav_page=765, javdb_page=129, missav_cycle=16,
                       missav_cycle_completed_at="2026-11-14 11:42:01",
                       recovery_until={"javdb_page": 159}, source_retry_at={"javdb": 1794644005},
                       missav_repeated_pages=[dict(page=255, at="2026-10-21 17:33:04",
                                                   reason="duplicate_fingerprint")],
                       last_discovery=dict(checked_at="2026-11-14 11:42:01", found_unique=2563,
                           walker_elapsed_sec=151.5, walker_budget_exhausted=True, walker_budget_sec=150,
                           sources=dict(missav=dict(archive_from=1861, pages=[page] * 2000,
                               status="page_limit", max_page=2000, stopped_page=2001,
                               cycle_completed=True, restart_page=61, search_items=653,
                               catalog_items=2332, recent_page_limit=60, archive_page_limit=180,
                               recent_pages=60, archive_pages=140, next_page=61,
                               last_archive=dict(page=2000, fingerprint="abc", head_fingerprint="def")),
                               javdb=dict(archive_from=129, status="access_limited", stopped_page=1,
                                          items=0, archive_pages=0, next_page=129),
                               supjav=dict(items=0, status="access_limited"))))
        metrics = ("previous_total new_to_catalog recent_added archive_added total_after "
                   "walker_recent_added walker_archive_added walker_discovery_added walker_unseen_before "
                   "walker_missav_verified walker_rejected_missav_404 walker_cached_dead_skipped "
                   "walker_deferred_unverified walker_official_checked walker_official_added "
                   "walker_official_not_found walker_official_deferred primary_links_repaired "
                   "walker_official_fastlane_added walker_official_fastlane_checked "
                   "walker_official_fastlane_success walker_official_fastlane_failed "
                   "walker_official_fastlane_not_found walker_official_fastlane_relinked")
        payload["last_discovery"].update(dict.fromkeys(metrics.split(), 0))
        output = io.StringIO()
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            self.assertEqual(self.load_crawl(payload), payload)
        self.assertEqual(output.getvalue(), "")

    def test_notify_queue_real_shape(self):
        fixture = Path(__file__).parent / "fixtures" / "notify_queue_state.json"
        m.NOTIFY_QUEUE_FILE.write_bytes(fixture.read_bytes())
        with patch.object(m, "now_ts", return_value=1794644005):
            rows = m.load_notify_queue()
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["item"]["views"])
        self.assertEqual(rows[0]["item"]["duration"], "")

    def test_cli_invalid_crawl_is_nonzero_and_preserves_bytes(self):
        raw = b'[  {"private": "never-print-this"}  ]\n'
        m.CRAWL_FILE.write_bytes(raw)
        env = dict(os.environ, **{name: str(getattr(m, name)) for name in (
            "CRAWL_FILE", "FETCH_STATE_FILE", "DATA_FILE", "HISTORY_FILE",
            "VIEWS_HISTORY_FILE", "NOTIFY_QUEUE_FILE")})
        result = subprocess.run([sys.executable, str(Path(m.__file__)), "--discovery-only"],
                                env=env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(m.CRAWL_FILE.read_bytes(), raw)
        self.assertIn("expected dict, got list", result.stderr)
        self.assertNotIn("never-print-this", result.stderr)
        self.assertEqual(list(self.root.iterdir()), [m.CRAWL_FILE])

    def test_all_collection_elements_checked(self):
        for key in ("pages", "missav_repeated_pages"):
            for bad in (None, [], "secret"):
                with self.subTest(key=key, bad=type(bad).__name__):
                    rows = [{"page": 1}] * 1000 + [bad]
                    payload = ({key: rows} if key == "missav_repeated_pages" else
                               {"last_discovery": {"sources": {"missav": {key: rows}}}})
                    with self.assertRaisesRegex(m.StateStructureError, key + r"\[1000\].*expected dict"):
                        self.load_crawl(payload)

    def test_invalid_nested_containers(self):
        for payload in ({"source_retry_at": []}, {"recovery_until": "secret"},
                        {"last_discovery": None}, {"missav_repeated_pages": {}},
                        {"last_discovery": {"sources": []}},
                        {"last_discovery": {"sources": {"javdb": []}}},
                        {"last_discovery": {"sources": {"missav": {"pages": {}}}}},
                        {"last_discovery": {"sources": {"missav": {"last_archive": []}}}}):
            with self.subTest(payload=payload), self.assertRaisesRegex(m.StateStructureError, "expected (dict|list)"):
                self.load_crawl(payload)

    def test_unknown_keys_preserved_and_scalar_mismatches_warn(self):
        payload = {"future": {"keep": [1]}, "missav_page": True,
                   "source_retry_at": {"javdb": "1794644005"}, "recovery_until": {"javdb_page": 159.5},
                   "last_discovery": {"walker_budget_exhausted": 1, "walker_elapsed_sec": "151.5",
                                      "sources": {"future_source": [1, 2]}}}
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            loaded = self.load_crawl(payload)
        self.assertEqual(loaded, payload)
        for message in ("expected int, got bool", "expected int, got float",
                        "expected bool, got int", "expected float, got str"):
            self.assertIn(message, output.getvalue())
        self.assertNotIn("1794644005", output.getvalue())
        m.save_json(m.CRAWL_FILE, loaded)
        self.assertEqual(json.loads(m.CRAWL_FILE.read_text()), payload)

    def test_missing_and_empty_initial_states(self):
        for name in ("CRAWL_FILE", "FETCH_STATE_FILE", "DATA_FILE", "HISTORY_FILE", "VIEWS_HISTORY_FILE"):
            path = getattr(m, name)
            self.assertEqual(m.load_json(path, {}), {})
            path.write_text("{}", encoding="utf-8")
            self.assertEqual(m.load_json(path, {}), {})

    def test_initial_discovery_missing_and_empty_crawl(self):
        with patch.dict(m.SOURCE_RETRY_AT, {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
            for empty in (False, True):
                if empty:
                    m.CRAWL_FILE.write_text("{}", encoding="utf-8")
                with patch.object(m, "scrape_missav", return_value=[]), \
                     patch.object(m, "scrape_missav_catalog", return_value=[]), \
                     patch.object(m, "scrape_javdb", return_value=[]), \
                     patch.object(m, "scrape_supjav", return_value=[]):
                    videos, state = m.get_latest_videos()
                self.assertEqual(videos, [])
                self.assertIsInstance(state["last_discovery"]["sources"], dict)
                self.assertGreaterEqual(state["missav_page"], 3)
    def test_unconfirmed_states_only_check_top_level(self):
        import missav_guard
        for path in (m.FETCH_STATE_FILE, m.DATA_FILE, m.HISTORY_FILE, m.VIEWS_HISTORY_FILE,
                     self.root / missav_guard.STATE_FILE.name):
            payload = {"future": [None, True], "fc2_market_not_found_at": 1794644005,
                       "fc2_host_stopped": True}
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(m.load_json(path, {}), payload)
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(m.StateStructureError, "expected dict, got list"):
                m.load_json(path, {})

    def test_invalid_other_state_prevents_any_save(self):
        m.DATA_FILE.write_bytes(b"[]\n")
        with patch.object(m, "save_json") as save, patch.object(m, "get_latest_videos") as discover:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(m.main(discovery_only=True), 1)
        save.assert_not_called()
        discover.assert_not_called()
        self.assertEqual(m.DATA_FILE.read_bytes(), b"[]\n")

    def test_guard_legacy_non_dict_fallback_without_saving(self):
        import missav_guard
        path = self.root / "guard.json"
        path.write_bytes(b"[]\n")
        with patch.object(missav_guard, "STATE_FILE", path), patch.object(m, "save_json") as save:
            guard = missav_guard.Guard()
        self.assertEqual(guard.checks, {})
        save.assert_not_called()
        self.assertEqual(path.read_bytes(), b"[]\n")

    def test_views_history_legacy_non_dict_fallback(self):
        m.VIEWS_HISTORY_FILE.write_text("[]")
        with patch.object(m, "save_json") as save:
            m.update_views_history([])
        self.assertEqual(save.call_args.args[1]["items"], {})

    def test_integer_elapsed_and_unknown_fields_are_silent(self):
        payload = {"future": [1], "last_discovery": {"walker_elapsed_sec": 151,
            "future_metric": None, "sources": {"future_source": [1]}}}
        with contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(self.load_crawl(payload), payload)
        self.assertEqual(output.getvalue(), "")

    def test_warning_deduplicated_across_loads(self):
        with contextlib.redirect_stderr(io.StringIO()) as output:
            for _ in range(3):
                self.load_crawl({"missav_page": True})
        self.assertEqual(output.getvalue().count("::warning::"), 1)

    def test_array_warning_aggregated(self):
        payload = {"last_discovery": {"sources": {"missav": {"pages": [{"page": "1"}] * 2000}}}}
        with contextlib.redirect_stderr(io.StringIO()) as output:
            self.load_crawl(payload)
            self.load_crawl(payload)
        self.assertEqual(output.getvalue().count("::warning::"), 1)
        self.assertIn("pages[0].page", output.getvalue())
        self.assertIn("2000", output.getvalue())

    def test_optional_step_propagates_structure_error(self):
        def broken():
            raise m.StateStructureError("broken")
        with self.assertRaises(m.StateStructureError):
            m.run_optional_step("state", broken)

    def test_repair_preflight_prevents_earlier_catalog_save(self):
        m.DATA_FILE.write_text('{"items": []}')
        m.FETCH_STATE_FILE.write_text('[]')
        with patch.object(m, "save_json") as save:
            with self.assertRaises(m.StateStructureError):
                m.repair_saved_titles()
        save.assert_not_called()

    def test_main_reuses_prevalidated_catalog_and_history(self):
        for path in (m.DATA_FILE, m.HISTORY_FILE):
            path.write_text('{}')
        original = m.json.load
        reads = []
        def read(stream):
            reads.append(Path(stream.name))
            return original(stream)
        video = {"code": "FC2-PPV-1234567", "title": "test", "sources": {}}
        # Stop after main has consumed these two inputs, before any enrichment or writes.
        with patch.object(m.json, "load", side_effect=read), \
             patch.object(m, "get_latest_videos", return_value=([video], {})), \
             patch.object(m, "save_discovery_cooldowns"), \
             patch.object(m, "load_notify_queue", side_effect=RuntimeError("stop")):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                m.main(discovery_only=True)
        self.assertEqual(reads.count(m.DATA_FILE), 1)
        self.assertEqual(reads.count(m.HISTORY_FILE), 1)

    def test_elapsed_bool_still_warns(self):
        with contextlib.redirect_stderr(io.StringIO()) as output:
            self.load_crawl({"last_discovery": {"walker_elapsed_sec": True}})
        self.assertIn("expected float, got bool", output.getvalue())

    def test_discovery_source_propagates_structure_error(self):
        with patch.object(m, "scrape_missav", side_effect=m.StateStructureError("broken")), \
             patch.object(m, "scrape_javdb") as javdb, \
             patch.object(m, "scrape_supjav") as supjav:
            with self.assertRaises(m.StateStructureError):
                m.get_latest_videos()
        javdb.assert_not_called()
        supjav.assert_not_called()

    def test_alternate_writers_preflight_before_work(self):
        import aggressive_discovery
        import efficient_discovery
        import walker_official_fastlane
        import missav_guard
        m.HISTORY_FILE.write_bytes(b"[]\n")
        for action in (m.refresh_update_metadata, m.repair_saved_titles,
                       aggressive_discovery.main, efficient_discovery.main,
                       walker_official_fastlane.main, missav_guard.run_guarded_monitor):
            with self.subTest(action=action.__module__ + "." + action.__name__), \
                 patch.object(m, "save_json") as save, \
                 patch.object(m.requests, "get") as network:
                with self.assertRaises(m.StateStructureError):
                    action()
                save.assert_not_called()
                network.assert_not_called()

    def test_optional_step_nested_exception_handler_exits_one(self):
        m.FETCH_STATE_FILE.write_bytes(b"[]\n")
        program = """
import sys
from pathlib import Path
import fc2_monitor as m

def broken():
    try:
        m.load_json(Path(sys.argv[1]), {})
    except Exception:
        pass
m.run_optional_step("state", broken)
"""
        result = subprocess.run([sys.executable, "-c", program, str(m.FETCH_STATE_FILE)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "fetch_state.json: $: expected dict, got list\n")
        self.assertEqual(m.FETCH_STATE_FILE.read_bytes(), b"[]\n")

    def test_fastlane_process_preserves_files_on_structure_error(self):
        m.DATA_FILE.write_bytes(b'{"items": []}\n')
        m.FETCH_STATE_FILE.write_bytes(b"[]\n")
        before = {p: p.read_bytes() for p in (m.DATA_FILE, m.FETCH_STATE_FILE)}
        env = dict(os.environ, **{name: str(getattr(m, name)) for name in (
            "CRAWL_FILE", "FETCH_STATE_FILE", "DATA_FILE", "HISTORY_FILE", "VIEWS_HISTORY_FILE")},
            MISSAV_GUARD_STATE_FILE=str(self.root / "guard.json"))
        result = subprocess.run([sys.executable, str(Path(m.__file__).with_name("walker_official_fastlane.py"))],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "fetch_state.json: $: expected dict, got list\n")
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_thread_pool_result_propagates_structure_error(self):
        from concurrent.futures import ThreadPoolExecutor
        import walker_official_fastlane as walker
        with patch.object(m, "fetch_fc2_market", side_effect=m.StateStructureError("broken")):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(walker.fetch_one, "FC2-PPV-1234567", {})
                with self.assertRaisesRegex(m.StateStructureError, "broken"):
                    future.result()

    def test_prune_only_preflights_once(self):
        import missav_guard as guard
        with patch.object(sys, "argv", ["missav_guard.py", "--prune-only"]), \
             patch.object(m, "validate_state_files") as validate, \
             patch.object(guard, "prune_saved") as prune:
            self.assertEqual(guard.main(), 0)
        validate.assert_called_once_with()
        prune.assert_called_once_with()
