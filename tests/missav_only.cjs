/* Neutral DOM fixtures only. No catalog, media, or destination requests. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium, webkit } = require("playwright");

// A renderer stuck in a microtask loop cannot service Playwright commands.
// Bound the whole test from Node so this regression fails instead of hanging CI.
const watchdog = setTimeout(() => {
  console.error("Navigation guard starved the browser event loop");
  process.exit(1);
}, 25000);
watchdog.unref();

const fixture = `<!doctype html><html><body>
  <button id="responsive">Respond</button><output id="response">0</output>
  <div id="grid">
    <article class="video-card" data-code="1234567" id="mixed">
      <a class="card-title" href="https://other.example.test/">Neutral sample</a>
      <div class="source-row"><a href="https://other.example.test/">Other</a><a id="retained" href="https://allowed.example.test/ja/fc2-ppv-1234567">Allowed</a></div>
    </article>
    <article class="ranking-card" data-code="2345678" id="clean">
      <a class="card-title" href="https://allowed.example.test/ja/fc2-ppv-2345678">Neutral ranking</a>
      <div class="source-row"><a href="https://allowed.example.test/ja/fc2-ppv-2345678">Allowed</a></div>
    </article>
    <article class="video-card" data-code="3456789" id="unavailable">
      <a class="card-title" href="https://other.example.test/">Neutral unavailable sample</a>
      <div class="source-row"><a href="https://other.example.test/">Other</a></div>
    </article>
  </div>
  <div id="menuDialog" data-code="1234567">
    <div class="menu-subheading">Available</div>
    <a href="https://allowed.example.test/ja/fc2-ppv-1234567">Allowed</a>
    <div class="menu-subheading">Other</div>
    <a href="https://other.example.test/">Other</a>
  </div>
  <script>
    window.retainedNode = document.getElementById("retained");
    window.retainedClicks = 0;
    retainedNode.addEventListener("click", event => { event.preventDefault(); retainedClicks++; });
    document.getElementById("responsive").addEventListener("click", () => {
      document.getElementById("response").textContent = "1";
    });
    window.guardTestMutations = 0;
    new MutationObserver(records => { guardTestMutations += records.length; })
      .observe(document.body, { childList: true, subtree: true, attributes: true });
  </script>
</body></html>`;

(async () => {
  const engine = process.env.GUARD_BROWSER === "webkit" ? webkit : chromium;
  const browser = await engine.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    page.setDefaultTimeout(5000);
    const errors = [];
    let requests = 0;
    page.on("pageerror", error => errors.push(String(error)));
    await page.route("**/*", route => { requests++; return route.abort(); });
    await page.setContent(fixture);
    const source = fs.readFileSync(path.join(__dirname, "../web/missav-only.js"), "utf8");
    const neutralSource = source.replace(/const MISSAV_HOSTS = new Set\([^;]+;/,
      'const MISSAV_HOSTS = new Set(["allowed.example.test"]);');
    assert.notEqual(neutralSource, source, "fixture must replace provider hosts");
    await page.addScriptTag({ content: neutralSource });

    async function settle() {
      await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 60)));
    }
    await settle();
    assert.equal(await page.locator("#mixed .source-row a").count(), 1);
    assert.equal(await page.locator("#unavailable a").count(), 0);
    assert.equal(await page.locator("#menuDialog a").count(), 1);
    assert.equal(await page.locator("#menuDialog .menu-subheading").count(), 1);
    assert.equal(await page.locator("#mixed .card-title").getAttribute("target"), "_blank");
    assert.equal(await page.evaluate(() => document.getElementById("retained") === window.retainedNode), true);
    await page.locator("#retained").click();
    assert.equal(await page.evaluate(() => window.retainedClicks), 1);

    // External changes still get sanitized, including cards appended in batches.
    await page.evaluate(() => {
      for (let index = 0; index < 20; index++) {
        const card = document.getElementById("mixed").cloneNode(true);
        card.removeAttribute("id");
        card.querySelectorAll("[id]").forEach(node => node.removeAttribute("id"));
        const unwanted = document.createElement("a");
        unwanted.href = "https://other.example.test/";
        unwanted.textContent = "Other";
        card.querySelector(".source-row").append(unwanted);
        document.getElementById("grid").append(card);
      }
    });
    await settle();
    assert.equal(await page.locator('.source-row a[href="https://other.example.test/"]').count(), 0);
    await page.locator("#responsive").click();
    assert.equal(await page.locator("#response").textContent(), "1");
    await settle();

    // Once clean, the guard must not keep writing nodes or attributes.
    await page.evaluate(() => { window.guardTestMutations = 0; });
    await settle();
    assert.equal(await page.evaluate(() => window.guardTestMutations), 0);
    assert.deepEqual(errors, []);
    assert.equal(requests, 0, "fixtures must not contact external sites");
    console.log(JSON.stringify({ browser: process.env.GUARD_BROWSER || "chromium", responsive: true, idleMutations: 0, externalRequests: requests }));
  } finally {
    await browser.close();
  }
})().then(() => clearTimeout(watchdog)).catch(error => {
  console.error(error);
  process.exit(1);
});
