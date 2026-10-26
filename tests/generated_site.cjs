const http = require('http');
const fs = require('fs');
const path = require('path');
const { chromium, webkit } = require('playwright');

const root = path.resolve(__dirname, '..', 'docs');
const mime = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
};

function server() {
  return http.createServer((req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    const rel = url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/+/, '');
    const file = path.resolve(root, rel);
    if (!file.startsWith(root + path.sep) && file !== path.join(root, 'index.html')) {
      res.writeHead(403).end('forbidden');
      return;
    }
    fs.readFile(file, (err, data) => {
      if (err) {
        res.writeHead(404).end('not found');
        return;
      }
      res.setHeader('content-type', mime[path.extname(file)] || 'application/octet-stream');
      res.setHeader('cache-control', 'no-store');
      res.end(data);
    });
  });
}

(async () => {
  const engine = process.env.GENERATED_BROWSER === 'chromium' ? chromium : webkit;
  const srv = server();
  await new Promise((resolve) => srv.listen(0, '127.0.0.1', resolve));
  const port = srv.address().port;
  const browser = await engine.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    const errors = [];
    page.on('pageerror', (err) => errors.push(String(err)));
    // The first paint must not depend on downloading the historical archive.
    await page.route('**/catalog-chunk-*.json*', (route) => route.abort());
    await page.goto(`http://127.0.0.1:${port}/`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#grid .video-card', { timeout: 10000 });
    const result = await page.evaluate(() => ({
      cards: document.querySelectorAll('#grid .video-card').length,
      busy: document.getElementById('grid')?.getAttribute('aria-busy'),
      count: document.getElementById('resultCount')?.textContent || '',
      title: document.querySelector('#grid .card-title')?.textContent || '',
    }));
    if (result.cards < 1) throw new Error(`no cards painted: ${JSON.stringify(result)}`);
    if (result.busy === 'true') throw new Error(`grid still busy: ${JSON.stringify(result)}`);
    if (result.count.trim() === '読み込み中…') throw new Error(`loading state stuck: ${JSON.stringify(result)}`);
    if (!result.title.trim()) throw new Error(`first card has no title: ${JSON.stringify(result)}`);
    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`);
    console.log(JSON.stringify({ browser: process.env.GENERATED_BROWSER || 'webkit', ...result }));
  } finally {
    await browser.close();
    await new Promise((resolve) => srv.close(resolve));
  }
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
