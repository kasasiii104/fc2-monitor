import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "node is required")
class MissavOnlyTests(unittest.TestCase):
    def test_direct_missav_url_requires_https_without_credentials(self):
        source = (Path(__file__).resolve().parents[1] / "web" / "missav-only.js").read_text(
            encoding="utf-8"
        )
        start = source.index("  const MISSAV_HOSTS")
        end = source.index("  function itemCode", start)
        script = source[start:end] + r'''
const assert = require("node:assert/strict");
const location = { href: "https://example.com/" };
const valid = "https://missav.ws/ja/fc2-ppv-1234567";
assert.equal(directMissavURL(valid, "1234567"), valid);
for (const value of [
  "javascript://missav.ws/ja/fc2-ppv-1234567",
  "http://missav.ws/ja/fc2-ppv-1234567",
  "data://missav.ws/ja/fc2-ppv-1234567",
  "https://user:pw@missav.ws/ja/fc2-ppv-1234567",
  "https://user@missav.ws/ja/fc2-ppv-1234567",
  "https://:pw@missav.ws/ja/fc2-ppv-1234567",
  "https://example.com/ja/fc2-ppv-1234567",
  "https://missav.ws/ja/other-1234567",
  "https://missav.ws/ja/fc2-ppv-7654321",
]) {
  assert.equal(directMissavURL(value, "1234567"), "", value);
}
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
