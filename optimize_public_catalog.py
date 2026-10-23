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
  async function fetchCatalogChunks(payload) {
    const chunks = Array.isArray(payload.chunks) ? payload.chunks : [];
    if (!chunks.length) return [];
    const results = new Array(chunks.length);
    let next = 0;
    const worker = async () => {
      while (true) {
        const index = next++;
        if (index >= chunks.length) return;
        const entry = chunks[index] || {};
        const url = new URL(entry.url || entry, document.baseURI);
        if (payload.version) url.searchParams.set("v", payload.version);
        const response = await fetch(url, { cache: "no-cache" });
        if (!response.ok) throw new Error(`Chunk ${index + 1}: HTTP ${response.status}`);
        const chunk = await response.json();
        if (!Array.isArray(chunk.items)) throw new Error(`Chunk ${index + 1}: invalid data`);
        results[index] = chunk.items;
        // Give Safari/iOS a paint opportunity between JSON parses.
        await new Promise((resolve) => setTimeout(resolve, 0));
      }
    };
    const concurrency = Math.min(2, chunks.length);
    await Promise.all(Array.from({ length: concurrency }, () => worker()));
    return results.flat();
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
    $("catalogItemCount").textContent = number(totalCount || items.length);
    $("catalogNewCount").textContent = number(items.filter((it) => it.is_new).length);
    render(false);
  }
'''


def patch_site_js() -> None:
    source = SITE_JS.read_text(encoding="utf-8")
    if "async function fetchCatalogChunks(payload)" not in source:
        marker = "  async function loadCatalog(background = false, expectedVersion = \"\") {"
        if marker not in source:
            raise RuntimeError("site.js loadCatalog marker changed; refusing unsafe patch")
        source = source.replace(marker, HELPERS + "\n" + marker, 1)

    old = """      const payload = await response.json();\n      if (!Array.isArray(payload.items)) throw new Error(\"Invalid catalog\");"""
    new = """      const payload = await response.json();\n      if (!Array.isArray(payload.items)) throw new Error(\"Invalid catalog\");\n      // Background refresh keeps the old complete feed visible until every\n      // chunk for the new version is available.\n      if (background && Array.isArray(payload.chunks) && payload.chunks.length) {\n        const archiveRows = await fetchCatalogChunks(payload);\n        payload.items = payload.items.concat(archiveRows);\n      }"""
    if old in source:
        source = source.replace(old, new, 1)
    elif "Background refresh keeps the old complete feed visible" not in source:
        raise RuntimeError("site.js payload marker changed; refusing unsafe patch")

    old = """      render(!background);\n      if (background) {"""
    new = """      render(!background);\n      // Let the recent cards paint before any archive download starts. This is\n      // important on Safari/iOS and slow mobile networks.\n      if (!background && Array.isArray(payload.chunks) && payload.chunks.length) {\n        const expectedTotal = Number(payload.item_count || items.length);\n        $(\"resultCount\").textContent = `${number(items.length)} / ${number(expectedTotal)} 作品読み込み済み…`;\n        await new Promise((resolve) => setTimeout(resolve, 350));\n        try {\n          const archiveRows = await fetchCatalogChunks(payload);\n          if (payload.version === catalogVersion) appendCatalogItems(archiveRows, expectedTotal);\n        } catch {\n          toast(\"過去作品の追加読み込みに失敗しました。新着作品はそのまま閲覧できます。\");\n        }\n      }\n      if (background) {"""
    if old in source:
        source = source.replace(old, new, 1)
    elif "Let the recent cards paint before any archive download starts" not in source:
        source = source.replace(
            "// Initial navigation becomes interactive after only the small inline\n"
            "      // slice.  Hydrate the archive afterwards; failures keep that recent feed\n"
            "      // usable instead of turning the whole page into an error state.",
            "// Let the recent cards paint before any archive download starts. This is\n"
            "      // important on Safari/iOS and slow mobile networks.",
        )
        needle = "        $(\"resultCount\").textContent = `${number(items.length)} / ${number(expectedTotal)} 作品読み込み済み…`;\n        try {"
        if needle in source:
            source = source.replace(needle, needle.replace("        try {", "        await new Promise((resolve) => setTimeout(resolve, 350));\n        try {"), 1)
        elif "await new Promise((resolve) => setTimeout(resolve, 350));" not in source:
            raise RuntimeError("site.js progressive render marker changed; refusing unsafe patch")

    source = source.replace("const concurrency = Math.min(3, chunks.length);", "const concurrency = Math.min(2, chunks.length);")
    chunk_marker = "        results[index] = chunk.items;\n"
    if chunk_marker in source and "Give Safari/iOS a paint opportunity" not in source:
        source = source.replace(
            chunk_marker,
            chunk_marker + "        // Give Safari/iOS a paint opportunity between JSON parses.\n        await new Promise((resolve) => setTimeout(resolve, 0));\n",
            1,
        )

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
