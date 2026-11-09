import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fc2_monitor as m
import walker_official_fastlane as fast


class NotifyQueueTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ('DATA_FILE', 'HISTORY_FILE', 'NOTIFY_QUEUE_FILE', 'CRAWL_FILE',
                     'FETCH_STATE_FILE', 'VIEWS_HISTORY_FILE', 'HTML_FILE'):
            self.enterContext(patch.object(m, name, root / name))
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(patch.object(m, 'now_ts', return_value=1_900_000_000))
        self.enterContext(patch.object(m, 'now_jst', return_value='2030-03-17 12:00:00'))
        self.enterContext(patch.object(m, 'INCLUDE_KEYWORDS', []))
        self.enterContext(patch.object(m, 'EXCLUDE_KEYWORDS', []))
        for name in ('enrich_new_fc2_market', 'refresh_fc2cmadb_titles',
                     'run_continuous_metadata_backfill', 'enrich_hwalker_market',
                     'refresh_japanese_titles', 'fill_missing_views', 'enrich_fc2_market',
                     'enrich_fc2_sample_assets', 'run_thumbnail_backfill',
                     'refresh_fc2_previews', 'update_views_history', 'write_site',
                     'save_discovery_cooldowns'):
            self.enterContext(patch.object(m, name, autospec=True))
        self.enterContext(patch.object(m, 'scrape_missav_rankings', return_value={}))
        self.latest = self.enterContext(patch.object(m, 'get_latest_videos'))
        self.send = self.enterContext(patch.object(m, 'send_telegram'))
        self.latest.return_value = ([self.item('8000002'), self.item('8000001')], {})
        m.save_json(m.HISTORY_FILE, {'ids': ['FC2-PPV-8000000']})

    def item(self, num):
        return m.enrich(num, 'サンプル作品', 'MissAV', 'https://example.test/' + num)

    def pending(self):
        return m.load_json(m.NOTIFY_QUEUE_FILE, {})['pending']

    def test_discovery_then_normal_notifies_known_pending_with_latest_title(self):
        self.assertEqual(m.main(discovery_only=True), 0)
        self.send.assert_not_called()
        self.assertEqual(len(self.pending()), 2)
        self.assertIn('FC2-PPV-8000002', m.load_json(m.HISTORY_FILE, {})['ids'])
        def enrich(items):
            for item in items:
                item['title'] = '補完された作品タイトル'
        with patch.object(m, 'enrich_new_fc2_market', new=enrich):
            self.assertEqual(m.main(), 0)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.send.call_count, 2)
        self.assertIn('8000001', self.send.call_args_list[0].args[0])
        self.assertIn('補完された作品タイトル', self.send.call_args_list[0].args[0])

    def test_failure_keeps_unsent_and_success_is_not_repeated(self):
        self.send.side_effect = [None, TimeoutError('offline')]
        self.assertEqual(m.main(), 0)
        self.assertEqual([r['code'] for r in self.pending()], ['FC2-PPV-8000002'])
        self.send.reset_mock(side_effect=True)
        self.assertEqual(m.main(), 0)
        self.send.assert_called_once()
        self.assertIn('8000002', self.send.call_args.args[0])
        self.assertEqual(self.pending(), [])

    def test_legacy_history_does_not_notify_known_items(self):
        m.save_json(m.HISTORY_FILE, {'ids': [v['code'] for v in self.latest.return_value[0]]})
        self.assertFalse(m.NOTIFY_QUEUE_FILE.exists())
        self.assertEqual(m.main(), 0)
        self.send.assert_not_called()
        self.assertEqual(self.pending(), [])

    def test_expiry_limit_and_duplicates(self):
        rows = [{'code': f'FC2-PPV-{7000000 + n}', 'queued_at': 1_900_000_000 - age, 'item': {}, 'held': True}
                for n, age in [(0, 101), (1, 90), (2, 80), (3, 70)]]
        rows.append({**rows[2], 'queued_at': 1_900_000_000})
        with patch.object(m, 'NOTIFY_QUEUE_MAX', 2), patch.object(m, 'NOTIFY_QUEUE_TTL_SEC', 100):
            m.save_notify_queue(rows)
            kept = m.load_notify_queue()
        self.assertEqual([row["code"] for row in kept], [row["code"] for row in rows[2:4]])
        self.assertIn('expired: 1', self.output.getvalue())
        self.assertIn('dropped oldest: 1', self.output.getvalue())

    def test_held_survives_filter_and_release_sends(self):
        item = self.item('8000001')
        m.hold_notification(item['code'], item, 'checking')
        m.hold_notification(item['code'], {**item, 'title': 'changed'}, 'retry')
        row = m.load_notify_queue()[0]
        self.assertEqual(row['item']['title'], item['title'])
        self.assertEqual(row['held_reason'], 'retry')
        with patch.object(m, 'EXCLUDE_KEYWORDS', ['8000001']):
            self.assertEqual(m.main(), 0)
        self.assertEqual([row['code'] for row in self.pending()], [item['code']])
        self.assertEqual(self.send.call_count, 1)
        self.assertIn('8000002', self.send.call_args.args[0])
        self.send.reset_mock()
        self.assertEqual(m.main(), 0)
        self.send.assert_not_called()
        self.assertEqual(len(self.pending()), 1)
        m.release_notification(item['code'])
        self.assertNotIn('held', self.pending()[0])
        self.assertNotIn('held_reason', self.pending()[0])
        self.assertEqual(m.main(), 0)
        self.send.assert_called_once()
        self.assertIn('8000001', self.send.call_args.args[0])
        self.assertEqual(self.pending(), [])

    def test_release_fills_empty_snapshot_and_missing_is_noop(self):
        code = 'FC2-PPV-7000001'
        m.save_notify_queue([{'code': code, 'queued_at': m.now_ts(), 'item': {}, 'held': True}])
        m.release_notification(code, self.item('7000001'))
        self.assertEqual(m.load_notify_queue()[0]['item']['title'], self.item('7000001')['title'])
        m.hold_notification(code, self.item('7000001'), 'again')
        m.release_notification(code, {**self.item('7000001'), 'title': 'replacement'})
        self.assertEqual(m.load_notify_queue()[0]['item']['title'], self.item('7000001')['title'])
        before = m.NOTIFY_QUEUE_FILE.read_text()
        m.release_notification('FC2-PPV-7000002')
        self.assertEqual(m.NOTIFY_QUEUE_FILE.read_text(), before)

    def test_malformed_rows_and_snapshots(self):
        def row(code, **kwargs):
            return {'code': code, 'queued_at': m.now_ts(), 'item': {}, **kwargs}
        rows = [row('invalid'), row('FC2-PPV-7000001', queued_at='123'),
                row('FC2-PPV-7000002', queued_at=True),
                row('FC2-PPV-7000003', item=[]),
                row('FC2-PPV-7000004', held='yes', item={
                    'code': 'wrong', 'sources': [], 'views': '100', 'title': [],
                    'duration': 12, 'source': False, 'url': {}, 'thumb': None}),
                row('FC2-PPV-7000005', item={'sources': {'valid': 'url', 'bad': 5},
                                                      'views': True})]
        mixed_keys = row('FC2-PPV-7000006', item={'sources': {1: 'bad', 'valid': 'url'}})
        self.assertEqual(m.normalize_notify_queue({'pending': [mixed_keys]})[0]['item']['sources'],
                         {'valid': 'url'})
        m.save_notify_queue(rows)
        normalized = m.load_notify_queue()
        self.assertEqual(len(normalized), 2)
        item = normalized[0]['item']
        self.assertEqual(item['code'], 'FC2-PPV-7000004')
        self.assertEqual(item['sources'], {})
        self.assertIsNone(item['views'])
        for key in ('title', 'duration', 'source', 'url', 'thumb'):
            self.assertEqual(item[key], '')
        self.assertIs(normalized[0]['held'], False)
        self.assertEqual(normalized[1]['item']['sources'], {'valid': 'url'})
        self.assertIsNone(normalized[1]['item']['views'])
        self.assertEqual(m.main(), 0)
        self.assertEqual(self.send.call_count, 4)
        self.assertEqual(self.pending(), [])

    def test_message_error_discards_only_bad_row(self):
        def break_first(items):
            next(item for item in items if item['code'] == 'FC2-PPV-8000001')['views'] = 'bad'
        with patch.object(m, 'fill_missing_views', new=break_first):
            self.assertEqual(m.main(), 0)
        self.send.assert_called_once()
        self.assertIn('8000002', self.send.call_args.args[0])
        self.assertEqual(self.pending(), [])
        self.assertIn('discarded malformed message: FC2-PPV-8000001', self.output.getvalue())

    def test_dead_missav_state_survives_latest_merge(self):
        old = self.item('8000001')
        old.update(missav_dead=True, missav_dead_checked_at=123, url='',
                   sources={'FC2公式': 'https://official.test/'})
        m.save_json(m.DATA_FILE, {'items': [old]})
        self.assertEqual(m.main(), 0)
        saved = next(item for item in m.load_json(m.DATA_FILE, {})['items']
                     if item['code'] == old['code'])
        self.assertIs(saved['missav_dead'], True)
        self.assertEqual(saved['missav_dead_checked_at'], 123)
        # Missing MissAV destinations use the standard template.
        self.assertEqual(saved['url'], m.missav_detail_urls('8000001')[0])
        self.assertEqual(saved['sources'], {**old['sources'], 'MissAV': saved['url']})

    def test_dead_merge_preserves_missav_mirror_and_replaces_official_url(self):
        for url in ('https://adult.contents.fc2.com/article/8000001/',
                    'https://missav.live/ja/fc2-ppv-8000001'):
            with self.subTest(url=url):
                old = self.item('8000001')
                old.update(missav_dead=True, url=url,
                           sources={'FC2公式': 'https://official.test/', 'MissAV': 'https://missav.live/ja/fc2-ppv-8000001'})
                m.save_json(m.DATA_FILE, {'items': [old]})
                self.assertEqual(m.main(), 0)
                saved = next(item for item in m.load_json(m.DATA_FILE, {})['items'] if item['code'] == old['code'])
                expected = url if m.discovery_source(url) == 'missav' else m.missav_detail_urls('8000001')[0]
                self.assertEqual(saved['url'], expected)
                self.assertEqual(saved['sources'], old['sources'])

    def test_dead_merge_tolerates_legacy_non_str_url_and_null_sources(self):
        old = self.item('8000001')
        for sources in (None, ['x'], 'text', 7):
            with self.subTest(sources=sources):
                old = self.item('8000001')
                old.update(missav_dead=True, url=123, sources=sources)
                m.save_json(m.DATA_FILE, {'items': [old]})
                self.assertEqual(m.main(), 0)
                saved = next(item for item in m.load_json(m.DATA_FILE, {})['items'] if item['code'] == old['code'])
                self.assertEqual(saved['url'], m.missav_detail_urls('8000001')[0])
                self.assertEqual(saved['sources']['MissAV'], saved['url'])

    def test_invalid_queue_is_empty(self):
        for payload in ([], {}, {'pending': {}}, {'pending': [{}]}):
            m.save_json(m.NOTIFY_QUEUE_FILE, payload)
            self.assertEqual(m.load_notify_queue(), [])
        m.NOTIFY_QUEUE_FILE.write_text('{broken')
        self.assertEqual(m.load_notify_queue(), [])

    def test_snapshot_fallback_and_excluded_items_are_removed(self):
        m.save_notify_queue([{'code': 'FC2-PPV-7000001', 'queued_at': m.now_ts(),
                              'item': self.item('7000001')}])
        m.save_json(m.HISTORY_FILE, {'ids': [v['code'] for v in self.latest.return_value[0]]})
        self.assertEqual(m.main(), 0)
        self.assertIn('7000001', self.send.call_args.args[0])
        m.save_notify_queue([{'code': 'FC2-PPV-7000001', 'queued_at': m.now_ts(),
                              'item': self.item('7000001')}])
        self.send.reset_mock()
        with patch.object(m, 'EXCLUDE_KEYWORDS', ['7000001']):
            self.assertEqual(m.main(), 0)
        self.send.assert_not_called()
        self.assertEqual(self.pending(), [])

    def test_queue_saved_before_history(self):
        original = m.save_json
        def checked(path, payload):
            if path == m.HISTORY_FILE:
                self.assertEqual(len(self.pending()), 2)
                raise OSError('interrupted')
            original(path, payload)
        with patch.object(m, 'save_json', side_effect=checked), self.assertRaises(OSError):
            m.main(discovery_only=True)
        self.assertEqual(len(self.pending()), 2)

    def test_first_run_only_sends_startup(self):
        m.HISTORY_FILE.unlink()
        self.assertEqual(m.main(), 0)
        self.assertEqual(self.pending(), [])
        self.send.assert_called_once()
        self.assertIn('監視を開始しました', self.send.call_args.args[0])

    def test_fastlane_preserves_newer_numbers_for_monitor(self):
        m.save_json(m.HISTORY_FILE, {'ids': ['FC2-PPV-8000000']})
        m.save_json(m.FETCH_STATE_FILE, {m.H_WALKER_CACHE_KEY: {
            'FC2-PPV-' + num: {'title': 'サンプル作品', 'checked_at': m.now_ts(),
                             'source_url': 'https://fc2cm.h-walker.net/movie/' + num + '/'}
            for num in ('7999999', '8000001')}})
        with patch.object(m, 'fetch_fc2_market', return_value={
                'outcome': 'success', 'fc2_title': '公式サンプル作品',
                'fc2_market_url': 'https://adult.contents.fc2.com/'}), \
             patch.object(fast, 'WORKERS', 1), patch.object(fast, 'CHECK_LIMIT', 10):
            self.assertEqual(fast.main(), 0)
        self.assertEqual(m.load_json(m.HISTORY_FILE, {})['ids'],
                         ['FC2-PPV-7999999', 'FC2-PPV-8000000'])
        self.assertEqual(len(m.load_json(m.DATA_FILE, {})['items']), 2)


if __name__ == '__main__':
    unittest.main()
