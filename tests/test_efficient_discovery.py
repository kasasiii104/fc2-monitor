import unittest
from unittest.mock import Mock, patch

import efficient_discovery as e


class EfficientDiscoveryTests(unittest.TestCase):
    def test_archive_dead_is_skipped_for_extended_window(self):
        guard = Mock()
        guard.checks = {"FC2-PPV-4980001": {"status": "dead", "checked_at": 1000}}
        self.assertTrue(e.cached_archive_dead(guard, "FC2-PPV-4980001", False, 1000 + 86400))

    def test_recent_dead_uses_normal_guard_retry_policy(self):
        guard = Mock()
        guard.checks = {"FC2-PPV-4980001": {"status": "dead", "checked_at": 1000}}
        self.assertFalse(e.cached_archive_dead(guard, "FC2-PPV-4980001", True, 1000 + 86400))

    def test_old_archive_dead_is_rechecked(self):
        guard = Mock()
        guard.checks = {"FC2-PPV-4980001": {"status": "dead", "checked_at": 1000}}
        self.assertFalse(e.cached_archive_dead(
            guard, "FC2-PPV-4980001", False, 1000 + e.ARCHIVE_DEAD_RECHECK_SEC + 1
        ))

    def test_verify_official_keeps_missav_destination(self):
        num = "8000001"
        official = f"https://adult.contents.fc2.com/article/{num}/?lang=ja"
        template = e.m.missav_detail_urls(num)[0]
        for url in (template, official, "https://missav.live/ja/fc2-ppv-8000001"):
            with self.subTest(url=url):
                row = e.base.make_item(f"FC2-PPV-{num}", {"title": "サンプル作品"}, "", recent=False)
                row["url"] = url
                with patch.object(e.m, "load_json", return_value={}), patch.object(e.m, "save_json"), patch.object(e.m, "fetch_fc2_market", return_value={"outcome": "success", "fc2_market_url": official}):
                    status, _ = e.verify_official(row, num, {}, 123)
                self.assertEqual(status, "alive")
                self.assertEqual(row["url"], url if e.m.discovery_source(url) == "missav" else template)
                self.assertEqual(row["source"], "FC2公式")
                self.assertEqual(row["sources"]["FC2公式"], official)


if __name__ == "__main__":
    unittest.main()
