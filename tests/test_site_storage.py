import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "node is required")
class SiteStorageTests(unittest.TestCase):
    def test_failed_writes_preserve_memory_changes(self):
        source = (Path(__file__).resolve().parents[1] / "web/site.js").read_text(
            encoding="utf-8"
        )
        helpers = source[source.index("  function readStore("):source.index("  const saved =")]
        updates = source[source.index("  function applySetChange("):source.index("  function toggleSet(")]
        save = source[source.index("  function saveProgress("):source.index("  function clearInlineState(")]
        script = helpers + updates + save + r'''
const assert = require("node:assert/strict");
const localStorage = {
  getItem: () => null,
  setItem: () => { throw new Error("storage unavailable"); },
};
let warnings = 0;
const toast = () => { warnings++; };
const watched = new Set(), watchTimes = {}, progress = {};
let progressTimer = 0;
const setTimeout = () => 1;
assert.equal(writeStore("test", []), false);
for (const key of ["fc2favs", "fc2watchlater", "fc2watched"]) {
  const values = new Set();
  updateStoredSet(values, key, "A");
  updateStoredSet(values, key, "B");
  assert.deepEqual([...values], ["A", "B"]);
  updateStoredSet(values, key, "A", true);
  assert.deepEqual([...values], ["B"]);
}
for (const key of ["fc2watchtimes", "fc2progress"]) {
  const values = {};
  updateStoredObject(values, key, {A: 1});
  updateStoredObject(values, key, {B: 2});
  assert.deepEqual(values, {A: 1, B: 2});
  updateStoredObject(values, key, {A: undefined});
  assert.deepEqual(values, {B: 2});
}
saveProgress({code: "A"}, {duration: 100, currentTime: 30});
flushProgress();
saveProgress({code: "B"}, {duration: 100, currentTime: 40});
flushProgress();
assert.deepEqual(progress, {A: 0.3, B: 0.4});
flushProgress();
assert.deepEqual(progress, {A: 0.3, B: 0.4});
assert.equal(warnings, 1);
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])

    def test_two_tabs_merge_work_changes(self):
        source = (Path(__file__).resolve().parents[1] / "web/site.js").read_text(
            encoding="utf-8"
        )
        helpers = source[source.index("  function readStore("):source.index("  const saved =")]
        updates = source[source.index("  function applySetChange("):source.index("  function toggleSet(")]
        save = source[source.index("  function saveProgress("):source.index("  function clearInlineState(")]
        script = helpers + updates + save + r'''
const assert = require("node:assert/strict");
const data = new Map();
const localStorage = {
  getItem: key => data.get(key) ?? null,
  setItem: (key, value) => data.set(key, value),
};
const toast = () => {};
const watched = new Set(), watchTimes = {}, progress = {};
let progressTimer = 0;
const setTimeout = () => 1;
for (const key of ["fc2favs", "fc2watchlater", "fc2watched"]) {
  const tabA = new Set(), tabB = new Set();
  updateStoredSet(tabA, key, "A");
  updateStoredSet(tabB, key, "B");
  assert.deepEqual([...tabB], ["A", "B"]);
  assert.deepEqual(readArray(key), ["A", "B"]);
  updateStoredSet(tabA, key, "A", true);
  assert.deepEqual([...tabA], ["B"]);
  assert.deepEqual(readArray(key), ["B"]);
}
for (const key of ["fc2watchtimes", "fc2progress"]) {
  const tabA = {}, tabB = {};
  updateStoredObject(tabA, key, { A: 1 });
  updateStoredObject(tabB, key, { B: 2 });
  assert.deepEqual(tabB, { A: 1, B: 2 });
  updateStoredObject(tabA, key, { A: undefined });
  assert.deepEqual(tabA, { B: 2 });
  assert.deepEqual(readObject(key), { B: 2 });
}
// Only pending works are merged at the delayed flush, even across multiple works.
Object.assign(progress, { stale: 0.1 });
saveProgress({code: "A"}, {duration: 100, currentTime: 30});
saveProgress({code: "C"}, {duration: 100, currentTime: 40});
localStorage.setItem("fc2progress", JSON.stringify({B: 0.7, stale: 0.9}));
flushProgress();
assert.deepEqual(progress, {B: 0.7, stale: 0.9, A: 0.3, C: 0.4});
assert.deepEqual(readObject("fc2progress"), progress);
localStorage.setItem("fc2progress", JSON.stringify({B: 0.8}));
flushProgress();
assert.deepEqual(progress, {B: 0.8});
for (const raw of ["broken", "null", "42", "{}", '[1,"B"]']) {
  localStorage.setItem("fc2favs", raw);
  const tab = new Set(["stale"]);
  updateStoredSet(tab, "fc2favs", "A");
  assert.deepEqual([...tab], raw === '[1,"B"]' ? ["B", "A"] : ["A"]);
}
for (const raw of ["broken", "null", "42", "[]"]) {
  localStorage.setItem("fc2watchtimes", raw);
  const tab = {stale: 1};
  updateStoredObject(tab, "fc2watchtimes", {A: 2});
  assert.deepEqual(tab, {A: 2});
}
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
