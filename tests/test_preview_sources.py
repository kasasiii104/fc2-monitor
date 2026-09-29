import unittest

import fc2_monitor as m


class PreviewSourceTests(unittest.TestCase):
    legacy = 'https://fourhoi.com/fc2-ppv-8000001/preview.mp4'

    def item(self, **values):
        return dict(code='FC2-PPV-8000001', code_num='8000001', **values)

    def test_existing_official_rows_regain_the_original_provider_without_crawling(self):
        item = self.item(preview='https://example.test/current.mp4', preview_source='FC2公式')
        public = m.catalog_payload([item], 'test')['items'][0]
        self.assertEqual(public['preview'], item['preview'])
        self.assertEqual(public['preview_fallbacks'], [self.legacy])
        self.assertNotIn('preview_fallbacks', item, 'Building does not mutate crawler data')

    def test_new_sample_retains_previous_url_and_original_provider(self):
        item = self.item(preview='https://example.test/previous.mp4')
        m.apply_fc2_sample_result(item, {'preview': 'https://example.test/current.mp4'}, 100)
        self.assertEqual(item['preview_fallbacks'], ['https://example.test/previous.mp4', self.legacy])
        m.apply_fc2_sample_result(item, {'preview': 'https://example.test/newest.mp4'}, 200)
        self.assertEqual(m.preview_source_urls(item), [
            'https://example.test/newest.mp4', 'https://example.test/current.mp4', self.legacy])

    def test_failed_metadata_refresh_does_not_discard_alternatives(self):
        item = self.item(preview='https://example.test/current.mp4',
                         preview_fallbacks=['https://example.test/previous.mp4'])
        m.apply_fc2_sample_result(item, None, 100)
        self.assertEqual(item['preview'], 'https://example.test/current.mp4')
        self.assertEqual(item['preview_fallbacks'], ['https://example.test/previous.mp4'])

    def test_duplicates_malformed_urls_and_credentials_are_excluded(self):
        item = self.item(preview='https://example.test/current.mp4', preview_fallbacks=[
            None, 2, {}, 'javascript:alert(1)', 'data:video/mp4,test', '/relative.mp4',
            'https://user:secret@example.test/private.mp4', 'https://[',
            'https://example.test/current.mp4', self.legacy, self.legacy])
        self.assertEqual(m.preview_source_urls(item), [item['preview'], self.legacy])

    def test_malformed_list_is_not_iterated_as_characters(self):
        self.assertEqual(m.preview_source_urls(self.item(preview_fallbacks='https://example.test/a.mp4')),
                         [self.legacy])

    def test_invalid_product_number_does_not_generate_a_provider_url(self):
        self.assertEqual(m.preview_source_urls({'code_num': '../other', 'preview': ''}), [])

    def test_current_legacy_source_is_not_duplicated(self):
        self.assertEqual(m.preview_source_urls(self.item(preview=self.legacy)), [self.legacy])


if __name__ == '__main__':
    unittest.main()
