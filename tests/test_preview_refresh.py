import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fc2_monitor as m


class PreviewRefreshTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(m, 'FETCH_STATE_FILE', Path(directory) / 'fetch.json'))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.now = 1_800_000_000
        self.enterContext(patch.object(m, 'now_ts', lambda: self.now))

    def item(self, number='8000001', **extra):
        return dict(code=f'FC2-PPV-{number}', code_num=number, **extra)

    def test_failed_refresh_preserves_existing_sample_and_retries_after_cooldown(self):
        for result in (None, {'preview': '', 'poster': 'https://example.test/poster.jpg'}):
            with self.subTest(result=result):
                item = self.item(preview='https://example.test/sample.mp4', preview_source='FC2公式')
                with patch.object(m, 'fetch_fc2_sample_assets', return_value=result) as fetch:
                    m.refresh_fc2_previews([item])
                    m.refresh_fc2_previews([item])
                    self.assertEqual(fetch.call_count, 1)
                    self.now += m.PREVIEW_REFRESH_SEC
                    m.refresh_fc2_previews([item])
                    self.assertEqual(fetch.call_count, 2)
                self.assertEqual(item['preview'], 'https://example.test/sample.mp4')
                self.assertEqual(item['preview_source'], 'FC2公式')

    def test_recent_missing_and_guessed_urls_do_not_bypass_retry_delay(self):
        items = [self.item(preview='', preview_checked_at=self.now),
                 self.item('8000002', preview='https://fourhoi.com/fc2-ppv-8000002/preview.mp4',
                           fc2_sample_checked_at=self.now)]
        with patch.object(m, 'fetch_fc2_sample_assets', return_value=None) as fetch:
            m.refresh_fc2_previews(items)
            fetch.assert_not_called()
            self.now += m.FAIL_SKIP_SEC
            m.refresh_fc2_previews(items)
            self.assertEqual(fetch.call_count, 2)

    def test_shrinking_queue_does_not_skip_unchecked_items(self):
        items = [self.item(str(8000001 + i), preview='') for i in range(3)]
        m.save_json(m.FETCH_STATE_FILE, {m.PREVIEW_REFRESH_CURSOR_KEY: 2})
        with patch.object(m, 'PREVIEW_REFRESH_LIMIT', 1), patch.object(m, 'fetch_fc2_sample_assets',
                return_value={'preview': 'https://example.test/sample.mp4'}) as fetch:
            for _ in items:
                m.refresh_fc2_previews(items)
            self.assertEqual({call.args[0] for call in fetch.call_args_list}, {x['code_num'] for x in items})
            self.assertEqual(fetch.call_count, len(items))
        self.assertTrue(all(x.get('preview_source') == 'FC2公式' for x in items))
        self.assertNotIn(m.PREVIEW_REFRESH_CURSOR_KEY, m.load_json(m.FETCH_STATE_FILE, {}))

    def test_more_recent_sample_check_prevents_duplicate_request(self):
        item = self.item(preview='https://example.test/sample.mp4', preview_source='FC2公式',
                         preview_checked_at=self.now - m.PREVIEW_REFRESH_SEC,
                         fc2_sample_checked_at=self.now)
        with patch.object(m, 'fetch_fc2_sample_assets') as fetch:
            m.refresh_fc2_previews([item])
            fetch.assert_not_called()

    def test_thumbnail_repair_respects_preview_cooldown_and_uses_unchecked_slot(self):
        failed = self.item('8000002', preview_checked_at=self.now - 2 * 3600)
        unchecked = self.item('8000001')
        with patch.object(m, 'THUMB_BACKFILL_LIMIT', 1), \
             patch.object(m, 'fetch_fc2_sample_assets', return_value=None) as fetch:
            m.run_thumbnail_backfill([failed, unchecked])
        fetch.assert_called_once_with('8000001')
        stats = m.load_json(m.FETCH_STATE_FILE, {})[m.THUMB_BACKFILL_STATS_KEY]
        self.assertEqual(stats['cooldown_skipped'], 1)

    def test_legacy_thumbnail_attempt_is_not_retried_immediately(self):
        item = self.item()
        m.save_json(m.FETCH_STATE_FILE, {m.THUMB_BACKFILL_ATTEMPTS_KEY: {item['code']: self.now}})
        with patch.object(m, 'fetch_fc2_sample_assets') as fetch:
            m.run_thumbnail_backfill([item])
        fetch.assert_not_called()

    def test_all_three_routes_share_success_and_failure_cooldown(self):
        for result in (None, {'preview': 'https://example.test/sample.mp4', 'poster': ''}):
            for first in (m.enrich_fc2_sample_assets, m.run_thumbnail_backfill, m.refresh_fc2_previews):
                with self.subTest(result=result, first=first.__name__):
                    m.save_json(m.FETCH_STATE_FILE, {})
                    item = self.item()
                    with patch.object(m, 'fetch_fc2_sample_assets', return_value=result) as fetch:
                        first([item])
                        m.enrich_fc2_sample_assets([item])
                        m.run_thumbnail_backfill([item])
                        m.refresh_fc2_previews([item])
                        self.now += 2 * 3600
                        m.enrich_fc2_sample_assets([item])
                        m.run_thumbnail_backfill([item])
                        m.refresh_fc2_previews([item])
                    self.assertEqual(fetch.call_count, 1)

    def test_failure_backoff_is_shared_capped_and_reset_after_recovery(self):
        original = 'https://example.test/saved.mp4'
        item = self.item(preview=original, preview_source='FC2公式')
        for delay in (12, 24, 48, 72, 72):
            with patch.object(m, 'fetch_fc2_sample_assets', return_value=None):
                m.enrich_fc2_sample_assets([item])
            self.assertEqual(item['fc2_sample_retry_at'], self.now + delay * 3600)
            self.assertEqual(item['preview'], original)
            self.assertFalse(m.sample_retry_due(item, self.now + delay * 3600 - 1))
            self.now += delay * 3600
            self.assertTrue(m.sample_retry_due(item, self.now))
        with patch.object(m, 'fetch_fc2_sample_assets', return_value={
                'preview': 'https://example.test/recovered.mp4', 'poster': 'https://example.test/poster.jpg'}):
            m.refresh_fc2_previews([item])
        self.assertEqual(item['fc2_sample_failures'], 0)
        self.assertEqual(item['fc2_sample_last_success'], self.now)
        self.assertEqual(item['fc2_sample_retry_at'], self.now + m.PREVIEW_REFRESH_SEC)

    def test_historical_acquisition_has_reserved_slots_without_starving_refresh(self):
        pending = [self.item(str(8000000 + i)) for i in range(8)]
        confirmed = [self.item(str(9000000 + i), preview='https://example.test/sample.mp4',
                               preview_source='FC2公式', fc2_sample_checked_at=1) for i in range(8)]
        with patch.object(m, 'PREVIEW_REFRESH_LIMIT', 5), \
             patch.object(m, 'fetch_fc2_sample_assets', return_value=None) as fetch:
            m.refresh_fc2_previews(confirmed + pending)
        codes = [call.args[0] for call in fetch.call_args_list]
        self.assertEqual(sum(code.startswith('8') for code in codes), 4)
        self.assertEqual(sum(code.startswith('9') for code in codes), 1)
        stats = m.load_json(m.FETCH_STATE_FILE, {})[m.PREVIEW_REFRESH_STATS_KEY]
        self.assertEqual(stats['never_checked_remaining'], 4)
        self.assertEqual(stats['backfill_processed'], 4)
        self.assertEqual(stats['refresh_processed'], 1)

    def test_unused_lane_capacity_is_reused(self):
        for pending_count, confirmed_count in ((1, 9), (9, 0), (0, 9)):
            with self.subTest(pending=pending_count, confirmed=confirmed_count):
                pending = [self.item(str(8000000 + i)) for i in range(pending_count)]
                confirmed = [self.item(str(9000000 + i), preview='https://example.test/sample.mp4',
                                       preview_source='FC2公式') for i in range(confirmed_count)]
                with patch.object(m, 'PREVIEW_REFRESH_LIMIT', 5), \
                     patch.object(m, 'fetch_fc2_sample_assets', return_value=None) as fetch:
                    m.refresh_fc2_previews(pending + confirmed)
                self.assertEqual(fetch.call_count, 5)

    def test_older_unchecked_record_precedes_new_discovery(self):
        historical = self.item('8000001', first_seen='2026-09-20 01:00:00')
        newer = self.item('9000001', first_seen='2026-09-28 01:00:00')
        for route, limit in ((m.run_thumbnail_backfill, 'THUMB_BACKFILL_LIMIT'),
                             (m.refresh_fc2_previews, 'PREVIEW_REFRESH_LIMIT')):
            with self.subTest(route=route.__name__):
                m.save_json(m.FETCH_STATE_FILE, {})
                with patch.object(m, limit, 1), patch.object(m, 'fetch_fc2_sample_assets', return_value=None) as fetch:
                    route([newer.copy(), historical.copy()])
                fetch.assert_called_once_with('8000001')

    def test_successful_poster_is_kept_even_when_preview_is_not_returned(self):
        item = self.item(preview='https://example.test/saved.mp4', preview_source='FC2公式')
        with patch.object(m, 'fetch_fc2_sample_assets', return_value={
                'preview': '', 'poster': 'https://example.test/poster.jpg'}):
            m.refresh_fc2_previews([item])
        self.assertEqual(item['thumb'], 'https://example.test/poster.jpg')
        self.assertEqual(item['preview'], 'https://example.test/saved.mp4')
        self.assertEqual(item['fc2_sample_failures'], 1)


if __name__ == '__main__':
    unittest.main()
