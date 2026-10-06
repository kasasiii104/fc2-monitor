import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
import fc2_monitor as m


class MonitorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, filename in {
            'DATA_FILE': 'data.json', 'HISTORY_FILE': 'notified.json',
            'CRAWL_FILE': 'crawl.json', 'FETCH_STATE_FILE': 'fetch.json',
            'VIEWS_HISTORY_FILE': 'views.json', 'HTML_FILE': 'index.html',
        }.items():
            self.enterContext(patch.object(m, name, root / filename))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(patch.dict(m.SOURCE_RETRY_AT, {}, clear=True))
        self.enterContext(patch.object(m.requests, 'post', side_effect=AssertionError('No notifications in tests')))

    def item(self, num='8000001'):
        return m.enrich(num, 'サンプル作品', 'MissAV', 'https://example.test/item/' + num)

    def sample_response(self, payload):
        response = Mock(status_code=200, url='https://example.test/sample')
        response.json.return_value = payload
        return response

    def test_non_string_sample_paths_are_rejected(self):
        for value in (None, 0, 1, -1, 1.5, True, {}, [], ['sample.mp4']):
            with self.subTest(value=value):
                self.assertEqual(m._absolute_fc2_asset(value), '')
        self.assertEqual(m._absolute_fc2_asset('//example.test/sample.mp4'), 'https://example.test/sample.mp4')
        self.assertEqual(m._absolute_fc2_asset('/sample.mp4'), 'https://adult.contents.fc2.com/sample.mp4')
        self.assertEqual(m._absolute_fc2_asset('javascript:alert(1)'), '')
        self.assertEqual(m._absolute_fc2_asset('https://['), '')

    def test_malformed_primary_field_does_not_hide_valid_fallback(self):
        response = self.sample_response({'path': 1, 'sample_path': 'https://example.test/sample.mp4',
                                         'poster_image_path': {}, 'poster': '//example.test/poster.jpg'})
        with patch.object(m.requests, 'get', return_value=response):
            self.assertEqual(m.fetch_fc2_sample_assets('8000001'), {
                'preview': 'https://example.test/sample.mp4', 'poster': 'https://example.test/poster.jpg',
            })

    def test_bad_sample_keeps_existing_assets_and_next_item_is_processed(self):
        items = [self.item('8000002'), self.item('8000001')]
        old = items[0].copy()
        responses = [self.sample_response({'path': 1, 'poster': 2}),
                     self.sample_response({'path': 'https://example.test/valid.mp4',
                                           'poster': 'https://example.test/valid.jpg'})]
        with patch.object(m.requests, 'get', side_effect=responses):
            m.run_thumbnail_backfill(items)
        self.assertEqual(items[0]['preview'], old['preview'])
        self.assertEqual(items[0]['thumb'], old['thumb'])
        self.assertEqual(items[1]['thumb'], 'https://example.test/valid.jpg')
        stats = m.load_json(m.FETCH_STATE_FILE, {})[m.THUMB_BACKFILL_STATS_KEY]
        self.assertEqual((stats['processed'], stats['failed'], stats['poster_updated']), (2, 1, 1))

    def test_discovery_only_proposes_contiguous_successful_cursors(self):
        old = {'missav_page': 10, 'javdb_page': 20}
        m.save_json(m.CRAWL_FILE, old)

        def catalog(pages, completed_pages, report=None):
            completed_pages.update([1, 2, 10, 12])  # Page 11 failed.
            return [self.item()]

        with patch.object(m, 'scrape_missav', return_value=[]), \
             patch.object(m, 'scrape_missav_catalog', side_effect=catalog), \
             patch.object(m, 'scrape_javdb', return_value=[]), \
             patch.object(m, 'scrape_supjav', return_value=[]):
            items, proposed = m.get_latest_videos()
        self.assertEqual(len(items), 1)
        self.assertEqual(proposed['missav_page'], 11)
        self.assertEqual(proposed['javdb_page'], 20)
        self.assertEqual(m.load_json(m.CRAWL_FILE, {}), old)

    def test_recovery_replays_all_48_missed_pages_and_keeps_blocked_source_pending(self):
        old = {'missav_page': 1017, 'javdb_page': 129,
               'recovery_until': {'missav_page': 1065, 'javdb_page': 153}}
        m.save_json(m.CRAWL_FILE, old)

        def catalog(pages, completed_pages, report=None):
            self.assertEqual(pages, list(range(1, m.PAGES + 1)) + list(range(1017, 1065)))
            completed_pages.update(pages)
            return [self.item()]

        with patch.object(m, 'scrape_missav', return_value=[]), \
             patch.object(m, 'scrape_missav_catalog', side_effect=catalog), \
             patch.object(m, 'scrape_javdb', return_value=[]), \
             patch.object(m, 'scrape_supjav', return_value=[]):
            _, proposed = m.get_latest_videos()
        self.assertEqual(proposed['missav_page'], 1065)
        self.assertEqual(proposed['recovery_until'], {'javdb_page': 153})
        self.assertEqual(m.load_json(m.CRAWL_FILE, {}), old)

    def test_blocked_and_unrecognized_pages_are_not_completed(self):
        good = BeautifulSoup('<a href="/fc2-ppv-8000001">サンプル</a>', 'html.parser')
        empty = BeautifulSoup('<html><body>Unavailable</body></html>', 'html.parser')

        def page(url):
            number = int(parse_qs(urlsplit(url).query).get('page', ['1'])[0])
            return {'ok': number != 2, 'status': 403 if number == 2 else 200,
                    'cloudflare': number == 2, 'soup': good if number == 1 else empty, 'final_url': url}

        for scraper in (m.scrape_missav_catalog, m.scrape_javdb):
            completed = set()
            with self.subTest(scraper=scraper.__name__), patch.object(m, 'fetch_page', side_effect=page):
                scraper([1, 2, 3], completed_pages=completed)
            self.assertEqual(completed, {1})

    def prepare_monitor(self):
        old = self.item('8000000')
        m.save_json(m.DATA_FILE, {'updated_at': 'old', 'items': [old]})
        m.save_json(m.HISTORY_FILE, {'ids': [old['code']]})
        m.save_json(m.CRAWL_FILE, {'missav_page': 10})
        self.enterContext(patch.object(m, 'get_latest_videos', return_value=([self.item()], {'missav_page': 22})))
        for name in ('enrich_new_fc2_market', 'refresh_fc2cmadb_titles', 'run_continuous_metadata_backfill',
                     'enrich_hwalker_market', 'refresh_japanese_titles', 'fill_missing_views', 'enrich_fc2_market',
                     'run_thumbnail_backfill', 'refresh_fc2_previews', 'enrich_fc2_sample_assets', 'update_views_history'):
            self.enterContext(patch.object(m, name, autospec=True, return_value=None))
        self.enterContext(patch.object(m, 'scrape_missav_rankings', return_value={'FC2-PPV-8000001': {'day': 1}}))
        self.notify = self.enterContext(patch.object(m, 'send_telegram', autospec=True))
        self.enterContext(patch.object(m, 'INCLUDE_KEYWORDS', []))
        self.enterContext(patch.object(m, 'EXCLUDE_KEYWORDS', []))

    def test_optional_and_notification_errors_do_not_prevent_save_or_ranking(self):
        self.prepare_monitor()
        m.run_thumbnail_backfill.side_effect = AttributeError('malformed sample')
        self.notify.side_effect = TimeoutError('notification unavailable')
        original_save = m.save_json

        def checked_save(path, payload):
            if path == m.CRAWL_FILE:
                self.assertEqual(len(m.load_json(m.DATA_FILE, {})['items']), 2)
                self.assertTrue(m.HTML_FILE.exists())
                self.assertTrue((m.HTML_FILE.parent / 'update.json').exists())
            return original_save(path, payload)

        with patch.object(m, 'save_json', side_effect=checked_save):
            self.assertEqual(m.main(), 0)
        catalog = m.load_json(m.HTML_FILE.parent / 'catalog.json', {})
        self.assertEqual(len(catalog['items']), 2)
        self.assertEqual(catalog['items'][0]['missav_rank_day'], 1)
        self.assertEqual(m.load_json(m.CRAWL_FILE, {}), {'missav_page': 22})

    def test_data_save_failure_does_not_advance_cursor(self):
        self.prepare_monitor()
        original_save = m.save_json

        def fail(path, payload):
            if path == m.DATA_FILE:
                raise OSError('disk full')
            original_save(path, payload)

        with patch.object(m, 'save_json', side_effect=fail), self.assertRaises(OSError):
            m.main()
        self.assertEqual(m.load_json(m.CRAWL_FILE, {}), {'missav_page': 10})
        self.notify.assert_not_called()

    def test_recrawl_preserves_japanese_title_and_its_actual_source(self):
        self.prepare_monitor()
        saved = self.item()
        saved['title_source'] = 'FC2CMADB'
        saved['source_title'] = '中文測試作品'
        saved['preview_checked_at'] = 123456
        saved['fc2_sample_failures'] = 3
        saved['fc2_sample_retry_at'] = 999999
        saved['fc2_sample_last_success'] = 123000
        saved['preview'] = 'https://example.test/current.mp4'
        saved['preview_fallbacks'] = ['https://example.test/previous.mp4']
        m.save_json(m.DATA_FILE, {'items': [saved]})
        m.save_json(m.HISTORY_FILE, {'ids': [saved['code']]})
        incoming = self.item()
        incoming['title'] = 'サンプル作品の非常に長い別表記 - MissAV | オンラインで無料'
        m.get_latest_videos.return_value = ([incoming], {'missav_page': 22})
        self.assertEqual(m.main(), 0)
        result = m.load_json(m.DATA_FILE, {})['items'][0]
        self.assertEqual(result['title'], saved['title'])
        self.assertEqual(result['title_source'], 'FC2CMADB')
        self.assertEqual(result['source_title'], saved['source_title'])
        self.assertEqual(result['preview_checked_at'], 123456)
        self.assertEqual(result['fc2_sample_failures'], 3)
        self.assertEqual(result['fc2_sample_retry_at'], 999999)
        self.assertEqual(result['fc2_sample_last_success'], 123000)
        self.assertEqual(result['preview'], saved['preview'])
        self.assertIn('https://example.test/previous.mp4', result['preview_fallbacks'])
        self.assertIn(incoming['preview'], result['preview_fallbacks'])
        self.notify.assert_not_called()

    def test_recrawl_recovers_saved_official_title_before_enrichment(self):
        self.prepare_monitor()
        saved = self.item()
        saved.update(title='中文測試作品 - MissAV | オンラインで無料', fc2_title='公式のサンプル')
        m.save_json(m.DATA_FILE, {'items': [saved]})
        m.save_json(m.HISTORY_FILE, {'ids': [saved['code']]})
        incoming = self.item()
        incoming['title'] = '中文測試作品的詳細名稱 - MissAV | オンラインで無料'
        m.get_latest_videos.return_value = ([incoming], {'missav_page': 22})

        def check_enrichment(items):
            self.assertEqual(items[0]['title'], '公式のサンプル')
            self.assertEqual(items[0]['title_source'], 'FC2公式')

        m.enrich_new_fc2_market.side_effect = check_enrichment
        self.assertEqual(m.main(), 0)
        self.assertEqual(m.load_json(m.DATA_FILE, {})['items'][0]['title'], '公式のサンプル')
        self.notify.assert_not_called()

    def test_site_write_failure_does_not_advance_cursor(self):
        self.prepare_monitor()
        with patch.object(m, 'write_site', side_effect=OSError('disk full')), self.assertRaises(OSError):
            m.main()
        self.assertEqual(m.load_json(m.CRAWL_FILE, {}), {'missav_page': 10})
        self.notify.assert_not_called()

    def test_failed_atomic_json_write_preserves_previous_file(self):
        m.save_json(m.CRAWL_FILE, {'missav_page': 10})
        with self.assertRaises(TypeError):
            m.save_json(m.CRAWL_FILE, {'missav_page': object()})
        self.assertEqual(m.load_json(m.CRAWL_FILE, {}), {'missav_page': 10})
        self.assertEqual(list(m.CRAWL_FILE.parent.glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
