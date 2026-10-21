import unittest

import advance_archive_cursor as a


class ArchiveCursorAdvanceTests(unittest.TestCase):
    def test_advances_only_confirmed_current_duplicate(self):
        state = {
            "missav_page": 255,
            "last_discovery": {"sources": {"missav": {
                "status": "repeated_page", "stopped_page": 255,
            }}},
        }
        changed, reason = a.advance(state)
        self.assertTrue(changed)
        self.assertEqual(reason, "advanced")
        self.assertEqual(state["missav_page"], 256)
        self.assertEqual(state["missav_repeated_pages"][-1]["page"], 255)

    def test_does_not_advance_fetch_or_access_failures(self):
        for status in ("fetch_failed", "access_limited", "page_mismatch", "unrecognized", "ok"):
            with self.subTest(status=status):
                state = {"missav_page": 255, "last_discovery": {"sources": {"missav": {
                    "status": status, "stopped_page": 255,
                }}}}
                changed, _ = a.advance(state)
                self.assertFalse(changed)
                self.assertEqual(state["missav_page"], 255)

    def test_stale_duplicate_report_cannot_jump_current_cursor(self):
        state = {
            "missav_page": 300,
            "last_discovery": {"sources": {"missav": {
                "status": "repeated_page", "stopped_page": 255,
            }}},
        }
        changed, reason = a.advance(state)
        self.assertFalse(changed)
        self.assertEqual(reason, "cursor_mismatch")
        self.assertEqual(state["missav_page"], 300)


if __name__ == "__main__":
    unittest.main()
