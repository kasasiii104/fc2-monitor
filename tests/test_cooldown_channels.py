import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fc2_monitor as m


CATALOG = "https://missav.ws/ja/fc2"
RANKING = CATALOG + "?sort=today_views"
DETAIL = "https://missav.ws/ja/fc2-ppv-8000001"


class CooldownChannelTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = 1800000000
        self.deadline = self.now + m.DISCOVERY_RETRY_SEC
        for name in ("CRAWL_FILE", "SOURCE_COOLDOWN_FILE", "FETCH_STATE_FILE"):
            self.enterContext(patch.object(m, name, root / getattr(m, name).name))
        self.enterContext(patch.dict(m.SOURCE_RETRY_AT, {}, clear=True))
        self.enterContext(patch.dict(m.CHANNEL_RETRY_AT, {}, clear=True))
        self.enterContext(patch.dict(m.os.environ, {"GITHUB_OUTPUT": str(root / "github-output")}))
        self.clock = self.enterContext(patch.object(m, "now_ts", return_value=self.now))
        self.enterContext(patch.object(m, "initialize_run_deadline"))
        self.enterContext(patch.object(m, "run_deadline_reached", return_value=False))
        self.http = self.enterContext(patch.object(m, "guarded_get", side_effect=AssertionError("No live HTTP")))
        self.stdout = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.enterContext(patch.object(m, "_STATE_WARNINGS_SEEN", set()))

    @staticmethod
    def response(url, status=200, body=""):
        return SimpleNamespace(status_code=status, url=url, headers={}, text=body + " " * 2100)

    def test_url_classification(self):
        missav = (
            ("/ja/search/fc2-ppv?page=2", "search"),
            ("/ja/fc2?page=2", "catalog"),
            ("/ja/fc2?sort=today_views", "ranking"),
            ("/ja/fc2-ppv-8000001", "detail"),
        )
        for host in ("missav.ws", "missav.live", "missav.ai"):
            for prefix in ("", "/dm597"):
                for path, channel in missav:
                    url = f"https://{host}{prefix}{path}"
                    with self.subTest(url=url):
                        self.assertEqual(m.discovery_channel(url), ("missav", channel))

        cases = {
            "https://javdb.com/search?q=FC2": ("javdb", "search"),
            "https://javdb.com/v/example": ("javdb", "detail"),
            "https://supjav.com/ja/category/maker/fc2ppv/page/2": ("supjav", "category"),
            "https://supjav.com/ja/?s=FC2PPV": ("supjav", "search"),
            "https://supjav.com/ja/123456.html": ("supjav", "detail"),
            "https://example.com/ja/fc2": ("", ""),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(m.discovery_channel(url), expected)

    def test_search_succeeds_on_next_run_after_catalog_denial(self):
        code = "FC2-PPV-8000001"

        def respond(url, purpose, **kwargs):
            if m.discovery_channel(url) == ("missav", "catalog"):
                return self.response(url, 403)
            body = f'<a href="/ja/fc2-ppv-8000001">{code} テスト</a>' if url == m.MISSAV_URL else ""
            return self.response(url, body=body)

        self.http.side_effect = respond
        items, state = m.get_latest_videos()
        self.assertEqual([item["code"] for item in items], [code])
        self.assertIn(("missav", "catalog"), {m.discovery_channel(call.args[0]) for call in self.http.call_args_list})
        m.save_json(m.CRAWL_FILE, state)
        m.save_source_cooldown_file()

        self.http.reset_mock()
        m.CHANNEL_RETRY_AT.clear()
        self.clock.return_value += 60
        items, state = m.get_latest_videos()
        self.assertEqual([item["code"] for item in items], [code])
        self.assertIn(m.MISSAV_URL, [call.args[0] for call in self.http.call_args_list])
        self.assertNotIn(("missav", "catalog"), {m.discovery_channel(call.args[0]) for call in self.http.call_args_list})
        self.assertEqual(state["last_discovery"]["sources"]["missav"]["catalog_items"], 0)
        self.assertEqual(m.DISCOVERY_FETCH_OUTCOMES["cooldown_skip"], 1)

    def test_legacy_deadlines_block_all_routes_until_expiry(self):
        legacy = {"source_retry_at": {"missav": self.deadline}}
        urls = (m.MISSAV_URL, CATALOG, RANKING, DETAIL)
        self.http.side_effect = lambda url, purpose, **kwargs: self.response(url)
        for location in ("file", "crawl"):
            with self.subTest(location=location):
                m.save_json(m.SOURCE_COOLDOWN_FILE, legacy if location == "file" else {"source_retry_at": {}})
                crawl = legacy if location == "crawl" else {}
                self.clock.return_value = self.now
                m.initialize_discovery_cooldowns(crawl)
                self.http.reset_mock()
                for url in urls:
                    self.assertEqual(m.fetch_page(url)["error"], "source_cooldown")
                self.http.assert_not_called()
                self.clock.return_value = self.deadline + 1
                m.initialize_discovery_cooldowns(crawl)
                for url in urls:
                    self.assertTrue(m.fetch_page(url)["ok"])
                self.assertEqual(self.http.call_count, len(urls))
                self.assertEqual(m.SOURCE_RETRY_AT, {})

    def test_save_keeps_legacy_loader_compatible_without_adding_legacy_denials(self):
        m.save_json(m.SOURCE_COOLDOWN_FILE, {"source_retry_at": {"javdb": self.now + 30}})
        m.record_source_denial(CATALOG, 403)
        m.save_discovery_cooldowns()
        saved = json.loads(m.SOURCE_COOLDOWN_FILE.read_text())
        self.assertIsInstance(saved["source_retry_at"], dict)
        self.assertEqual(saved, {"source_retry_at": {"javdb": self.now + 30},
                                 "channel_retry_at": {"missav": {"catalog": self.deadline}}})
        self.assertEqual(m.load_json(m.CRAWL_FILE, {}), saved)
        self.clock.return_value = self.deadline
        m.save_discovery_cooldowns()
        empty = {"source_retry_at": {}, "channel_retry_at": {}}
        self.assertEqual(m.load_json(m.SOURCE_COOLDOWN_FILE, {}), empty)
        self.assertEqual(m.load_json(m.CRAWL_FILE, {}), empty)

    def test_crawl_restores_channel_lost_when_legacy_code_rewrites_file(self):
        m.record_source_denial(CATALOG, 403)
        m.save_discovery_cooldowns()
        m.save_json(m.SOURCE_COOLDOWN_FILE, {"source_retry_at": {}})
        m.CHANNEL_RETRY_AT.clear()
        m.initialize_discovery_cooldowns(m.load_json(m.CRAWL_FILE, {}))
        self.assertEqual(m.fetch_page(CATALOG)["error"], "source_cooldown")
        self.http.assert_not_called()
        self.assertFalse(m.source_in_cooldown(m.MISSAV_URL))

    def test_discovery_only_degrades_only_when_all_discovery_routes_are_blocked(self):
        channels = {"missav": {"catalog": self.deadline}, "javdb": {"search": self.deadline},
                    "supjav": {"category": self.deadline}}
        self.http.side_effect = lambda url, purpose, **kwargs: self.response(url)
        m.save_json(m.SOURCE_COOLDOWN_FILE, {"source_retry_at": {}, "channel_retry_at": channels})
        self.assertEqual(m.main(discovery_only=True), 1)
        self.assertNotIn("::warning::", self.stdout.getvalue())
        self.assertIn(m.MISSAV_URL, [call.args[0] for call in self.http.call_args_list])

        channels["missav"]["search"] = self.deadline
        m.save_json(m.SOURCE_COOLDOWN_FILE, {"source_retry_at": {}, "channel_retry_at": channels})
        self.http.reset_mock()
        self.assertEqual(m.main(discovery_only=True), 0)
        self.http.assert_not_called()
        self.assertIn("::warning::[main discovery] all sources access_limited", self.stdout.getvalue())

    def test_public_status_uses_only_discovery_routes_and_first_available_deadline(self):
        crawl = {"last_discovery": {"checked_at": "recent", "sources": {
            "missav": {"status": "access_limited"}, "javdb": {"status": "ok"}, "supjav": {"status": "checked"},
        }}}
        m.save_json(m.SOURCE_COOLDOWN_FILE, {"source_retry_at": {}, "channel_retry_at": {
            "missav": {"catalog": self.now + 40, "ranking": self.deadline},
            "javdb": {"detail": self.deadline}, "supjav": {"category": self.now + 30},
        }})
        expected = {"missav": {"status": "retry_due", "next_retry_at": 0, "checked_at": "recent"},
                    "javdb": {"status": "ok", "next_retry_at": 0, "checked_at": "recent"},
                    "supjav": {"status": "access_limited", "next_retry_at": self.now + 30,
                               "checked_at": "recent"}}
        self.assertEqual(m.build_update_status([], crawl, {})["sources"], expected)

        crawl["channel_retry_at"] = {"missav": {"catalog": self.now + 60}}
        m.CHANNEL_RETRY_AT["missav"] = {"search": self.now + 80, "catalog": self.now + 50}
        expected["missav"].update(status="access_limited", next_retry_at=self.now + 60)
        self.assertEqual(m.build_update_status([], crawl, {})["sources"], expected)
        self.clock.return_value = self.now + 60
        expected["missav"].update(status="retry_due", next_retry_at=0)
        expected["supjav"].update(status="checked", next_retry_at=0)
        self.assertEqual(m.build_update_status([], crawl, {})["sources"], expected)

    def test_crawl_schema_validates_nested_channel_deadlines(self):
        for channels in ([], {"missav": []}):
            with self.subTest(channels=channels), self.assertRaises(m.StateStructureError):
                m.validate_crawl_state(m.CRAWL_FILE, {"channel_retry_at": channels})
        m.validate_crawl_state(m.CRAWL_FILE, {"channel_retry_at": {"missav": {"catalog": self.deadline}}})
        m.validate_crawl_state(m.CRAWL_FILE, {"channel_retry_at": {"missav": {"catalog": "invalid"}}})
        self.assertIn("$.channel_retry_at.missav.catalog: expected int", self.stderr.getvalue())

    def test_views_category_cooldown_allows_detail_request(self):
        m.record_source_denial(m.SUPJAV_URL, 403)
        detail = "https://supjav.com/ja/123456.html"
        item = {"code": "FC2-PPV-8000001", "sources": {"Supjav": detail}}
        self.http.side_effect = lambda url, purpose, **kwargs: self.response(
            url, body="<article>FC2-PPV-8000001 <span>12 views</span></article>")
        m.fill_missing_views([item])
        self.assertEqual([call.args[0] for call in self.http.call_args_list], [detail])
        self.assertEqual(item["views"], 12)
        stats = m.load_json(m.FETCH_STATE_FILE, {})[m.VIEW_STATS_KEY]
        self.assertEqual(stats["next_retry_at"], self.deadline)
