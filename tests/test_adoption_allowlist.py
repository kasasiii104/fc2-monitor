import copy
import unittest
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
import fc2_monitor as m
import missav_guard as g


class AdoptionAllowlistTests(unittest.TestCase):
    def test_absolute_assets_use_media_allowlist(self):
        for raw in ('https://storage1.contents.fc2.com/a', 'https://vip-videoprem1.fc2.com/a', '/sample/a'):
            with self.subTest(raw=raw):
                self.assertTrue(m._absolute_fc2_asset(raw))
        for raw in ('https://xiaojiadianmovie.be/a', 'http://storage1.contents.fc2.com/a',
                    'https://user:pass@storage1.contents.fc2.com/a', 'https://evilfc2.com/a'):
            with self.subTest(raw=raw):
                self.assertEqual(m._absolute_fc2_asset(raw), '')

    def test_sample_fetch_and_apply_reject_foreign_media(self):
        bad = 'https://xiaojiadianmovie.be/a'
        response = Mock(status_code=200, url='https://adult.contents.fc2.com/api/v2/videos/1234567/sample')
        response.json.return_value = {'path': bad, 'poster_image_path': bad}
        with patch.object(m, 'guarded_get', return_value=response):
            self.assertEqual(m.fetch_fc2_sample_assets('1234567')['reason'], 'media_host_rejected')
        item = m.enrich('1234567', '作品', 'MissAV', 'https://missav.live/fc2-ppv-1234567')
        before = copy.deepcopy(item)
        m.apply_fc2_sample_result(item, {'preview': bad, 'poster': bad}, 100)
        self.assertEqual(item['preview'], before['preview'])
        self.assertEqual(item['thumb'], before['thumb'])

    def test_saved_fallbacks_and_public_media_are_filtered_without_mutation(self):
        item = {'code': 'FC2-PPV-1234567', 'thumb': 'https://xiaojiadianmovie.be/a',
                'preview': 'http://storage1.contents.fc2.com/a',
                'preview_fallbacks': ['https://evilfc2.com/a', 'https://storage1.contents.fc2.com/a'],
                'url': 'https://legacy.example/work'}
        before = copy.deepcopy(item)
        self.assertEqual(m.preview_source_urls(item), ['https://storage1.contents.fc2.com/a',
                         'https://fourhoi.com/fc2-ppv-1234567/preview.mp4'])
        public = m.public_item(item)
        self.assertNotIn('thumb', public)
        self.assertEqual(public['code'], item['code'])
        self.assertEqual(public['url'], item['url'])
        self.assertEqual(item, before)
        self.assertNotIn('thumb', m.public_item({'code': 'FC2-PPV-1234567'}))

    def test_listing_href_adoption(self):
        for scrape, host, path in ((m.scrape_missav, 'missav.live', '/fc2-ppv-1234567'),
                                  (m.scrape_missav_catalog, 'missav.live', '/fc2-ppv-1234567'),
                                  (m.scrape_supjav, 'supjav.com', '/1234567.html'),
                                  (m.scrape_javdb, 'javdb.com', '/v/1234567')):
            for accepted in (False, True):
                with self.subTest(scrape=scrape.__name__, accepted=accepted):
                    urls = ['https://evil.example' + path, 'http://' + host + path,
                            'https://user:pass@' + host + path]
                    good = 'https://www.' + host + path
                    if accepted:
                        urls.append(good)
                    soup = BeautifulSoup(''.join('<a href="' + url + '">FC2-PPV-1234567 作品</a>' for url in urls), 'html.parser')
                    info = dict(ok=True, status=200, soup=soup, final_url='https://' + host + '/')
                    with patch.object(m, 'fetch_page', return_value=info):
                        rows = scrape(pages=[1])
                    self.assertEqual(len(rows), int(accepted))
                    if accepted:
                        self.assertEqual(rows[0]['url'], good)
                        self.assertEqual(m.merge_videos([rows])[0]['sources'][rows[0]['source']], good)

    def test_listing_href_surrounding_whitespace_is_stripped_not_rejected(self):
        for scrape, host, path in ((m.scrape_missav, 'missav.live', '/fc2-ppv-1234567'),
                                  (m.scrape_missav_catalog, 'missav.live', '/fc2-ppv-1234567'),
                                  (m.scrape_supjav, 'supjav.com', '/1234567.html'),
                                  (m.scrape_javdb, 'javdb.com', '/v/1234567')):
            for raw in (' ' + path, path + ' \n', ' https://' + host + path + ' '):
                with self.subTest(scrape=scrape.__name__, raw=raw):
                    soup = BeautifulSoup('<a href="' + raw + '">FC2-PPV-1234567 作品</a>', 'html.parser')
                    info = dict(ok=True, status=200, soup=soup, final_url='https://' + host + '/')
                    with patch.object(m, 'fetch_page', return_value=info):
                        rows = scrape(pages=[1])
                    self.assertEqual([row['url'] for row in rows], ['https://' + host + path])

    def test_ranking_href_surrounding_whitespace_is_stripped_not_rejected(self):
        import io
        from contextlib import redirect_stdout
        for raw in ('/fc2-ppv-1234567 ', ' /ja/fc2-ppv-1234567', '　/fc2-ppv-1234567　',
                    '/fc2-ppv-1234567 \n', ' https://missav.ws/ja/fc2-ppv-1234567 '):
            with self.subTest(raw=raw):
                soup = BeautifulSoup('<a href="' + raw + '">作品</a>', 'html.parser')
                out = io.StringIO()
                with patch.object(m, 'run_deadline_reached', return_value=False), \
                        patch.object(m, 'source_in_cooldown', return_value=False), \
                        patch.object(m, 'fetch_page', side_effect=lambda url, purpose=None: dict(
                            ok=True, status=200, soup=soup, final_url=url)), redirect_stdout(out):
                    result = m.scrape_missav_fc2_rankings()
                self.assertEqual(set(result['periods']), set(m.RANK_PERIODS))
                for outcome in result['periods'].values():
                    self.assertEqual(outcome, {'status': 'ok', 'codes': ['FC2-PPV-1234567']})
                self.assertNotIn('[Href rejected]', out.getvalue())
        # Whitespace inside the value is still rejected, not silently repaired.
        soup = BeautifulSoup('<a href="/fc2-ppv-\t1234567">作品</a><a href="/fc2-ppv-1234567\\">作品</a>', 'html.parser')
        with patch.object(m, 'run_deadline_reached', return_value=False), \
                patch.object(m, 'source_in_cooldown', return_value=False), \
                patch.object(m, 'fetch_page', side_effect=lambda url, purpose=None: dict(
                    ok=True, status=200, soup=soup, final_url=url)), redirect_stdout(io.StringIO()):
            result = m.scrape_missav_fc2_rankings()
        for outcome in result['periods'].values():
            self.assertEqual((outcome['status'], outcome.get('reason')), ('invalid_response', 'no_ranked_works'))

    def test_walker_next_link_surrounding_whitespace_is_stripped(self):
        current = m.H_WALKER_URL + '/'
        for raw in ('/next ', ' /next', '　/next\n', ' ' + m.H_WALKER_URL + '/next '):
            with self.subTest(raw=raw):
                soup = BeautifulSoup('<a href="' + raw + '">次の50件</a>', 'html.parser')
                nxt = m._hwalker_next_url(soup, current)
                self.assertEqual(nxt, m.H_WALKER_URL + '/next')
                self.assertTrue(m.url_allowed_for(nxt, 'walker'))

    def test_href_rejected_count_is_reported_and_schema_typed(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from pathlib import Path
        for scrape, host, path, accepts_completed in (
                (m.scrape_missav_catalog, 'missav.live', '/fc2-ppv-1234567', True),
                (m.scrape_javdb, 'javdb.com', '/v/1234567', True),
                (m.scrape_supjav, 'supjav.com', '/1234567.html', False)):
            for rejected in (2, 0):
                with self.subTest(scrape=scrape.__name__, rejected=rejected):
                    urls = ['https://evil%d.example%s' % (i, path) for i in range(rejected)]
                    urls.append('https://' + host + path)
                    soup = BeautifulSoup(''.join('<a href="' + u + '">FC2-PPV-1234567 作品</a>' for u in urls), 'html.parser')
                    info = dict(ok=True, status=200, soup=soup, final_url='https://' + host + '/')
                    report = {}
                    with patch.object(m, 'fetch_page', return_value=info), redirect_stdout(io.StringIO()):
                        rows = scrape([1], set(), report) if accepts_completed else scrape([1], report)
                    self.assertEqual(len(rows), 1)
                    if rejected:
                        self.assertEqual(report['href_rejected'], rejected)
                        self.assertIs(type(report['href_rejected']), int)
                    else:
                        self.assertNotIn('href_rejected', report)
        # report=None callers keep working when hrefs are rejected.
        soup = BeautifulSoup('<a href="https://evil.example/1234567.html">FC2-PPV-1234567</a>', 'html.parser')
        info = dict(ok=True, status=200, soup=soup, final_url='https://supjav.com/')
        with patch.object(m, 'fetch_page', return_value=info), redirect_stdout(io.StringIO()):
            self.assertEqual(m.scrape_supjav([1]), [])
            self.assertEqual(m.scrape_javdb([1]), [])
        # The state validator knows the key: int is silent, a wrong type warns.
        for source in ('missav', 'javdb', 'supjav'):
            for value, warned in ((2, False), ('2', True)):
                with self.subTest(source=source, value=value):
                    err = io.StringIO()
                    state = {'last_discovery': {'sources': {source: {'href_rejected': value}}}}
                    with patch.object(m, '_STATE_WARNINGS_SEEN', set()), redirect_stderr(err):
                        m.validate_crawl_state(Path('crawl_state.json'), state)
                    self.assertEqual('href_rejected' in err.getvalue(), warned)

    def test_guard_rejected_mirrors_never_prove_dead(self):
        rejected = {'error': 'host_rejected', 'status': 0}
        missing = {'status': 404}
        alive = {'status': 200, 'final_url': 'https://missav.live/ja/fc2-ppv-1234567'}
        for responses, expected in (([rejected, alive], 'alive'),
                                    ([rejected] * 3, 'unknown'),
                                    ([rejected, missing, missing], 'unknown')):
            with self.subTest(expected=expected, responses=responses):
                with patch.object(m, 'load_json', return_value={}), patch.object(g, '_existence_request', side_effect=responses) as request:
                    result = g.Guard().probe('1234567', force=True)
                self.assertEqual(result['status'], expected)
                self.assertEqual(request.call_count, len(responses))
                if expected == 'unknown':
                    self.assertEqual(result['reason'], 'host_rejected')

class R2RegressionTests(unittest.TestCase):
    def test_r2_url_parser_and_adoption(self):
        bad = [r'https://evil.example\.missav.ws/a', r'https://evil.example\.fc2.com/a',
               r'https://fourhoi.com\@evil.example/', r'https://evil.example\@fourhoi.com/',
               'https://mis\tsav.ws/', 'https://missav.ws/\n', 'https://missav.ws/a b',
               'https://日本.missav.ws/']
        with patch.object(m.requests, 'get') as get:
            for url in bad:
                with self.subTest(url=url):
                    self.assertFalse(m.url_allowed_for(url, 'missav'))
                    self.assertFalse(m.url_allowed_for(url, 'media'))
                    self.assertEqual(m._absolute_fc2_asset(url), '')
                    self.assertEqual(m.preview_source_urls({'preview': url}), [])
                    with self.assertRaises(m.HostNotAllowed):
                        m.guarded_get(url, 'missav', headers={}, timeout=1)
            get.assert_not_called()
        self.assertTrue(m.url_allowed_for('https://missav.ws./', 'missav'))
        for path in ('/a?x=%20&b=~', '/%E6%97%A5', '/日本'):
            self.assertTrue(m.url_allowed_for('https://fourhoi.com' + path, 'media'))

    def test_r2_sample_media_rejection(self):
        res = Mock(status_code=200)
        res.json.return_value = {'path': 'https://xiaojiadianmovie.be/a', 'poster': 'https://fourhoi.com/a'}
        with patch.object(m, 'guarded_get', return_value=res):
            meta = m.fetch_fc2_sample_assets('1234567')
        self.assertEqual(meta.get('reason'), 'media_host_rejected')
        item = {'fc2_sample_failures': 2, 'preview': 'https://fourhoi.com/old', 'preview_fallbacks': []}
        m.apply_fc2_sample_result(item, meta, 100)
        self.assertEqual(item['fc2_sample_failures'], 2)
        self.assertEqual(item['fc2_sample_retry_at'], 100 + m.FAIL_SKIP_SEC)
        self.assertEqual(item['thumb'], 'https://fourhoi.com/a')
        self.assertEqual(item['preview'], 'https://fourhoi.com/old')

    def test_r2_rejection_aggregate_logs(self):
        import io
        from contextlib import redirect_stdout
        soup = BeautifulSoup('<a href="http://missav.ws/fc2-ppv-1234567">work</a>', 'html.parser')
        info = dict(ok=True, status=200, soup=soup, final_url='https://missav.ws/ja/fc2?page=10')
        out, completed, report = io.StringIO(), set(), {}
        with patch.object(m, 'fetch_page', return_value=info), redirect_stdout(out):
            self.assertEqual(m.scrape_missav_catalog([10], completed, report), [])
        lines = [x for x in out.getvalue().splitlines() if x.startswith('[Href rejected]')]
        self.assertEqual(lines, ['[Href rejected] source=missav count=1'])
        self.assertEqual(completed, {10})
        self.assertEqual(report['status'], 'ok')
        out = io.StringIO()
        with redirect_stdout(out):
            m.catalog_payload([{'thumb': 'https://evil.example/a', 'preview': 'https://evil.example/b',
                                'preview_fallbacks': ['https://evil.example/c']}], '', {'ok': True})
        self.assertEqual(out.getvalue().splitlines(), [f'[Media host rejected] field={f} count=1' for f in ('thumb', 'preview', 'preview_fallbacks')])

    def test_r2_all_listing_boundaries_and_zero_logs(self):
        import io
        from contextlib import redirect_stdout
        for scrape, host, path in ((m.scrape_missav, 'missav.ws', '/fc2-ppv-1234567'),
                                  (m.scrape_missav_catalog, 'missav.ws', '/fc2-ppv-1234567'),
                                  (m.scrape_supjav, 'supjav.com', '/1234567'),
                                  (m.scrape_javdb, 'javdb.com', '/v/1234567')):
            for rejected in (True, False):
                href = 'https://evil.example\\.' + host + path if rejected else 'https://' + host + path
                soup = BeautifulSoup('<a href="' + href + '">FC2-PPV-1234567</a>', 'html.parser')
                out = io.StringIO()
                with patch.object(m, 'fetch_page', return_value=dict(ok=True, status=200, soup=soup, final_url='https://' + host + '/')), redirect_stdout(out):
                    rows = scrape([1])
                self.assertEqual(len(rows), int(not rejected))
                lines = [line for line in out.getvalue().splitlines() if line.startswith('[Href rejected]')]
                self.assertEqual(len(lines), int(rejected))
                self.assertNotIn('evil.example', out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            m.catalog_payload([{'thumb': 'https://fourhoi.com/a'}], '', {'ok': True})
        self.assertEqual(out.getvalue(), '')

    def test_r2_media_aggregate_standalone(self):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            m.catalog_payload([{'thumb': 'https://evil.example/a'}], '', {'ok': True})
        self.assertEqual(out.getvalue(), '[Media host rejected] field=thumb count=1\n')
