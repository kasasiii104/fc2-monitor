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


if __name__ == "__main__":
    unittest.main()
