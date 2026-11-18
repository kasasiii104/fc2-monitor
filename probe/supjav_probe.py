"""One-off probe: can a real Chromium on GitHub Actions reach supjav listings?

Prints, for each target URL: final URL, page title, whether Cloudflare
block/challenge text is present, and how many FC2PPV codes appear in the
rendered HTML. Read-only; no state is written anywhere.
"""
import re
import sys

from playwright.sync_api import sync_playwright

TARGETS = [
    "https://supjav.com/ja/category/maker/fc2ppv",
    "https://supjav.com/ja/tag/fc2ppv?asgtbndr=1",
]

BLOCK_MARKERS = ["you have been blocked", "attention required", "just a moment",
                 "performing security verification", "セキュリティ検証"]


def probe(page, url):
    print(f"== {url}", flush=True)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"goto error: {e}", flush=True)
    # give a JS challenge time to resolve, if it is resolvable at all
    for _ in range(6):
        page.wait_for_timeout(5000)
        html = page.content()
        low = html.lower()
        if "fc2ppv" in low and not any(m in low for m in BLOCK_MARKERS):
            break
        if "you have been blocked" in low:
            break
    html = page.content()
    low = html.lower()
    codes = set(re.findall(r"fc2ppv[-_ ]?(\d{6,8})", low))
    print(f"final_url: {page.url}")
    print(f"title: {page.title()}")
    print(f"blocked: {'you have been blocked' in low}")
    print(f"challenge: {any(m in low for m in BLOCK_MARKERS)}")
    print(f"html_bytes: {len(html)}")
    print(f"fc2ppv_codes_found: {len(codes)}")
    print(f"sample_codes: {sorted(codes)[:5]}")
    print(flush=True)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"),
            locale="ja-JP",
        )
        page = context.new_page()
        for url in TARGETS:
            probe(page, url)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
