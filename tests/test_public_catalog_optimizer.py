import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import optimize_public_catalog as o
import embed_catalog_bootstrap as b


class PublicCatalogOptimizerTests(unittest.TestCase):
    def test_split_catalog_twice_preserves_all_rows_and_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            docs = Path(directory)
            catalog = docs / "catalog.json"
            rows = [{"code": str(i), "title": "作品"} for i in range(905)]
            catalog.write_text(json.dumps({"version": "abc", "items": rows}), encoding="utf-8")
            with patch.object(o, "DOCS", docs), patch.object(o, "CATALOG", catalog):
                first = o.split_catalog()
                before = {p.name: p.read_bytes() for p in docs.iterdir()}
                self.assertEqual(o.split_catalog(), first)
            self.assertEqual({p.name: p.read_bytes() for p in docs.iterdir()}, before)
            manifest = json.loads(catalog.read_text(encoding="utf-8"))
            recovered = list(manifest["items"])
            for entry in manifest["chunks"]:
                recovered.extend(json.loads((docs / entry["url"]).read_bytes())["items"])
            self.assertEqual(recovered, rows)

    def test_split_catalog_invalid_archive_leaves_files_unchanged(self):
        for failure in ("missing", "unreadable", "chunk_count", "total_count", "items"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                docs = Path(directory)
                catalog = docs / "catalog.json"
                entry = {"url": "catalog-chunk-001.json", "count": 1}
                manifest = {"items": [{"code": "1"}], "progressive": True,
                            "item_count": 2, "chunks": [entry]}
                chunk = docs / entry["url"]
                if failure != "missing":
                    chunk.write_text("invalid JSON" if failure == "unreadable" else
                                     json.dumps({"items": None if failure == "items" else [{"code": "2"}]}),
                                     encoding="utf-8")
                if failure == "chunk_count":
                    entry["count"] = 2
                if failure == "total_count":
                    manifest["item_count"] = 3
                catalog.write_text(json.dumps(manifest), encoding="utf-8")
                (docs / "catalog-chunk-002.json").write_text('{"items":[]}', encoding="utf-8")
                before = {p.name: p.read_bytes() for p in docs.iterdir()}
                with patch.object(o, "DOCS", docs), patch.object(o, "CATALOG", catalog):
                    with self.assertRaises((RuntimeError, OSError, ValueError)):
                        o.split_catalog()
                self.assertEqual({p.name: p.read_bytes() for p in docs.iterdir()}, before)

    def test_split_catalog_plain_input_matches_legacy_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            docs = Path(directory)
            catalog = docs / "catalog.json"
            rows = [{"code": str(i), "title": "作品"} for i in range(905)]
            payload = {"version": "abc", "items": rows}
            catalog.write_text(json.dumps(payload), encoding="utf-8")
            expected = {}
            chunks = []
            for offset in range(0, len(rows) - 96, 400):
                part = rows[96 + offset:96 + offset + 400]
                name = f"catalog-chunk-{offset // 400 + 1:03d}.json"
                expected[name] = {"items": part}
                chunks.append({"url": name, "count": len(part)})
            expected["catalog.json"] = dict(payload, items=rows[:96], item_count=len(rows),
                                            chunks=chunks, progressive=True)
            with patch.object(o, "DOCS", docs), patch.object(o, "CATALOG", catalog):
                o.split_catalog()
            self.assertEqual({p.name: p.read_bytes() for p in docs.iterdir()}, {
                name: json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                for name, data in expected.items()})

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
            self.assertIn('fetchWithTimeout(url, { cache: "no-cache" })', patched)
            self.assertIn("for (let attempt = 1; attempt <= 3; attempt++)", patched)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_catalog_timeout_covers_body_and_releases_loading_flags(self):
        source = (Path(__file__).resolve().parents[1] / "web" / "site.js").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "site.js"
            target.write_text(source, encoding="utf-8")
            with patch.object(o, "SITE_JS", target):
                o.patch_site_js()
            generated = target.read_text(encoding="utf-8")
            subprocess.run(["node", "--check", str(target)], check=True, capture_output=True)
            for name, script in (("web", source), ("generated", generated)):
                with self.subTest(loader=name):
                    functions = script[script.index("  const CATALOG_TIMEOUT_MS"):script.index("  restoreURL();", script.index("  const CATALOG_TIMEOUT_MS"))]
                    harness = r'''
const assert = require("node:assert/strict");
let ready = true, catalogLoading = false, checkingUpdates = false;
let lastCatalogCheck = 0, catalogVersion = "old", failedLoad = false;
const document = {hidden: false, baseURI: "https://example.test/",
  body: {dataset: {catalog: "catalog.json", updates: "updates.json"}}};
const navigator = {onLine: true};
const catalogRefreshBusy = () => false;
const renderUpdateStatus = () => {};
const nativeSetTimeout = setTimeout, nativeClearTimeout = clearTimeout;
const timers = new Set();
global.setTimeout = (fn) => {const id = nativeSetTimeout(fn, 5); timers.add(id); return id;};
global.clearTimeout = (id) => {timers.delete(id); nativeClearTimeout(id);};
let bodyStarted = 0, aborted = 0;
global.fetch = async (url, {signal}) => ({ok: true, json: () => {
  bodyStarted++;
  assert.equal(timers.size, 1, "deadline must remain active during body reception");
  return new Promise((resolve, reject) => signal.addEventListener("abort", () => {
    aborted++; reject(new Error("AbortError"));
  }));
}});
'''
                    checks = r'''
(async () => {
  await checkForCatalogUpdate();
  assert.equal(checkingUpdates, false);
  await loadCatalog(true);
  assert.equal(catalogLoading, false);
  assert.equal(bodyStarted, 2);
  assert.equal(aborted, 2);
  assert.equal(timers.size, 0);
  global.fetch = async () => ({ok: true, json: async () => ({items: []})});
  assert.deepEqual(await fetchWithTimeout("ok"), {items: []});
  assert.equal(timers.size, 0);
  global.fetch = async () => ({ok: true, json: async () => {throw new Error("invalid JSON");}});
  await assert.rejects(fetchWithTimeout("bad"), /invalid JSON/);
  assert.equal(timers.size, 0);
})().catch(err => {console.error(err); process.exitCode = 1;});
'''
                    result = subprocess.run(["node", "-e", harness + functions + checks],
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)

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

    def test_optimize_then_embed_versions_final_js_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            docs = Path(directory)
            site = docs / "site.js"
            index = docs / "index.html"
            catalog = docs / "catalog.json"
            site.write_bytes((Path(__file__).resolve().parents[1] / "web" / "site.js").read_bytes())
            index.write_text('<body><script src="site.js?v=stale" defer></script></body>',
                             encoding="utf-8")
            catalog.write_text(json.dumps({"version": "abc", "items": [{"code": "123"}]}),
                               encoding="utf-8")
            with patch.object(o, "DOCS", docs), patch.object(o, "SITE_JS", site), \
                 patch.object(o, "INDEX", index), patch.object(o, "CATALOG", catalog), \
                 patch.object(b, "SITE_JS", site), patch.object(b, "INDEX", index), \
                 patch.object(b, "CATALOG", catalog):
                self.assertEqual(o.main(), 0)
                optimized = site.read_bytes()
                self.assertEqual(b.main(), 0)
                final_js = site.read_bytes()
                self.assertNotEqual(final_js, optimized)
                expected = hashlib.sha256(final_js).hexdigest()[:12]
                html = index.read_bytes()
                self.assertIn(f'site.js?v={expected}"'.encode(), html)
                with patch.object(b, "sync_generated_asset_version") as sync:
                    self.assertEqual(b.main(), 0)
                    sync.assert_not_called()
                self.assertEqual(site.read_bytes(), final_js)
                self.assertEqual(index.read_bytes(), html)


if __name__ == "__main__":
    unittest.main()
