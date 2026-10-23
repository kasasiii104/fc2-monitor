#!/usr/bin/env python3
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

DOCS = Path("docs")
CATALOG = DOCS / "catalog.json"
DATA = DOCS / "data.json"
INDEX = DOCS / "index.html"
SITE_JS = DOCS / "site.js"
MISSAV_HOSTS = {"missav.ws", "missav.live", "missav.ai"}
DIRECT_RE = re.compile(r"/(?:ja/)?fc2-ppv-(\d{6,8})(?:/|$)", re.I)
PRELOAD_RE = re.compile(r"\s*<link\s+rel=\"preload\"\s+href=\"catalog\.json[^\"]*\"\s+as=\"fetch\"\s+crossorigin\s*/?>\s*", re.I | re.S)
SITE_JS_RE = re.compile(r"site\.js\?v=[^\"']+")
BOOTSTRAP_ITEMS = 800
CHUNK_ITEMS = 1200
CHUNK_GLOB = "catalog-chunk-*.json"


def code_num(item):
    raw = str(item.get("code_num") or item.get("code") or "")
    m = re.search(r"(\d{6,8})", raw)
    return m.group(1) if m else ""


def duration_seconds(text):
    parts = [int(x) for x in re.findall(r"\d+", str(text or ""))]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return 0


def public_from_saved(item):
    """Rebuild a public record from the authoritative full docs/data.json row."""
    sources = dict(item.get("sources") or {})
    num = code_num(item)
    row = {
        "code": item.get("code") or (f"FC2-PPV-{num}" if num else ""),
        "code_num": num,
        "title": item.get("title") or item.get("code") or "",
        "url": item.get("url") or sources.get("MissAV") or "",
        "thumb": item.get("thumb") or "",
        "preview": item.get("preview") or "",
        "preview_fallbacks": item.get("preview_fallbacks") if isinstance(item.get("preview_fallbacks"), list) else [],
        "duration": item.get("duration") or "",
        "duration_sec": duration_seconds(item.get("duration") or ""),
        "views": item.get("views") if isinstance(item.get("views"), int) else 0,
        "views_source": item.get("views_source") or "",
        "first_seen": item.get("first_seen") or "",
        "is_new": bool(item.get("is_new")),
        "sources": sources,
        "trend_6h": item.get("trend_6h") if isinstance(item.get("trend_6h"), int) else None,
        "trend_24h": item.get("trend_24h") if isinstance(item.get("trend_24h"), int) else None,
        "missav_rank_day": item.get("missav_rank_day") if isinstance(item.get("missav_rank_day"), int) else None,
        "missav_rank_week": item.get("missav_rank_week") if isinstance(item.get("missav_rank_week"), int) else None,
        "missav_rank_month": item.get("missav_rank_month") if isinstance(item.get("missav_rank_month"), int) else None,
        "missav_rank_total": item.get("missav_rank_total") if isinstance(item.get("missav_rank_total"), int) else None,
        "fc2_market_url": item.get("fc2_market_url") or "",
        "fc2_rating": item.get("fc2_rating"),
        "fc2_review_count": item.get("fc2_review_count"),
        "fc2_rating_source": item.get("fc2_rating_source") or "",
    }
    return {
        key: value for key, value in row.items()
        if value is not None and value != "" and value is not False and value != []
    }


def direct_missav(url, expected=""):
    if not isinstance(url, str) or not url.strip():
        return ""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return ""
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or host not in MISSAV_HOSTS:
        return ""
    m = DIRECT_RE.search(p.path or "")
    if not m:
        return ""
    if expected and m.group(1) != expected:
        return ""
    return url.strip()


def compact_item(item):
    item = dict(item)
    expected = code_num(item)
    original_sources = item.get("sources") if isinstance(item.get("sources"), dict) else {}
    missav = direct_missav(original_sources.get("MissAV"), expected) or direct_missav(item.get("url"), expected)

    # A title/card may only open a confirmed direct MissAV work page.
    if missav:
        item["url"] = missav
    else:
        item.pop("url", None)

    # Preserve source-count filtering without exposing outbound URLs other than MissAV.
    if original_sources:
        item["sources"] = {
            name: (missav if name == "MissAV" and missav else "")
            for name in original_sources.keys()
        }
    elif missav:
        item["sources"] = {"MissAV": missav}
    else:
        item.pop("sources", None)

    # Never expose FC2 official as a viewing destination.
    item.pop("fc2_market_url", None)
    item.pop("views_source", None)

    # Retain one backup preview at most.
    primary = item.get("preview") or ""
    fallbacks = item.get("preview_fallbacks") if isinstance(item.get("preview_fallbacks"), list) else []
    deduped = []
    for url in fallbacks:
        if isinstance(url, str) and url and url != primary and url not in deduped:
            deduped.append(url)
    if deduped:
        item["preview_fallbacks"] = deduped[:1]
    else:
        item.pop("preview_fallbacks", None)
    return item, bool(missav)


