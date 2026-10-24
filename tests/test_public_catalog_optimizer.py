import hashlib
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
                 patch.object(o, "FIRST_PAINT_ITEMS", 160), patch.object(o, "CHUNK_ITEMS", 600):
                total, chunks = o.split_catalog()
            manifest = json.loads(catalog.read_text(encoding="utf-8"))
            self.assertEqual((total, chunks), (2505, 4))
            self.assertEqual(len(manifest["items"]), 160)
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
            self.assertIn("async function fetchCatalogChunks(payload, onChunk = null)", patched)
            self.assertIn("Detach archive hydration from initial loading", patched)
            self.assertIn("Background refresh keeps the old complete feed visible", patched)
            self.assertIn("Math.min(saveData || mobile ? 1 : 3, chunks.length)", patched)
            self.assertIn('cache: "force-cache"', patched)
            self.assertIn("staged.length >= (mobile ? 1600 : 2400)", patched)
            self.assertIn("catalogPaintPause(mobile ? 1200 : 250)", patched)
            self.assertIn("void hydrateCatalogArchive(payload)", patched)
            self.assertIn('fetchWithTimeout(url, { cache: "no-cache" }, 12000)', patched)
            self.assertIn("for (let attempt = 1; attempt <= 3; attempt++)", patched)

    def test_generated_js_version_matches_deployed_bytes_and_preload_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            docs = Path(directory)
            site = docs / "site.js"
            index = docs / "index.html"
            site.write_text("console.log('generated loader');", encoding="utf-8")
            index.write_text(
                '<link rel="preload" href="catalog.json?v=old" as="fetch" crossorigin />\n'
                '<script src="site.js?v=stale" defer></script>',
                encoding="utf-8",
            )
            with patch.object(o, "SITE_JS", site), patch.object(o, "INDEX", index):
                version = o.sync_generated_asset_version()
            expected = hashlib.sha256(site.read_bytes()).hexdigest()[:12]
            self.assertEqual(version, expected)
            html = index.read_text(encoding="utf-8")
            self.assertIn(f'site.js?v={expected}', html)
            self.assertNotIn('rel="preload"', html)


if __name__ == "__main__":
    unittest.main()
