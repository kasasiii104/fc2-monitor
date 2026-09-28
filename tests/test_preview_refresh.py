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


if __name__ == '__main__':
    unittest.main()
