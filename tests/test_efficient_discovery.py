import unittest
from unittest.mock import Mock

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


if __name__ == "__main__":
    unittest.main()
