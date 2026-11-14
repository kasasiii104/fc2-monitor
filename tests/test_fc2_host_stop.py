import unittest
import io
from contextlib import redirect_stdout
from unittest.mock import patch, Mock
from pathlib import Path
import tempfile
import walker_official_fastlane as fast
from email.utils import formatdate
import fc2_monitor as m
import efficient_discovery as e

class HostStopTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(m.os.environ, {"RUNNER_TEMP": "", "GITHUB_RUN_ID": "", "RUN_DEADLINE_SEC": "2700"}))
        self.enterContext(patch.object(m, "_RUN_STARTED", None))
        flag = patch.object(m, '_FC2_STOPPED_THIS_RUN', False, create=True)
        flag.start()
        self.addCleanup(flag.stop)

    def test_sample_stop_uses_response_time(self):
        for lane in (m.enrich_fc2_sample_assets, m.refresh_fc2_previews, m.run_thumbnail_backfill):
            with self.subTest(lane=lane.__name__):
                m._FC2_STOPPED_THIS_RUN = False
                state, clock = {}, [1000]
                def response(*args):
                    clock[0] = 1100
                    return {'http_status': 429, 'retry_after': '60'}
                with patch.object(m, 'load_json', return_value=state), patch.object(m, 'save_json'), patch.object(m, 'now_ts', side_effect=lambda: clock[0]), patch.object(m, 'fetch_fc2_sample_assets', side_effect=response):
                    lane([{'code': 'FC2-PPV-1234567', 'code_num': '1234567'}])
                self.assertEqual(state['fc2_market_host_stop']['until'], 1160)

    def test_host_stop_keeps_longer_deadline(self):
        state = {}
        with patch.object(m, 'save_json'):
            m.record_fc2_host_stop(state, 429, '3600', 1000)
            previous = state['fc2_market_host_stop'].copy()
            m.record_fc2_host_stop(state, 403, '60', 1001)
            self.assertEqual(state['fc2_market_host_stop'], previous)
            m.record_fc2_host_stop(state, 403, '60', 5000)
            self.assertEqual(state['fc2_market_host_stop'], dict(host=m.FC2_MARKET_HOST, status=403, seconds=60, until=5060))

    def test_host_stop_replaces_invalid_state(self):
        for previous in (None, [], 'bad', {'until': 'bad'}, {'until': None}, {'until': float('nan')}, {'until': float('inf')}):
            with self.subTest(previous=previous), patch.object(m, 'save_json'):
                state = {'fc2_market_host_stop': previous}
                m.record_fc2_host_stop(state, 429, '60', 1000)
                self.assertEqual(state['fc2_market_host_stop']['until'], 1060)

    def test_denial_preserves_article_attempt(self):
        for status in ('success', 'not_found'):
            for http_status in (403, 429):
                with self.subTest(status=status, http_status=http_status):
                    item = {'code': 'FC2-PPV-1234567', 'fc2_market_last_attempt': 10}
                    previous = dict(status=status, failures=2, attempts=3, last_attempt=10,
                                    next_retry_at=999, last_route='backfill')
                    state = {m.BACKFILL_ATTEMPTS_KEY: {item['code']: previous.copy()}}
                    with patch.object(m, 'save_json'):
                        m.record_fc2_market_result(item, {'http_status': http_status}, state, 100, 'test')
                    self.assertEqual(state[m.BACKFILL_ATTEMPTS_KEY][item['code']], previous)
                    self.assertEqual(item['fc2_market_last_attempt'], 10)

    def test_efficient_rejected_candidates_leave_no_shared_records(self):
        state, checks = {}, {}
        for i, outcome in enumerate(('failed', 'not_found') * 3):
            code = f'FC2-PPV-{1234567+i}'
            with patch.object(m, 'fetch_fc2_market', return_value={'outcome': outcome}), patch.object(m, 'save_json') as save:
                e.verify_official({'code': code}, code.split('-')[-1], checks, 100, state)
            save.assert_not_called()
            self.assertEqual(checks[code]["status"], outcome)
            for records in state.values():
                if isinstance(records, dict):
                    self.assertNotIn(code, records)

    def test_retry_after_variants(self):
        now = 1800000000
        for raw, expected in [('90', 90), (formatdate(now + 120, usegmt=True), 120), (None, m.FAIL_SKIP_SEC), ('bad', m.FAIL_SKIP_SEC), ('999999999', 86400), ('0', 0), (formatdate(now - 120, usegmt=True), 0), ('1.5', m.FAIL_SKIP_SEC), ('-1', m.FAIL_SKIP_SEC), (formatdate(now + 120), 120)]:
            with self.subTest(raw=raw):
                state = {}
                with patch.object(m, "save_json"):
                    m.record_fc2_market_result({'code': 'FC2-PPV-1234567'}, {'outcome': 'failed', 'http_status': 429, 'retry_after': raw}, state, now, 'test')
                self.assertEqual(state['fc2_market_host_stop']['until'], now + expected)
                self.assertEqual(set(state['fc2_market_host_stop']), {'host', 'status', 'seconds', 'until'})
                self.assertTrue(m.fc2_market_blocked(state, now + expected + 1))
                m._FC2_STOPPED_THIS_RUN = False
                self.assertFalse(m.fc2_market_blocked(state, now + expected))

    def test_batch_stops_without_consuming_next_attempt(self):
        items = [{'code': 'FC2-PPV-' + n, 'code_num': n} for n in ['1234567', '2345678']]
        state = {}
        output = io.StringIO()
        with redirect_stdout(output), patch.object(m, 'fetch_fc2_market', return_value={'http_status': 403, 'outcome': 'failed'}) as fetch, patch.object(m, 'save_json'):
            m._fc2_batch(items, state, 'test', 2)
        self.assertEqual(fetch.call_count, 1)
        self.assertNotIn(items[1]['code'], state.get(m.BACKFILL_ATTEMPTS_KEY, {}))
        self.assertNotIn('fc2_market_last_attempt', items[1])
        self.assertIn('host=adult.contents.fc2.com status=403 seconds=', output.getvalue())
        self.assertIn('carried=1', output.getvalue())

    def test_efficient_obeys_and_records_shared_stop(self):
        state = {}
        with patch.object(m, 'save_json'), patch.object(m, 'fetch_fc2_market', return_value={'http_status': 429, 'outcome': 'failed'}) as fetch:
            self.assertEqual(e.verify_official({'code': 'FC2-PPV-1234567'}, '1234567', {}, 100, state)[0], 'blocked')
            e.verify_official({'code': 'FC2-PPV-2345678'}, '2345678', {}, 100, state)
        self.assertEqual(fetch.call_count, 1)

    def test_persisted_stop_fixture_expires_in_next_run(self):
        state = m.load_json(Path(__file__).parent / 'fixtures/fc2_host_stop.json', {})
        stop = state['fc2_market_host_stop']
        self.assertTrue(m.fc2_market_blocked(state, stop['until'] - 1))
        self.assertFalse(m.fc2_market_blocked(state, stop['until']))

    def test_sample_api_propagates_retry_after(self):
        response = Mock(status_code=429, headers={'Retry-After': '75'})
        with patch.object(m.requests, 'get', return_value=response):
            meta = m.fetch_fc2_sample_assets('1234567')
        self.assertEqual((meta['http_status'], meta['retry_after']), (429, '75'))

    def test_sample_lanes_stop_and_preserve_carried_items(self):
        for lane in [m.enrich_fc2_sample_assets, m.refresh_fc2_previews, m.run_thumbnail_backfill]:
            m._FC2_STOPPED_THIS_RUN = False
            with self.subTest(lane=lane.__name__), tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
                items = [{'code': 'FC2-PPV-' + n, 'code_num': n} for n in ['1234567', '2345678']]
                with patch.object(m, 'FETCH_STATE_FILE', Path(tmp) / 'state.json'), patch.object(m, 'fetch_fc2_sample_assets', return_value={'http_status': 403}) as fetch:
                    previous = {item['code']: {'status': 'not_found', 'last_attempt': 10, 'next_retry_at': 999, 'last_route': 'backfill'} for item in items}
                    m.save_json(m.FETCH_STATE_FILE, {m.BACKFILL_ATTEMPTS_KEY: previous})
                    lane(items)
                    first_state = m.load_json(Path(tmp) / 'state.json', {})
                    self.assertEqual(first_state[m.BACKFILL_ATTEMPTS_KEY], previous)
                    if lane == m.refresh_fc2_previews:
                        self.assertEqual(first_state[m.PREVIEW_REFRESH_STATS_KEY]['processed'], 0)
                    lane(items)
                self.assertEqual(fetch.call_count, 1)
                self.assertTrue(all('fc2_market_last_attempt' not in item and 'preview_checked_at' not in item and 'fc2_sample_checked_at' not in item for item in items))
                state = m.load_json(Path(tmp) / 'state.json', {})
                self.assertFalse(state.get(m.THUMB_BACKFILL_ATTEMPTS_KEY))
                if lane == m.refresh_fc2_previews:
                    self.assertEqual(state[m.PREVIEW_REFRESH_STATS_KEY]['processed'], 0)
                carried = next(item for item in items if item['code_num'] != fetch.call_args.args[0])
                self.assertNotIn('fc2_sample_checked_at', carried)
                self.assertNotIn('fc2_market_last_attempt', carried)

    def test_fastlane_shared_stop_and_single_worker_denial(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            with patch.object(m, 'FETCH_STATE_FILE', Path(tmp) / 'state.json'), patch.object(m, 'DATA_FILE', Path(tmp) / 'data.json'), patch.object(m, 'CRAWL_FILE', Path(tmp) / 'crawl.json'), patch.object(m, 'HISTORY_FILE', Path(tmp) / 'history.json'), patch.object(fast, 'WORKERS', 1):
                m.save_json(m.FETCH_STATE_FILE, {m.H_WALKER_CACHE_KEY: {f'FC2-PPV-{n}': {'title': 'サンプル作品'} for n in ['1234567', '2345678']}})
                with patch.object(m, 'fetch_fc2_market', return_value={'http_status': 429, 'retry_after': '90'}) as fetch:
                    fast.main()
                    fast.main()
                self.assertEqual(fetch.call_count, 1)
                state = m.load_json(m.FETCH_STATE_FILE, {})
                self.assertEqual(state['fc2_market_host_stop']['seconds'], 90)
                self.assertFalse(state.get(m.BACKFILL_ATTEMPTS_KEY))

    def test_legacy_ekyc_state_remains_supported(self):
        state = {'fc2_market_ekyc_blocked_at': 100}
        self.assertTrue(m.fc2_market_blocked(state, 101))
        self.assertFalse(m.fc2_market_blocked(state, 100 + m.FAIL_SKIP_SEC))

    def test_fastlane_candidates_only_keep_success(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            with patch.object(m, 'FETCH_STATE_FILE', Path(tmp) / 'state.json'), patch.object(m, 'DATA_FILE', Path(tmp) / 'data.json'), patch.object(m, 'CRAWL_FILE', Path(tmp) / 'crawl.json'), patch.object(m, 'HISTORY_FILE', Path(tmp) / 'history.json'), patch.object(fast, 'WORKERS', 1):
                codes = [f'FC2-PPV-{1234567+i}' for i in range(3)]
                m.save_json(m.FETCH_STATE_FILE, {m.H_WALKER_CACHE_KEY: {code: {'title': 'サンプル作品'} for code in codes}})
                with patch.object(m, 'fetch_fc2_market', side_effect=lambda num: {'outcome': 'success', 'fc2_title': 'サンプル作品'} if num == codes[2].split('-')[-1] else {'outcome': 'failed' if num == codes[0].split('-')[-1] else 'not_found'}), patch.object(m, 'save_json', wraps=m.save_json) as save:
                    fast.main()
                state = m.load_json(m.FETCH_STATE_FILE, {})
                for key in (m.BACKFILL_ATTEMPTS_KEY, 'fc2_market_checked', 'fc2_market_failed'):
                    self.assertFalse(set(codes[:2]) & set(state.get(key, {})))
                self.assertEqual(state[fast.CHECK_KEY][codes[0]]['status'], 'failed')
                self.assertEqual(state[fast.CHECK_KEY][codes[1]]['status'], 'not_found')
                rec = state[m.BACKFILL_ATTEMPTS_KEY][codes[2]]
                self.assertEqual((rec['status'], rec['attempts'], rec['last_route']), ('success', 1, 'official_fastlane'))
                self.assertEqual(sum(call.args[0] == m.FETCH_STATE_FILE for call in save.call_args_list), 1)

    def test_article_propagates_retry_after(self):
        with patch.object(m, 'fetch_page', return_value={'status': 429, 'retry_after': '45'}):
            meta = m.fetch_fc2_market('1234567')
        self.assertEqual((meta['outcome'], meta['retry_after']), ('blocked', '45'))

    def test_efficient_429_stop_uses_response_time(self):
        state, clock = {}, [1000]

        def fetch(num):
            clock[0] = 1100
            return {'outcome': 'blocked', 'http_status': 429, 'retry_after': '60'}

        with patch.object(m, 'save_json'), patch.object(m, 'fetch_fc2_market', side_effect=fetch), patch.object(m, 'now_ts', side_effect=lambda: clock[0]):
            result = e.verify_official({'code': 'FC2-PPV-1234567'}, '1234567', {}, 1000, state)
        self.assertEqual(result[0], 'blocked')
        self.assertEqual(state['fc2_market_host_stop']['until'], 1160)

    def test_efficient_403_stop_uses_response_time(self):
        state, clock = {}, [1000]
        default_seconds = m.fc2_retry_after(None, 1100)

        def fetch(num):
            clock[0] = 1100
            return {'outcome': 'blocked', 'http_status': 403}

        with patch.object(m, 'save_json'), patch.object(m, 'fetch_fc2_market', side_effect=fetch), patch.object(m, 'now_ts', side_effect=lambda: clock[0]):
            result = e.verify_official({'code': 'FC2-PPV-1234567'}, '1234567', {}, 1000, state)
        self.assertEqual(result[0], 'blocked')
        self.assertEqual(state['fc2_market_host_stop']['until'], 1100 + default_seconds)

    def test_efficient_not_found_and_failed_retry_delays(self):
        code, now = 'FC2-PPV-1234567', 1800000000
        for outcome, delay in [('not_found', e.ARCHIVE_DEAD_RECHECK_SEC), ('failed', e.OFFICIAL_RETRY_SEC)]:
            with self.subTest(outcome=outcome):
                checks = {}
                with patch.object(m, 'fetch_fc2_market', return_value={'outcome': outcome}):
                    e.verify_official({'code': code}, '1234567', checks, now, {})
                self.assertFalse(e.official_due(checks, code, now + delay - 1))
                self.assertTrue(e.official_due(checks, code, now + delay))

    def test_efficient_denial_leaves_candidate_for_next_run(self):
        for status in (403, 429):
            with self.subTest(status=status):
                m._FC2_STOPPED_THIS_RUN = False
                state, checks = {}, {}
                code, now = 'FC2-PPV-1234567', 1800000000
                with patch.object(m, 'save_json'), patch.object(m, 'fetch_fc2_market', return_value={'http_status': status, 'retry_after': '90'}) as fetch:
                    self.assertEqual(e.verify_official({'code': code}, '1234567', checks, now, state)[0], 'blocked')
                    self.assertNotIn(code, checks)
                    e.verify_official({'code': code}, '1234567', checks, now + 1, state)
                    fetch.assert_called_once()
                m._FC2_STOPPED_THIS_RUN = False
                self.assertTrue(e.official_due(checks, code, now + 90))
                with patch.object(m, 'fetch_fc2_market', return_value={'outcome': 'not_found'}) as fetch:
                    e.verify_official({'code': code}, '1234567', checks, now + 90, state)
                    fetch.assert_called_once()

    def test_efficient_ekyc_still_records_failed_check(self):
        checks = {}
        code = 'FC2-PPV-1234567'
        with patch.object(m, 'fetch_fc2_market', return_value={'outcome': 'blocked', 'blocked': 'ekyc'}):
            self.assertEqual(e.verify_official({'code': code}, '1234567', checks, 1800000000, {})[0], 'blocked')
        self.assertEqual(checks[code]['status'], 'failed')
