import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fc2_monitor as m
import walker_official_fastlane as fast


class WalkerOfficialFastlaneTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        for name, filename in (
            ("DATA_FILE", "data.json"),
            ("FETCH_STATE_FILE", "fetch.json"),
            ("HISTORY_FILE", "history.json"),
            ("CRAWL_FILE", "crawl.json"),
        ):
            self.enterContext(patch.object(m, name, root / filename))
        self.enterContext(patch.object(fast, "CHECK_LIMIT", 10))
        self.enterContext(patch.object(fast, "WORKERS", 1))
        self.enterContext(patch.object(fast, "BATCH_SIZE", 10))
        self.enterContext(patch.object(m, "now_ts", return_value=1_800_000_000))
        self.enterContext(patch.object(m, "now_jst", return_value="2027-01-15 00:00:00"))

    def record(self, num, title="サンプル作品"):
        return {
            "title": title,
            "source_url": f"https://fc2cm.h-walker.net/movie/{num}/",
            "checked_at": 1_799_999_000,
        }

    def test_only_fc2_confirmed_rows_are_added_and_use_official_primary_link(self):
        m.save_json(m.DATA_FILE, {"items": []})
        m.save_json(m.FETCH_STATE_FILE, {
            m.H_WALKER_CACHE_KEY: {
                "FC2-PPV-8000001": self.record("8000001"),
                "FC2-PPV-8000002": self.record("8000002"),
            }
        })

        def fetch(num):
            if num == "8000001":
                return {
                    "outcome": "success",
                    "fc2_market_url": "https://adult.contents.fc2.com/article/8000001/?lang=ja",
                    "fc2_title": "公式サンプル作品",
                    "fc2_rating": 4.5,
                    "fc2_review_count": 12,
                }
            return {"outcome": "not_found", "not_found": True}

        with patch.object(m, "fetch_fc2_market", side_effect=fetch):
            self.assertEqual(fast.main(), 0)

        items = m.load_json(m.DATA_FILE, {})["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["code"], "FC2-PPV-8000001")
        self.assertEqual(items[0]["source"], "FC2公式")
        self.assertEqual(items[0]["url"], "https://adult.contents.fc2.com/article/8000001/?lang=ja")
        self.assertEqual(items[0]["title"], "公式サンプル作品")
        self.assertFalse(items[0]["is_new"])

        state = m.load_json(m.FETCH_STATE_FILE, {})
        self.assertEqual(state[fast.CHECK_KEY]["FC2-PPV-8000001"]["status"], "alive")
        self.assertEqual(state[fast.CHECK_KEY]["FC2-PPV-8000002"]["status"], "not_found")

    def test_restores_official_primary_after_generic_missav_relink(self):
        item = {
            "code": "FC2-PPV-8000001",
            "code_num": "8000001",
            "source": "FC2公式",
            "url": "https://missav.ws/ja/fc2-ppv-8000001",
            "fc2_market_url": "https://adult.contents.fc2.com/article/8000001/?lang=ja",
            "sources": {"MissAV": "https://missav.ws/ja/fc2-ppv-8000001"},
        }
        self.assertGreater(fast.restore_official_primary_links([item]), 0)
        self.assertEqual(item["url"], item["fc2_market_url"])
        self.assertEqual(item["sources"]["FC2公式"], item["fc2_market_url"])


if __name__ == "__main__":
    unittest.main()
