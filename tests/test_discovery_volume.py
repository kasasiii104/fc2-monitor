import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
import fc2_monitor as m


class DiscoveryVolumeTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ('DATA_FILE', 'HISTORY_FILE', 'CRAWL_FILE', 'FETCH_STATE_FILE',
                     'VIEWS_HISTORY_FILE', 'HTML_FILE'):
            self.enterContext(patch.object(m, name, root / name))
        self.enterContext(patch.dict(m.SOURCE_RETRY_AT, {}, clear=True))
        self.enterContext(patch.object(m, 'PAGES', 16))
        self.enterContext(patch.object(m, 'BACKFILL_PAGES', 48))
        self.enterContext(patch.object(m.requests, 'get', side_effect=AssertionError('No external requests')))
        self.enterContext(patch.object(m.requests, 'post', side_effect=AssertionError('No external notifications')))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def scan(self, denied_page=None):
        def fetch(url):
            page = int(parse_qs(urlsplit(url).query).get('page', ['1'])[0])
            if page == denied_page:
                return {'ok': False, 'status': 403, 'cloudflare': True, 'soup': None}
            content = (f'<input type="number" value="{page}" max="2000">'
                       f'<a href="/ja/fc2-ppv-{8000000 + page}">テスト作品</a>')
            return {'ok': True, 'status': 200, 'cloudflare': False, 'final_url': url,
                    'soup': BeautifulSoup(content, 'html.parser')}
        with patch.object(m, 'scrape_missav', return_value=[]) as search, \
             patch.object(m, 'scrape_javdb', return_value=[]), \
             patch.object(m, 'scrape_supjav', return_value=[]), \
             patch.object(m, 'fetch_page', side_effect=fetch) as request:
            rows, state = m.get_latest_videos()
        search.assert_called_once_with(list(range(1, 17)))
        return rows, state, request

    def test_expanded_scan_resumes_saved_cursor_and_next_run_advances_48_pages(self):
        original = {'missav_page': 1197, 'javdb_page': 129}
        m.save_json(m.CRAWL_FILE, original)
        rows, state, request = self.scan()
        report = state['last_discovery']['sources']['missav']
        self.assertEqual(request.call_count, 64)
        self.assertEqual(len(rows), 64)
        self.assertEqual((report['recent_page_limit'], report['archive_page_limit']), (16, 48))
        self.assertEqual((report['recent_pages'], report['archive_pages']), (16, 48))
        self.assertEqual(state['missav_page'], 1245)
        self.assertEqual(m.load_json(m.CRAWL_FILE, {}), original)
        self.assertEqual(sum(x['discovery_kind'] == 'recent' for x in rows), 16)
        self.assertEqual(sum(x['discovery_kind'] == 'archive' for x in rows), 48)
        m.save_json(m.CRAWL_FILE, state)
        _, next_state, _ = self.scan()
        self.assertEqual(next_state['missav_page'], 1293)

    def test_cursor_inside_new_recent_window_has_no_gap_or_duplicate_page(self):
        m.save_json(m.CRAWL_FILE, {'missav_page': 9, 'javdb_page': 129})
        _, state, request = self.scan()
        pages = [int(parse_qs(urlsplit(call.args[0]).query).get('page', ['1'])[0])
                 for call in request.call_args_list]
        self.assertEqual(pages, list(range(1, 65)))
        self.assertEqual(state['missav_page'], 65)

    def test_larger_batch_stops_at_access_denial_without_skipping_failed_pages(self):
        m.save_json(m.CRAWL_FILE, {'missav_page': 1197, 'javdb_page': 129})
        _, state, request = self.scan(denied_page=1200)
        self.assertEqual(request.call_count, 20)  # 16 recent, 3 archive, 1 denied.
        self.assertEqual(state['missav_page'], 1200)
        report = state['last_discovery']['sources']['missav']
        self.assertEqual((report['status'], report['archive_pages']), ('access_limited', 3))
        self.assertEqual(m.load_json(m.CRAWL_FILE, {})['missav_page'], 1197)

    def test_larger_batch_still_respects_published_page_limit_and_restarts(self):
        m.save_json(m.CRAWL_FILE, {'missav_page': 1990, 'javdb_page': 129})
        _, state, request = self.scan()
        self.assertEqual(request.call_count, 27)  # 16 recent and 1990..2000 only.
        report = state['last_discovery']['sources']['missav']
        self.assertEqual(report['archive_pages'], 11)
        self.assertTrue(report['cycle_completed'])
        self.assertEqual(state['missav_page'], 17)

    def test_duplicate_rows_do_not_inflate_saved_additions_or_catalog(self):
        old = m.enrich('8000001', '保存済み作品', 'MissAV', 'https://example.test/old')
        old['first_seen'] = '2026-09-01 00:00:00'
        recent = m.enrich('8000002', '新しい作品', 'MissAV', 'https://example.test/recent')
        recent['discovery_kind'] = 'recent'
        archive = m.enrich('7000001', '過去作品', 'MissAV', 'https://example.test/archive')
        archive['discovery_kind'] = 'archive'
        m.save_json(m.DATA_FILE, {'items': [old]})
        m.save_json(m.HISTORY_FILE, {'ids': [old['code']]})
        rows = m.merge_videos([[old, recent], [old, recent, archive]])
        progress = {'missav_page': 1245, 'last_discovery': {'sources': {}}}
        with patch.object(m, 'get_latest_videos', return_value=(rows, progress)), \
             patch.object(m, 'send_telegram') as notify:
            self.assertEqual(m.main(discovery_only=True), 0)
        notify.assert_not_called()
        self.assertEqual(len(m.load_json(m.DATA_FILE, {})['items']), 3)
        discovery = m.load_json(m.CRAWL_FILE, {})['last_discovery']
        self.assertEqual((discovery['recent_added'], discovery['archive_added']), (1, 1))


if __name__ == '__main__':
    unittest.main()
