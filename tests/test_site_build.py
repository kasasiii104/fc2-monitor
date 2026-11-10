import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import json
import hashlib
import fc2_monitor as m

class SiteBuildTests(unittest.TestCase):
    def test_atomic_site_output_matches_previous_bytes(self):
        items = [{'code': 'FC2-PPV-1234567', 'title': 'サンプル\n<&>'}]
        updated_at = '2026-09-27 13:00:00'
        status = {'sources': {}}
        payload = m.catalog_payload(items, updated_at, status)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        payload['version'] = hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:16]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'index.html'
            with patch.object(m, 'HTML_FILE', output):
                m.write_site(items, updated_at, 1, update_status=status)
            self.assertEqual((output.parent / 'catalog.json').read_bytes(),
                             json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
            self.assertEqual(output.read_bytes(), m.render_html(items, updated_at, 1).encode('utf-8'))

    def test_replace_failure_preserves_existing_site_file_and_removes_temporary(self):
        real_replace = m.os.replace
        for filename in ('catalog.json', 'index.html'):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'index.html'
                target = output.parent / filename
                target.write_bytes('旧内容\n'.encode('utf-8'))

                def fail_target(source, destination):
                    self.assertEqual(Path(source).parent, output.parent)
                    if Path(destination) == target:
                        raise OSError('replace failed')
                    return real_replace(source, destination)

                with patch.object(m, 'HTML_FILE', output), patch.object(m.os, 'replace', side_effect=fail_target):
                    with self.assertRaisesRegex(OSError, 'replace failed'):
                        m.write_site([], '2026-09-27 13:00:00', 0, update_status={'sources': {}})
                self.assertEqual(target.read_bytes(), '旧内容\n'.encode('utf-8'))
                self.assertEqual(list(output.parent.glob('*.tmp')), [])

    def test_save_json_preserves_previous_bytes(self):
        payload = {'title': '日本語', 'items': [1, None], 'text': '改行\n'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            m.save_json(path, payload)
            self.assertEqual(path.read_bytes(),
                             (json.dumps(payload, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))

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
            self.assertLess(len(html.encode()),20000)
            self.assertNotIn('__ASSET_VERSION__',html)
            for name in ('site.css','site.js','catalog.json','update.json'):
                self.assertTrue((output.parent/name).is_file())
            catalog = json.loads((output.parent/'catalog.json').read_text())
            update = json.loads((output.parent/'update.json').read_text())
            self.assertEqual(len(catalog['items']),1)
            self.assertEqual(update['version'], catalog['version'])
            self.assertEqual(update['item_count'],1)
            self.assertLess((output.parent/'update.json').stat().st_size, 300)
