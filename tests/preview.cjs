/* Real inline playback against a local media server without CORS headers.
 * The fixture is a generated, silent two-second blue frame, not provider media.
 */
const { chromium, webkit } = require("playwright");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const root = path.resolve(process.env.FIXTURE_DIR || "test-results/fixture");
const clip = Buffer.from(fs.readFileSync(path.join(__dirname, "fixtures/preview.mp4.base64"), "utf8"), "base64");
const listen = (server) => new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const origin = (server) => `http://127.0.0.1:${server.address().port}`;

(async () => {
  let mode = "play", mediaRequests = 0;
  const requestModes = [];
  const media = http.createServer((req, res) => {
    mediaRequests++;
    requestModes.push(req.headers["sec-fetch-mode"] || "unknown");
    if (mode === "hang") return;
    if (mode === "missing") { res.writeHead(404); return res.end(); }
    const range = (req.headers.range || "").match(/^bytes=(\d+)-(\d*)$/);
    const start = range ? Number(range[1]) : 0;
    const end = range && range[2] ? Math.min(Number(range[2]), clip.length - 1) : clip.length - 1;
    res.writeHead(range ? 206 : 200, {
      "Content-Type": "video/mp4", "Accept-Ranges": "bytes", "Cache-Control": "no-store",
      "Content-Length": end - start + 1,
      ...(range ? { "Content-Range": `bytes ${start}-${end}/${clip.length}` } : {}),
    });
    res.end(clip.subarray(start, end + 1));
  });
  await listen(media);
  const app = http.createServer((req, res) => {
    const name = new URL(req.url, "http://local.test").pathname.slice(1) || "index.html";
    if (name === "poster.svg") {
      res.writeHead(200, { "Content-Type": "image/svg+xml" });
      return res.end('<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90"><rect width="160" height="90" fill="#315c78"/></svg>');
    }
    if (name === "catalog.json") {
      const catalog = JSON.parse(fs.readFileSync(path.join(root, name)));
      for (const item of catalog.items) {
        item.preview = `${origin(media)}/sample.mp4`;
        item.thumb = `${origin(app)}/poster.svg`;
      }
      res.writeHead(200, { "Content-Type": "application/json" });
      return res.end(JSON.stringify(catalog));
    }
    const file = path.resolve(root, name);
    if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) {
      res.writeHead(404); return res.end();
    }
    const types = { ".js": "application/javascript", ".css": "text/css", ".json": "application/json" };
    res.writeHead(200, { "Content-Type": types[path.extname(name)] || "text/html" });
    res.end(fs.readFileSync(file));
  });
  await listen(app);
  let browser;
  const launch = () => (process.env.PREVIEW_BROWSER === "webkit" ? webkit : chromium).launch({
    headless: true,
    ...(process.env.CHROMIUM_PATH && process.env.PREVIEW_BROWSER !== "webkit"
      ? { executablePath: process.env.CHROMIUM_PATH, args: ["--no-sandbox", "--single-process", "--no-zygote"] }
      : {}),
  });
  try {
    for (const mobile of [false, true]) {
      browser = await launch();
      const context = await browser.newContext({
        viewport: mobile ? { width: 390, height: 844 } : { width: 1280, height: 900 },
        isMobile: mobile, hasTouch: mobile,
      });
      const errors = [];
      const consoleErrors = [];
      await context.route("**/*", (route) => {
        const host = new URL(route.request().url()).hostname;
        return host === "127.0.0.1" ? route.continue() : route.abort();
      });
      const page = await context.newPage();
      page.setDefaultTimeout(6000);
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") consoleErrors.push(message.text()); });
      mode = "play";
      await page.goto(origin(app));
      await page.locator('#grid[aria-busy="false"] .thumb-link').first().waitFor();
      const tap = (locator) => mobile ? locator.tap() : locator.click();
      const first = page.locator("#grid .thumb-link").first();
      await tap(first);
      await page.waitForFunction(() => {
        const v = document.querySelector("#grid video");
        return v && !v.paused && v.currentTime > 0.1;
      }).catch((error) => {
        console.error(JSON.stringify({ mediaRequests, requestModes, consoleErrors }));
        throw error;
      });
      assert.equal(await page.locator("#grid video").count(), 1);
      assert.equal(await page.locator(".inline-preview-state").count(), 0);
      assert.equal(await page.locator("video").getAttribute("crossorigin"), null);
      assert.ok(await page.locator("video").evaluate((v) => v.muted && v.playsInline));
      await tap(first);
      assert.equal(await page.locator("video").count(), 0, "Second tap stops inline playback");

      if (!mobile) {
        await page.mouse.move(0, 0);
        await first.hover();
        await page.waitForFunction(() => document.querySelector("#grid video")?.currentTime > 0.1);
        await page.evaluate(() => { window.hoverVideo = document.querySelector("#grid video"); });
        await tap(first);
        assert.ok(await page.evaluate(() => window.hoverVideo === document.querySelector("#grid video")), "Tap retains an already loaded hover player");
        await page.mouse.move(0, 0);
        assert.equal(await page.locator("video").count(), 1, "Tapped preview survives pointer exit");
        await tap(first);
      }

      const ranking = page.locator("#rankingRail .thumb-link").first();
      await tap(ranking);
      await page.waitForFunction(() => document.querySelector("#rankingRail video")?.currentTime > 0.1);
      await tap(first);
      await page.waitForFunction(() => document.querySelector("#grid video")?.currentTime > 0.1);
      assert.equal(await page.locator("video").count(), 1, "Only the selected card plays");
      await tap(first);

      // A failed source keeps the thumbnail tappable for a fresh retry.
      mode = "missing";
      await tap(first);
      await page.locator("#grid .inline-preview-state.error").first().waitFor();
      mode = "play";
      await tap(first);
      await page.waitForFunction(() => document.querySelector("#grid video")?.currentTime > 0.1);
      await tap(first);

      // A connection that never returns must not leave an endless loading card.
      await page.clock.install();
      mode = "hang";
      await tap(first);
      assert.equal(await page.locator("#grid video").count(), 1);
      await page.clock.fastForward(15001);
      await page.locator("#grid .inline-preview-state.error").first().waitFor();
      assert.equal(await page.locator("video").count(), 0);
      mode = "play";
      await tap(first);
      await page.waitForFunction(() => document.querySelector("#grid video")?.currentTime > 0.1);
      await tap(first);
      assert.deepEqual(errors, []);
      console.log(JSON.stringify({ browser: process.env.PREVIEW_BROWSER || "chromium", mobile, playback: true, retry: true, mediaRequests }));
      await context.close();
      await browser.close();
      browser = null;
    }
    assert.ok(requestModes.includes("no-cors") || process.env.PREVIEW_BROWSER === "webkit");
  } finally {
    if (browser) await browser.close();
    app.closeAllConnections(); media.closeAllConnections();
    await Promise.all([new Promise((resolve) => app.close(resolve)), new Promise((resolve) => media.close(resolve))]);
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
