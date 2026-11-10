import contextlib
import io
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
import fc2_monitor as m


class JavdbLinkTests(unittest.TestCase):
    def scrape(self, html, pages=None):
        info = {'ok': True, 'status': 200, 'cloudflare': False,
                'soup': BeautifulSoup(html, 'html.parser'),
                'final_url': 'https://javdb.com/search?q=FC2'}
        completed = set()
        with patch.object(m, 'fetch_page', return_value=info) as fetch, \
                contextlib.redirect_stdout(io.StringIO()):
            rows = m.scrape_javdb(pages or [1], completed)
        return rows, completed, fetch

    def test_card_tags_and_actors_cannot_replace_detail_url_or_title(self):
        rows, completed, _ = self.scrape('''<div>
            <a href="/v/abc" title="FC2-PPV-1234567 original">open</a>
            <a href="/tags?x">FC2-PPV-1234567 a much longer unrelated tag title</a>
            <a href="/actors/y">FC2-PPV-7654321 actor</a></div>''')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['url'], 'https://javdb.com/v/abc')
        self.assertNotIn('unrelated', rows[0]['title'])
        self.assertEqual(completed, {1})

    def test_parent_with_multiple_codes_cannot_supply_number(self):
        rows, completed, _ = self.scrape('''<div>
            FC2-PPV-1234567 FC2-PPV-7654321
            <a href="/v/abc">open</a></div>''')
        self.assertEqual(rows, [])
        self.assertEqual(completed, set())

    def test_parent_with_multiple_details_cannot_supply_number(self):
        rows, completed, _ = self.scrape('''<div>FC2-PPV-1234567
            <a href="/v/abc">open</a><a href="/v/def">open</a></div>''')
        self.assertEqual(rows, [])
        self.assertEqual(completed, set())

    def test_external_and_non_detail_links_are_not_recognized(self):
        for href in ('https://other.example/v/abc', '//other.example/v/abc',
                     'https://javdb.com.other.example/v/abc', '/tags?x', '/actors/y'):
            with self.subTest(href=href):
                rows, completed, _ = self.scrape(
                    f'<a href="{href}">FC2-PPV-1234567</a>')
                self.assertEqual(rows, [])
                self.assertEqual(completed, set())

    def test_normal_cards_complete_all_requested_pages(self):
        rows, completed, fetch = self.scrape('''
            <div><a href="/v/abc" title="FC2-PPV-1234567 first">open</a></div>
            <div>FC2-PPV-7654321 second <a href="/v/def">open</a></div>
            <div><a href="https://javdb.com/v/ghi">FC2-PPV-2345678 third</a></div>
            ''', [1, 2, 7])
        self.assertEqual({row['url'] for row in rows}, {
            'https://javdb.com/v/abc', 'https://javdb.com/v/def', 'https://javdb.com/v/ghi'})
        self.assertEqual(completed, {1, 2, 7})
        self.assertEqual(fetch.call_count, 3)

    def test_anchor_number_takes_priority_over_parent(self):
        rows, _, _ = self.scrape('''<div>FC2-PPV-7654321
            <a href="/v/abc" title="FC2-PPV-1234567 first">open</a></div>''')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['code'], 'FC2-PPV-1234567')
