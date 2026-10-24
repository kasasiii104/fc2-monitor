#!/usr/bin/env python3
"""Embed the small progressive catalog manifest into index.html for instant first paint.

The normal catalog remains available as catalog.json and archive chunks. This only
removes the extra network round trip before the first cards can render, which is
especially important on iPhone/Safari over cellular networks.
"""

import json
from pathlib import Path

INDEX = Path("docs/index.html")
CATALOG = Path("docs/catalog.json")
START = "<!-- catalog-bootstrap:start -->"
END = "<!-- catalog-bootstrap:end -->"


def main() -> int:
    if not INDEX.exists() or not CATALOG.exists():
        raise SystemExit("docs/index.html or docs/catalog.json is missing")

    payload = json.loads(CATALOG.read_text(encoding="utf-8"))
    # Keep the embedded payload identical to catalog.json. Escape '<' so a
    # scraped title can never terminate the application/json script element.
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    bootstrap = f'''{START}
<script id="catalogBootstrapData" type="application/json">{encoded}</script>
<script>
(() => {{
  const node = document.getElementById("catalogBootstrapData");
  if (!node || !window.fetch) return;
  const embedded = node.textContent || "";
  const nativeFetch = window.fetch.bind(window);
  let served = false;
  window.fetch = function(input, init) {{
    if (!served) {{
      try {{
        const requestUrl = new URL(typeof input === "string" ? input : input.url, document.baseURI);
        const catalogUrl = new URL(document.body.dataset.catalog || "catalog.json", document.baseURI);
        if (requestUrl.origin === catalogUrl.origin && requestUrl.pathname === catalogUrl.pathname) {{
          served = true;
          return Promise.resolve(new Response(embedded, {{
            status: 200,
            headers: {{ "Content-Type": "application/json; charset=utf-8" }}
          }}));
        }}
      }} catch (_) {{}}
    }}
    return nativeFetch(input, init);
  }};
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
    print(
        f"[Catalog bootstrap] embedded_bytes={len(encoded.encode('utf-8'))} "
        f"inline_items={len(payload.get('items') or [])} total={payload.get('item_count')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
