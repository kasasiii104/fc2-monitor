import unittest

import aggressive_discovery as a


class AggressiveDiscoveryTests(unittest.TestCase):
    def test_accepts_japanese_walker_title_as_archive(self):
        row = a.make_item("FC2-PPV-4980001", {
            "title": "限定販売 素人女子の初撮り作品",
            "rating": 4.5,
            "review_count": 12,
            "source_url": "https://fc2cm.h-walker.net/sample/",
            "checked_at": 123,
        }, "2026-10-20 14:00:00", recent=False)
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "限定販売 素人女子の初撮り作品")
        self.assertEqual(row["title_source"], "FC2ウォーカー")
        self.assertEqual(row["discovery_kind"], "archive")
        self.assertFalse(row["is_new"])
        self.assertEqual(row["first_seen"], "")
        self.assertEqual(row["fc2_rating"], 4.5)
        self.assertEqual(row["fc2_review_count"], 12)

    def test_rejects_unresolved_chinese_or_number_only_title(self):
        self.assertIsNone(a.make_item("FC2-PPV-4980002", {
            "title": "中文字幕无码视频",
        }, "2026-10-20 14:00:00"))
        self.assertIsNone(a.make_item("FC2-PPV-4980003", {
            "title": "FC2-PPV-4980003",
        }, "2026-10-20 14:00:00"))

    def test_recent_walker_head_is_marked_new_for_site(self):
        row = a.make_item("FC2-PPV-4980004", {
            "title": "新人限定 美少女個人撮影",
        }, "2026-10-20 14:00:00", recent=True)
        self.assertTrue(row["is_new"])
        self.assertEqual(row["discovery_kind"], "recent")
        self.assertEqual(row["first_seen"], "2026-10-20 14:00:00")


if __name__ == "__main__":
    unittest.main()
