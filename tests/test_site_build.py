import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import json
import fc2_monitor as m

class SiteBuildTests(unittest.TestCase):
    def test_public_catalog_keeps_zero_reviews_and_omits_internal_state(self):
        item = {'code':'FC2-PPV-1234567','fc2_review_count':0,'fc2_rating':None,'fc2_market_last_attempt':123}
        data = m.catalog_payload([item],'2026-09-27 13:00:00')
        self.assertEqual(data['items'][0]['fc2_review_count'],0)
        self.assertNotIn('fc2_market_last_attempt',data['items'][0])
        self.assertNotIn('search_links',data['items'][0])
        self.assertIn('{code}',data['search_templates']['MissAV'])

    def test_build_is_offline_and_has_complete_deployable_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'index.html'
            with patch.object(m,'HTML_FILE',output), patch.object(m.requests,'get',side_effect=AssertionError('Network forbidden')):
                m.write_site([{'code':'FC2-PPV-1234567','title':'サンプル'}],'2026-09-27 13:00:00',1)
            html = output.read_text()
            self.assertLess(len(html.encode()),15000)
            self.assertNotIn('__ASSET_VERSION__',html)
            for name in ('site.css','site.js','catalog.json'):
                self.assertTrue((output.parent/name).is_file())
            self.assertEqual(len(json.loads((output.parent/'catalog.json').read_text())['items']),1)
