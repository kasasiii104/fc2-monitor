import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import optimize_public_catalog as o


class PublicCatalogOptimizerTests(unittest.TestCase):
    def test_split_catalog_keeps_small_first_paint_and_complete_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docs = root / "docs"
            docs.mkdir()
            rows = [{"code": f"FC2-PPV-{i:07d}", "title": f"作品{i}"} for i in range(2505)]
            catalog = docs / "catalog.json"
            catalog.write_text(json.dumps({"version": "abc", "items": rows}), encoding="utf-8")
            with patch.object(o, "DOCS", docs), patch.object(o, "CATALOG", catalog), \
                 patch.object(o, "FIRST_PAINT_ITEMS", 300), patch.object(o, "CHUNK_ITEMS", 1000):
                total, chunks = o.split_catalog()
            manifest = json.loads(catalog.read_text(encoding="utf-8"))
            self.assertEqual((total, chunks), (2505, 3))
            self.assertEqual(len(manifest["items"]), 300)
            self.assertEqual(manifest["item_count"], 2505)
            self.assertTrue(manifest["progressive"])
            recovered = list(manifest["items"])
            for entry in manifest["chunks"]:
                recovered.extend(json.loads((docs / entry["url"]).read_text(encoding="utf-8"))["items"])
            self.assertEqual([x["code"] for x in recovered], [x["code"] for x in rows])

    def test_generated_site_loader_is_patched_for_progressive_hydration(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "site.js"
            source = (Path(__file__).resolve().parents[1] / "web" / "site.js").read_text(encoding="utf-8")
            target.write_text(source, encoding="utf-8")
            with patch.object(o, "SITE_JS", target):
                o.patch_site_js()
            patched = target.read_text(encoding="utf-8")
            self.assertIn("async function fetchCatalogChunks(payload)", patched)
            self.assertIn("Initial navigation becomes interactive after only the small inline", patched)
            self.assertIn("Background refresh keeps the old complete feed visible", patched)


if __name__ == "__main__":
    unittest.main()
