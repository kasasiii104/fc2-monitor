import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import fc2_monitor as m
import aggressive_discovery
import efficient_discovery
import missav_guard
import repair_code_only_titles
import walker_official_fastlane
import walker_title_repair
from bs4 import BeautifulSoup


class RunDeadlineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patches = [patch.dict(os.environ, {"RUNNER_TEMP": tmp.name,
                   "GITHUB_RUN_ID": "deadline-test", "GITHUB_RUN_ATTEMPT": "1", "RUN_DEADLINE_SEC": "2700"})]
        patches += [patch.object(m, key, value, create=True) for key, value in
                    (("_RUN_STARTED", None), ("_RUN_BUDGET", 2700), ("_DEADLINE_LOGGED", set()))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_invalid_origin_replaced_and_shared(self):
        path = self.root / m.RUN_DEADLINE_FILENAME
        for raw in ('{', 'null', '[]', '{}', json.dumps(dict(run='other-1', started_monotonic=10, started_wall=900))):
            with self.subTest(raw=raw):
                path.write_text(raw)
                m._RUN_STARTED = None
                with patch.object(m.time, 'monotonic', return_value=100), patch.object(m.time, 'time', return_value=1000):
                    m.initialize_run_deadline()
                saved = path.read_text()
                self.assertEqual(json.loads(saved)['started_monotonic'], 100)
                m._RUN_STARTED = None
                with patch.object(m.time, 'monotonic', return_value=200), patch.object(m.time, 'time', return_value=1100):
                    m.initialize_run_deadline()
                    self.assertEqual(m.run_time_left(), 2600)
                self.assertEqual(path.read_text(), saved)

    def test_origin_replace_failure_keeps_local_deadline(self):
        path = self.root / m.RUN_DEADLINE_FILENAME
        path.write_text('{}')
        with patch.object(m.os, 'replace', side_effect=OSError('unavailable')) as replace, patch.object(m.time, 'monotonic', return_value=100):
            m.initialize_run_deadline()
            self.assertEqual(m.run_time_left(), 2700)
        replace.assert_called_once()
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_later_process_inherits_origin(self):
        with patch.object(m.time, "monotonic", return_value=100), patch.object(m.time, "time", return_value=1000):
            m.initialize_run_deadline()
        code = ("import fc2_monitor as m; from unittest.mock import patch; "
                "patch.object(m.time, 'monotonic', return_value=200).start(); "
                "patch.object(m.time, 'time', return_value=1100).start(); "
                "m.initialize_run_deadline(); print(m.run_time_left())")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        self.assertEqual(float(result.stdout.strip()), 2600)

    def test_invalid_or_other_run_is_not_inherited(self):
        for run, mono, wall in [("other-1", 10, 900), ("deadline-test-1", 201, 900),
                                ("deadline-test-1", 10, 1101), ("deadline-test-1", "bad", 900),
                                ("deadline-test-1", float("nan"), 900), ("deadline-test-1", True, 900)]:
            with self.subTest(run=run, mono=mono, wall=wall):
                (self.root / m.RUN_DEADLINE_FILENAME).write_text(json.dumps(
                    {"run": run, "started_monotonic": mono, "started_wall": wall}))
                m._RUN_STARTED = None
                with patch.object(m.time, "monotonic", return_value=200), patch.object(m.time, "time", return_value=1100):
                    m.initialize_run_deadline()
                    self.assertEqual(m.run_time_left(), 2700)

    def test_import_does_not_create_origin(self):
        subprocess.run([sys.executable, "-c", "import fc2_monitor, efficient_discovery, walker_official_fastlane, walker_title_repair, repair_code_only_titles, aggressive_discovery, missav_guard"], capture_output=True, check=True)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_budget_uses_default(self):
        for value in ("bad", "-1", "0", "nan", "inf"):
            m._RUN_STARTED = None
            with patch.dict(os.environ, {"RUN_DEADLINE_SEC": value}):
                m.initialize_run_deadline()
                self.assertEqual(m._RUN_BUDGET, m.DEFAULT_RUN_DEADLINE_SEC)

    def test_no_run_id_or_runner_temp_uses_local_origin(self):
        for key in ("GITHUB_RUN_ID", "RUNNER_TEMP"):
            m._RUN_STARTED = None
            with patch.dict(os.environ, {key: ""}), patch.object(m.time, "monotonic", return_value=100):
                m.initialize_run_deadline()
                self.assertEqual(m.run_time_left(), 2700)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_expired_helpers_leave_saved_state_untouched(self):
        m._RUN_STARTED = 0
        for module in (aggressive_discovery, efficient_discovery, missav_guard,
                       repair_code_only_titles, walker_official_fastlane, walker_title_repair):
            with self.subTest(module=module.__name__), patch.object(m.time, "monotonic", return_value=2700), patch.object(m.requests, "get") as get, patch.object(m, "save_json") as save:
                self.assertEqual(module.main(), 0)
                get.assert_not_called()
                save.assert_not_called()

    def test_optional_policy_is_explicit_and_preserves_action_kwargs(self):
        action = unittest.mock.Mock()
        with patch.object(m, "run_deadline_reached", return_value=True):
            self.assertTrue(m.run_optional_step("arbitrary", action, needs_network=False, photo="saved"))
            action.assert_called_once_with(photo="saved")
            self.assertFalse(m.run_optional_step("Views history", action))

    def test_refresh_metadata_propagates_rank_and_view_errors(self):
        for failing in ("scrape_missav_rankings", "fill_missing_views"):
            with self.subTest(failing=failing), patch.object(m, "validate_state_files"), patch.object(m, "load_json", return_value={"items": [{"code": "FC2-PPV-1234567"}]}), patch.object(m, "scrape_missav_rankings", return_value={}), patch.object(m, "apply_missav_ranks"), patch.object(m, "fill_missing_views"), patch.object(m, "write_site"), patch.object(m.requests, "get", side_effect=AssertionError("network forbidden")), patch.object(m, failing, side_effect=RuntimeError("source failure")), patch.object(m, "run_deadline_reached", return_value=False), patch.object(m, "save_json") as save:
                with self.assertRaisesRegex(RuntimeError, "source failure"):
                    m.refresh_update_metadata()
                save.assert_not_called()

    def test_rank_deadline_preserves_unattempted_snapshots(self):
        from copy import deepcopy
        state = {m.RANK_STATE_KEY: {p: {"status": "ok", "checked_at": "old", "last_success_at": "old", "codes": ["FC2-PPV-1234567"]} for p in m.RANK_PERIODS}}
        original = deepcopy(state)
        # Outer check, first host, fetch boundary; then the inner-loop boundary.
        for checks, response in [([True], {}), ([False, True], {}), ([False, False], {"error": "run_deadline"}), ([False, False, True], {"ok": False})]:
            with self.subTest(checks=checks), patch.object(m, "run_deadline_reached", side_effect=checks), patch.object(m, "source_in_cooldown", return_value=False), patch.object(m, "fetch_page", return_value=response), patch.object(m, "record_source_denial") as denial:
                result = m.scrape_missav_fc2_rankings()
            self.assertEqual(result, {"version": 2, "periods": {}})
            denial.assert_not_called()
            items = [{"code": "FC2-PPV-1234567"}]
            with patch.object(m, "load_json", return_value=state), patch.object(m, "save_json"):
                m.apply_missav_ranks(items, result, "new")
                m.validate_crawl_state(m.FETCH_STATE_FILE, state)
            self.assertEqual(state, original)
            for period in m.RANK_PERIODS:
                self.assertEqual(items[0]["missav_rank_" + period], 1)

    def test_partial_rank_success_changes_only_completed_period(self):
        from copy import deepcopy
        state = {m.RANK_STATE_KEY: {p: {"status": "ok", "checked_at": "old", "last_success_at": "old", "codes": ["FC2-PPV-1234567"]} for p in m.RANK_PERIODS}}
        original = deepcopy(state)
        first = next(iter(m.RANK_PERIODS))
        soup = BeautifulSoup('<a href="/ja/fc2-ppv-1234568">作品</a>', 'html.parser')
        with patch.object(m, "run_deadline_reached", side_effect=[False, False, True]), patch.object(m, "source_in_cooldown", return_value=False), patch.object(m, "fetch_page", side_effect=lambda url: {"ok": True, "status": 200, "soup": soup, "final_url": url}):
            ranks = m.scrape_missav_fc2_rankings()
        self.assertEqual(set(ranks["periods"]), {first})
        items = [{"code": "FC2-PPV-1234567"}, {"code": "FC2-PPV-1234568"}]
        with patch.object(m, "load_json", return_value=state), patch.object(m, "save_json"):
            m.apply_missav_ranks(items, ranks, "new")
            m.validate_crawl_state(m.FETCH_STATE_FILE, state)
            m.reconcile_saved_ranks(items, state)
        for period in m.RANK_PERIODS:
            if period == first:
                self.assertEqual(state[m.RANK_STATE_KEY][period]["checked_at"], "new")
                self.assertEqual(items[1]["missav_rank_" + period], 1)
            else:
                self.assertEqual(state[m.RANK_STATE_KEY][period], original[m.RANK_STATE_KEY][period])
                self.assertEqual(items[0]["missav_rank_" + period], 1)

    def test_fetch_boundary_deadline_keeps_listing_progress_and_status(self):
        for source in ("missav", "javdb"):
            completed, report = set(), {}
            action = m.scrape_missav_catalog if source == "missav" else m.scrape_javdb
            with self.subTest(source=source), patch.object(m, "run_deadline_reached", return_value=False), patch.object(m, "fetch_page", return_value={"error": "run_deadline"}) as fetch, patch.object(m, "record_source_denial") as denial:
                self.assertEqual(action([3, 4], completed_pages=completed, report=report), [])
            self.assertEqual(completed, set())
            self.assertEqual(report.get("status", "ok"), "ok")
            self.assertEqual(fetch.call_count, 1)
            denial.assert_not_called()

    def test_catalog_inner_boundary_deadline_is_not_fetch_failure(self):
        report = {}
        with patch.object(m, "run_deadline_reached", side_effect=[False, True]), patch.object(m, "fetch_page") as fetch:
            self.assertEqual(m.scrape_missav_catalog([3], report=report), [])
        self.assertEqual(report.get("status", "ok"), "ok")
        fetch.assert_not_called()

    def test_other_listings_stop_at_fetch_boundary_deadline(self):
        for action in (m.scrape_missav, m.scrape_supjav, m.harvest_supjav_views, m.scrape_hwalker_market, m.audit_discovery):
            with self.subTest(action=action.__name__), patch.object(m, "validate_state_files"), patch.object(m, "load_json", return_value={}), patch.object(m, "run_deadline_reached", return_value=False), patch.object(m, "fetch_page", return_value={"error": "run_deadline"}) as fetch, patch.object(m, "record_source_denial") as denial:
                result = action()
            self.assertEqual(fetch.call_count, 1)
            denial.assert_not_called()
            if action == m.scrape_hwalker_market:
                self.assertEqual(result, ({}, m.H_WALKER_URL + "/", 0))

    def test_expired_requests_and_optional_steps_are_skipped(self):
        m._RUN_STARTED = 0
        with patch.object(m.time, "monotonic", return_value=2700), patch.object(m.requests, "get") as get:
            self.assertFalse(m.fetch_page("https://example.test")["ok"])
            self.assertEqual(m.fetch_fc2_sample_assets("1234567")["outcome"], "deferred")
            self.assertFalse(m.run_optional_step("fetch", get))
            get.assert_not_called()

    def test_batch_saves_completed_work_without_counting_carryover(self):
        items = [{"code": f"FC2-PPV-{1234567+i}"} for i in range(3)]
        state, clock = {}, [0]
        m._RUN_STARTED = 0
        def fetch(num):
            clock[0] += 1400
            return {"outcome": "success", "title": "テスト作品"}
        with patch.object(m.time, "monotonic", side_effect=lambda: clock[0]), patch.object(m, "fetch_fc2_market", side_effect=fetch) as access, patch.object(m, "save_json") as save:
            result = m._fc2_batch(items, state, "test", 3)
        self.assertEqual(access.call_count, 2)
        self.assertEqual(result["tried"], 2)
        self.assertNotIn(items[2]["code"], state[m.BACKFILL_ATTEMPTS_KEY])
        self.assertTrue(save.called)

    def test_before_deadline_processes_all(self):
        items = [{"code": f"FC2-PPV-{1234567+i}"} for i in range(3)]
        m._RUN_STARTED = 0
        with patch.object(m.time, "monotonic", return_value=10), patch.object(m, "fetch_fc2_market", return_value={"outcome": "success"}) as access, patch.object(m, "save_json"):
            result = m._fc2_batch(items, {}, "test", 3)
        self.assertEqual(access.call_count, 3)
        self.assertEqual(result["tried"], 3)

    def test_unprocessed_archive_page_is_not_completed(self):
        m._RUN_STARTED = 0
        clock = [0]
        def fetch(url):
            clock[0] = 2700
            return {"ok": True, "status": 200, "final_url": url,
                    "soup": BeautifulSoup('<a href="/ja/fc2-ppv-1234567">作品</a>', 'html.parser')}
        completed = set()
        with patch.object(m.time, "monotonic", side_effect=lambda: clock[0]), patch.object(m, "fetch_page", side_effect=fetch) as access:
            found = m.scrape_missav_catalog([3, 4], completed_pages=completed)
        self.assertEqual(access.call_count, 1)
        self.assertEqual(completed, {3})
        self.assertEqual(len(found), 1)

    def test_main_saves_partial_discoveries_and_notification_queue(self):
        for key in ("DATA_FILE", "HISTORY_FILE", "CRAWL_FILE", "FETCH_STATE_FILE", "NOTIFY_QUEUE_FILE", "VIEWS_HISTORY_FILE"):
            p = patch.object(m, key, self.root / (key + '.json'))
            p.start()
            self.addCleanup(p.stop)
        m.save_json(m.DATA_FILE, {"items": [{"code": "FC2-PPV-1234567", "title": "既存作品"}]})
        m.save_json(m.HISTORY_FILE, {"ids": ["FC2-PPV-1234567"]})
        m._RUN_STARTED = 0
        clock = [0]
        def discover():
            clock[0] = 2700
            return [m.enrich('1234568', '新規作品', 'MissAV', 'https://example.test/item')], {"missav_page": 4}
        with patch.object(m.time, "monotonic", side_effect=lambda: clock[0]), patch.object(m, "get_latest_videos", side_effect=discover), patch.object(m, "write_site") as site, patch.object(m.requests, "get") as get, patch.object(m.requests, "post") as post, patch.object(m, "send_telegram", side_effect=TimeoutError("offline")):
            self.assertEqual(m.main(), 0)
        self.assertEqual(len(m.load_json(m.DATA_FILE, {})['items']), 2)
        self.assertEqual(m.load_json(m.CRAWL_FILE, {})['missav_page'], 4)
        self.assertEqual(len(m.load_notify_queue()), 1)
        site.assert_called_once()
        get.assert_not_called()
        post.assert_not_called()

    def test_expired_thumbnail_preserves_attempts_and_assets(self):
        m._RUN_STARTED = 0
        items = [{"code": "FC2-PPV-1234567", "thumb": "saved"}]
        with patch.object(m.time, "monotonic", return_value=2700), patch.object(m, "load_json", return_value={}), patch.object(m, "save_json") as save, patch.object(m, "fetch_fc2_sample_assets") as fetch:
            m.run_thumbnail_backfill(items)
        fetch.assert_not_called()
        self.assertEqual(items[0]['thumb'], 'saved')
        self.assertEqual(save.call_args.args[1][m.THUMB_BACKFILL_STATS_KEY]['processed'], 0)
        self.assertNotIn('thumb_backfill_checked_at', items[0])

    def test_deadline_retry_queue_does_not_increment_attempts(self):
        guard = unittest.mock.Mock(retry_queue={}, dirty=False)
        item = {"code": "FC2-PPV-1234567"}
        missav_guard._queue_retry(item, {"reason": "run_deadline"}, guard)
        self.assertEqual(guard.retry_queue[item['code']]['attempts'], 0)
        self.assertNotIn('last_tried_at', guard.retry_queue[item['code']])

    def test_expired_fastlane_worker_does_not_fetch(self):
        m._RUN_STARTED = 0
        with patch.object(m.time, "monotonic", return_value=2700), patch.object(m, "fetch_fc2_market") as fetch:
            result = walker_official_fastlane.fetch_one('FC2-PPV-1234567', {})
        self.assertEqual(result[-1]['outcome'], 'deferred')
        fetch.assert_not_called()

    def test_fastlane_stops_submissions_and_saves_completed_results(self):
        m._RUN_STARTED = 0
        clock = [0]
        state = {m.H_WALKER_CACHE_KEY: {
            f'FC2-PPV-{1234567+i}': {'title': 'サンプル作品', 'checked_at': m.now_ts()}
            for i in range(3)}}
        payload = {'items': []}
        def load(path, default):
            return payload if path == m.DATA_FILE else state
        def fetch(num):
            clock[0] = 2700
            return {'outcome': 'success', 'fc2_title': '公式作品'}
        with patch.object(m.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(m, 'load_json', side_effect=load), patch.object(m, 'validate_state_files'), patch.object(m, 'fetch_fc2_market', side_effect=fetch) as access, patch.object(m, 'save_json') as save, patch.object(walker_official_fastlane, 'WORKERS', 1):
            self.assertEqual(walker_official_fastlane.main(), 0)
        self.assertEqual(access.call_count, 1)
        self.assertEqual(state['walker_official_fastlane_stats']['checked'], 1)
        self.assertEqual(len(payload['items']), 1)
        self.assertTrue(save.called)

    def test_corrupt_origin_is_ignored(self):
        for raw in ('{', 'null', '[]', '{}'):
            (self.root / m.RUN_DEADLINE_FILENAME).write_text(raw)
            m._RUN_STARTED = None
            with patch.object(m.time, 'monotonic', return_value=100):
                m.initialize_run_deadline()
                self.assertEqual(m.run_time_left(), 2700)

    def test_deferred_article_preserves_prior_attempt(self):
        item = {'code': 'FC2-PPV-1234567'}
        state = {m.BACKFILL_ATTEMPTS_KEY: {item['code']: {'attempts': 2}}}
        result = m.record_fc2_market_result(item, {'outcome': 'deferred'}, state, 100, 'test')
        self.assertEqual(result['status'], 'deferred')
        self.assertEqual(state[m.BACKFILL_ATTEMPTS_KEY][item['code']], {'attempts': 2})
