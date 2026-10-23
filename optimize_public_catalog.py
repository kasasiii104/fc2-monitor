#!/usr/bin/env python3
"""Make the public catalog fast to first paint without losing archive search.

The crawler keeps docs/data.json as its canonical state.  The browser does not
need that file.  This post-build step rewrites docs/catalog.json into a small
manifest with a small inline recent slice and puts the remainder into bounded
chunks.  It also patches the generated docs/site.js so the inline slice renders
immediately, then the archive hydrates in the background with limited
concurrency.
"""
from __future__ import annotations

import json
from pathlib import Path

DOCS = Path("docs")
CATALOG = DOCS / "catalog.json"
SITE_JS = DOCS / "site.js"
FIRST_PAINT_ITEMS = 300
CHUNK_ITEMS = 1200


def compact_write(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def split_catalog() -> tuple[int, int]:
    payload = json.loads(CATALOG.read_text(encoding="utf-8"))
    rows = payload.get("items")
    if not isinstance(rows, list):
        raise RuntimeError("catalog.json does not contain an items array")

    # Remove stale chunks from older layouts so the Pages artifact cannot grow
    # forever and every manifest references only this build's files.
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
      }
    };
    const concurrency = Math.min(3, chunks.length);
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
    new = """      render(!background);\n      // Initial navigation becomes interactive after only the small inline\n      // slice.  Hydrate the archive afterwards; failures keep that recent feed\n      // usable instead of turning the whole page into an error state.\n      if (!background && Array.isArray(payload.chunks) && payload.chunks.length) {\n        const expectedTotal = Number(payload.item_count || items.length);\n        $(\"resultCount\").textContent = `${number(items.length)} / ${number(expectedTotal)} 作品読み込み済み…`;\n        try {\n          const archiveRows = await fetchCatalogChunks(payload);\n          if (payload.version === catalogVersion) appendCatalogItems(archiveRows, expectedTotal);\n        } catch {\n          toast(\"過去作品の追加読み込みに失敗しました。新着作品はそのまま閲覧できます。\");\n        }\n      }\n      if (background) {"""
    if old in source:
        source = source.replace(old, new, 1)
    elif "Initial navigation becomes interactive after only the small inline" not in source:
        raise RuntimeError("site.js render marker changed; refusing unsafe patch")

    SITE_JS.write_text(source, encoding="utf-8")


def main() -> int:
    total, chunks = split_catalog()
    patch_site_js()
    print(f"[Public catalog optimize] items={total} inline={min(total, FIRST_PAINT_ITEMS)} chunks={chunks}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
