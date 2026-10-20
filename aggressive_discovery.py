#!/usr/bin/env python3
import os
import re

import fc2_monitor as m

DISCOVERY_LIMIT = max(1, int(os.environ.get("H_WALKER_DISCOVERY_LIMIT", "2000")))
RECENT_PAGES = max(1, int(os.environ.get("H_WALKER_RECENT_PAGES", "4")))


def make_item(code, rec, stamp, recent=False):
    num = code.split("-")[-1]
    if not re.fullmatch(r"\d{5,8}", num):
        return None
    title = m.clean_title(rec.get("title") or "", num)
    # Never inflate the catalog with number-only or unresolved Chinese titles.
    if not title or m.needs_jp_title(title):
        return None

    product_url = f"https://adult.contents.fc2.com/article/{num}/?lang=ja"
    walker_url = rec.get("source_url") or (m.H_WALKER_URL + "/")
    row = m.enrich(num, title, "FC2ウォーカー", product_url)
    row.update({
        "sources": {"FC2ウォーカー": walker_url, "FC2公式": product_url},
        "source_label": "FC2ウォーカー / FC2公式",
        "title_source": "FC2ウォーカー",
        "hwalker_url": walker_url,
        "hwalker_checked_at": int(rec.get("checked_at") or m.now_ts()),
        "fc2_market_url": product_url,
        "fc2_market_not_found": False,
        "discovery_kind": "recent" if recent else "archive",
        "is_new": bool(recent),
        # Archive backfill must not jump to the top of the normal new-sort feed.
        "first_seen": stamp if recent else "",
        "last_seen": stamp if recent else "",
    })
    rating = rec.get("rating")
    reviews = rec.get("review_count")
    if isinstance(rating, (int, float)) and 0 <= rating <= 5:
        row["fc2_rating"] = float(rating)
        row["fc2_rating_source"] = "FC2ウォーカー"
    if isinstance(reviews, int) and reviews >= 0:
        row["fc2_review_count"] = reviews
        row.setdefault("fc2_rating_source", "FC2ウォーカー")
    return row


def main():
    payload = m.load_json(m.DATA_FILE, {"items": []})
    items = payload.get("items") or []
    existing = {item.get("code") for item in items if item.get("code")}
    stamp = m.now_jst()

    # Always sample the Walker head as an independent recent lane. The normal
    # Walker cursor can be thousands of pages deep while this keeps new works visible.
    recent_records = {}
    try:
        recent_records, _, recent_pages = m.scrape_hwalker_market(
            start_url=m.H_WALKER_URL + "/", page_limit=RECENT_PAGES
        )
    except Exception as exc:
        recent_pages = 0
        print(f"[Walker discovery] recent_error={type(exc).__name__}")

    state = m.load_json(m.FETCH_STATE_FILE, {})
    cache = state.get(m.H_WALKER_CACHE_KEY)
    cache = cache if isinstance(cache, dict) else {}

    # Latest head records win over an older cached copy of the same code.
    combined = dict(cache)
    now = m.now_ts()
    for code, rec in recent_records.items():
        combined[code] = {**rec, "checked_at": now}
    recent_codes = set(recent_records)

    unseen = [(code, rec) for code, rec in combined.items() if code not in existing]
    unseen.sort(key=lambda pair: (
        pair[0] not in recent_codes,
        -int((pair[1] or {}).get("checked_at") or 0),
        -int(pair[0].split("-")[-1]) if pair[0].split("-")[-1].isdigit() else 0,
    ))

    added = []
    recent_added = archive_added = rejected_title = 0
    for code, rec in unseen:
        if len(added) >= DISCOVERY_LIMIT:
            break
        row = make_item(code, rec or {}, stamp, recent=code in recent_codes)
        if row is None:
            rejected_title += 1
            continue
        items.append(row)
        existing.add(code)
        added.append(code)
        if code in recent_codes:
            recent_added += 1
        else:
            archive_added += 1

    if added:
        m.sanitize_item_titles(items)
        payload["items"] = items
        payload["updated_at"] = stamp
        m.save_json(m.DATA_FILE, payload)

        # Archive discoveries are historical and must never trigger a later
        # "new work" notification merely because another source sees them.
        # Recent Walker-head discoveries remain eligible for a later notification
        # when an independent recent source confirms them.
        history = m.load_json(m.HISTORY_FILE, {"ids": []})
        ids = set(history.get("ids") or [])
        ids.update(code for code in added if code not in recent_codes)
        m.save_json(m.HISTORY_FILE, {"updated_at": stamp, "ids": sorted(ids)})

        crawl = m.load_json(m.CRAWL_FILE, {})
        report = crawl.setdefault("last_discovery", {})
        report["walker_recent_added"] = recent_added
        report["walker_archive_added"] = archive_added
        report["walker_discovery_added"] = len(added)
        report["walker_unseen_before"] = len(unseen)
        report["total_after"] = len(items)
        m.save_json(m.CRAWL_FILE, crawl)

    print("[Walker discovery] " + m.json.dumps({
        "recent_pages": recent_pages,
        "recent_records": len(recent_records),
        "cache_records": len(cache),
        "unseen_before": len(unseen),
        "added": len(added),
        "recent_added": recent_added,
        "archive_added": archive_added,
        "rejected_unresolved_title": rejected_title,
        "remaining_unseen_estimate": max(0, len(unseen) - len(added) - rejected_title),
        "catalog_total": len(items),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
