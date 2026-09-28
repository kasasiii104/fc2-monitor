import contextlib
import copy
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

import fc2_monitor as m


class TitleSelectionTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        for name, filename in (('DATA_FILE', 'data.json'), ('FETCH_STATE_FILE', 'fetch.json'),
                               ('HTML_FILE', 'index.html')):
            self.enterContext(patch.object(m, name, Path(directory) / filename))
        self.enterContext(patch.object(m.requests, 'get', side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch.object(m.requests, 'post', side_effect=AssertionError('Notifications forbidden')))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(patch.object(m, 'now_ts', return_value=1_800_000_000))

    def item(self, title='中文測試作品', **extra):
        return {'code': 'FC2-PPV-8000001', 'code_num': '8000001', 'title': title, **extra}

    def test_chinese_hints_do_not_count_as_japanese(self):
        for title in ('无码人妻視頻在線觀看', '这个女孩们推荐作品', '中文字幕處女調教', '到11/18 1500分【150cm，巨乳】F罩杯25歲，剛成為藥劑師', '第一次拍照和露臉！陽光般光芒四射的東方美人', 'SNS粉絲數超過30萬！大腦錯誤。背部和傳教士'):
            with self.subTest(title=title):
                self.assertTrue(m.looks_chinese_title(title))
                self.assertTrue(m.needs_jp_title(title))
        self.assertFalse(m.looks_chinese_title('人妻限定販売'))
        self.assertFalse(m.needs_jp_title('人妻限定販売'))

    def test_site_description_does_not_count_as_japanese(self):
        for separator in (' - ', ' — ', ' | ', '｜'):
            raw = '中文測試作品' + separator + 'MissAV | オンラインで無料'
            with self.subTest(separator=separator):
                self.assertTrue(m.needs_jp_title(raw))
                self.assertEqual(m.clean_title(raw, '8000001'), '中文測試作品')
                self.assertFalse(m.is_better_title(raw, 'サンプル作品'))
        self.assertEqual(m.clean_title('サンプル作品 - MissAV | オンラインで無料', '8000001'), 'サンプル作品')
        self.assertFalse(m.needs_jp_title('サンプル作品'))
        self.assertEqual(m.clean_title('サンプル - 前編 | 特別版', '8000001'), 'サンプル - 前編 | 特別版')

    def test_extractor_uses_japanese_heading_after_rejecting_polluted_metadata(self):
        soup = BeautifulSoup('''
            <meta property="og:title" content="FC2-PPV-8000001 中文測試作品 - MissAV | オンラインで無料">
            <title>中文測試作品 - MissAV | オンラインで無料</title>
            <h1>FC2-PPV-8000001 サンプル作品</h1>
        ''', 'html.parser')
        self.assertEqual(m.extract_page_title(soup, '8000001'), 'サンプル作品')

    def test_restores_official_title_without_changing_access_or_other_metadata(self):
        item = self.item('長いサンプル作品の仮タイトル - MissAV | オンラインで無料',
                         fc2_title='公式のサンプル', title_source='MissAV',
                         fc2_market_not_found=True, fc2_market_url='',
                         fc2_market_last_success=123, fc2_market_last_attempt=456,
                         fc2_rating=4.2, missav_rank_day=1,
                         preview='https://example.test/preview.mp4')
        before = copy.deepcopy(item)
        self.assertEqual(m.sanitize_item_titles([item]), 1)
        self.assertEqual(item['title'], '公式のサンプル')
        self.assertEqual(item['title_source'], 'FC2公式')
        self.assertEqual({k: v for k, v in item.items() if k not in ('title', 'title_source')},
                         {k: v for k, v in before.items() if k not in ('title', 'title_source')})
        self.assertEqual(m.sanitize_item_titles([item]), 0)

    def test_non_japanese_or_invalid_cached_title_does_not_replace_japanese(self):
        for official in ('中文測試作品 - MissAV | オンラインで無料', 'お探しの商品が見つかりませんでした'):
            with self.subTest(official=official):
                item = self.item('サンプル作品', fc2_title=official, title_source='FC2CMADB')
                m.sanitize_item_titles([item])
                self.assertEqual(item['title'], 'サンプル作品')
                self.assertEqual(item['title_source'], 'FC2CMADB')
        self.assertEqual(item['fc2_title'], '')
        self.assertTrue(item['fc2_market_not_found'])

    def test_official_response_uses_same_clean_language_check(self):
        item = self.item()
        counts = m.apply_fc2_market_metadata(item, {
            'fc2_title': '中文測試作品 - MissAV | オンラインで無料',
        }, m.now_ts())
        self.assertEqual(counts['title_updated'], 0)
        self.assertEqual(item['fc2_title'], '中文測試作品')
        self.assertNotIn('title_source', item)
        counts = m.apply_fc2_market_metadata(item, {'fc2_title': '公式のサンプル'}, m.now_ts())
        self.assertEqual(counts['title_updated'], 1)
        self.assertEqual(item['title_source'], 'FC2公式')

    def test_fallback_requeues_polluted_title_and_records_japanese_source(self):
        item = self.item('中文測試作品 - MissAV | オンラインで無料')
        chinese = BeautifulSoup('<h1>中文測試作品的詳細名稱 - MissAV | オンラインで無料</h1>', 'html.parser')
        japanese = BeautifulSoup('<h1>サンプル作品</h1>', 'html.parser')
        with patch.object(m, 'fetch_soup', side_effect=[chinese, chinese, chinese, japanese]) as fetch:
            m.refresh_japanese_titles([item])
        self.assertEqual(fetch.call_count, 4)
        self.assertEqual(item['title'], 'サンプル作品')
        self.assertEqual(item['title_source'], 'Supjav')
        self.assertNotIn(item['code'], m.load_json(m.FETCH_STATE_FILE, {})['title_fail'])

    def test_fallback_without_japanese_keeps_title_pending_and_respects_retry_delay(self):
        item = self.item()
        chinese = BeautifulSoup('<h1>中文測試作品的詳細名稱 - MissAV | オンラインで無料</h1>', 'html.parser')
        with patch.object(m, 'fetch_soup', return_value=chinese) as fetch:
            m.refresh_japanese_titles([item])
            attempted = fetch.call_count
            m.refresh_japanese_titles([item])
            self.assertEqual(fetch.call_count, attempted)
        self.assertEqual(item['title'], '中文測試作品')
        self.assertEqual(m.load_json(m.FETCH_STATE_FILE, {})['title_fail'][item['code']], m.now_ts())

    def test_walker_only_fills_title_when_japanese_original_is_available(self):
        for title, expected in (('中文測試作品的詳細名稱 - MissAV | オンラインで無料', '中文測試作品'),
                                ('サンプル作品', 'サンプル作品')):
            with self.subTest(title=title):
                m.save_json(m.FETCH_STATE_FILE, {})
                item = self.item()
                records = {item['code']: {'title': title, 'source_url': 'https://example.test/'}}
                with patch.object(m, 'scrape_hwalker_market', return_value=(records, '', 1)):
                    m.enrich_hwalker_market([item])
                self.assertEqual(item['title'], expected)

    def test_build_repairs_saved_and_public_data_offline_and_is_idempotent(self):
        payload = {'updated_at': '2026-09-28 07:12:01', 'items': [
            self.item('中文測試作品 - MissAV | オンラインで無料', fc2_title='公式のサンプル',
                      fc2_market_last_attempt=123, is_new=True, missav_rank_day=1),
            {'code': 'FC2-PPV-8000002', 'code_num': '8000002',
             'title': '中文測試作品 - MissAV | オンラインで無料'},
        ]}
        m.save_json(m.DATA_FILE, payload)
        m.rebuild_site()
        saved = m.load_json(m.DATA_FILE, {})
        catalog = m.load_json(m.HTML_FILE.parent / 'catalog.json', {})
        update = m.load_json(m.HTML_FILE.parent / 'update.json', {})
        self.assertEqual(saved['updated_at'], payload['updated_at'])
        self.assertEqual(saved['items'][0]['title'], '公式のサンプル')
        self.assertEqual(saved['items'][0]['fc2_market_last_attempt'], 123)
        self.assertEqual(saved['items'][1]['title'], '中文測試作品')
        self.assertTrue(m.needs_jp_title(saved['items'][1]['title']))
        self.assertEqual([x['title'] for x in saved['items']], [x['title'] for x in catalog['items']])
        self.assertEqual(update['version'], catalog['version'])
        self.assertEqual((update['item_count'], update['new_count']), (2, 1))
        with patch.object(m, 'save_json', wraps=m.save_json) as save:
            m.rebuild_site()
        self.assertNotIn(m.DATA_FILE, [call.args[0] for call in save.call_args_list])
        self.assertEqual(m.load_json(m.HTML_FILE.parent / 'update.json', {}), update)


if __name__ == '__main__':
    unittest.main()
