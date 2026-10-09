import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from bs4 import BeautifulSoup
import fc2_monitor as m


class UpdatePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for key, name in {'DATA_FILE': 'data.json', 'FETCH_STATE_FILE': 'fetch.json',
                          'CRAWL_FILE': 'crawl.json', 'HTML_FILE': 'index.html',
                          'VIEWS_HISTORY_FILE': 'views.json', 'HISTORY_FILE': 'history.json'}.items():
            self.enterContext(patch.object(m, key, root / name))
        self.enterContext(patch.dict(m.SOURCE_RETRY_AT, {}, clear=True))
        self.enterContext(patch.object(m, 'now_ts', return_value=1800000000))
        self.enterContext(patch.object(m.requests, 'get', side_effect=AssertionError('Network must be mocked')))
        self.enterContext(patch.object(m.requests, 'post', side_effect=AssertionError('No notifications')))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def item(self, number):
        return {'code': f'FC2-PPV-{number}', 'code_num': str(number), 'title': 'テストの作品',
                'first_seen': '2026-09-01 00:00:00', 'sources': {}}

    def page(self, html, url=''):
        return {'ok': True, 'status': 200, 'cloudflare': False, 'size': 30000,
                'soup': BeautifulSoup(html, 'html.parser'), 'final_url': url}

    def test_success_replaces_period_instead_of_leaving_old_winners(self):
        items = [dict(self.item(8000001), missav_rank_day=1), self.item(8000002)]
        m.apply_missav_ranks(items, {'FC2-PPV-8000002': {'day': 1}}, 'new')
        self.assertIsNone(items[0].get('missav_rank_day'))
        self.assertEqual(items[1]['missav_rank_day'], 1)
        self.assertEqual(len(items), 2)

    def test_failure_retains_verified_period_without_refreshing_its_timestamp(self):
        items = [self.item(8000001), self.item(8000002)]
        m.apply_missav_ranks(items, {'FC2-PPV-8000001': {'day': 1, 'week': 1}}, 'first')
        m.apply_missav_ranks(items, {'FC2-PPV-8000002': {'day': 1}}, 'second')
        self.assertIsNone(items[0].get('missav_rank_day'))
        self.assertEqual(items[0]['missav_rank_week'], 1)
        status = m.load_json(m.FETCH_STATE_FILE, {})['missav_rankings_v2']
        self.assertEqual(status['day']['last_success_at'], 'second')
        self.assertEqual(status['week']['last_success_at'], 'first')
        self.assertNotEqual(status['week']['status'], 'ok')

    def test_empty_and_partial_failures_never_destroy_verified_snapshot(self):
        items = [self.item(8000001)]
        m.apply_missav_ranks(items, {'FC2-PPV-8000001': {'day': 1}}, 'first')
        m.apply_missav_ranks(items, {}, 'second')
        self.assertEqual(items[0]['missav_rank_day'], 1)
        status = m.load_json(m.FETCH_STATE_FILE, {})['missav_rankings_v2']['day']
        self.assertEqual(status['last_success_at'], 'first')
        self.assertEqual(status['checked_at'], 'second')

    def test_invalid_period_payloads_preserve_the_last_verified_ranks(self):
        for codes in ('FC2-PPV-8000002', [None], ['FC2-PPV-8000002'] * 2, ['invalid'], []):
            with self.subTest(codes=codes):
                items = [self.item(8000001), self.item(8000002)]
                m.apply_missav_ranks(items, {'FC2-PPV-8000001': {'day': 1}}, 'first')
                m.apply_missav_ranks(items, {'version': 2, 'periods': {'day': {'status': 'ok', 'codes': codes}}}, 'second')
                self.assertEqual(items[0]['missav_rank_day'], 1)
                self.assertIsNone(items[1]['missav_rank_day'])
                status = m.load_json(m.FETCH_STATE_FILE, {})[m.RANK_STATE_KEY]['day']
                self.assertEqual((status['status'], status['last_success_at']), ('invalid_response', 'first'))

    def test_unknown_ranked_codes_do_not_create_unchecked_catalog_entries(self):
        items = [self.item(8000001)]
        m.apply_missav_ranks(items, {'FC2-PPV-8000002': {'day': 1}}, 'now')
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0].get('missav_rank_day'))
        status = m.load_json(m.FETCH_STATE_FILE, {})['missav_rankings_v2']['day']
        self.assertEqual((status['count'], status['listed_count']), (1, 0))

    def test_parser_rejects_wrong_destination_and_does_not_use_search_or_text_only_links(self):
        def source(url):
            html = '<nav><a href="/ja/search/fc2-ppv-8999999">FC2-PPV-8999999</a></nav>'
            html += '<a href="https://example.test/fc2-ppv-8999998">FC2-PPV-8999998</a>'
            html += '<a href="/advert">FC2-PPV-8999997</a><a href="/ja/fc2-ppv-8000001">テスト</a>'
            html += '<a href="/ja/fc2-ppv-8000001">重複</a>'
            html += '<a href="https://[">不正なリンク</a>'
            return self.page(html, url)
        with patch.object(m, 'fetch_page', side_effect=source):
            result = m.scrape_missav_rankings()
        self.assertEqual(result['periods']['day']['codes'], ['FC2-PPV-8000001'])
        with patch.object(m, 'fetch_page', return_value=self.page('<a href="/ja/fc2-ppv-8000001">テスト</a>', 'https://missav.ws/ja/fc2')):
            result = m.scrape_missav_rankings()
        self.assertNotEqual(result['periods']['day']['status'], 'ok')

    def test_rank_denial_stops_other_period_requests_and_sets_retry(self):
        with patch.object(m, 'fetch_page', return_value={'ok': False, 'status': 403, 'soup': None}) as fetch:
            result = m.scrape_missav_rankings()
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(all(x['status'] == 'access_limited' for x in result['periods'].values()))
        self.assertGreater(m.SOURCE_RETRY_AT['missav'], m.now_ts())

    def test_publisher_dynamic_route_is_accepted_only_when_locale_and_sort_match(self):
        def source(url):
            return self.page('<a href="/dm597/ja/fc2-ppv-8000001">テスト</a>', url.replace('/ja/', '/dm597/ja/'))
        with patch.object(m, 'fetch_page', side_effect=source) as fetch:
            result = m.scrape_missav_rankings()
        self.assertEqual(fetch.call_count, 4)
        self.assertTrue(all(x['status'] == 'ok' and x['codes'] == ['FC2-PPV-8000001'] for x in result['periods'].values()))
        for final in ('https://missav.ws/dm597/en/fc2?sort=today_views',
                      'https://missav.ws/dm597/ja/fc2?sort=published_at',
                      'https://missav.ws/other/ja/fc2?sort=today_views'):
            with patch.object(m, 'fetch_page', return_value=self.page('<a href="/ja/fc2-ppv-8000001">テスト</a>', final)):
                result = m.scrape_missav_rankings()
            self.assertNotEqual(result['periods']['day']['status'], 'ok')

    def test_saved_snapshot_overrides_legacy_duplicates_during_offline_build(self):
        items = [self.item(8000001), self.item(8000002)]
        m.apply_missav_ranks(items, {'FC2-PPV-8000002': {'day': 1}}, 'first')
        items[0]['missav_rank_day'] = 1
        m.save_json(m.DATA_FILE, {'items': items, 'updated_at': 'first'})
        m.rebuild_site()
        saved = m.load_json(m.HTML_FILE.parent / 'catalog.json', {})
        self.assertEqual([x['code'] for x in saved['items'] if x.get('missav_rank_day')], ['FC2-PPV-8000002'])

    def test_view_parser_never_uses_another_work_or_related_card(self):
        soup = BeautifulSoup('<article><h2>FC2-PPV-8000002</h2><span class="views">999 views</span></article>'
                             '<article><h2>FC2-PPV-8000001</h2><span class="views">12 views</span></article>', 'html.parser')
        self.assertEqual(m.supjav_work_views(soup, '8000001'), 12)
        self.assertIsNone(m.supjav_work_views(soup, '8000003'))
        soup = BeautifulSoup('<article><h1>FC2-PPV-8000001</h1><aside class="related"><h2>FC2-PPV-8000002</h2><span>999 views</span></aside></article>', 'html.parser')
        self.assertIsNone(m.supjav_work_views(soup, '8000001'))
        self.assertEqual(m.supjav_work_views(BeautifulSoup('<article>FC2-PPV-8000001 <span>0 views</span></article>', 'html.parser'), '8000001'), 0)

    def test_old_views_are_checked_in_bounded_batches_and_next_run_moves_on(self):
        items = [self.item(8000001), self.item(8000002), self.item(8000003)]
        calls = []
        def source(url):
            calls.append(url)
            if '/category/' in url:
                return self.page('<main>一覧に対象なし</main>', url)
            num = next(str(i) for i in (8000001, 8000002, 8000003) if str(i) in url)
            return self.page(f'<article><h2>FC2-PPV-{num}</h2><span>12 views</span></article>', url)
        with patch.object(m, 'fetch_page', side_effect=source), patch.object(m, 'VIEW_FETCH_LIMIT', 1):
            m.fill_missing_views(items)
            m.fill_missing_views(items)
        self.assertEqual([x['code'] for x in items if x.get('views') == 12], ['FC2-PPV-8000001', 'FC2-PPV-8000002'])
        self.assertNotIn('last_views_attempt', items[2])
        self.assertEqual(len([u for u in calls if '?s=' in u]), 2)

    def test_views_cooldown_makes_no_request_and_does_not_mark_items_attempted(self):
        items = [dict(self.item(8000001), views=123, views_source='Supjav')]
        m.SOURCE_RETRY_AT['supjav'] = m.now_ts() + 3600
        with patch.object(m, 'fetch_page') as fetch:
            m.fill_missing_views(items)
        fetch.assert_not_called()
        self.assertEqual(items[0]['views'], 123)
        self.assertNotIn('last_views_attempt', items[0])
        status = m.load_json(m.FETCH_STATE_FILE, {})['views_refresh_v2_stats']
        self.assertEqual((status['status'], status['tried']), ('access_limited', 0))
        self.assertGreater(status['next_retry_at'], m.now_ts())

    def test_views_denial_stops_batch_and_retains_good_value_and_last_success(self):
        item = dict(self.item(8000001), views=123, views_source='Supjav', last_views_fetch=100)
        def source(url):
            if '/category/' in url:
                return self.page('<main>一覧に対象なし</main>', url)
            return {'ok': False, 'status': 403, 'soup': None}
        with patch.object(m, 'fetch_page', side_effect=source) as fetch:
            m.fill_missing_views([item, self.item(8000002)])
        self.assertEqual(fetch.call_count, 4)
        self.assertEqual((item['views'], item['last_views_fetch']), (123, 100))
        self.assertFalse(item['views_updated'])

    def test_status_exposes_past_additions_limits_and_retry_without_private_state(self):
        crawl = {'missav_page': 357, 'last_discovery': {'checked_at': 'recent', 'recent_added': 0,
                 'archive_added': 11, 'sources': {'missav': {'status': 'ok', 'next_page': 357},
                                               'javdb': {'status': 'access_limited'}}},
                 'source_retry_at': {'javdb': m.now_ts() + 3600}}
        state = {'fc2_market_ekyc_blocked_at': m.now_ts(), 'metadata_backfill_v3_stats': {'remaining_candidates': 8631},
                 'hwalker_title_cache': {'hidden': 'must not leak'}, 'title_fail': {'hidden': 123}}
        status = m.build_update_status([self.item(8000001)], crawl, state)
        self.assertEqual(status['discovery']['archive_added'], 11)
        self.assertEqual(status['discovery']['next_archive_page'], 357)
        self.assertEqual(status['official']['status'], 'access_limited')
        self.assertGreater(status['official']['next_retry_at'], m.now_ts())
        self.assertNotIn('hidden', json.dumps(status))

    def test_views_without_a_new_measurement_do_not_gain_a_fake_trend(self):
        item = dict(self.item(8000001), views=123, views_source='Supjav', views_updated=False)
        m.save_json(m.VIEWS_HISTORY_FILE, {'items': {item['code']: {'points': [{'t': m.now_ts() - 6*3600, 'v': 100}, {'t': m.now_ts() - 5*3600, 'v': 123}]}}})
        m.update_views_history([item])
        self.assertIsNone(item['trend_6h'])
        self.assertIsNone(item['trend_24h'])

    def test_unknown_views_are_not_published_as_measured_zero(self):
        self.assertIsNone(m.public_item(self.item(8000001))['views'])
        self.assertEqual(m.public_item(dict(self.item(8000001), views=0, views_source='Supjav'))['views'], 0)

    def test_discovery_merges_measured_zero_without_related_work_counts(self):
        page = self.page('<article><a href="/ja/123.html">FC2-PPV-8000001</a><span>0 views</span>'
                         '<aside class="related"><a href="/ja/456.html">FC2-PPV-8000002</a><span>999 views</span></aside></article>')
        with patch.object(m, 'fetch_page', return_value=page):
            rows = m.scrape_supjav([1])
        zero = next(x for x in rows if x['code'] == 'FC2-PPV-8000001')
        self.assertEqual((zero['views'], zero['views_source']), (0, 'Supjav'))
        original = m.enrich('8000001', 'テスト作品', 'MissAV', 'https://example.test/item')
        merged = m.merge_videos([[original], [zero]])
        self.assertEqual((merged[0]['views'], merged[0]['views_source']), (0, 'Supjav'))

    def test_metadata_only_repair_preserves_catalog_and_archive_cursor_without_notifications(self):
        items = [dict(self.item(8000001), thumb='https://example.test/a.jpg', is_new=True), self.item(8000002)]
        crawl = {'missav_page': 357, 'javdb_page': 17,
                 'source_retry_at': {'supjav': m.now_ts() + 3600},
                 'last_discovery': {'checked_at': 'previous', 'recent_added': 2, 'archive_added': 11}}
        m.save_json(m.DATA_FILE, {'updated_at': 'previous', 'items': items, 'custom': 'retain'})
        m.save_json(m.CRAWL_FILE, crawl)
        m.save_json(m.FETCH_STATE_FILE, {'title_fail': {'FC2-PPV-8000001': 123}})
        def source(url):
            self.assertIn('/ja/fc2?sort=', url)
            return self.page('<a href="/ja/fc2-ppv-8000002">テスト</a>', url)
        with patch.object(m, 'fetch_page', side_effect=source) as fetch, patch.object(m, 'send_telegram') as notify:
            self.assertEqual(m.refresh_update_metadata(), 0)
        self.assertEqual(fetch.call_count, 4)
        notify.assert_not_called()
        saved = m.load_json(m.DATA_FILE, {})
        self.assertEqual(saved['custom'], 'retain')
        self.assertEqual([x['code'] for x in saved['items']], [x['code'] for x in items])
        for before, after in zip(items, saved['items']):
            for field, value in before.items():
                self.assertEqual(after[field], value)
        self.assertEqual(m.load_json(m.CRAWL_FILE, {}), crawl)
        state = m.load_json(m.FETCH_STATE_FILE, {})
        self.assertEqual(state['title_fail'], {'FC2-PPV-8000001': 123})
        catalog = m.load_json(m.HTML_FILE.parent / 'catalog.json', {})
        self.assertEqual(catalog['update_status']['discovery']['archive_added'], 11)
        self.assertEqual(catalog['update_status']['views']['status'], 'access_limited')
        self.assertEqual(catalog['items'][1]['missav_rank_day'], 1)


if __name__ == '__main__':
    unittest.main()
