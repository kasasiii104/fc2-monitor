#!/usr/bin/env python3
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

DOCS = Path("docs")
CATALOG = DOCS / "catalog.json"
INDEX = DOCS / "index.html"
MISSAV_HOSTS = {"missav.ws", "missav.live", "missav.ai"}
DIRECT_RE = re.compile(r"/(?:ja/)?fc2-ppv-(\d{6,8})(?:/|$)", re.I)
PRELOAD_RE = re.compile(r"\s*<link\s+rel=\"preload\"\s+href=\"catalog\.json[^\"]*\"\s+as=\"fetch\"\s+crossorigin\s*/?>\s*", re.I | re.S)


def code_num(item):
    raw = str(item.get("code_num") or item.get("code") or "")
    m = re.search(r"(\d{6,8})", raw)
    return m.group(1) if m else ""


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
        compact_sources = {name: (missav if name == "MissAV" and missav else "") for name in original_sources.keys()}
        item["sources"] = compact_sources
    elif missav:
        item["sources"] = {"MissAV": missav}
    else:
        item.pop("sources", None)

    # Never expose FC2 official as a viewing destination.
    item.pop("fc2_market_url", None)
    # Client does not use this field for rendering/filtering.
    item.pop("views_source", None)

    # Keep one backup preview at most. This substantially reduces the 25k-item
    # bootstrap while retaining failover for stale/blocked primary samples.
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


def main():
    payload = json.loads(CATALOG.read_text(encoding="utf-8"))
    items = payload.get("items") or []
    before = CATALOG.stat().st_size
    clickable = 0
    compacted = []
    for row in items:
        item, has_missav = compact_item(row)
        clickable += int(has_missav)
        compacted.append(item)
    payload["items"] = compacted

    templates = payload.get("search_templates") if isinstance(payload.get("search_templates"), dict) else {}
    payload["search_templates"] = {"MissAV": templates.get("MissAV", "https://missav.ws/ja/fc2-ppv-{code}")}
    CATALOG.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    after = CATALOG.stat().st_size

    if INDEX.exists():
        html = INDEX.read_text(encoding="utf-8")
        # The catalog was being prefetched before site.js and then fetched again;
        # at 17+ MB this can make iOS appear stuck on "読み込み中". Let site.js
        # own the single request instead.
        html = PRELOAD_RE.sub("\n", html)
        INDEX.write_text(html, encoding="utf-8")

    print(json.dumps({
        "items": len(compacted),
        "missav_clickable": clickable,
        "bytes_before": before,
        "bytes_after": after,
        "saved_bytes": before - after,
        "saved_pct": round((before - after) * 100 / before, 1) if before else 0,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