def patch_site_js():
    if not SITE_JS.exists():
        return False
    js = SITE_JS.read_text(encoding="utf-8")
    if "PUBLIC_CHUNK_LOADER_V1" in js:
        return False

    marker = "  let searchTemplates = {};\n"
    if marker not in js:
        raise RuntimeError("site.js searchTemplates marker not found")

    helper = r'''  // PUBLIC_CHUNK_LOADER_V1: render the small bootstrap immediately,
  // then add archive chunks in the background without blocking first paint.
  let searchTemplates = {};
  let catalogChunkToken = 0;
  function prepareCatalogItem(it) {
    return {
      ...it,
      code_num: String(it.code_num || it.code?.split("-").pop() || ""),
      _codeNumber: codeNumber(it.code_num || it.code?.split("-").pop()),
      _ts: parseSeen(it.first_seen),
      _sources: Object.keys(it.sources || {}).length,
      _search: normalize(
        `${it.code} ${it.title} ${Object.keys(it.sources || {}).join(" ")}`,
      ),
    };
  }
  function rebuildCatalogIndexes() {
    homeRankingCache = Object.fromEntries(
      homePeriods.map((period) => {
        const field = "missav_rank_" + period;
        return [
          period,
          items
            .filter((it) => it[field] > 0)
            .sort((a, b) => a[field] - b[field])
            .slice(0, 10),
        ];
      }),
    );
    homeRankingKey = "";
    byCode = new Map(items.map((it) => [it.code, it]));
  }
  async function loadCatalogChunks(chunks, version, token) {
    if (!Array.isArray(chunks) || !chunks.length) return;
    let changed = false;
    for (const name of chunks) {
      if (token !== catalogChunkToken || version !== catalogVersion) return;
      try {
        const url = new URL(name, document.baseURI);
        if (version) url.searchParams.set("v", version);
        const response = await fetch(url, { cache: "force-cache" });
        if (!response.ok) throw new Error("HTTP " + response.status);
        const payload = await response.json();
        const rows = Array.isArray(payload) ? payload : payload.items;
        if (!Array.isArray(rows)) throw new Error("Invalid catalog chunk");
        for (const raw of rows) {
          const it = prepareCatalogItem(raw);
          if (byCode.has(it.code)) continue;
          items.push(it);
          byCode.set(it.code, it);
          changed = true;
        }
        await new Promise((resolve) => {
          if ("requestIdleCallback" in window)
            requestIdleCallback(() => resolve(), { timeout: 500 });
          else setTimeout(resolve, 0);
        });
      } catch {
        // A failed archive chunk must not hide the already usable bootstrap.
        break;
      }
    }
    if (changed && token === catalogChunkToken && version === catalogVersion) {
      rebuildCatalogIndexes();
      render(false);
    }
  }
'''
    js = js.replace(marker, helper, 1)

    old_mapping = r'''      items = payload.items.map((it) => ({
        ...it,
        code_num: String(it.code_num || it.code?.split("-").pop() || ""),
        _codeNumber: codeNumber(it.code_num || it.code?.split("-").pop()),
        _ts: parseSeen(it.first_seen),
        _sources: Object.keys(it.sources || {}).length,
        _search: normalize(
          `${it.code} ${it.title} ${Object.keys(it.sources || {}).join(" ")}`,
        ),
      }));
      homeRankingCache = Object.fromEntries(
        homePeriods.map((period) => {
          const field = "missav_rank_" + period;
          return [
            period,
            items
              .filter((it) => it[field] > 0)
              .sort((a, b) => a[field] - b[field])
              .slice(0, 10),
          ];
        }),
      );
      homeRankingKey = "";
      byCode = new Map(items.map((it) => [it.code, it]));
'''
    new_mapping = '''      catalogChunkToken += 1;\n      items = payload.items.map(prepareCatalogItem);\n      rebuildCatalogIndexes();\n'''
    if old_mapping not in js:
        raise RuntimeError("site.js catalog mapping block not found")
    js = js.replace(old_mapping, new_mapping, 1)

    count_old = '      $("catalogItemCount").textContent = number(items.length);\n'
    count_new = '      $("catalogItemCount").textContent = number(payload.item_count_total || items.length);\n'
    if count_old not in js:
        raise RuntimeError("site.js catalog count marker not found")
    js = js.replace(count_old, count_new, 1)

    render_old = '      render(!background);\n      if (background) {\n'
    render_new = '''      render(!background);\n      void loadCatalogChunks(payload.chunks || [], catalogVersion, catalogChunkToken);\n      if (background) {\n'''
    if render_old not in js:
        raise RuntimeError("site.js render marker not found")
    js = js.replace(render_old, render_new, 1)

    SITE_JS.write_text(js, encoding="utf-8")
    return True


