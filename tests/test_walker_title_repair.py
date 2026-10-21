import unittest

import walker_title_repair as w


class WalkerTitleRepairTests(unittest.TestCase):
    def item(self, title):
        return {
            "code": "FC2-PPV-8000001",
            "code_num": "8000001",
            "title": title,
        }

    def cache(self, title):
        return {
            "FC2-PPV-8000001": {
                "title": title,
                "source_url": "https://fc2cm.h-walker.net/0/2/1/0/0/",
                "checked_at": 123,
            }
        }

    def test_replaces_chinese_title_with_walker_formal_title(self):
        item = self.item("中文字幕无码这个女孩们推荐")
        stats = w.repair_titles([item], self.cache("【初回限定】素人女子の個人撮影"))
        self.assertEqual(stats["repaired"], 1)
        self.assertEqual(item["title"], "【初回限定】素人女子の個人撮影")
        self.assertEqual(item["title_source"], "FC2ウォーカー")

    def test_replaces_bare_fc2_number(self):
        item = self.item("FC2-PPV-8000001")
        stats = w.repair_titles([item], self.cache("人妻限定販売"))
        self.assertEqual(stats["repaired"], 1)
        self.assertEqual(item["title"], "人妻限定販売")

    def test_trusts_kanji_only_walker_title(self):
        item = self.item("中文測試作品")
        stats = w.repair_titles([item], self.cache("人妻限定販売"))
        self.assertEqual(stats["repaired"], 1)
        self.assertEqual(item["title"], "人妻限定販売")

    def test_does_not_overwrite_good_japanese_title(self):
        item = self.item("かわいい素人女子の初撮り")
        stats = w.repair_titles([item], self.cache("別の正式タイトル"))
        self.assertEqual(stats["repaired"], 0)
        self.assertEqual(item["title"], "かわいい素人女子の初撮り")

    def test_rejects_chinese_walker_candidate(self):
        item = self.item("FC2-PPV-8000001")
        stats = w.repair_titles([item], self.cache("中文字幕无码视频"))
        self.assertEqual(stats["repaired"], 0)
        self.assertEqual(stats["rejected_walker_title"], 1)
        self.assertEqual(item["title"], "FC2-PPV-8000001")


if __name__ == "__main__":
    unittest.main()
