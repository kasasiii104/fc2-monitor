#!/usr/bin/env python3
"""Make the public catalog fast to first paint without losing archive search.

The crawler keeps docs/data.json as its canonical state. The browser only needs
a small recent manifest for first paint plus bounded archive chunks. The generated
site.js is patched for progressive hydration, then index.html is synchronized to
the *actual generated JS bytes* so browsers never reuse a stale loader under an
old cache key.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

DOCS = Path("docs")
CATALOG = DOCS / "catalog.json"
SITE_JS = DOCS / "site.js"
INDEX = DOCS / "index.html"
FIRST_PAINT_ITEMS = 160
CHUNK_ITEMS = 600


def compact_write(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def split_catalog() -> tuple[int, int]:
    payload = json.loads(CATALOG.read_text(encoding="utf-8"))
    rows = payload.get("items")
    if not isinstance(rows, list):
        raise RuntimeError("catalog.json does not contain an items array")

    for old in DOCS.glob("catalog-chunk-*.json"):
        old.unlink()

    inline = rows[:FIRST_PAINT_ITEMS]
    remainder = rows[FIRST_PAINT_ITEMS:]
    chunks = []
    for offset in range(0, len(remainder), CHUNK_ITEMS):
        part = remainder[offset : offset + CHUNK_ITEMS]
        name = f"catalog-chunk-{offset // CHUNK_ITEMS + 1:03d}.json"
        compact_write(DOCS / name, {"items": part})
        chunks.append({"url": name, "count": len(part)})

    manifest = dict(payload)
    manifest["items"] = inline
    manifest["item_count"] = len(rows)
    manifest["chunks"] = chunks
    manifest["progressive"] = True
    compact_write(CATALOG, manifest)
    return len(rows), len(chunks)


HELPERS = r'''
  async function fetchCatalogChunks(payload, onChunk = null) {
    const chunks = Array.isArray(payload.chunks) ? payload.chunks : [];
    if (!chunks.length)
      return { rows: [], failed: 0, loadedChunks: 0, totalChunks: 0 };
    const results = new Array(chunks.length);
    let next = 0, failed = 0, loadedChunks = 0;
    const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const fetchOne = async (index) => {
      const entry = chunks[index] || {};
      const url = new URL(entry.url || entry, document.baseURI);
      if (payload.version) url.searchParams.set("v", payload.version);
      let lastError = null;
      for (let attempt = 1; attempt <= 3; attempt++) {
        try {
          // The version query makes each deploy immutable. Reusing a previously
          // downloaded chunk is much faster than forcing 20+ MB over the wire
          // again on every reload.
          const response = await fetch(url, { cache: "force-cache" });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const chunk = await response.json();
          if (!Array.isArray(chunk.items)) throw new Error("invalid data");
          return chunk.items;
        } catch (err) {
          lastError = err;
          if (attempt < 3) await pause(250 * attempt);
        }
      }
      console.warn(`Catalog chunk ${index + 1} failed`, lastError);
      return null;
    };
    const worker = async () => {
      while (true) {
        const index = next++;
        if (index >= chunks.length) return;
        const rows = await fetchOne(index);
        if (!rows) {
          failed++;
          continue;
        }
        results[index] = rows;
        loadedChunks++;
        if (onChunk) await onChunk(rows, index, loadedChunks, chunks.length);
        // Give Safari/iOS a paint opportunity between JSON parses.
        await pause(0);
      }
    };
    const saveData = !!navigator.connection?.saveData;
    const concurrency = Math.min(saveData ? 1 : 4, chunks.length);
    await Promise.all(Array.from({ length: concurrency }, () => worker()));
    return {
      rows: results.flatMap((rows) => rows || []),
      failed,
      loadedChunks,
      totalChunks: chunks.length,
    };
  }
  function appendCatalogItems(rows, totalCount) {
    const seen = new Set(items.map((it) => it.code));
    for (const it of rows) {
      if (!it?.code || seen.has(it.code)) continue;
      seen.add(it.code);
      const codeNum = String(it.code_num || it.code?.split("-").pop() || "");
      items.push({
        ...it,
        code_num: codeNum,
        _codeNumber: codeNumber(codeNum),
        _ts: parseSeen(it.first_seen),
        _sources: Object.keys(it.sources || {}).length,
        _search: normalize(`${it.code} ${it.title} ${Object.keys(it.sources || {}).join(" ")}`),
      });
    }
    homeRankingCache = Object.fromEntries(
      homePeriods.map((period) => {
        const field = "missav_rank_" + period;
        return [period, items.filter((it) => it[field] > 0)
          .sort((a, b) => a[field] - b[field]).slice(0, 10)];
      }),
    );
    homeRankingKey = "";
    byCode = new Map(items.map((it) => [it.code, it]));
    const expected = Number(totalCount || items.length);
    $("catalogItemCount").textContent = number(expected);
    $("catalogNewCount").textContent = number(items.filter((it) => it.is_new).length);
    render(false);
    if (items.length < expected)
      $("resultCount").textContent = `${number(items.length)} / ${number(expected)} 作品読み込み済み…`;
  }
'''


def patch_site_js() -> None:
    source = SITE_JS.read_text(encoding="utf-8")
    if "async function fetchCatalogChunks(payload, onChunk = null)" not in source:
        marker = "  async function loadCatalog(background = false, expectedVersion = \"\") {"
        if marker not in source:
            raise RuntimeError("site.js loadCatalog marker changed; refusing unsafe patch")
        source = source.replace(marker, HELPERS + "\n" + marker, 1)

    old = """      const payload = await response.json();\n      if (!Array.isArray(payload.items)) throw new Error(\"Invalid catalog\");"""
    new = """      const payload = await response.json();\n      if (!Array.isArray(payload.items)) throw new Error(\"Invalid catalog\");\n      // Background refresh keeps the old complete feed visible until the new\n      // version is fully available. A partial deploy must never replace it.\n      if (background && Array.isArray(payload.chunks) && payload.chunks.length) {\n        const archive = await fetchCatalogChunks(payload);\n        if (archive.failed) throw new Error(\"Incomplete catalog deployment\");\n        payload.items = payload.items.concat(archive.rows);\n      }"""
    if old in source:
        source = source.replace(old, new, 1)
    elif "Background refresh keeps the old complete feed visible" not in source:
        raise RuntimeError("site.js payload marker changed; refusing unsafe patch")

    # The manifest knows the real total. Do not make the header look like the
    # catalog only contains the 160 first-paint rows.
    source = source.replace(
        '      $("catalogItemCount").textContent = number(items.length);',
        '      $("catalogItemCount").textContent = number(payload.item_count || items.length);',
        1,
    )

    old = """      render(!background);\n      if (background) {"""
    new = """      render(!background);\n      // Paint the recent rows immediately, then hydrate archive chunks in\n      // small visible batches. The old implementation waited for all 45 chunks\n      // and discarded every successful chunk if a single request failed.\n      if (!background && Array.isArray(payload.chunks) && payload.chunks.length) {\n        const expectedTotal = Number(payload.item_count || items.length);\n        $(\"resultCount\").textContent = `${number(items.length)} / ${number(expectedTotal)} 作品読み込み済み…`;\n        await new Promise((resolve) => setTimeout(resolve, 120));\n        let staged = [];\n        const flush = async () => {\n          if (!staged.length || payload.version !== catalogVersion) return;\n          const batch = staged;\n          staged = [];\n          appendCatalogItems(batch, expectedTotal);\n          await new Promise((resolve) => requestAnimationFrame(() => resolve()));\n        };\n        const archive = await fetchCatalogChunks(payload, async (rows) => {\n          if (payload.version !== catalogVersion) return;\n          staged.push(...rows);\n          // Four 600-row chunks per paint keeps sorting/render work bounded\n          // while users see steady progress instead of a long frozen counter.\n          if (staged.length >= 2400) await flush();\n        });\n        await flush();\n        if (archive.failed)\n          toast(`${archive.failed}個の過去データを再取得できませんでした。表示済み作品はそのまま使えます。`);\n        else if (items.length >= expectedTotal)\n          $(\"resultCount\").textContent = `${number(items.length)} 作品`;\n      }\n      if (background) {"""
    if old in source:
        source = source.replace(old, new, 1)
    elif "Paint the recent rows immediately" not in source:
        raise RuntimeError("site.js progressive render marker changed; refusing unsafe patch")

    SITE_JS.write_text(source, encoding="utf-8")


def sync_generated_asset_version() -> str:
    """Cache-bust the generated JS using the bytes that are actually deployed."""
    text = INDEX.read_text(encoding="utf-8")
    js_version = hashlib.sha256(SITE_JS.read_bytes()).hexdigest()[:12]
    text, count = re.subn(r'(site\.js\?v=)[^"\s]+', rf'\g<1>{js_version}', text, count=1)
    if count != 1:
        raise RuntimeError("index.html site.js version marker missing")
    # Safari can issue a second fetch when a fetch-preload's credentials mode
    # differs from fetch(). The manifest is small now; direct fetch is safer.
    text = re.sub(
        r'(?:^|\n)\s*<link\s+rel="preload"\s+href="catalog\.json[^>]+?/>',
        "",
        text,
        count=1,
        flags=re.S,
    )
    INDEX.write_text(text, encoding="utf-8")
    return js_version


def main() -> int:
    total, chunks = split_catalog()
    patch_site_js()
    js_version = sync_generated_asset_version()
    print(
        f"[Public catalog optimize] items={total} inline={min(total, FIRST_PAINT_ITEMS)} "
        f"chunks={chunks} js={js_version}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
