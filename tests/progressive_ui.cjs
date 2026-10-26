/* Neutral fixtures: test scrolling while archive batches arrive. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, webkit } = require('playwright');

const root = path.resolve(__dirname, '..', 'test-results', 'progressive-fixture');
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'catalog.json'), 'utf8'));
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json' };
const watchdog = setTimeout(() => { console.error('Progressive UI test timed out'); process.exit(1); }, 90000);
watchdog.unref();

const server = http.createServer((req, res) => {
  const pathname = new URL(req.url, 'http://127.0.0.1').pathname;
  const file = path.resolve(root, pathname === '/' ? 'index.html' : pathname.slice(1));
  if (!file.startsWith(root + path.sep)) return res.writeHead(403).end();
  fs.readFile(file, (error, data) => {
    if (error) return res.writeHead(404).end();
    res.setHeader('content-type', mime[path.extname(file)] || 'application/octet-stream');
    res.end(data);
  });
});

async function checkScenario(browser, origin, width, sort) {
  const page = await browser.newPage({ viewport: { width, height: 844 } });
  page.setDefaultTimeout(10000);
  const errors = [];
  const network = [];
  page.on('pageerror', error => errors.push(String(error)));
  let allowedChunks = 0;
  let waiting = [];
  function release(limit) {
    allowedChunks = limit;
    const ready = waiting.filter(entry => entry.index <= limit);
    waiting = waiting.filter(entry => entry.index > limit);
    ready.forEach(entry => entry.resolve());
  }
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.hostname === '127.0.0.1') return route.continue();
    // Synthetic thumbnails only; never contact a media provider.
    if (url.hostname === 'images.example.test') return route.fulfill({
      contentType: 'image/svg+xml',
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="270"><rect width="480" height="270" fill="#567"/></svg>',
    });
    network.push(url.hostname);
    return route.abort();
  });
  await page.route('**/catalog-chunk-*.json*', async route => {
    const name = path.basename(new URL(route.request().url()).pathname);
    const index = Number(name.match(/(\d+)\.json$/)[1]);
    if (index > allowedChunks) await new Promise(resolve => waiting.push({ index, resolve }));
    if (!page.isClosed()) await route.fulfill({ contentType: 'application/json', body: fs.readFileSync(path.join(root, name)) });
  });
  try {
    await page.goto(`${origin}/?sort=${sort}`, { waitUntil: 'domcontentloaded' });
    await page.addScriptTag({ path: path.resolve(__dirname, '..', 'web', 'missav-only.js') });
    await page.waitForSelector('#grid .video-card:not(.bootstrap-card)');
    await page.evaluate(() => {
      window.scrollTo(0, document.getElementById('feedWindow').getBoundingClientRect().top + scrollY + 1800);
    });
    await page.waitForFunction(() => scrollY > 1500);
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await page.evaluate(() => {
      window.testRankingNode = document.querySelector('#rankingRail .ranking-card');
      window.testAnchorRemovals = 0;
      new MutationObserver(records => {
        for (const record of records)
          if ([...record.removedNodes].includes(window.testAnchor)) window.testAnchorRemovals++;
      }).observe(document.getElementById('grid'), { childList: true });
    });
    const batch = width < 821 ? 4 : 6;
    for (let limit = batch; limit < manifest.chunks.length + batch; limit += batch) {
      const before = await page.evaluate(() => {
        window.testAnchor = [...document.querySelectorAll('#grid .video-card')].find(node => {
          const rect = node.getBoundingClientRect();
          return rect.bottom > 150 && rect.top < innerHeight;
        });
        if (!window.testAnchor) throw new Error('No visible scroll anchor');
        window.testAnchorRemovals = 0;
        return { y: scrollY, top: window.testAnchor.getBoundingClientRect().top };
      });
      const count = Math.min(limit, manifest.chunks.length);
      const expected = manifest.items.length + manifest.chunks.slice(0, count).reduce((sum, entry) => sum + entry.count, 0);
      release(count);
      await page.waitForFunction(expected => {
        const count = document.getElementById('resultCount').textContent.replaceAll(',', '');
        return parseInt(count, 10) === expected;
      }, expected);
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const after = await page.evaluate(() => ({
        connected: window.testAnchor.isConnected,
        top: window.testAnchor.getBoundingClientRect().top,
        y: scrollY,
        removals: window.testAnchorRemovals,
        rankingRetained: window.testRankingNode === document.querySelector('#rankingRail .ranking-card'),
        mounted: document.querySelectorAll('#grid .video-card').length,
      }));
      assert.equal(after.connected, true, 'hydration must retain the visible card');
      assert.equal(after.removals, 0, 'visible cards must never disappear during a batch');
      assert.ok(Math.abs(after.top - before.top) < 2, `scroll anchor moved: ${JSON.stringify({ before, after })}`);
      if (sort === 'new') assert.ok(Math.abs(after.y - before.y) < 2, 'append-only batches must preserve scrollY');
      assert.equal(after.rankingRetained, true, 'unchanged rankings must not flicker');
      assert.ok(after.mounted < 100, 'archive hydration must remain virtualized');
      // The reader can continue down the page between batches.
      await page.evaluate(() => window.scrollBy(0, 350));
      await page.waitForFunction(y => scrollY > y + 300, after.y);
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    }
    assert.deepEqual(errors, []);
    assert.deepEqual(network, [], 'all content in this fixture must be synthetic');
    console.log(JSON.stringify({ browser: process.env.PROGRESSIVE_BROWSER || 'chromium', width, sort, loaded: manifest.item_count, scrolling: true, cardsRetained: true }));
  } finally {
    release(Infinity);
    await page.close();
  }
}

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const engine = process.env.PROGRESSIVE_BROWSER === 'webkit' ? webkit : chromium;
  const browser = await engine.launch({ headless: true });
  try {
    await checkScenario(browser, origin, 390, 'new');
    await checkScenario(browser, origin, 1280, 'code_desc');
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().then(() => clearTimeout(watchdog)).catch(error => { console.error(error); process.exit(1); });
