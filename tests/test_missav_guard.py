import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fc2_monitor as m
import missav_guard as g


class MissAVGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.data = root / "data.json"
        self.history = root / "notified.json"
        self.state = root / "guard.json"
        self.patches = [
            patch.object(m, "DATA_FILE", self.data),
            patch.object(m, "HISTORY_FILE", self.history),
            patch.object(m, "NOTIFY_QUEUE_FILE", root / "queue.json"),
            patch.object(g, "STATE_FILE", self.state),
            patch.object(m, "now_ts", return_value=1_900_000_000),
            patch.object(m, "now_jst", return_value="2030-03-17 12:00:00"),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def probe(status, url, **extra):
        return {
            "status": status,
            "final_url": url,
            "access_limited": status in (401, 403, 429),
            "error": None,
            **extra,
        }

    def test_dead_requires_all_missav_hosts_to_be_explicitly_missing(self):
        with patch.object(g, "_existence_request", side_effect=lambda url: self.probe(404, url)):
            result = g.Guard().probe("8000001", force=True)
        self.assertEqual(result["status"], "dead")
        self.assertEqual(result["reason"], "all_missav_hosts_404")
        self.assertEqual(result["attempts"], 3)

    def test_live_mirror_wins_over_a_404_and_exposes_working_url(self):
        calls = []
        live = "https://missav.live/ja/fc2-ppv-8000001"
        def page(url):
            calls.append(url)
            if len(calls) == 1:
                return self.probe(404, url)
            return self.probe(200, live)
        with patch.object(g, "_existence_request", side_effect=page):
            result = g.Guard().probe("8000001", force=True)
        self.assertEqual(result["status"], "alive")
        self.assertEqual(result["live_url"], live)
        self.assertEqual(len(calls), 2)

    def test_access_denial_is_unknown_and_never_treated_as_404(self):
        with patch.object(g, "_existence_request", return_value=self.probe(403, "https://missav.ws/ja/fc2-ppv-8000001")):
            result = g.Guard().probe("8000001", force=True)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "access_limited")

    def test_new_items_are_fail_closed_and_live_items_are_relinked(self):
        guard = g.Guard()
        videos = [
            {"code": "FC2-PPV-8000001", "code_num": "8000001", "url": "https://missav.ws/ja/fc2-ppv-8000001", "sources": {"MissAV": "https://missav.ws/ja/fc2-ppv-8000001"}},
            {"code": "FC2-PPV-8000002", "code_num": "8000002"},
        ]
        live = "https://missav.live/ja/fc2-ppv-8000001"
        with patch.object(guard, "probe", side_effect=[
            {"status": "alive", "live_url": live},
            {"status": "unknown", "reason": "access_limited"}
        ]):
            kept = g.filter_unseen(videos, set(), guard)
        self.assertEqual([x["code"] for x in kept], ["FC2-PPV-8000001"])
        self.assertEqual(kept[0]["url"], live)
        self.assertEqual(kept[0]["sources"]["MissAV"], live)

    def test_prune_saved_removes_confirmed_404_and_repairs_live_link(self):
        items = [
            {"code": "FC2-PPV-8000001", "code_num": "8000001", "title": "A"},
            {"code": "FC2-PPV-8000002", "code_num": "8000002", "title": "B", "url": "https://missav.ws/ja/fc2-ppv-8000002"},
        ]
        m.save_json(m.DATA_FILE, {"updated_at": "old", "items": items})
        m.save_json(m.HISTORY_FILE, {"ids": [x["code"] for x in items]})
        live = "https://missav.live/ja/fc2-ppv-8000002"

        def page(url):
            if "8000001" in url:
                return self.probe(404, url)
            return self.probe(200, live)

        with patch.object(g, "_existence_request", side_effect=page):
            stats = g.prune_saved(limit=2)
        saved = m.load_json(m.DATA_FILE, {})["items"]
        history = m.load_json(m.HISTORY_FILE, {})["ids"]
        self.assertEqual(stats["removed"], 1)
        self.assertEqual(stats["relinked"], 1)
        self.assertEqual([x["code"] for x in saved], ["FC2-PPV-8000002"])
        self.assertEqual(saved[0]["url"], live)
        self.assertEqual(saved[0]["sources"]["MissAV"], live)
        self.assertEqual(history, ["FC2-PPV-8000002"])

    def test_official_dead_items_are_protected_and_can_recover(self):
        fallback = "https://adult.contents.fc2.com/article/8000001/?lang=ja"
        cases = [
            ({"source": "FC2公式"}, fallback, True),
            ({"fc2_market_last_success": "123", "fc2_market_url": "https://official.example/market"}, "https://official.example/market", True),
            ({"fc2_market_last_success": 123, "sources": {"FC2公式": "https://official.example/source"}}, "https://official.example/source", True),
            ({"fc2_market_last_success": 123, "fc2_market_not_found": True}, fallback, False),
            ({}, fallback, False),
        ]
        code = "FC2-PPV-8000001"
        live = "https://missav.live/ja/fc2-ppv-8000001"
        for metadata, official, protected in cases:
            with self.subTest(metadata=metadata):
                item = {"code": code, "url": live, **metadata}
                item["sources"] = {**item.get("sources", {}), "MissAV": live, "Other": "https://example.com"}
                m.save_json(m.DATA_FILE, {"items": [item]})
                m.save_json(m.HISTORY_FILE, {"ids": [code]})
                pending = [{"code": c, "queued_at": m.now_ts(), "item": {"code": c}}
                           for c in (code, "FC2-PPV-8000002")]
                m.save_notify_queue(pending)
                with patch.object(g, "_existence_request", side_effect=lambda url: self.probe(404, url)):
                    stats = g.prune_saved()
                saved = m.load_json(m.DATA_FILE, {})["items"]
                self.assertEqual(stats["protected"], int(protected))
                self.assertEqual(stats["removed"], int(not protected))
                self.assertEqual(m.load_json(g.STATE_FILE, {})["last_audit"]["protected"], int(protected))
                self.assertEqual(m.load_json(m.HISTORY_FILE, {})["ids"], [code] if protected else [])
                self.assertEqual([row["code"] for row in m.load_notify_queue()],
                                 [code, "FC2-PPV-8000002"])
                self.assertEqual(m.load_notify_queue()[0].get("held", False), not protected)
                if not protected:
                    self.assertEqual(saved, [])
                    continue
                self.assertEqual(saved[0]["url"], m.missav_detail_urls("8000001")[0])
                self.assertEqual(saved[0]["sources"]["FC2公式"], official)
                self.assertEqual(saved[0]["sources"]["Other"], "https://example.com")
                self.assertEqual(saved[0]["sources"]["MissAV"], saved[0]["url"])
                self.assertIs(saved[0]["missav_dead"], True)
                self.assertEqual(saved[0]["missav_dead_checked_at"], m.now_ts())
                with patch.object(g, "_existence_request", return_value=self.probe(200, live)):
                    stats = g.prune_saved()
                recovered = m.load_json(m.DATA_FILE, {})["items"][0]
                self.assertEqual(stats["relinked"], 1)
                self.assertEqual(recovered["url"], live)
                self.assertEqual(recovered["sources"]["MissAV"], live)
                self.assertNotIn("missav_dead", recovered)
                self.assertNotIn("missav_dead_checked_at", recovered)

    def test_invalidated_item_compacts_to_missav(self):
        from compact_public_catalog import compact_item
        item = {"code": "FC2-PPV-8000001", "source": "FC2公式",
                "fc2_market_url": "https://adult.contents.fc2.com/article/8000001/?lang=ja"}
        g.invalidate_missav_link(item)
        compacted, direct = compact_item(item)
        self.assertTrue(direct)
        self.assertEqual(compacted["url"], m.missav_detail_urls("8000001")[0])
        self.assertNotIn("fc2_market_url", compacted)

    def test_live_url_clears_flags_even_when_url_is_unchanged(self):
        live = "https://missav.live/ja/fc2-ppv-8000001"
        item = {"url": live, "sources": {"MissAV": live},
                "missav_dead": True, "missav_dead_checked_at": 123}
        self.assertTrue(g.apply_live_url(item, {"status": "alive", "live_url": live}))
        self.assertNotIn("missav_dead", item)
        self.assertNotIn("missav_dead_checked_at", item)


if __name__ == "__main__":
    unittest.main()
