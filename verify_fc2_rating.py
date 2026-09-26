#!/usr/bin/env python3
import re
import fc2_monitor as m

CODES = ["4943439", "4935067", "4940935", "4937237"]

for code in CODES:
    url = f"https://adult.contents.fc2.com/article/{code}/?lang=ja"
    info = m.fetch_page(url)
    print(f"\n=== VERIFY FC2-PPV-{code} status={info.get('status')} final={info.get('final_url')} ===")
    if not info.get("soup"):
        continue
    soup = info["soup"]
    text = soup.get_text(" ", strip=True)
    print("parser_rating=", m.extract_fc2_market_rating(soup, text))
    print("parser_reviews=", m.extract_fc2_market_review_count(soup, text))
    # Print only short diagnostic snippets around rating/review-related nodes.
    seen = set()
    for node in soup.find_all(True):
        attrs = " ".join(f"{k}={v}" for k,v in node.attrs.items())
        nt = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
        hay = (attrs + " " + nt).lower()
        if any(k in hay for k in ["rating", "review", "評価", "レビュー", "star"]):
            sig = (node.name, attrs[:180], nt[:260])
            if sig in seen:
                continue
            seen.add(sig)
            print("NODE", node.name, attrs[:180], "TEXT=", nt[:260])
            if len(seen) >= 30:
                break
