#!/usr/bin/env python3
"""Embed the progressive catalog manifest and harden the mobile first paint.

The public site keeps catalog.json + archive chunks for the full library, but the
first 96 rows are also embedded in index.html. Safari should never need a network
round trip before showing those rows, and a broken optional browser API must not
leave the page stuck on skeleton cards.
"""

from __future__ import annotations

import json
from pathlib import Path

INDEX = Path("docs/index.html")
CATALOG = Path("docs/catalog.json")
SITE_JS = Path("docs/site.js")
START = "<!-- catalog-bootstrap:start -->"
END = "<!-- catalog-bootstrap:end -->"
BOOTSTRAP_GLOBAL = "__FC2_CATALOG_BOOTSTRAP__"


def patch_loader() -> None:
    if not SITE_JS.exists():
        raise SystemExit("docs/site.js is missing")
    source = SITE_JS.read_text(encoding="utf-8")

    # The old bootstrap temporarily replaced window.fetch and manufactured a
    # Response object. That adds an unnecessary compatibility layer on iOS.
    # Read the already-parsed embedded payload directly for the first paint.
    marker = '''      const url = new URL(document.body.dataset.catalog, document.baseURI);\n      if (expectedVersion) url.searchParams.set("v", expectedVersion);\n      const response = await fetchWithTimeout(url, { cache: "no-cache" }, 12000);\n      if (!response.ok) throw new Error("HTTP " + response.status);\n      const payload = await response.json();'''
    replacement = '''      let payload = null;\n      if (!background && window.__FC2_CATALOG_BOOTSTRAP__ &&\n          Array.isArray(window.__FC2_CATALOG_BOOTSTRAP__.items)) {\n        payload = window.__FC2_CATALOG_BOOTSTRAP__;\n        window.__FC2_CATALOG_BOOTSTRAP__ = null;\n      } else {\n        const url = new URL(document.body.dataset.catalog, document.baseURI);\n        if (expectedVersion) url.searchParams.set("v", expectedVersion);\n        const response = await fetchWithTimeout(url, { cache: "no-cache" }, 12000);\n        if (!response.ok) throw new Error("HTTP " + response.status);\n        payload = await response.json();\n      }'''
    if marker in source:
        source = source.replace(marker, replacement, 1)
    elif "window.__FC2_CATALOG_BOOTSTRAP__" not in source:
        raise RuntimeError("generated site.js catalog loader marker changed")

    # ResizeObserver is an enhancement, not a prerequisite for rendering.
    # Guard both top-level observers so an older/embedded Safari implementation
    # cannot abort the script before loadCatalog() is reached.
    source = source.replace(
        '  new ResizeObserver(updateRankingArrows).observe($("rankingRail"));',
        '  if ("ResizeObserver" in window) new ResizeObserver(updateRankingArrows).observe($("rankingRail"));',
        1,
    )
    source = source.replace(
        '  new ResizeObserver(() => {',
        '  if ("ResizeObserver" in window) new ResizeObserver(() => {',
        1,
    )

    SITE_JS.write_text(source, encoding="utf-8")


def main() -> int:
    if not INDEX.exists() or not CATALOG.exists():
        raise SystemExit("docs/index.html or docs/catalog.json is missing")

    payload = json.loads(CATALOG.read_text(encoding="utf-8"))
    if not isinstance(payload.get("items"), list):
        raise SystemExit("catalog.json has no items array")

    # Escape '<' so a scraped title can never terminate the application/json
    # script element. The normal JS reads this object directly; no fetch shim.
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    bootstrap = f'''{START}
<script id="catalogBootstrapData" type="application/json">{encoded}</script>
<script>
(() => {{
  const node = document.getElementById("catalogBootstrapData");
  if (!node) return;
  try {{
    const payload = JSON.parse(node.textContent || "{{}}");
    if (!Array.isArray(payload.items)) return;
    window.{BOOTSTRAP_GLOBAL} = payload;

    // Paint a small usable fallback immediately. The full application replaces
    // these nodes a moment later. If application JS is blocked or an optional
    // API throws, users still see and can open recent works instead of skeletons.
    const grid = document.getElementById("grid");
    const count = document.getElementById("resultCount");
    if (!grid || !payload.items.length) return;
    const esc = (value) => String(value ?? "").replace(/[&<>\"']/g, (c) =>
      ({{"&":"&amp;","<":"&lt;",">":"&gt;",'\"':"&quot;","'":"&#39;"}})[c]);
    const safe = (value) => {{
      try {{ const u = new URL(value, document.baseURI); return /^https?:$/.test(u.protocol) ? u.href : ""; }}
      catch (_) {{ return ""; }}
    }};
    grid.innerHTML = payload.items.slice(0, 24).map((it) => {{
      const code = esc(it.code || "");
      const title = esc(it.title || it.code || "");
      const url = safe(it.url);
      const thumb = safe(it.thumb);
      return `<article class="video-card bootstrap-card" data-code="${{code}}"><div class="thumb-shell"><a class="thumb-link" href="${{esc(url)}}" target="_blank" rel="noopener noreferrer">${{thumb ? `<img src="${{esc(thumb)}}" alt="" loading="eager" decoding="async" referrerpolicy="no-referrer" width="480" height="270">` : ""}}</a></div><div class="card-body">${{url ? `<a class="card-title" href="${{esc(url)}}" target="_blank" rel="noopener noreferrer">${{title}}</a>` : `<div class="card-title">${{title}}</div>`}}<div class="card-code">${{code}}</div></div></article>`;
    }}).join("");
    grid.setAttribute("aria-busy", "false");
    if (count) count.textContent = `${{payload.items.length}} / ${{payload.item_count || payload.items.length}} 作品読み込み済み…`;
  }} catch (error) {{
    console.warn("Catalog bootstrap parse failed", error);
  }}
}})();
</script>
{END}'''

    html = INDEX.read_text(encoding="utf-8")
    if START in html and END in html:
        before, rest = html.split(START, 1)
        _, after = rest.split(END, 1)
        html = before + bootstrap + after
    else:
        if "</body>" not in html:
            raise SystemExit("index.html has no </body>")
        html = html.replace("</body>", bootstrap + "\n</body>", 1)
    INDEX.write_text(html, encoding="utf-8")

    patch_loader()
    print(
        f"[Catalog bootstrap] embedded_bytes={len(encoded.encode('utf-8'))} "
        f"inline_items={len(payload.get('items') or [])} total={payload.get('item_count')} "
        "direct_loader=true fallback_paint=true"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
