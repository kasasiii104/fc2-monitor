import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fc2_monitor as m


class OfficialStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1_800_000_000
        for name, value in [('FETCH_STATE_FILE', Path(self.tmp.name) / 'state.json'),
                            ('now_ts', lambda: self.now)]:
            p = patch.object(m, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def item(self, code='1234567', **extra):
        return dict(code=f'FC2-PPV-{code}', code_num=code, title=f'FC2-PPV-{code}', is_new=True, **extra)

    def state(self):
        return m.load_json(m.FETCH_STATE_FILE, {})

    def rec(self, item):
        return self.state()[m.BACKFILL_ATTEMPTS_KEY][item['code']]

    def test_new_success_is_not_untried_or_fetched_again_by_other_routes(self):
        item = self.item()
        # A real success may omit ratings. It must still not be fetched twice.
        with patch.object(m, 'fetch_fc2_market', return_value={'fc2_title': 'サンプル作品', 'fc2_market_url': 'https://example.test/'}) as fetch:
            m.enrich_new_fc2_market([item])
            m.run_continuous_metadata_backfill([item])
            m.enrich_fc2_market([item])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.rec(item)['status'], 'success')
        self.assertEqual(self.rec(item)['last_route'], 'new')
        self.assertEqual(self.state()[m.CONTINUOUS_BACKFILL_STATS_KEY]['remaining_untried'], 0)

    def test_every_route_writes_same_record_and_success_resets_failure(self):
        item = self.item(fc2_rating=4.1)
        with patch.object(m, 'fetch_fc2_market', return_value=None):
            m.enrich_new_fc2_market([item])
        self.assertEqual(self.rec(item)['failures'], 1)
        self.assertEqual(item['fc2_rating'], 4.1)
        self.now += m.BACKFILL_RETRY_BASE_SEC
        with patch.object(m, 'fetch_fc2_market', return_value={'outcome': 'failed', 'http_status': 429}):
            m.run_continuous_metadata_backfill([item])
        self.assertEqual(self.rec(item)['failures'], 2)
        self.assertEqual(self.rec(item)['last_route'], 'backfill')
        self.now += 2 * m.BACKFILL_RETRY_BASE_SEC
        with patch.object(m, 'fetch_fc2_market', return_value={'fc2_title': 'サンプル作品', 'fc2_rating': 4.5, 'fc2_review_count': 0}):
            m.enrich_fc2_market([item])
        rec = self.rec(item)
        self.assertEqual((rec['status'], rec['last_route'], rec['failures'], rec['attempts']), ('success', 'refresh', 0, 3))
        self.assertEqual(item['fc2_review_count'], 0)
        self.assertNotIn(item['code'], self.state()['fc2_market_failed'])
        self.now += m.FC2_MARKET_REFRESH_SEC
        with patch.object(m, 'fetch_fc2_market', side_effect=TimeoutError):
            m.enrich_fc2_market([item])
        self.assertEqual(item['fc2_rating'], 4.5)
        self.assertEqual(self.rec(item)['last_success'], rec['last_success'])

    def test_eKYC_records_actual_attempt_but_stops_all_routes(self):
        items = [self.item('1234567'), self.item('2345678')]
        with patch.object(m, 'fetch_fc2_market', return_value={'blocked': 'eKYC'}) as fetch:
            m.enrich_new_fc2_market(items)
            m.run_continuous_metadata_backfill(items)
            m.enrich_fc2_market(items)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.rec(items[0])['status'], 'blocked')
        self.assertEqual(self.rec(items[0])['failures'], 0)
        self.assertNotIn(items[1]['code'], self.state()[m.BACKFILL_ATTEMPTS_KEY])
        self.now += m.FAIL_SKIP_SEC
        with patch.object(m, 'fetch_fc2_market', return_value={'fc2_title': 'サンプル'}) as fetch:
            m.run_continuous_metadata_backfill(items)
        self.assertEqual(fetch.call_count, 2)

    def test_not_found_terminal_in_every_route(self):
        for route in (m.enrich_new_fc2_market, m.run_continuous_metadata_backfill, m.enrich_fc2_market):
            with self.subTest(route=route.__name__):
                m.save_json(m.FETCH_STATE_FILE, {})
                item = self.item()
                with patch.object(m, 'fetch_fc2_market', return_value={'not_found': True}) as fetch:
                    route([item])
                    self.now += 10 * 86400
                    m.enrich_new_fc2_market([item])
                    m.run_continuous_metadata_backfill([item])
                    m.enrich_fc2_market([item])
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(self.rec(item)['status'], 'not_found')

    def test_migrates_success_and_newer_failure_without_replaying_access(self):
        items = [self.item('1234567'), self.item('2345678')]
        m.save_json(m.FETCH_STATE_FILE, {'fc2_market_checked': {x['code']: self.now - 60 for x in items},
                                      'fc2_market_failed': {items[1]['code']: self.now - 30}})
        with patch.object(m, 'fetch_fc2_market') as fetch:
            m.run_continuous_metadata_backfill(items)
        fetch.assert_not_called()
        self.assertEqual(self.rec(items[0])['status'], 'success')
        self.assertEqual(self.rec(items[1])['status'], 'failed')
        self.assertEqual(self.state()[m.CONTINUOUS_BACKFILL_STATS_KEY]['remaining_untried'], 0)

    def test_backoff_cap(self):
        self.assertEqual([m.fc2_retry_delay(x) for x in range(1, 6)], [43200, 86400, 172800, 259200, 259200])

    def test_http_outcomes_and_short_ekyc_redirect(self):
        for info, outcome in [({'status': 404}, 'not_found'), ({'status': 410}, 'not_found'),
                              ({'status': 429}, 'failed'), ({'status': 403, 'cloudflare': True}, 'failed'),
                              ({'status': 0, 'error': 'timeout'}, 'failed'),
                              ({'status': 200, 'final_url': 'https://redirect.fc2.com/ekyc_auth'}, 'blocked')]:
            with self.subTest(info=info), patch.object(m, 'fetch_page', return_value=info):
                result = m.fetch_fc2_market('1234567')
                self.assertEqual(result['outcome'], outcome)
                self.assertEqual(result['http_status'], info['status'])

    def test_review_only_response_does_not_relabel_fallback_rating(self):
        item = self.item(fc2_rating=4.2, fc2_rating_source='FC2ウォーカー')
        with patch.object(m, 'fetch_fc2_market', return_value={'fc2_review_count': 7}):
            m.enrich_new_fc2_market([item])
        self.assertEqual(item['fc2_rating_source'], 'FC2ウォーカー')

    def test_interruption_recovers_metadata_without_duplicate_request(self):
        stale = self.item()
        with patch.object(m,'fetch_fc2_market',return_value={'fc2_title':'サンプル作品','fc2_rating':4.4,'fc2_review_count':12}):
            m.enrich_new_fc2_market([dict(stale)])
        with patch.object(m,'fetch_fc2_market') as fetch:
            m.run_continuous_metadata_backfill([stale])
            m.enrich_fc2_market([stale])
        fetch.assert_not_called()
        self.assertEqual(stale['title'],'サンプル作品')
        self.assertEqual(stale['fc2_rating'],4.4)
        self.assertEqual(self.rec(stale)['attempts'],1)


if __name__ == '__main__':
    unittest.main()
