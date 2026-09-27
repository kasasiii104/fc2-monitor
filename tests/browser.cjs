/* Run after tests/build_fixture.py. Never contacts catalog or media providers. */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");
const assert = require("node:assert/strict");
const root = path.resolve(process.env.FIXTURE_DIR || "test-results/fixture");
const output = path.resolve(process.env.TEST_OUTPUT || "test-results");
fs.mkdirSync(output, { recursive: true });
const palette = [
  ["#314a51", "#b4c5bd"],
  ["#576258", "#d8d1b4"],
  ["#79635b", "#dbc3b3"],
  ["#3b465d", "#bfbdce"],
  ["#5e6552", "#d1d2b5"],
  ["#695548", "#c4b09d"],
];
function poster(id) {
  const [a, b] = palette[Number(id) % 6];
  return `<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360"><defs><linearGradient id="g" x2="1" y2="1"><stop stop-color="${a}"/><stop offset="1" stop-color="${b}"/></linearGradient></defs><rect width="640" height="360" fill="url(#g)"/><circle cx="450" cy="100" r="95" fill="${b}" opacity=".55"/><path d="M0 300 170 150 330 285 485 200 640 325v35H0" fill="${a}" opacity=".85"/><path d="M0 350 200 285 400 330 640 240v120H0" fill="${a}" opacity=".5"/><text x="32" y="47" font-family="Arial" font-size="14" letter-spacing="4" fill="white" opacity=".8">SAMPLE COLLECTION / 0${Number(id) + 1}</text><text x="32" y="310" font-family="Arial" font-size="37" font-weight="bold" fill="white">${["EVERYDAY", "SLOW DAYS", "AFTERNOON", "WEEKEND", "JOURNEY", "MOMENTS"][Number(id) % 6]}</text></svg>`;
}
async function setup(browser, viewport, corrupt = false, fail = false, clock = false) {
  const context = await browser.newContext({
    viewport,
    isMobile: viewport.width < 768,
    hasTouch: viewport.width < 768,
  });
  let media = 0,
    errors = [],
    blocked = 0,
    catalogRequests = 0,
    manifestRequests = 0,
    publishedCatalog = null;
  await context.route("**/*", async (route) => {
    const u = new URL(route.request().url());
    if (u.hostname === "fixture.test") {
      const name = u.pathname === "/" ? "index.html" : u.pathname.slice(1);
      if (name === "catalog.json") catalogRequests++;
      if (name === "update.json") manifestRequests++;
      if (name === "catalog.json" && fail) {
        fail = false;
        return route.fulfill({ status: 503, body: "temporary" });
      }
      if (publishedCatalog && ["catalog.json", "update.json"].includes(name))
        return route.fulfill({ status: 200, contentType: "application/json",
          body: JSON.stringify(name === "catalog.json" ? publishedCatalog : { version: publishedCatalog.version }) });
      const file = path.join(root, name);
      if (!file.startsWith(root + path.sep) || !fs.existsSync(file))
        return route.fulfill({ status: 404, body: "" });
      return route.fulfill({
        status: 200,
        contentType: name.endsWith(".json")
          ? "application/json"
          : name.endsWith(".js")
            ? "application/javascript"
            : name.endsWith(".css")
              ? "text/css"
              : "text/html",
        body: fs.readFileSync(file),
      });
    }
    if (u.hostname === "images.example.test")
      return route.fulfill({
        status: 200,
        contentType: "image/svg+xml",
        body: poster(u.pathname.match(/\d+/)[0]),
      });
    if (u.hostname === "media.example.test") {
      media++;
      return route.fulfill({ status: 404, body: "" });
    }
    if (u.hostname === "example.test")
      return route.fulfill({
        status: 200,
        contentType: "text/html",
        body: "<h1>Sample destination</h1>",
      });
    blocked++;
    return route.abort();
  });
  if (corrupt)
    await context.addInitScript(() => {
      if (sessionStorage.getItem("testSeeded")) return;
      sessionStorage.setItem("testSeeded", "1");
      localStorage.setItem("fc2favs", "{bad JSON");
      localStorage.setItem("fc2watchtimes", "[]");
      localStorage.setItem(
        "fc2searchhist",
        JSON.stringify(['<img src=x onerror="window.injected=true">']),
      );
    });
  const page = await context.newPage();
  if (clock) await page.clock.install();
  page.setDefaultTimeout(8000);
  page.on("pageerror", (e) => errors.push(e.message));
  await page.addInitScript(() => {
    window.longTasks = [];
    new PerformanceObserver((l) =>
      window.longTasks.push(...l.getEntries().map((x) => x.duration)),
    ).observe({ type: "longtask", buffered: true });
  });
  await page.goto("http://fixture.test/");
  return {
    context,
    page,
    errors,
    publishCatalog(value) { publishedCatalog = value; },
    failNextCatalog() { fail = true; },
    get catalogRequests() { return catalogRequests; },
    get manifestRequests() { return manifestRequests; },
    get media() {
      return media;
    },
    get blocked() {
      return blocked;
    },
  };
}
async function waitFeed(page) {
  await page.locator('#grid[aria-busy="false"] .video-card').first().waitFor();
}
async function nav(page, name) {
  const selector = (await page.locator(".bottom-nav").isVisible())
    ? ".bottom-nav"
    : ".sidebar";
  await page.locator(`${selector} [data-page="${name}"]`).click();
}
(async () => {
  const launch = () => chromium.launch({
    headless: true,
    ...(process.env.CHROMIUM_PATH
      ? { executablePath: process.env.CHROMIUM_PATH }
      : {}),
    args: [
      "--no-sandbox",
      ...(process.env.CHROMIUM_PATH ? ["--single-process", "--no-zygote"] : []),
    ],
  });
  let browser = await launch();
  const report = {};
  try {
    const desktop = await setup(browser, { width: 1440, height: 980 }, true);
    const p = desktop.page;
    await waitFeed(p);
    await p.mouse.move(0, 0);
    assert.equal(await p.locator(".brand-mark").count(), 0, "Play logo is removed");
    assert.ok(await p.locator("#homeRankings").isVisible(), "MissAV ranking rail is visible on home");
    assert.equal(await p.locator("#rankingRail .ranking-card").count(), 10, "MissAV day ranking has 10 cards");
    await p.locator('[data-home-rank="week"]').click();
    assert.ok((await p.locator("#rankingRail").getAttribute("aria-label")).includes("週間"), "Ranking period switches");
    await p.locator("#rankingAll").click();
    assert.equal(await p.locator("#listTitle").innerText(), "MissAV 週間ランキング", "Ranking opens as a full list");
    await nav(p, "home");
    assert.match(await p.locator("#resultCount").innerText(), /7,064/);
    assert.ok((await p.locator(".video-card").count()) < 60);
    assert.equal(desktop.media, 0, "No videos downloaded before interaction");
    assert.equal(
      await p.evaluate(() => window.injected),
      undefined,
      "Titles are escaped",
    );
    await p.screenshot({ path: path.join(output, "desktop.png") });
    report.desktopInitial = {
      cards: await p.locator(".video-card").count(),
      htmlBytes: fs.statSync(path.join(root, "index.html")).size,
      longTasks: await p.evaluate(() => window.longTasks),
    };
    await p.locator("#q").fill("８００１２３４");
    await p.locator("#searchForm").evaluate((el) => el.requestSubmit());
    assert.equal(await p.locator(".video-card").count(), 1);
    assert.equal(
      await p.locator(".video-card").getAttribute("data-code"),
      "FC2-PPV-8001234",
    );
    await p.locator("[data-more]").first().click();
    await p.locator('[data-action="save"]').click();
    await nav(p, "popular");
    assert.equal(
      await p.locator(".video-card").first().getAttribute("data-code"),
      "FC2-PPV-8000049",
    );
    await nav(p, "library");
    await p.locator('#contextTabs [data-page="saved"]').click();
    assert.equal(
      await p.locator(".video-card").count(),
      1,
      "Saved library ignores previous popular filter",
    );
    await p.reload();
    await waitFeed(p);
    assert.equal(
      await p.locator(".video-card").count(),
      1,
      "Saved state persists",
    );
    const popupPromise = p.waitForEvent("popup");
    await p.locator(".card-title").first().click();
    await (await popupPromise).close();
    await nav(p, "history");
    assert.equal(await p.locator(".video-card").count(), 1);
    await nav(p, "home");
    await p.locator("#filterButton").click();
    await p.locator('[data-filter="rating"][data-value="4.5"]').click();
    await p.locator("#filterDialog [data-close]").last().click();
    assert.ok(
      Number((await p.locator("#resultCount").innerText()).replace(/\D/g, "")) <
        7064,
    );
    assert.ok(
      (await p.locator(".rating").allTextContents()).every(
        (t) => Number(t.match(/\d\.\d/)[0]) >= 4.5,
      ),
    );
    await nav(p, "library");
    assert.equal(
      await p.locator(".video-card").count(),
      1,
      "Library navigation clears hidden filters",
    );
    await nav(p, "home");
    for (let i = 0; i < 12; i++) {
      await p.evaluate(() => scrollTo(0, document.body.scrollHeight));
      await p.waitForTimeout(70);
    }
    assert.ok(
      (await p.locator(".video-card").count()) < 80,
      "DOM stays bounded after long scrolling",
    );
    report.deepScroll = {
      cards: await p.locator(".video-card").count(),
      scroll: await p.evaluate(() => scrollY),
    };
    await p.evaluate(() => scrollTo(0, 0));
    await p.waitForTimeout(100);
    assert.equal(
      await p.locator(".video-card").first().getAttribute("data-code"),
      "FC2-PPV-8000000",
    );
    await p.locator("#q").fill("存在しない検索文字列");
    await p.locator("#searchForm").evaluate((el) => el.requestSubmit());
    assert.equal(await p.locator(".video-card").count(), 0);
    assert.ok(await p.locator("#emptyState").isVisible());
    await nav(p, "home");
    assert.equal(await p.locator(".card-preview-button").count(), 0);
    assert.equal(await p.locator("#previewDialog").count(), 0);
    assert.ok(
      await p.locator(".thumb-link").first().isVisible(),
      "Thumbnail is the inline preview action",
    );
    await p.locator(".thumb-link").first().click();
    await p.waitForTimeout(100);
    assert.ok(
      await p.locator(".inline-preview-state.error").first().isVisible(),
      "Unavailable inline preview has actionable error",
    );
    const mediaBeforeRetry = desktop.media;
    await p.locator(".thumb-link").first().click();
    await p.waitForTimeout(100);
    assert.ok(desktop.media > mediaBeforeRetry, "Inline preview retry requests the media again");
    assert.equal(await p.locator("#previewPlayer").count(), 0);
    assert.deepEqual(desktop.errors, []);
    assert.equal(desktop.blocked, 0);
    await desktop.context.close();
    await browser.close();
    browser = await launch();

    const mobile = await setup(browser, { width: 390, height: 844 });
    const m = mobile.page;
    await waitFeed(m);
    assert.ok(await m.locator(".bottom-nav").isVisible());
    assert.ok(
      (await m.locator(".card-preview-button").count()) === 0,
      "Card preview button is removed on mobile",
    );
    assert.ok(
      await m.locator(".thumb-link").first().isVisible(),
      "Thumbnail is easy to tap on mobile",
    );
    assert.equal(await m.locator("#q").isVisible(), false);
    assert.ok(
      await m.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      "No mobile horizontal overflow",
    );
    await m.screenshot({ path: path.join(output, "mobile.png") });
    await m.locator(".quick-later").first().click();
    await nav(m, "library");
    await m.locator('#contextTabs [data-page="later"]').click();
    assert.equal(await m.locator(".video-card").count(), 1);
    await nav(m, "home");
    await m.locator("#mobileSearch").click();
    await m.locator("#q").fill("8000010");
    await m.locator(".search-submit").click();
    assert.equal(await m.locator(".video-card").count(), 1);
    await m.locator("#searchBack").click();
    assert.equal(await m.locator("#q").isVisible(), false);
    await m.locator("[data-more]").first().click();
    assert.ok(await m.locator("#menuDialog").isVisible());
    await m.locator("#menuDialog [data-close]").click();
    await nav(m, "home");
    await m.setViewportSize({ width: 844, height: 390 });
    await m.waitForTimeout(100);
    assert.ok(
      await m.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      "No landscape overflow",
    );
    assert.deepEqual(mobile.errors, []);
    assert.equal(mobile.blocked, 0);
    report.mobile = { passed: true };
    await mobile.context.close();
    await browser.close();
    browser = await launch();

    const retry = await setup(
      browser,
      { width: 1280, height: 800 },
      false,
      true,
    );
    await retry.page.locator("[data-retry]").waitFor();
    await retry.page.locator("[data-retry]").click();
    await waitFeed(retry.page);
    assert.deepEqual(retry.errors, []);
    await retry.context.close();
    await browser.close();
    browser = await launch();
    const refresh = await setup(browser, { width: 1280, height: 800 }, false, false, true);
    const r = refresh.page;
    await waitFeed(r);
    await r.mouse.move(0, 0);
    const initialCatalog = JSON.parse(fs.readFileSync(path.join(root, "catalog.json")));
    let response = r.waitForResponse((res) => new URL(res.url()).pathname === "/update.json");
    await r.clock.fastForward(5 * 60 * 1000);
    await (await response).finished();
    await r.clock.runFor(50);
    assert.equal(refresh.catalogRequests, 1, "Unchanged manifest does not redownload the full catalog");
    assert.equal(refresh.manifestRequests, 1);
    const nextCatalog = structuredClone(initialCatalog);
    nextCatalog.version = "test-version-two";
    nextCatalog.updated_at = "2026-09-27 20:00:00";
    nextCatalog.items[0].title = "更新済みサンプル";
    nextCatalog.items[0].missav_rank_day = 1;
    nextCatalog.items.push({ ...nextCatalog.items[0], code: "FC2-PPV-8999999", code_num: "8999999" });
    refresh.publishCatalog(nextCatalog);
    await r.clock.fastForward(5 * 60 * 1000);
    await r.waitForFunction(() => document.querySelector("[data-updated-at]").textContent === "2026-09-27 20:00:00");
    assert.match(await r.locator("#resultCount").innerText(), /7,065/);
    assert.equal(await r.locator('#grid [data-code="FC2-PPV-8000000"] .card-title').innerText(), "更新済みサンプル");
    assert.ok(await r.locator("#homeRankings").isVisible(), "Ranking remains available after automatic refresh");

    // Keep search, saved items, and a usable feed when a background download fails.
    await r.locator("#q").fill("8000000");
    await r.locator("#searchForm").evaluate((el) => el.requestSubmit());
    await r.locator("[data-more]").first().click();
    await r.locator('[data-action="save"]').click();
    await r.evaluate(() => document.activeElement.blur());
    refresh.publishCatalog({ ...nextCatalog, version: "test-version-three", updated_at: "2026-09-27 21:00:00" });
    refresh.failNextCatalog();
    response = r.waitForResponse((res) => new URL(res.url()).pathname === "/catalog.json" && res.status() === 503);
    await r.clock.fastForward(5 * 60 * 1000);
    await (await response).finished();
    await r.clock.runFor(50);
    assert.equal(await r.locator("#q").inputValue(), "8000000");
    assert.equal(await r.locator("#grid .video-card").count(), 1);
    assert.equal(await r.locator("[data-updated-at]").first().innerText(), "2026-09-27 20:00:00");
    await r.clock.fastForward(5 * 60 * 1000);
    await r.waitForFunction(() => document.querySelector("[data-updated-at]").textContent === "2026-09-27 21:00:00");
    assert.equal(await r.locator("#q").inputValue(), "8000000");
    assert.equal(await r.locator("#savedCount").innerText(), "1");

    // Keep an in-flight preview alive while its media response is pending.
    // Other tests above cover the separate media-error/retry behavior.
    await r.route("https://media.example.test/**", () => {});
    await r.locator(".thumb-link").first().click();
    assert.equal(await r.locator("#grid video").count(), 1);
    const countBeforePreview = refresh.manifestRequests;
    await r.clock.fastForward(5 * 60 * 1000);
    await r.clock.runFor(50);
    assert.equal(refresh.manifestRequests, countBeforePreview, "Playback defers background refresh");
    assert.equal(await r.locator("#grid video").count(), 1);
    await r.evaluate(() => {
      Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await r.clock.fastForward(5 * 60 * 1000);
    await r.clock.runFor(50);
    assert.equal(refresh.manifestRequests, countBeforePreview, "Hidden pages do not poll");
    response = r.waitForResponse((res) => new URL(res.url()).pathname === "/update.json");
    await r.evaluate(() => {
      Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await (await response).finished();
    assert.equal(refresh.manifestRequests, countBeforePreview + 1, "Returning to the page checks for updates");
    assert.deepEqual(refresh.errors, []);
    assert.equal(refresh.blocked, 0);
    await refresh.context.close();
    report.autoRefresh = { passed: true };
    report.completed = [
      "desktop and mobile navigation",
      "search and full-width ID",
      "persistent saved/later/history",
      "popular ranking",
      "filter reset across navigation",
      "bounded scrolling DOM",
      "escaping and corrupt storage",
      "lazy media/error handling",
      "catalog failure/retry",
      "automatic refresh, lightweight polling, recovery, and preview preservation",
    ];
    fs.writeFileSync(
      path.join(output, "browser-results.json"),
      JSON.stringify(report, null, 2),
    );
    console.log(JSON.stringify(report, null, 2));
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
