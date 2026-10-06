import contextlib
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
import fc2_monitor as m


class ArchiveDiscoveryTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        for name in ('DATA_FILE', 'HISTORY_FILE', 'CRAWL_FILE', 'FETCH_STATE_FILE', 'VIEWS_HISTORY_FILE', 'HTML_FILE'):
            self.enterContext(patch.object(m, name, Path(root) / (name + ('.html' if name == 'HTML_FILE' else '.json'))))
        self.enterContext(patch.dict(m.SOURCE_RETRY_AT, {}, clear=True))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(patch.object(m.requests, 'post', side_effect=AssertionError('No external notifications')))

    def page(self, number, code=None, maximum=2000, current=None):
        code = code or str(8000000 + number)
        current = number if current is None else current
        html = f'<div><input type="number" value="{current}" max="{maximum}"> / {maximum}</div><a href="/ja/fc2-ppv-{code}">テストの作品</a>'
        return {'ok': True, 'status': 200, 'cloudflare': False, 'soup': BeautifulSoup(html, 'html.parser'),
                'final_url': 'https://missav.ws/ja/fc2?page=' + str(number)}

    def test_pagination_reads_published_limit_and_selected_page(self):
        self.assertEqual(m.listing_pagination(self.page(12)['soup']), {'current_page': 12, 'max_page': 2000})
        soup = BeautifulSoup('<div><input x-model="page" value="7"> / 2,000</div>', 'html.parser')
        self.assertEqual(m.listing_pagination(soup), {'current_page': 7, 'max_page': 2000})

    def test_live_numbered_pagination_without_input_or_rel_last(self):
        links = ''.join(f'<a href="/dm597/ja/fc2?page={page}">{page}</a>'
                        for page in [2, 3, 4, 5, 6, 7, 8, 9, 10, 1999, 2000])
        soup = BeautifulSoup(links + '<a rel="next" href="/dm597/ja/fc2?page=2">次へ</a>', 'html.parser')
        self.assertEqual(m.listing_pagination(soup), {'current_page': 1, 'max_page': 2000})
        # An out-of-range request is clamped to page 2000 without a redirect.
        last = BeautifulSoup('<a rel="prev" href="/dm597/ja/fc2?page=1999">前へ</a>', 'html.parser')
        self.assertEqual(m.listing_pagination(last)['current_page'], 2000)

    def test_local_numbered_window_is_not_mistaken_for_the_last_page(self):
        links = ''.join(f'<a href="?page={page}">{page}</a>' for page in [1, 2, 46, 47, 48, 49, 51, 52, 53, 54])
        soup = BeautifulSoup(links + '<a rel="next" href="?page=51">次へ</a>', 'html.parser')
        self.assertEqual(m.listing_pagination(soup), {'current_page': 50, 'max_page': None})

    def test_out_of_range_is_not_requested_or_counted_as_completed(self):
        completed, report = set(), {}
        with patch.object(m, 'fetch_page', side_effect=[self.page(1), self.page(2)]) as fetch:
            rows = m.scrape_missav_catalog([1, 2, 2239, 2240], completed, report)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(completed, {1, 2})
        self.assertEqual(len(rows), 2)
        self.assertEqual(report['status'], 'page_limit')

    def test_repeated_http_200_page_does_not_advance_cursor(self):
        completed, report = set(), {}
        with patch.object(m, 'fetch_page', side_effect=[self.page(1), self.page(2), self.page(10), self.page(11, code='8000010')]) as fetch:
            m.scrape_missav_catalog([1, 2, 10, 11, 12], completed, report)
        self.assertEqual(completed, {1, 2, 10})
        self.assertEqual(fetch.call_count, 4)
        self.assertEqual((report['status'], report['stopped_page']), ('repeated_page', 11))

    def test_previous_run_fingerprint_also_blocks_repeated_archive_page(self):
        completed = set()
        report = {'last_archive': {'page': 10, 'fingerprint': hashlib.sha256(b'8000010').hexdigest()[:16]}}
        with patch.object(m, 'fetch_page', return_value=self.page(11, code='8000010')):
            m.scrape_missav_catalog([11], completed, report)
        self.assertEqual(completed, set())
        self.assertEqual(report['status'], 'repeated_page')

    def test_wrong_page_returned_after_redirect_is_not_success(self):
        completed, report = set(), {}
        with patch.object(m, 'fetch_page', return_value=self.page(42, current=1)):
            self.assertEqual(m.scrape_missav_catalog([42], completed, report), [])
        self.assertFalse(completed)
        self.assertEqual(report['status'], 'page_mismatch')

    def test_denial_stops_page_batch_without_trying_mirror_domains(self):
        denied = {'ok': False, 'status': 403, 'cloudflare': True, 'soup': None}
        for scraper in (m.scrape_missav_catalog, m.scrape_javdb):
            with self.subTest(scraper=scraper.__name__), patch.object(m, 'fetch_page', return_value=denied) as fetch:
                completed, report = set(), {}
                scraper([1, 2, 10, 11], completed, report)
                self.assertEqual(fetch.call_count, 1)
                self.assertFalse(completed)
                self.assertEqual(report['status'], 'access_limited')

    def test_denial_cooldown_is_shared_across_a_sources_domains_and_paths(self):
        response = Mock(status_code=403, text='<title>Just a moment...</title>', url='https://missav.ws/ja/fc2')
        with patch.object(m.requests, 'get', return_value=response) as request, patch.object(m, 'now_ts', return_value=1000):
            m.fetch_page('https://missav.ws/ja/fc2')
            self.assertEqual(m.fetch_page('https://missav.live/ja/search/fc2-ppv')['error'], 'source_cooldown')
            with self.assertRaises(m.requests.HTTPError):
                m.fetch_soup('https://missav.ai/ja/fc2-ppv-8000001')
            self.assertEqual(request.call_count, 1)
        self.assertEqual(m.SOURCE_RETRY_AT['missav'], 1000 + m.DISCOVERY_RETRY_SEC)

    def test_generic_503_is_not_mislabelled_as_a_bot_challenge(self):
        response = Mock(status_code=503, text='<title>Service unavailable</title>', url='https://missav.ws/ja/fc2')
        with patch.object(m.requests, 'get', return_value=response):
            result = m.fetch_page(response.url)
        self.assertFalse(result['cloudflare'])

    def test_legacy_cursor_beyond_confirmed_limit_restarts_in_valid_range(self):
        old = {'missav_page': 2239, 'javdb_page': 129}
        m.save_json(m.CRAWL_FILE, old)
        def catalog(pages, completed_pages, report):
            report['max_page'] = 2000
            completed_pages.update(range(1, m.PAGES + 1))
            return [m.enrich('8000001', 'テストの作品', 'MissAV', 'https://example.test/1')]
        with patch.object(m, 'scrape_missav', return_value=[]) as search, patch.object(m, 'scrape_missav_catalog', side_effect=catalog), patch.object(m, 'scrape_javdb', return_value=[]), patch.object(m, 'scrape_supjav', return_value=[]):
            _, proposed = m.get_latest_videos()
        search.assert_called_once_with(list(range(1, m.PAGES + 1)))
        self.assertEqual(proposed['missav_page'], m.PAGES + 1)
        self.assertEqual(proposed['javdb_page'], 129)
        self.assertTrue(proposed['last_discovery']['sources']['missav']['cycle_completed'])
        self.assertEqual(m.load_json(m.CRAWL_FILE, {}), old)

    def test_no_limit_no_success_never_resets_or_advances_archive_cursor(self):
        m.save_json(m.CRAWL_FILE, {'missav_page': 2239, 'javdb_page': 129})
        row = m.enrich('8000001', 'テストの作品', 'MissAV', 'https://example.test/1')
        with patch.object(m, 'scrape_missav', return_value=[row]), patch.object(m, 'scrape_missav_catalog', return_value=[]), patch.object(m, 'scrape_javdb', return_value=[]), patch.object(m, 'scrape_supjav', return_value=[]):
            _, proposed = m.get_latest_videos()
        self.assertEqual(proposed['missav_page'], 2239)

    def test_all_sources_failing_still_saves_denial_without_cursor_progress(self):
        old = {'missav_page': 123, 'javdb_page': 129}
        m.save_json(m.CRAWL_FILE, old)
        def empty():
            m.SOURCE_RETRY_AT['missav'] = 50000
            return [], {**old, 'missav_page': 999}
        with patch.object(m, 'get_latest_videos', side_effect=empty):
            self.assertEqual(m.main(discovery_only=True), 1)
        state = m.load_json(m.CRAWL_FILE, {})
        self.assertEqual(state['source_retry_at']['missav'], 50000)
        self.assertEqual(state['missav_page'], 123)

    def test_archive_only_run_saves_old_and_new_without_notifications_or_enrichment(self):
        old = m.enrich('8000001', '保存済みの作品', 'MissAV', 'https://example.test/1')
        old.update(first_seen='2026-09-01 00:00:00', missav_rank_day=3, preview='https://example.test/saved.mp4')
        new = m.enrich('7000001', '過去の作品', 'MissAV', 'https://example.test/2')
        new['discovery_kind'] = 'archive'
        m.save_json(m.DATA_FILE, {'items': [old]})
        m.save_json(m.HISTORY_FILE, {'ids': [old['code']]})
        progress = {'missav_page': 21, 'last_discovery': {}}
        with patch.object(m, 'get_latest_videos', return_value=([new], progress)), patch.object(m, 'enrich_new_fc2_market') as enrich, patch.object(m, 'send_telegram') as notify:
            self.assertEqual(m.main(discovery_only=True), 0)
        enrich.assert_not_called()
        notify.assert_not_called()
        data = m.load_json(m.DATA_FILE, {})['items']
        self.assertEqual(len(data), 2)
        preserved = next(x for x in data if x['code'] == old['code'])
        self.assertEqual(preserved['preview'], old['preview'])
        self.assertEqual(preserved['missav_rank_day'], 3)
        self.assertFalse(data[0]['is_new'])
        state = m.load_json(m.CRAWL_FILE, {})
        self.assertEqual((state['last_discovery']['recent_added'], state['last_discovery']['archive_added']), (0, 1))


if __name__ == '__main__':
    unittest.main()
