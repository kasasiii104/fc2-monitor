import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "node is required")
class SiteRankTests(unittest.TestCase):
    def test_rank_normalization_and_period_fallback(self):
        source = (Path(__file__).resolve().parents[1] / "web" / "site.js").read_text(
            encoding="utf-8"
        )
        start = source.index("  const rankNumber =")
        end = source.index("  function hasQuery", start)
        script = source[start:end] + r'''
const assert = require("node:assert/strict");
const state = { rank: "day" };
for (const value of [1, 42, Number.MAX_SAFE_INTEGER, "1", "0042", "9007199254740991"]) {
  assert.equal(rankNumber(value), Number(value));
  assert.equal(bestRank({missav_rank_day: value}), Number(value));
  assert.equal(rankValue({missav_rank_day: value}), Number(value));
}
for (const value of [undefined, null, true, false, [], [1], {}, 0, -1, 1.5,
    NaN, Infinity, -Infinity, 9007199254740992, "9007199254740992",
    "", " ", " 1", "1 ", "1\n", "0", "-1", "1.5", "1e2", "0x10",
    "<img src=x onerror=alert(1)>", "１"]) {
  assert.equal(rankNumber(value), 0);
  assert.equal(bestRank({missav_rank_day: value}), 0);
  assert.equal(rankValue({missav_rank_day: value}), 0);
  assert.equal(bestRank({missav_rank_day: value, missav_rank_week: "7"}), 7);
}
assert.equal(bestRank({}), 0);
assert.equal(bestRank({missav_rank_day: 9, missav_rank_week: 2}), 9);
assert.equal(bestRank({missav_rank_month: "3", missav_rank_total: 1}), 3);
assert.equal(bestRank({missav_rank_total: "4"}), 4);
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])

    def test_menu_preview_prefers_connected_origin_card(self):
        source = (Path(__file__).resolve().parents[1] / "web" / "site.js").read_text(
            encoding="utf-8"
        )
        start = source.index('    if (action === "preview") {')
        end = source.index('    closeDialog($("menuDialog"));', source.index('      return;', start))
        handler = source[start:end]
        script = r'''const assert = require("node:assert/strict");
const code = "123", action = "preview";
const originShell = {}, fallbackShell = {};
let menuCard, selected, searches = 0;
const $ = () => ({});
const CSS = { escape: value => value };
const document = { querySelector: () => { searches++; return fallbackShell; } };
const closeDialog = () => { menuCard = null; };
const toggleInlinePreview = (value, shell) => { assert.equal(value, code); selected = shell; };
function preview() {
''' + handler + r'''
}
menuCard = { isConnected: true, querySelector: () => originShell };
preview();
assert.equal(selected, originShell);
assert.equal(searches, 0);
assert.equal(menuCard, null);
menuCard = { isConnected: false, querySelector: () => { throw Error("detached origin"); } };
preview();
assert.equal(selected, fallbackShell);
assert.equal(searches, 1);
preview();
assert.equal(selected, fallbackShell);
assert.equal(searches, 2);
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
