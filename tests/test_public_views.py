import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import compact_public_catalog as c
import fc2_monitor as m
import optimize_public_catalog as o
import embed_catalog_bootstrap as b


class PublicViewsTests(unittest.TestCase):
    def test_unknown_views_are_omitted_and_conversion_is_shared(self):
        for value in [None, True, False, "unknown", "123", "1,234", 1.5, 0, 1234]:
            with self.subTest(value=value):
                raw = {"code": "FC2-PPV-1234567", "views": value}
                expected = value if type(value) is int else None
                self.assertEqual(m.public_item(raw)["views"], expected)
                compact, _ = c.compact_item(c.public_from_saved(raw))
                direct, _ = c.compact_item(raw)
                built = m.catalog_payload([raw], "now", {"status": "test"})["items"][0]
                for row in [compact, direct, built]:
                    if expected is None:
                        self.assertNotIn("views", row)
                    else:
                        self.assertEqual(row["views"], expected)
        self.assertNotIn("views", c.public_from_saved({"code": "1234567"}))

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_site_views_display_sort_filter_and_rank(self):
        source = (Path(__file__).resolve().parents[1] / "web/site.js").read_text()
        label = source[source.index("  function viewsLabel"):source.index("  function newItem")]
        select = source[source.index("  function selectList"):source.index("  function externalLink")]
        script = label + select + r'''
const assert = require("node:assert/strict");
const number = String, normalize = x => x || "", hasQuery = () => true;
const state = {sort: "views"};
const items = [{code: "missing", _ts: 9}, {code: "zero", views: 0, _ts: 1},
  {code: "null", views: null, _ts: 8}, {code: "known", views: 100000, _ts: 2},
  {code: "undefined", views: undefined, _ts: 7}];
for (const v of [undefined, null, true, false, NaN]) assert.equal(viewsLabel(v), "");
assert.equal(viewsLabel(0), "0回視聴");
assert.deepEqual(selectList().map(x => x.code), ["known", "zero", "missing", "null", "undefined"]);
state.vmin = "1";
assert.deepEqual(selectList().map(x => x.code), ["known"]);
state.vmin = ""; state.chip = "views10";
assert.deepEqual(selectList().map(x => x.code), ["known"]);
state.chip = ""; state.page = "popular"; state.rank = "views";
assert.deepEqual(selectList().map(x => x.code), ["known", "zero"]);
'''
        result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])

    def test_compact_optimize_embed_twice_preserves_unknown_and_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            site, index, catalog = (root / name for name in ["site.js", "index.html", "catalog.json"])
            site.write_bytes((Path(__file__).resolve().parents[1] / "web/site.js").read_bytes())
            index.write_text('<body><script src="site.js?v=old" defer></script></body>')
            raw = [{"code": "1234567"}, {"code": "1234568", "views": None}, {"code": "1234569", "views": 0}]
            rows = [c.compact_item(c.public_from_saved(row))[0] for row in raw]
            catalog.write_text(json.dumps({"version": "test", "items": rows}))
            with patch.multiple(o, DOCS=root, SITE_JS=site, INDEX=index, CATALOG=catalog), patch.multiple(b, SITE_JS=site, INDEX=index, CATALOG=catalog):
                self.assertEqual(o.main(), 0)
                self.assertEqual(b.main(), 0)
                first = [p.read_bytes() for p in [site, index, catalog]]
                digest = hashlib.sha256(site.read_bytes()).hexdigest()[:12]
                self.assertIn(f'site.js?v={digest}"', index.read_text())
                payload = json.loads(catalog.read_text())
                payload["items"] = [c.compact_item(row)[0] for row in payload["items"]]
                catalog.write_text(json.dumps(payload))
                self.assertEqual(o.main(), 0)
                self.assertEqual(b.main(), 0)
                self.assertEqual(first, [p.read_bytes() for p in [site, index, catalog]])
            output = json.loads(catalog.read_text())["items"]
            self.assertNotIn("views", output[0])
            self.assertNotIn("views", output[1])
            self.assertEqual(output[2]["views"], 0)
