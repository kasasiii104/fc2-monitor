import unittest
import io
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch
import fc2_monitor as m
import missav_guard as g
import repair_code_only_titles as repair


def response(status=200, location=None, url="https://missav.ws/start"):
    return SimpleNamespace(status_code=status, headers={"Location": location} if location else {},
                           url=url, text="", close=Mock(), raise_for_status=Mock())


class GuardedFetchTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(m, "run_deadline_reached", return_value=False))
        self.enterContext(patch.object(m, "source_in_cooldown", return_value=False))
        self.get = self.enterContext(patch.object(m.requests, "get"))

    def test_invalid_start_makes_no_request(self):
        for url in ("https://example.com/?q=secret", "http://missav.ws/", "https://user:secret@missav.ws/"):
            with self.subTest(url=url), self.assertRaises(m.HostNotAllowed) as caught:
                m.guarded_get(url, "missav", headers={}, timeout=1)
            self.assertEqual(caught.exception.hop, 0)
            self.assertNotIn("secret", str(caught.exception))
        self.get.assert_not_called()

    def test_relative_and_cross_host_redirects(self):
        first, second, final = response(302, "/next"), response(307, "https://missav.live/end"), response(url="https://missav.live/end")
        self.get.side_effect = [first, second, final]
        result = m.guarded_get("https://missav.ws/start", "missav", headers={"cookie": "secret", "Authorization": "secret", "Range": "bytes=0-0"}, timeout=1, stream=True)
        self.assertIs(result, final)
        self.assertEqual(result.url, "https://missav.live/end")
        self.assertEqual([c.args[0] for c in self.get.call_args_list], ["https://missav.ws/start", "https://missav.ws/next", "https://missav.live/end"])
        self.assertIn("cookie", self.get.call_args_list[1].kwargs["headers"])
        self.assertEqual(self.get.call_args_list[2].kwargs["headers"], {"Range": "bytes=0-0"})
        for call in self.get.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertTrue(call.kwargs["stream"])
        first.close.assert_called_once()
        second.close.assert_called_once()
        final.close.assert_not_called()

    def test_same_host_response_cookie_merge_and_accumulation(self):
        first, second = response(302, "/next"), response(302, "/end")
        first.cookies = m.requests.cookies.cookiejar_from_dict({"session": "new", "first": "one"})
        second.cookies = m.requests.cookies.cookiejar_from_dict({"second": "two", "session": "newest"})
        self.get.side_effect = [first, second, response()]
        m.guarded_get("https://missav.ws/start", "missav",
                      headers={"cOoKiE": "session=old; caller=kept"}, timeout=1)
        self.assertEqual(self.get.call_args_list[1].kwargs["headers"],
                         {"Cookie": "session=new; caller=kept; first=one"})
        self.assertEqual(self.get.call_args_list[2].kwargs["headers"],
                         {"Cookie": "session=newest; caller=kept; first=one; second=two"})
        self.assertEqual(self.get.call_args_list[0].kwargs["headers"],
                         {"cOoKiE": "session=old; caller=kept"})

    def test_subdomain_redirect_discards_cookies_even_on_return(self):
        first, second, third = (response(302, "/next"),
                                response(302, "https://www.missav.ws/next"),
                                response(302, "https://missav.ws/end"))
        for res in (first, second, third):
            res.cookies = m.requests.cookies.cookiejar_from_dict({"session": "secret"})
        self.get.side_effect = [first, second, third, response()]
        m.guarded_get("https://missav.ws/start", "missav",
                      headers={"Cookie": "caller=secret", "Authorization": "secret"}, timeout=1)
        self.assertIn("session=secret", self.get.call_args_list[1].kwargs["headers"]["Cookie"])
        for call in self.get.call_args_list[2:]:
            self.assertEqual(call.kwargs["headers"], {})

    def test_mock_response_cookies_are_ignored(self):
        first = response(302, "/next")
        first.cookies = Mock()
        self.get.side_effect = [first, response()]
        m.guarded_get("https://missav.ws/start", "missav", headers={"Cookie": "caller=kept"}, timeout=1)
        self.assertEqual(self.get.call_args_list[1].kwargs["headers"], {"Cookie": "caller=kept"})

    def test_bad_redirect_never_makes_second_request(self):
        for target in ("https://example.com/", "http://missav.ws/", "https://user@missav.ws/", "https://javdb.com/"):
            self.get.reset_mock()
            first = response(302, target)
            self.get.return_value = first
            with self.subTest(target=target), self.assertRaises(m.HostNotAllowed):
                m.guarded_get("https://missav.ws/start", headers={}, timeout=1)
            self.get.assert_called_once()
            first.close.assert_called_once()

    def test_limit_and_missing_location(self):
        self.get.side_effect = lambda *a, **kw: response(302, "/next")
        with self.assertRaises(m.HostNotAllowed) as caught:
            m.guarded_get("https://missav.ws/start", "missav", headers={}, timeout=1)
        self.assertEqual(caught.exception.reason, "too_many_redirects")
        self.assertEqual(self.get.call_count, m.MAX_FETCH_REDIRECTS + 1)
        self.get.side_effect = None
        for res in (response(302), SimpleNamespace(status_code=200, url="https://missav.ws/start")):
            self.get.return_value = res
            self.assertIs(m.guarded_get("https://missav.ws/start", "missav", headers={}, timeout=1), res)

    def test_page_rejection_does_not_record_denial(self):
        self.get.return_value = response(302, "https://javdb.com/")
        with patch.object(m, "record_source_denial") as record:
            page = m.fetch_page("https://missav.ws/start")
        self.assertEqual((page["error"], page["status"], page["rejected_host"], page["final_url"]),
                         ("host_rejected", 0, "javdb.com", "https://missav.ws/start"))
        record.assert_not_called()
        self.get.assert_called_once()

    def test_official_rejection_preserves_ekyc(self):
        self.get.return_value = response(302, "https://redirect.fc2.com/ekyc_auth?secret=value")
        result = m.fetch_fc2_market("8000001")
        self.assertEqual((result["outcome"], result["reason"], result["blocked"]), ("blocked", "eKYC", "eKYC"))
        self.get.assert_called_once()
        self.get.reset_mock()
        self.get.return_value = response(302, "https://video.fc2.com/")
        self.assertEqual(m.fetch_page("https://adult.contents.fc2.com/", "fc2_official")["error"], "host_rejected")
        self.get.assert_called_once()

    def test_auxiliary_routes_do_not_follow_rejected_redirect(self):
        routes = ((lambda: g._existence_request("https://missav.ws/ja/fc2-ppv-8000001"), 1),
                  (lambda: repair.fetch_title({"code_num": "8000001", "url": "https://missav.ws/ja/fc2-ppv-8000001"}, state={}), 3),
                  (lambda: m.fetch_fc2cmadb_title("8000001"), 1),
                  (lambda: m.fetch_fc2_sample_assets("8000001"), 1))
        for route, attempts in routes:
            self.get.reset_mock()
            self.get.side_effect = lambda *a, **kw: response(302, "https://example.com/private")
            route()
            self.assertEqual(self.get.call_count, attempts)
            for call in self.get.call_args_list:
                self.assertNotIn("example.com", call.args[0])
                self.assertFalse(call.kwargs["allow_redirects"])

    def test_all_supported_redirect_statuses(self):
        for status in (301, 302, 303, 307, 308):
            self.get.reset_mock()
            self.get.side_effect = [response(status, "/next"), response(url="https://missav.ws/next")]
            result = m.guarded_get("https://missav.ws/start", "missav", headers={}, timeout=1)
            self.assertEqual(result.url, "https://missav.ws/next")
            self.assertEqual(self.get.call_count, 2)
            self.assertTrue(all(c.kwargs["allow_redirects"] is False for c in self.get.call_args_list))

    def test_page_redirect_limit_is_distinct_from_rejection(self):
        self.get.side_effect = lambda *a, **kw: response(302, "/loop")
        with patch.object(m, "record_source_denial") as record:
            result = m.fetch_page("https://missav.ws/start")
        self.assertEqual(result["error"], "too_many_redirects")
        self.assertEqual(result["status"], 0)
        record.assert_not_called()

    def test_soup_propagates_rejection(self):
        self.get.return_value = response(302, "https://example.com/")
        with self.assertRaises(m.HostNotAllowed):
            m.fetch_soup("https://missav.ws/start", "missav")
        self.get.assert_called_once()

    def test_auto_resolution_excludes_media_and_preserves_source(self):
        self.assertEqual(m.resolve_fetch_purpose("https://missav.ws/start"), "missav")
        for url in ("https://fourhoi.com/", "https://video.fc2.com/"):
            with self.assertRaises(m.HostNotAllowed):
                m.resolve_fetch_purpose(url)
        self.get.assert_not_called()
        self.get.side_effect = [response(302, "https://missav.live/next"), response(url="https://missav.live/next")]
        self.assertEqual(m.guarded_get("https://missav.ws/start", headers={}, timeout=1).url, "https://missav.live/next")

    def test_sample_ekyc_returns_none_without_following(self):
        self.get.return_value = response(302, "https://redirect.fc2.com/ekyc_auth?secret=value")
        self.assertIsNone(m.fetch_fc2_sample_assets("8000001"))
        self.get.assert_called_once()

    def test_rejection_logs_and_attributes_contain_no_url(self):
        self.get.return_value = response(302, "https://user:password@example.com/secret?token=private")
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(m.HostNotAllowed) as caught:
            m.guarded_get("https://missav.ws/start", "missav", headers={}, timeout=1)
        self.assertEqual(caught.exception.host, "example.com")
        self.assertFalse(isinstance(caught.exception, m.requests.RequestException))
        for value in ("https://", "password", "secret", "token", "private"):
            self.assertNotIn(value, output.getvalue() + repr(vars(caught.exception)))
        self.assertEqual(output.getvalue().count("\n"), 1)
        self.assertIn("[Host rejected] purpose=missav host=example.com hop=1 reason=host_rejected", output.getvalue())

    def test_existence_preserves_stream_range_and_timeout(self):
        res = response(url="https://missav.ws/start")
        managed = Mock()
        managed.__enter__ = Mock(return_value=res)
        managed.__exit__ = Mock(return_value=False)
        managed.status_code, managed.headers = 200, {}
        self.get.return_value = managed
        result = g._existence_request("https://missav.ws/start")
        self.assertEqual(result["status"], 200)
        call = self.get.call_args
        self.assertEqual(call.kwargs["headers"]["Range"], "bytes=0-0")
        self.assertEqual(call.kwargs["timeout"], (4, 8))
        self.assertTrue(call.kwargs["stream"])
        self.assertFalse(call.kwargs["allow_redirects"])
        managed.__exit__.assert_called_once()

    def test_title_refresh_catches_soup_rejection(self):
        item = {"code": "FC2-PPV-8000001", "code_num": "8000001", "title": "FC2-PPV-8000001"}
        state = {}
        self.get.side_effect = lambda *a, **kw: response(302, "https://example.com/private")
        with patch.object(m, "load_json", return_value=state), patch.object(m, "save_json"), patch.object(m.time, "sleep"):
            m.refresh_japanese_titles([item])
        self.assertEqual(self.get.call_count, 5)
        self.assertIn(item["code"], state["title_fail"])
        for call in self.get.call_args_list:
            self.assertNotIn("example.com", call.args[0])
            self.assertFalse(call.kwargs["allow_redirects"])

    def test_rejected_start_page_and_existence_return_failure(self):
        page = m.fetch_page("http://missav.ws/")
        self.assertEqual(page["error"], "host_rejected")
        probe = g._existence_request("https://example.com/")
        self.assertEqual((probe["error"], probe["status"]), ("host_rejected", 0))
        self.get.assert_not_called()


class RejectionAccountingTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(m, 'run_deadline_reached', return_value=False))
        self.enterContext(patch.object(m, 'source_in_cooldown', return_value=False))
        self.enterContext(patch.object(m, 'now_ts', return_value=2000000))
        self.enterContext(patch.object(m, 'save_json'))
        self.enterContext(patch.object(m, '_FC2_STOPPED_THIS_RUN', False))
        self.get = self.enterContext(patch.object(m.requests, 'get', side_effect=lambda *a, **k: response(302, 'https://example.com/private')))
        self.item = {'code': 'FC2-PPV-8000001', 'code_num': '8000001', 'title': '保存済みタイトル', 'preview': 'saved', 'thumb': 'saved', 'fc2_rating': 4, 'views': 123}

    def test_market_rejection_accounting_and_due(self):
        state = {m.BACKFILL_ATTEMPTS_KEY: {self.item['code']: {'failures': 3, 'last_success': 10, 'title_priority': True}}}
        original = dict(self.item)
        meta = m.fetch_fc2_market('8000001')
        self.assertEqual(meta['outcome'], 'rejected')
        result = m.record_fc2_market_result(self.item, meta, state, m.now_ts(), 'test')
        rec = state[m.BACKFILL_ATTEMPTS_KEY][self.item['code']]
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual((rec['failures'], rec['last_success'], rec['title_priority']), (3, 10, True))
        self.assertEqual(rec['next_retry_at'], m.now_ts() + m.FAIL_SKIP_SEC)
        self.assertNotIn(self.item['code'], state.get('fc2_market_failed', {}))
        self.assertFalse(m.fc2_market_due(self.item, state, m.now_ts()))
        for key, value in original.items():
            self.assertEqual(self.item[key], value)

    def test_market_candidate_and_batch_rejected(self):
        meta = m.fetch_fc2_market('8000001')
        state = {}
        result = m.record_fc2_market_result(self.item, meta, state, m.now_ts(), 'test', candidate=True)
        self.assertEqual(result['status'], 'rejected')
        self.assertFalse(state.get(m.BACKFILL_ATTEMPTS_KEY))
        self.assertNotIn('fc2_market_ekyc_blocked_at', state)
        stats = m._fc2_batch([self.item], state, 'test', 1)
        self.assertEqual((stats['rejected'], stats['transient_failed'], stats['success']), (1, 0, 0))
        self.get.reset_mock()
        self.assertEqual(m._fc2_batch([self.item], state, 'test', 1)['tried'], 0)
        self.get.assert_not_called()

    def test_sample_rejection_preserves_assets_and_failures(self):
        self.item['fc2_sample_failures'] = 3
        meta = m.fetch_fc2_sample_assets('8000001')
        self.assertEqual(meta['outcome'], 'rejected')
        m.apply_fc2_sample_result(self.item, meta, m.now_ts())
        self.assertEqual(self.item['fc2_sample_failures'], 3)
        self.assertEqual(self.item['fc2_sample_retry_at'], m.now_ts() + m.FAIL_SKIP_SEC)
        self.assertEqual((self.item['preview'], self.item['thumb']), ('saved', 'saved'))
        self.assertFalse(m.sample_retry_due(self.item, m.now_ts()))

    def test_fc2cmadb_rejection_does_not_retry_headers(self):
        meta = m.fetch_fc2cmadb_title('8000001')
        self.assertEqual(meta['outcome'], 'rejected')
        self.get.assert_called_once()

    def test_rankings_rejection_reason(self):
        result = m.scrape_missav_fc2_rankings()
        for rec in result["periods"].values():
            self.assertEqual((rec['status'], rec['reason']), ('fetch_failed', 'host_rejected'))

    def test_efficient_candidate_rejection(self):
        import efficient_discovery as efficient
        checks = {}
        status, meta = efficient.verify_official(self.item, '8000001', checks, m.now_ts(), state={})
        self.assertEqual(status, 'rejected')
        self.assertEqual(checks[self.item['code']]['status'], 'rejected')
        self.assertFalse(efficient.official_due(checks, self.item['code'], m.now_ts() + m.FAIL_SKIP_SEC - 1))
        self.assertTrue(efficient.official_due(checks, self.item['code'], m.now_ts() + m.FAIL_SKIP_SEC))

    def test_repair_rejection_excluded_from_summary(self):
        out = io.StringIO()
        state = {}
        with redirect_stdout(out):
            repair.fetch_title(self.item, state=state)
        rec = state[repair.STATE_KEY]['rejections'][self.item['code']]
        self.assertEqual((rec['status'], rec['reason']), ('rejected', 'host_rejected'))
        self.assertNotIn('error=', out.getvalue())

    def test_views_rejection_fixed_retry_preserves_counts(self):
        state = {m.VIEW_ATTEMPTS_KEY: {self.item['code']: {'failures': 3, 'last_success': 10}}}
        self.item['url'] = 'https://supjav.com/8000001.html'
        with patch.object(m, 'load_json', return_value=state):
            m.fill_missing_views([self.item])
            rec = state[m.VIEW_ATTEMPTS_KEY][self.item['code']]
            self.assertEqual((rec['failures'], rec['last_success']), (3, 10))
            self.assertEqual(rec['next_retry_at'], m.now_ts() + m.FAIL_SKIP_SEC)
            self.assertEqual(self.item['views'], 123)
            self.assertEqual(state[m.VIEW_STATS_KEY]['failed'], 0)
            self.assertGreater(state[m.VIEW_STATS_KEY]['rejected'], 0)
            self.get.reset_mock()
            m.fill_missing_views([self.item])
            self.assertFalse(any('8000001' in c.args[0] for c in self.get.call_args_list))

    def test_fc2cmadb_rejection_timestamp_wait(self):
        state = {}
        self.item['title'] = self.item['code']
        with patch.object(m, 'load_json', return_value=state):
            out = io.StringIO()
            with redirect_stdout(out):
                m.refresh_fc2cmadb_titles([self.item])
            self.assertIn('rejected=1', out.getvalue())
            self.assertEqual(state['fc2cmadb_failed'][self.item['code']], m.now_ts())
            self.assertNotIn('fc2cmadb_blocked_at', state)
            self.get.reset_mock()
            m.refresh_fc2cmadb_titles([self.item])
            self.get.assert_not_called()
            self.assertEqual(self.item['title'], self.item['code'])

    def test_titles_rejection_excluded_from_summary_and_host_stop(self):
        items = [dict(self.item, code=f'FC2-PPV-800000{i}', code_num=f'800000{i}', title=f'FC2-PPV-800000{i}') for i in range(1, 5)]
        state = {}
        out = io.StringIO()
        with patch.object(m, 'load_json', return_value=state), redirect_stdout(out):
            m.refresh_japanese_titles(items)
            self.assertEqual(state['japanese_title_stats']['rejected'], 5 * len(items))
            self.assertNotIn('error=', out.getvalue())
            self.assertFalse(state['title_hosts_blocked'])
            self.assertEqual(self.get.call_count, 5 * len(items))
            self.get.reset_mock()
            m.refresh_japanese_titles(items)
            self.get.assert_not_called()

    def test_guard_rejection_does_not_increment_retry(self):
        with patch.object(m, 'load_json', return_value={}):
            guard = g.Guard()
        result = guard.probe('8000001')
        self.assertEqual((result['status'], result['reason']), ('unknown', 'host_rejected'))
        g._queue_retry(self.item, result, guard)
        row = guard.retry_queue[self.item['code']]
        self.assertEqual(row['attempts'], 0)
        self.get.reset_mock()
        self.assertEqual(guard.probe('8000001'), result)
        self.get.assert_not_called()
        self.assertNotIn('missav_dead', self.item)

    def test_sample_callers_rejection_stats_and_wait(self):
        for caller, key in ((m.refresh_fc2_previews, m.PREVIEW_REFRESH_STATS_KEY),
                            (m.run_thumbnail_backfill, m.THUMB_BACKFILL_STATS_KEY),
                            (m.enrich_fc2_sample_assets, None)):
            with self.subTest(caller=caller.__name__):
                state = {}
                item = dict(self.item, fc2_sample_failures=3)
                with patch.object(m, 'load_json', return_value=state):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        caller([item])
                    self.assertEqual(item['fc2_sample_failures'], 3)
                    self.assertEqual(item['fc2_sample_retry_at'], m.now_ts() + m.FAIL_SKIP_SEC)
                    self.assertEqual((item['preview'], item['thumb']), ('saved', 'saved'))
                    if key:
                        self.assertEqual(state[key]['rejected'], 1)
                        self.assertEqual(state[key].get('failed', 0), 0)
                        self.assertEqual(state[key].get('updated', 0), 0)
                    else:
                        self.assertIn('updated=0 rejected=1', out.getvalue())
                    self.get.reset_mock()
                    caller([item])
                    self.get.assert_not_called()

    def test_catalog_rejection_keeps_cursor_reason(self):
        for caller in (m.scrape_missav_catalog, m.scrape_javdb):
            report, completed = {}, set()
            caller([2], completed_pages=completed, report=report)
            self.assertFalse(completed)
            self.assertEqual((report['status'], report['reason']), ('fetch_failed', 'host_rejected'))

    def test_ranking_rejection_keeps_previous_rank(self):
        code = self.item['code']
        state = {m.RANK_STATE_KEY: {'day': {'status': 'ok', 'codes': [code], 'last_success_at': 'old'}}}
        with patch.object(m, 'load_json', return_value=state):
            m.apply_missav_ranks([self.item], m.scrape_missav_rankings(), 'new')
        self.assertEqual(self.item['missav_rank_day'], 1)
        self.assertEqual(state[m.RANK_STATE_KEY]['day']['last_success_at'], 'old')
        self.assertEqual(state[m.RANK_STATE_KEY]['day']['reason'], 'host_rejected')

    def test_fastlane_rejection_not_failed_and_wait(self):
        import walker_official_fastlane as fast
        code = self.item['code']
        state = {m.H_WALKER_CACHE_KEY: {code: {'title': '保存済みタイトル', 'checked_at': 1999999}}}
        with patch.object(m, 'load_json', side_effect=lambda path, default=None, **kw: state if path == m.FETCH_STATE_FILE else default), patch.object(fast, 'WORKERS', 1):
            fast.main()
            stats = state['walker_official_fastlane_stats']
            self.assertEqual((stats['failed'], stats['success'], stats['not_found'], stats['rejected']), (0, 0, 0, 1))
            self.assertEqual(state[fast.CHECK_KEY][code]['status'], 'rejected')
            self.assertFalse(fast.due(state[fast.CHECK_KEY], code, m.now_ts() + m.FAIL_SKIP_SEC - 1))
            self.get.reset_mock()
            fast.main()
            self.get.assert_not_called()

    def test_too_many_redirects_is_rejected(self):
        self.get.side_effect = lambda url, **kw: response(302, url, url)
        meta = m.fetch_fc2_market('8000001')
        self.assertEqual((meta['outcome'], meta['reason']), ('rejected', 'too_many_redirects'))

    def test_repair_rejection_fixed_wait(self):
        state = {}
        original = dict(self.item)
        self.assertEqual(repair.fetch_title(self.item, state), ('', ''))
        rec = state[repair.STATE_KEY]['rejections'][self.item['code']]
        self.assertEqual((rec['status'], rec['next_retry_at']), ('rejected', m.now_ts() + m.FAIL_SKIP_SEC))
        self.get.reset_mock()
        self.assertEqual(repair.fetch_title(self.item, state), ('', ''))
        self.get.assert_not_called()
        self.assertEqual(self.item, original)

    def test_supjav_rejection_report(self):
        report = {}
        self.assertEqual(m.scrape_supjav([1], report=report), [])
        self.assertEqual(report, {'status': 'fetch_failed', 'reason': 'host_rejected'})