def refresh_index_asset_version():
    if not INDEX.exists() or not SITE_JS.exists():
        return ""
    digest = hashlib.sha256(SITE_JS.read_bytes()).hexdigest()[:12]
    html = INDEX.read_text(encoding="utf-8")
    html = PRELOAD_RE.sub("\n", html)
    html = SITE_JS_RE.sub(f"site.js?v={digest}", html)
    INDEX.write_text(html, encoding="utf-8")
    return digest


def main():
    if not DATA.exists():
        raise RuntimeError("docs/data.json is required for an idempotent public build")
    manifest = json.loads(CATALOG.read_text(encoding="utf-8")) if CATALOG.exists() else {}
    saved = json.loads(DATA.read_text(encoding="utf-8"))
    saved_items = saved.get("items") or []

    # Always rebuild from the authoritative full catalog. Never re-compact the
    # already chunked 800-item bootstrap, otherwise repeated runs would lose the
    # archive chunks even though docs/data.json still contains them.
    payload = {
        key: value for key, value in manifest.items()
        if key not in {"items", "chunks", "item_count_total", "search_templates"}
    }
    payload["updated_at"] = saved.get("updated_at") or manifest.get("updated_at") or ""
    templates = manifest.get("search_templates") if isinstance(manifest.get("search_templates"), dict) else {}
    payload["search_templates"] = {
        "MissAV": templates.get("MissAV", "https://missav.ws/ja/fc2-ppv-{code}")
    }

    clickable = 0
    compacted = []
    for raw in saved_items:
        item, has_missav = compact_item(public_from_saved(raw))
        clickable += int(has_missav)
        compacted.append(item)

    # Estimate the old one-shot payload for a useful bootstrap reduction metric.
    full_payload = dict(payload)
    full_payload["items"] = compacted
    full_bytes = len(json.dumps(full_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    for old in DOCS.glob(CHUNK_GLOB):
        old.unlink()

    chunk_names = []
    remaining = compacted[BOOTSTRAP_ITEMS:]
    for index in range(0, len(remaining), CHUNK_ITEMS):
        name = f"catalog-chunk-{index // CHUNK_ITEMS + 1:03d}.json"
        rows = remaining[index:index + CHUNK_ITEMS]
        (DOCS / name).write_text(
            json.dumps({"items": rows}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        chunk_names.append(name)

    payload["items"] = compacted[:BOOTSTRAP_ITEMS]
    payload["chunks"] = chunk_names
    payload["item_count_total"] = len(compacted)
    CATALOG.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    js_patched = patch_site_js()
    asset_version = refresh_index_asset_version()
    bootstrap_bytes = CATALOG.stat().st_size
    chunk_bytes = sum((DOCS / name).stat().st_size for name in chunk_names)
    print(json.dumps({
        "items": len(compacted),
        "bootstrap_items": min(len(compacted), BOOTSTRAP_ITEMS),
        "chunks": len(chunk_names),
        "missav_clickable": clickable,
        "full_payload_bytes": full_bytes,
        "bootstrap_bytes": bootstrap_bytes,
        "chunk_bytes": chunk_bytes,
        "initial_saved_pct": round((full_bytes - bootstrap_bytes) * 100 / full_bytes, 1) if full_bytes else 0,
        "site_js_patched": js_patched,
        "site_js_version": asset_version,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
