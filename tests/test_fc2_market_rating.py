import unittest

from bs4 import BeautifulSoup

import fc2_monitor as m


class FC2MarketRatingTests(unittest.TestCase):
    def rating(self, markup='', text=''):
        return m.extract_fc2_market_rating(BeautifulSoup(markup, 'html.parser'), text)

    def test_script_rejects_out_of_range_whole_numbers(self):
        for raw in ('50', '15', '-1', '5.01', '1e309', '4.8/10'):
            for body in (f'ratingValue:{raw}', f'"ratingValue": "{raw}"',
                         '{"aggregateRating":{"ratingValue":"' + raw + '"}}'):
                with self.subTest(body=body):
                    self.assertIsNone(self.rating(f'<script>{body}</script>'))

    def test_visible_rating_rejects_invalid_numbers_and_scales(self):
        for raw in ('15/5', '4.8/10', '50', '-1', '5.001', '1e309', '4.8.2'):
            for text in (raw, '評価: ' + raw, '5点満点中 ' + raw):
                with self.subTest(text=text):
                    self.assertIsNone(self.rating(text=text))

    def test_normal_ratings_from_visible_json_ld_and_meta(self):
        for text in ('4.8/5', '平均評価: 4.8', '5点満点中4.8'):
            with self.subTest(text=text):
                self.assertEqual(self.rating(text=text), 4.8)
        self.assertEqual(self.rating('<script type="application/ld+json">'
                                     '{"aggregateRating":{"ratingValue":4.5}}'
                                     '</script>'), 4.5)
        self.assertEqual(self.rating('<meta itemprop="ratingValue" content="4.8">'), 4.8)

    def test_explicit_structured_scale_must_be_five(self):
        self.assertIsNone(self.rating('<script>{"aggregateRating":'
                                      '{"ratingValue":4.8,"bestRating":10}}</script>'))
        self.assertIsNone(self.rating('<meta itemprop="ratingValue" content="4.8/10">'))
        self.assertIsNone(self.rating('<meta itemprop="ratingValue" content="50">'))

    def test_script_fallback_runs_after_successful_json_parse(self):
        self.assertEqual(self.rating('<script>{"ratingValue": "4.6"}</script>'), 4.6)

    def test_script_fallback_requires_quoted_key(self):
        self.assertIsNone(self.rating('<script>ratingValue:4.6</script>'))

    def test_review_counts_are_not_ratings(self):
        for text in ('評価件数: 5', 'レビュー数: 5', '評価: 5件', '評価 4 reviews'):
            with self.subTest(text=text):
                self.assertIsNone(self.rating(text=text))
        markup = '<script>{"aggregateRating":{"reviewCount":5}}</script>'
        self.assertIsNone(self.rating(markup))
        soup = BeautifulSoup(markup, 'html.parser')
        self.assertEqual(m.extract_fc2_market_review_count(soup, ''), 5)

    def test_visible_rating_cannot_backtrack_to_partial_number_or_scale(self):
        for text in ('評価: 4/10 reviews', '評価: 1e-1件',
                     '評価: 4.8/10 件', '評価: 4.8/10 reviews',
                     '評価: 4.8/10人', '評価: 4.8/10 ratings'):
            with self.subTest(text=text):
                self.assertIsNone(self.rating(text=text))

    def test_visible_valid_ratings_remain_supported(self):
        for text in ('評価: 4.5', '評価: 4.5/5', '評価 4.5 (12件)'):
            with self.subTest(text=text):
                self.assertEqual(self.rating(text=text), 4.5)

    def test_script_fallback_rejects_partial_number_or_scale(self):
        for raw in ('4/10', '4.8/10', '4.8/10 件', '4.8/10 reviews'):
            with self.subTest(raw=raw):
                self.assertIsNone(self.rating(
                    '<script>{"ratingValue": "' + raw + '"}</script>'))