class R2RedirectTests(unittest.TestCase):
    def test_r2_cookie_paths_and_duplicate_names(self):
        for target, expected in (('/public', 'name=public'), ('/private/x', 'name=private')):
            first = response(302, target)
            first.cookies = m.requests.cookies.RequestsCookieJar()
            first.cookies.set('name', 'private', domain='missav.ws', path='/private', secure=True)
            first.cookies.set('name', 'public', domain='missav.ws', path='/public', secure=True)
            with patch.object(m.requests, 'get', side_effect=[first, response()]) as get:
                m.guarded_get('https://missav.ws/start', 'missav', headers={}, timeout=1)
            self.assertEqual(get.call_args_list[1].kwargs['headers'].get('Cookie'), expected)

    def test_r2_utf8_location(self):
        with patch.object(m.requests, 'get', side_effect=[response(302, '/caf\xc3\xa9'), response()]) as get:
            m.guarded_get('https://missav.ws/start', 'missav', headers={}, timeout=1)
        self.assertEqual(m.requests.Request('GET', get.call_args.args[0]).prepare().url, 'https://missav.ws/caf%C3%A9')

    def test_r2_prepared_authority_mismatch(self):
        real = m.requests.Request
        with patch.object(m.requests, 'Request') as prepare, patch.object(m.requests, 'get') as get:
            prepare.return_value.prepare.return_value = real('GET', 'https://evil.example/').prepare()
            with self.assertRaises(m.HostNotAllowed):
                m.guarded_get('https://missav.ws/', 'missav', headers={}, timeout=1)
            get.assert_not_called()
