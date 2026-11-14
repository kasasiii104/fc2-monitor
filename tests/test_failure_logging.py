import contextlib
import io
import unittest
from unittest.mock import Mock, patch

import fc2_monitor as m
import repair_code_only_titles as repair


SECRET = 'https://webhook.example/private-token SECRET-TOKEN private-title'


class FailureLoggingTests(unittest.TestCase):
    def setUp(self):
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(patch.object(m, 'run_deadline_reached', return_value=False))
        self.enterContext(patch.object(m, 'source_in_cooldown', return_value=False))

    def assert_safe(self, operation, error, count):
        output = self.output.getvalue()
        self.assertIn(f'[{operation}]', output)
        self.assertIn(f'error={error} count={count}', output)
        for secret in ('https://', 'SECRET-TOKEN', 'private-token', 'private-title'):
            self.assertNotIn(secret, output)

    def test_fetch_page_failure_logs_without_changing_result(self):
        with patch.object(m.requests, 'get', side_effect=TimeoutError(SECRET)):
            result = m.fetch_page('https://example.test/')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], SECRET)
        self.assert_safe('fetch_page', 'TimeoutError', 1)

    def test_parse_failure_logs_and_keeps_page_fallback(self):
        response = Mock(text='x' * 3000, status_code=200, url='https://example.test/', headers={})
        with patch.object(m.requests, 'get', return_value=response), patch.object(m, 'BeautifulSoup', side_effect=ValueError(SECRET)):
            result = m.fetch_page(response.url)
        self.assertIsNone(result['soup'])
        self.assertEqual(result['title'], '')
        self.assert_safe('fetch_page', 'ValueError', 1)

    def test_optional_failure_counts_items_and_continues(self):
        action = Mock(side_effect=RuntimeError(SECRET))
        self.assertFalse(m.run_optional_step('Metadata', action, [{}, {}]))
        later = Mock()
        self.assertTrue(m.run_optional_step('Later', later))
        later.assert_called_once_with()
        self.assertIn('::warning::Metadata failed (RuntimeError); continuing with saved/partial metadata count=2', self.output.getvalue())
        for secret in ('https://', 'SECRET-TOKEN', 'private-token', 'private-title'):
            self.assertNotIn(secret, self.output.getvalue())

    def test_optional_does_not_swallow_state_structure_error(self):
        with self.assertRaises(m.StateStructureError):
            m.run_optional_step('State', Mock(side_effect=m.StateStructureError(SECRET)))
        self.assertEqual(self.output.getvalue(), '')

    def test_optional_deadline_still_skips_action(self):
        action = Mock()
        with patch.object(m, 'run_deadline_reached', return_value=True):
            self.assertFalse(m.run_optional_step('Metadata', action, []))
        action.assert_not_called()
        self.assertEqual(self.output.getvalue(), '')

    def test_japanese_title_failures_aggregate(self):
        items = [{'code': f'FC2-PPV-{num}', 'code_num': str(num),
                  'title': f'FC2-PPV-{num}', 'is_new': True}
                 for num in (8000001, 8000002)]
        with patch.object(m, 'load_json', return_value={}), patch.object(m, 'save_json'), \
                patch.object(m, 'fetch_soup', side_effect=ValueError(SECRET)) as fetch:
            m.refresh_japanese_titles(items)
        self.assertGreater(fetch.call_count, 1)
        self.assert_safe('refresh_japanese_titles', 'ValueError', fetch.call_count)
        lines = [line for line in self.output.getvalue().splitlines()
                 if line.startswith('[refresh_japanese_titles]')]
        self.assertEqual(len(lines), 1)

    def test_latest_video_failures_identify_each_source(self):
        with patch.object(m, 'load_json', return_value={}), \
                patch.object(m, 'SOURCE_RETRY_AT', {}), \
                patch.object(m, 'scrape_missav', side_effect=ValueError(SECRET)), \
                patch.object(m, 'scrape_javdb', side_effect=ValueError(SECRET)), \
                patch.object(m, 'scrape_supjav', side_effect=ValueError(SECRET)):
            with self.assertRaises(RuntimeError):
                m.get_latest_videos()
        lines = [line for line in self.output.getvalue().splitlines()
                 if line.startswith('[get_latest_videos]')]
        self.assertEqual(lines, [f'[get_latest_videos] source={source} error=ValueError count=1'
                                 for source in ('missav', 'javdb', 'supjav')])

    def test_code_only_failures_aggregate(self):
        item = {'code': 'FC2-PPV-8000001', 'code_num': '8000001', 'title': 'FC2-PPV-8000001'}
        with patch.object(m, 'load_json', return_value={}), patch.object(repair.requests, 'get', side_effect=TimeoutError(SECRET)) as get:
            self.assertEqual(repair.fetch_title(item), ('', ''))
        self.assert_safe('code-only title', 'TimeoutError', get.call_count)
        self.assertEqual(len(self.output.getvalue().splitlines()), 1)
