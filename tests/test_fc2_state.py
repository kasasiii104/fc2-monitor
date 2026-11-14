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
        with patch.object(m, 'fetch_fc2_market', return_value={'outcome': 'failed', 'http_status': 503}):
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

    def test_not_found_thirty_day_boundary_and_renewal(self):
        item = self.item()
        with patch.object(m, 'fetch_fc2_market', return_value={'outcome': 'not_found'}) as fetch:
            m.enrich_new_fc2_market([item])
            self.assertEqual(self.rec(item)['not_found_at'], self.now)
            self.now += 30 * 86400 - 1
            m.run_continuous_metadata_backfill([item])
            self.assertEqual(fetch.call_count, 1)
            self.now += 1
            m.run_continuous_metadata_backfill([item])
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(self.rec(item)['not_found_at'], self.now)
            m.enrich_fc2_market([item])
            self.assertEqual(fetch.call_count, 2)

    def test_legacy_not_found_wait_and_backfill_limit(self):
        items = [self.item(str(n), fc2_market_not_found=True) for n in range(100, 103)]
        m.save_json(m.FETCH_STATE_FILE, {m.BACKFILL_ATTEMPTS_KEY: {
            items[0]['code']: {'status': 'not_found', 'last_attempt': self.now - 90 * 86400}}})
        with patch.object(m, 'fetch_fc2_market', return_value={'outcome': 'success', 'fc2_title': '復活作品'}) as fetch:
            m.run_continuous_metadata_backfill(items)
            fetch.assert_not_called()
            for item in items:
                self.assertEqual(self.rec(item)['not_found_at'], self.now)
            self.now += 30 * 86400
            with patch.object(m, 'BACKFILL_OFFICIAL_LIMIT', 1):
                m.run_continuous_metadata_backfill(items)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(sum(not x['fc2_market_not_found'] for x in items), 1)

    def test_legacy_not_found_without_last_attempt_is_not_selected(self):
        for record in (None, {'status': 'not_found'}):
            with self.subTest(record=record):
                item = self.item(fc2_market_not_found=True)
                attempts = {} if record is None else {item['code']: record}
                m.save_json(m.FETCH_STATE_FILE, {m.BACKFILL_ATTEMPTS_KEY: attempts})
                with patch.object(m, 'fetch_fc2_market') as fetch:
                    m.run_continuous_metadata_backfill([item])
                fetch.assert_not_called()
                self.assertEqual(self.state()[m.CONTINUOUS_BACKFILL_STATS_KEY]['selected'], 0)
                self.assertEqual(self.rec(item)['not_found_at'], self.now)

    def test_not_found_rechecks_keep_original_stats_population(self):
        regular = self.item('100')
        waiting = self.item('101', fc2_market_not_found=True)
        due = self.item('102', fc2_market_not_found=True)
        m.save_json(m.FETCH_STATE_FILE, {m.BACKFILL_ATTEMPTS_KEY: {
            due['code']: {'status': 'not_found', 'last_attempt': self.now - 30 * 86400,
                          'not_found_at': self.now - 30 * 86400}}})
        with patch.object(m, 'BACKFILL_OFFICIAL_LIMIT', 0), patch.object(m, 'fetch_fc2_market') as fetch:
            m.run_continuous_metadata_backfill([regular, waiting, due])
        fetch.assert_not_called()
        stats = self.state()[m.CONTINUOUS_BACKFILL_STATS_KEY]
        for key in ('candidates_before', 'untried_before', 'title_missing_before',
                    'rating_missing_before', 'review_missing_before', 'remaining_candidates'):
            self.assertEqual(stats[key], 1, key)
        for key in ('retry_ready_before', 'retry_waiting_before'):
            self.assertEqual(stats[key], 0, key)
        self.assertEqual(stats['not_found_recheck_due'], 1)
        self.assertEqual(stats['not_found_waiting'], 1)

    def test_legacy_observation_persists_when_loader_caller_does_not_save(self):
        for record in (None, {'status': 'not_found'}, {'status': 'failed'}):
            with self.subTest(record=record):
                item = self.item(fc2_market_not_found=True)
                attempts = {} if record is None else {item['code']: record}
                m.save_json(m.FETCH_STATE_FILE, {m.BACKFILL_ATTEMPTS_KEY: attempts})
                observed = self.now
                m.load_fc2_market_state([item])
                self.assertEqual(self.rec(item)['not_found_at'], observed)
                self.now += 30 * 86400
                state = m.load_fc2_market_state([item])
                self.assertTrue(m.fc2_market_due(item, state, self.now))
                self.assertEqual(self.rec(item)['not_found_at'], observed)

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
                              ({'status': 429}, 'blocked'), ({'status': 403, 'cloudflare': True}, 'blocked'),
                              ({'status': 0, 'error': 'timeout'}, 'failed'),
                              ({'status': 200, 'final_url': 'https://redirect.fc2.com/ekyc_auth'}, 'blocked')]:
            with self.subTest(info=info), patch.object(m, 'fetch_page', return_value=info):
                result = m.fetch_fc2_market('1234567')
                self.assertEqual(result['outcome'], outcome)
                self.assertEqual(result['http_status'], info['status'])

    def fetch_identity_page(self, final_url, body):
        markup = f'<html><body>{body}</body></html>'
        info = {'ok': True, 'status': 200, 'final_url': final_url,
                'text': markup, 'soup': m.BeautifulSoup(markup, 'html.parser')}
        with patch.object(m, 'fetch_page', return_value=info):
            return m.fetch_fc2_market('1234')

    def assert_unexpected_identity(self, final_url, body):
        result = self.fetch_identity_page(final_url, body)
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['reason'], 'unexpected_page')
        self.assertNotIn('not_found', result)

    def test_market_identity_rejects_longer_article_code(self):
        self.assert_unexpected_identity('https://adult.contents.fc2.com/article/12345/', '作品 12345')

    def test_market_identity_rejects_embedded_body_code(self):
        self.assert_unexpected_identity('https://adult.contents.fc2.com/search/', '作品 51234')

    def test_market_identity_accepts_article_with_query(self):
        result = self.fetch_identity_page('https://adult.contents.fc2.com/article/1234/?lang=ja', '作品 1234')
        self.assertEqual(result['outcome'], 'success')

    def test_market_identity_accepts_article_without_body_code(self):
        for suffix in ('', '/', '/?lang=ja'):
            with self.subTest(suffix=suffix):
                result = self.fetch_identity_page(f'https://adult.contents.fc2.com/article/1234{suffix}', '作品紹介')
                self.assertEqual(result['outcome'], 'success')

    def test_market_identity_rejects_other_host_article(self):
        for host in ('example.com', 'adult.contents.fc2.com.example.com'):
            with self.subTest(host=host):
                self.assert_unexpected_identity(f'https://{host}/article/1234/', '作品紹介')

    def test_market_identity_accepts_bounded_body_code(self):
        result = self.fetch_identity_page('https://adult.contents.fc2.com/search/', '作品 FC2-1234 番号')
        self.assertEqual(result['outcome'], 'success')

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
