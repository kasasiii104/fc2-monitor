import contextlib
import copy
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

import fc2_monitor as m


class TitleSelectionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(m.os.environ, {"RUNNER_TEMP": "", "GITHUB_RUN_ID": "", "RUN_DEADLINE_SEC": "2700"}))
        self.enterContext(patch.object(m, "_RUN_STARTED", None))
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

    def test_deadline_between_sources_does_not_record_failure(self):
        item = self.item()
        with patch.object(m, 'run_deadline_reached', side_effect=[False, False, True]), patch.object(m, 'fetch_soup', return_value=BeautifulSoup('', 'html.parser')) as fetch:
            m.refresh_japanese_titles([item])
        state = m.load_json(m.FETCH_STATE_FILE, {})
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(state['japanese_title_stats']['requests'], 1)
        self.assertEqual(state['japanese_title_stats']['tried'], 0)
        self.assertNotIn(item['code'], state['title_fail'])

    def test_chinese_hints_do_not_count_as_japanese(self):
        for title in ('无码人妻視頻在線觀看', '这个女孩们推荐作品', '中文字幕處女調教', '到11/18 1500分【150cm，巨乳】F罩杯25歲，剛成為藥劑師', '第一次拍照和露臉！陽光般光芒四射的東方美人', 'SNS粉絲數超過30萬！大腦錯誤。背部和傳教士'):
            with self.subTest(title=title):
                self.assertTrue(m.looks_chinese_title(title))
                self.assertTrue(m.needs_jp_title(title))
        self.assertFalse(m.looks_chinese_title('人妻限定販売'))
        self.assertFalse(m.needs_jp_title('人妻限定販売'))
        for title in (
            '980pt 僅限 3 天！ 【I Cup】「我就是胸？」諮詢巨乳想見人，結果胸',
            '* 退出某大型FC2銷售群【GW限定特賣】天使般的奇蹟般的一年級生。',
            '* 數量有限@4739pt → 2930pt 【完整出場】H罩杯巨乳已婚女人',
            '*限定巨乳【3天限定特賣】Vtuber Yomi*，毛茸茸的天然Gcup',
            '*請多多支持新人m(__)m【素人奇聞趣事NTR】科羅娜也聚集了',
        ):
            with self.subTest(real_production_chinese=title):
                self.assertTrue(m.looks_chinese_title(title))
                self.assertTrue(m.needs_jp_title(title))

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

    def test_shared_kanji_do_not_reject_normal_japanese(self):
        for title in ('週末の旅とその後の記録', '長い一日の物語', '開発の結果を発表します',
                      '時間をかけた新しい作品', '数量限定の特別版', '推薦された本を紹介します',
                      '体験してみた新しい世界', '東方への旅と万年筆の話'):
            with self.subTest(title=title):
                self.assertFalse(m.looks_chinese_title(title))
                self.assertFalse(m.needs_jp_title(title))
                item = self.item(title, fc2_title=title)
                m.sanitize_item_titles([item])
                self.assertEqual(item['title'], title)
        self.assertTrue(m.needs_jp_title('這部サンプル作品真的很好看'))

    def test_pending_title_keeps_source_text_and_can_recover_reclassified_text(self):
        item = self.item()
        m.sanitize_item_titles([item])
        self.assertEqual(item['source_title'], '中文測試作品')
        self.assertEqual(item['title'], item['code'])
        item['source_title'] = 'その後のサンプル作品'
        m.sanitize_item_titles([item])
        self.assertEqual(item['title'], 'その後のサンプル作品')

    def test_extractor_uses_japanese_heading_after_rejecting_polluted_metadata(self):
        soup = BeautifulSoup('''
            <meta property="og:title" content="FC2-PPV-8000001 中文測試作品 - MissAV | オンラインで無料">
            <title>中文測試作品 - MissAV | オンラインで無料</title>
            <h1>FC2-PPV-8000001 サンプル作品</h1>
        ''', 'html.parser')
        self.assertEqual(m.extract_page_title(soup, '8000001'), 'サンプル作品')

    def test_detail_numberless_titles_ignore_related_work_identifiers(self):
        soup = BeautifulSoup('''
            <meta property="og:title" content="サンプル作品">
            <meta name="twitter:title" content="サンプル作品">
            <title>サンプル作品</title><h1>サンプル作品</h1>
            <section><a href="/ja/fc2-ppv-8000002">FC2-PPV-8000002 関連作品</a></section>
            <h1>FC2-PPV-8000002 関連作品</h1>
        ''', 'html.parser')
        self.assertEqual(m.extract_page_title(soup, '8000001'), 'サンプル作品')

    def test_detail_rejects_other_work_in_title_candidates(self):
        for tag in ('<meta property="og:title" content="FC2-PPV-8000002 サンプル作品">',
                    '<meta name="twitter:title" content="FC2-PPV-8000002 サンプル作品">',
                    '<title>FC2-PPV-8000002 サンプル作品</title>',
                    '<h1>FC2-PPV-8000002 サンプル作品</h1>'):
            with self.subTest(tag=tag):
                soup = BeautifulSoup(tag + '<p>FC2-PPV-8000001</p>', 'html.parser')
                self.assertEqual(m.extract_page_title(soup, '8000001'), '')

    def test_detail_rejects_other_work_canonical_path(self):
        for path in ('/ja/fc2-ppv-8000002', '/articles/8000002', '/ja/fc2-ppv-18000001'):
            with self.subTest(path=path):
                soup = BeautifulSoup(
                    '<h1>FC2-PPV-8000001 サンプル作品</h1>'
                    f'<link rel="canonical" href="https://missav.ws{path}?q=8000001">',
                    'html.parser')
                self.assertEqual(m.extract_page_title(soup, '8000001'), '')
        for href in ('/ja/fc2-ppv-8000001', '/ja/detail?q=8000002'):
            with self.subTest(href=href):
                soup = BeautifulSoup(
                    f'<h1>サンプル作品</h1><link rel="canonical" href="{href}">', 'html.parser')
                self.assertEqual(m.extract_page_title(soup, '8000001'), 'サンプル作品')

    def test_detail_response_identity_and_legacy_numberless_titles(self):
        for final_url, body, accepted in (
            ('https://missav.ws/ja/fc2-ppv-8000002', 'サンプル作品', False),
            ('https://outside.test/ja/fc2-ppv-8000001', 'サンプル作品', False),
            ('https://missav.ws/ja/fc2-ppv-18000001', 'サンプル作品', False),
            ('https://missav.ws/ja/fc2-ppv-8000001', 'FC2-PPV-8000002 サンプル作品', False),
            ('https://missav.ws/ja/fc2-ppv-8000001', 'FC2-PPV-8000001 サンプル作品', True),
            ('https://missav.ws/ja/detail', 'サンプル作品', True),
        ):
            with self.subTest(final_url=final_url, body=body):
                m.save_json(m.FETCH_STATE_FILE, {})
                item = self.item()
                response = Mock(status_code=200, text='<h1>' + body + '</h1>', url=final_url)
                empty = BeautifulSoup('', 'html.parser')
                real_fetch = m.fetch_soup
                with patch.object(m.requests, 'get', return_value=response), patch.object(
                        m, 'fetch_soup', side_effect=lambda url: real_fetch(url)
                        if url.startswith('https://missav.ws/') else empty) as fetch:
                    m.refresh_japanese_titles([item])
                self.assertEqual(item.get('title_source') == 'MissAV', accepted)
                self.assertEqual(fetch.call_count, 1 if accepted else 5)

    def test_fc2cmadb_identity_and_numberless_payload(self):
        for article, path, accepted in (
            ({'article_id': 8000002}, '/articles/8000001', False),
            ({'article_id': 18000001}, '/articles/8000001', False),
            ({'url': '/articles/8000002'}, '/articles/8000001', False),
            ({'article_id': 8000001}, '/articles/8000002', False),
            ({'article_id': 8000001}, '/articles/8000001', True),
            ({}, '/articles/detail', True),
            ({'id': 99999}, '/articles/detail', True),
        ):
            with self.subTest(article=article, path=path):
                payload = {'props': {'article': dict(article, title='サンプル作品')}}
                response = Mock(status_code=200, headers={'content-type': 'application/json'},
                                text='{}', url=m.FC2CMADB_URL + path)
                response.json.return_value = payload
                with patch.object(m.requests, 'get', return_value=response) as get:
                    result = m.fetch_fc2cmadb_title('8000001')
                self.assertEqual(bool(result and result.get('title')), accepted)
                self.assertEqual(get.call_count, 1 if accepted else 2)

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
        japanese = BeautifulSoup('<h1>検索結果</h1><a href="/ja/123.html">FC2-PPV-8000001 サンプル作品</a>', 'html.parser')
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

    def test_search_titles_must_belong_to_requested_work(self):
        soup = BeautifulSoup('''<title>サンプル作品の検索結果</title>
            <h1>検索結果</h1><a href="/?s=FC2PPV+8000001">FC2-PPV-8000001 検索結果</a>
            <a href="/ja/1.html">FC2-PPV-8000002 別のサンプル作品</a>''', 'html.parser')
        self.assertEqual(m.extract_search_title(soup, '8000001'), '')
        soup.append(BeautifulSoup('<a href="/ja/2.html" title="FC2-PPV-8000001 正しいサンプル作品">FC2-PPV-8000001 正しいサンプル作品</a>', 'html.parser'))
        self.assertEqual(m.extract_search_title(soup, '8000001'), '正しいサンプル作品')

    def test_historical_queue_prioritizes_untried_then_oldest_without_cursor_skips(self):
        items = [self.item(code=f'FC2-PPV-{num}', code_num=str(num)) for num in range(8000001, 8000004)]
        m.save_json(m.FETCH_STATE_FILE, {'title_cursor': 2,
                    'title_fail': {items[0]['code']: m.now_ts() - m.FAIL_SKIP_SEC - 1}})
        soup = BeautifulSoup('<h1>日本語のサンプル作品</h1>', 'html.parser')
        with patch.object(m, 'TITLE_FETCH_LIMIT', 1), patch.object(m, 'fetch_soup', return_value=soup):
            m.refresh_japanese_titles(items)
            self.assertEqual(items[1]['title'], '日本語のサンプル作品')
            m.refresh_japanese_titles(items)
            self.assertEqual(items[2]['title'], '日本語のサンプル作品')
            m.refresh_japanese_titles(items)
            self.assertEqual(items[0]['title'], '日本語のサンプル作品')
        stats = m.load_json(m.FETCH_STATE_FILE, {})['japanese_title_stats']
        self.assertEqual((stats['pending_after'], stats['historical_tried']), (0, 1))

    def test_blocked_hosts_are_not_requested_for_every_pending_title(self):
        items = [self.item(code=f'FC2-PPV-{num}', code_num=str(num)) for num in range(8000001, 8000004)]
        error = m.requests.HTTPError(response=Mock(status_code=403))
        with patch.object(m, 'fetch_soup', side_effect=error) as fetch:
            m.refresh_japanese_titles(items)
            self.assertEqual(fetch.call_count, 5)
            m.refresh_japanese_titles(items)
            self.assertEqual(fetch.call_count, 5)
        state = m.load_json(m.FETCH_STATE_FILE, {})
        self.assertEqual(len(state['title_fail']), 1)
        self.assertEqual(state['japanese_title_stats']['untried'], 2)

    def test_walker_parses_real_link_shapes_ratings_and_pagination(self):
        pages = [BeautifulSoup('''<table><tr>
            <td><a href="https://adult.contents.fc2.com/aff.php?aid=8000001&amp;ref=test">時間をかけた作品</a></td>
            <td>4.25</td><td>123件</td></tr></table>
            <a href="/0/2/1/0/0/">次の 50 件</a>''', 'html.parser'),
            BeautifulSoup('''<table><tr><td><a href="https://adult.contents.fc2.com/article/8000002/">その後のサンプル</a></td>
            <td>3.5</td><td>0件</td></tr></table>''', 'html.parser')]
        with patch.object(m, 'fetch_page', side_effect=[{'status': 200, 'soup': p} for p in pages]) as fetch:
            records, next_url, count = m.scrape_hwalker_market(page_limit=2)
        self.assertEqual((len(records), next_url, count), (2, '', 2))
        self.assertEqual(fetch.call_args_list[1].args[0], m.H_WALKER_URL + '/0/2/1/0/0/')
        self.assertEqual(records['FC2-PPV-8000001']['rating'], 4.25)
        self.assertEqual(records['FC2-PPV-8000001']['review_count'], 123)
        self.assertEqual(records['FC2-PPV-8000002']['review_count'], 0)

    def test_repeated_host_timeouts_do_not_delay_the_whole_catalog(self):
        items = [self.item(code=f'FC2-PPV-{num}', code_num=str(num)) for num in range(8000001, 8000021)]
        with patch.object(m, 'fetch_soup', side_effect=m.requests.Timeout) as fetch:
            m.refresh_japanese_titles(items)
        self.assertEqual(fetch.call_count, 15)  # Three failures per existing host.
        stats = m.load_json(m.FETCH_STATE_FILE, {})['japanese_title_stats']
        self.assertEqual((stats['tried'], stats['untried']), (3, 17))

    def test_walker_does_not_advance_over_unrecognized_or_blocked_pages(self):
        soup = BeautifulSoup('<h1>Unavailable</h1><a href="/next/">次の50件</a>', 'html.parser')
        for status in (200, 403):
            with self.subTest(status=status), patch.object(m, 'fetch_page', return_value={'status': status, 'soup': soup}):
                records, next_url, count = m.scrape_hwalker_market(start_url=m.H_WALKER_URL + '/saved/')
            self.assertEqual((records, next_url, count), ({}, m.H_WALKER_URL + '/saved/', 0))

    def test_walker_cache_supplies_later_discoveries_during_cooldown(self):
        record = {'FC2-PPV-8000001': {'title': '後から追加された作品', 'rating': 4.0,
                                    'review_count': 10, 'source_url': m.H_WALKER_URL + '/'}}
        with patch.object(m, 'scrape_hwalker_market', return_value=(record, m.H_WALKER_URL + '/next/', 1)) as crawl:
            m.enrich_hwalker_market([])
            item = self.item(fc2_rating_source='FC2公式', fc2_rating=5.0, fc2_review_count=20)
            m.enrich_hwalker_market([item])
        self.assertEqual(crawl.call_count, 1)
        self.assertEqual(item['title'], '後から追加された作品')
        self.assertEqual((item['fc2_rating'], item['fc2_review_count']), (5.0, 20))
        state = m.load_json(m.FETCH_STATE_FILE, {})
        self.assertEqual(state[m.H_WALKER_CURSOR_KEY], m.H_WALKER_URL + '/next/')

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
        self.assertEqual(saved['items'][1]['title'], 'FC2-PPV-8000002')
        self.assertTrue(m.needs_jp_title(saved['items'][1]['title']))
        self.assertEqual([x['title'] for x in saved['items']], [x['title'] for x in catalog['items']])
        self.assertEqual(update['version'], catalog['version'])
        self.assertEqual((update['item_count'], update['new_count']), (2, 1))
        with patch.object(m, 'save_json', wraps=m.save_json) as save:
            m.rebuild_site()
        self.assertNotIn(m.DATA_FILE, [call.args[0] for call in save.call_args_list])
        self.assertEqual(m.load_json(m.HTML_FILE.parent / 'update.json', {}), update)

    def test_title_repair_restores_snapshot_without_rolling_back_new_data_or_notifying(self):
        recovery = m.DATA_FILE.parent / 'recovery.json'
        m.save_json(recovery, {'items': [self.item('その後の日本語作品', title_source='MissAV'),
                self.item('以前のサンプル', code='FC2-PPV-8000002')]})
        items = [self.item('FC2-PPV-8000001', preview='https://example.test/new.mp4', missav_rank_day=1),
                 self.item('更新されたサンプル', code='FC2-PPV-8000002'),
                 self.item('新しく追加されたサンプル', code='FC2-PPV-8000003')]
        m.save_json(m.DATA_FILE, {'updated_at': '2026-09-29 14:00:00', 'items': items})
        with patch.dict(os.environ, {'TITLE_RECOVERY_FILE': str(recovery)}), \
             patch.object(m, 'enrich_hwalker_market'), patch.object(m, 'send_telegram') as notify:
            m.repair_saved_titles()
        saved = m.load_json(m.DATA_FILE, {})
        self.assertEqual(saved['updated_at'], '2026-09-29 14:00:00')
        self.assertEqual(len(saved['items']), 3)
        self.assertEqual(saved['items'][0]['title'], 'その後の日本語作品')
        self.assertEqual(saved['items'][0]['preview'], items[0]['preview'])
        self.assertEqual(saved['items'][0]['missav_rank_day'], 1)
        self.assertEqual(saved['items'][1]['title'], '更新されたサンプル')
        notify.assert_not_called()
        stats = m.load_json(m.FETCH_STATE_FILE, {})['japanese_title_repair_stats']
        self.assertEqual(stats['restored_from_snapshot'], 1)

    def test_priority_helper_preserves_attempt_history(self):
        import prioritize_title_backfill as helper
        item = self.item()
        rec = dict(last_attempt=1, attempts=7, failures=2, status='failed', next_retry_at=2)
        m.save_json(m.DATA_FILE, {'items': [item]})
        m.save_json(m.FETCH_STATE_FILE, {m.BACKFILL_ATTEMPTS_KEY: {item['code']: rec}})
        helper.main()
        state = m.load_json(m.FETCH_STATE_FILE, {})
        self.assertEqual(state[m.BACKFILL_ATTEMPTS_KEY][item['code']], {**rec, 'title_priority': True})
        m.validate_crawl_state(m.FETCH_STATE_FILE, state)

    def test_priority_selection_respects_cooldown_and_limit(self):
        items = [self.item(code=f'FC2-PPV-{n}', code_num=str(n)) for n in range(8000001, 8000005)]
        records = {items[0]['code']: dict(last_attempt=1, title_priority=True),
                   items[1]['code']: dict(last_attempt=m.now_ts(), next_retry_at=m.now_ts()+100, title_priority=True)}
        m.save_json(m.FETCH_STATE_FILE, {m.BACKFILL_ATTEMPTS_KEY: records})
        with patch.object(m, 'BACKFILL_OFFICIAL_LIMIT', 1), patch.object(m, 'fetch_fc2_market', return_value={'outcome': 'failed'}) as fetch:
            m.run_continuous_metadata_backfill(items)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[0], '8000001')
        saved = m.load_json(m.FETCH_STATE_FILE, {})[m.BACKFILL_ATTEMPTS_KEY]
        self.assertNotIn('title_priority', saved[items[0]['code']])
        self.assertTrue(saved[items[1]['code']]['title_priority'])

    def test_priority_cleared_only_after_actual_attempt(self):
        for meta in ({'outcome': 'success'}, {'outcome': 'failed'}, {'outcome': 'not_found'},
                     {'http_status': 403}, {'http_status': 429}, {'outcome': 'deferred'}):
            with self.subTest(meta=meta), patch.object(m, '_FC2_STOPPED_THIS_RUN', False):
                item = self.item()
                state = {m.BACKFILL_ATTEMPTS_KEY: {item['code']: {'title_priority': True}}}
                with patch.object(m, 'fetch_fc2_market', return_value=meta):
                    m.attempt_fc2_market(item, state, 'backfill')
                self.assertEqual(bool(state[m.BACKFILL_ATTEMPTS_KEY][item['code']].get('title_priority')),
                                 meta.get('outcome') == 'deferred' or meta.get('http_status') in (403, 429))
        for blocked in (False, True):
            state = {m.BACKFILL_ATTEMPTS_KEY: {item['code']: {'title_priority': True}}}
            with patch.object(m, 'run_deadline_reached', return_value=not blocked), patch.object(m, 'fc2_market_blocked', return_value=blocked), patch.object(m, 'fetch_fc2_market') as fetch:
                self.assertIsNone(m.attempt_fc2_market(item, state, 'backfill'))
            fetch.assert_not_called()
            self.assertTrue(state[m.BACKFILL_ATTEMPTS_KEY][item['code']]['title_priority'])


if __name__ == '__main__':
    unittest.main()
