#!/usr/bin/env python3
"""Walker discovery lane optimized for useful catalog growth.

Recent Walker entries keep the normal MissAV existence TTL so newly indexed
works can appear quickly. Historical entries that were already confirmed 404
on every MissAV mirror are not re-requested for 30 days; that lets each run
reach still-unchecked archive candidates instead of spending its budget on the
same dead records.
"""

import os
import time

import aggressive_discovery as base
import fc2_monitor as m
import missav_guard as mg

DISCOVERY_LIMIT = max(1, int(os.environ.get("H_WALKER_DISCOVERY_LIMIT", "2000")))
RECENT_PAGES = max(1, int(os.environ.get("H_WALKER_RECENT_PAGES", "4")))
DISCOVERY_BUDGET_SEC = max(60, int(os.environ.get("H_WALKER_DISCOVERY_BUDGET_SEC", "360")))
ARCHIVE_DEAD_RECHECK_SEC = max(86400, int(os.environ.get("H_WALKER_ARCHIVE_DEAD_RECHECK_SEC", str(30 * 86400))))


def cached_archive_dead(guard, code, recent, now):
    if recent:
        return False
    rec = guard.checks.get(code) if isinstance(guard.checks.get(code), dict) else {}
    return rec.get("status") == "dead" and now - int(rec.get("checked_at") or 0) < ARCHIVE_DEAD_RECHECK_SEC


def main():
    started = time.monotonic()
    payload = m.load_json(m.DATA_FILE, {"items": []})
    items = payload.get("items") or []
    existing = {item.get("code") for item in items if item.get("code")}
    stamp = m.now_jst()
    guard = mg.Guard()
    relinked = base.repair_primary_links(items)

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
    rejected_missav_404 = cached_dead_skipped = deferred_unverified = verified = 0
    budget_exhausted = False
    for code, rec in unseen:
        if len(added) >= DISCOVERY_LIMIT:
            break
        if time.monotonic() - started >= DISCOVERY_BUDGET_SEC:
            budget_exhausted = True
            print(f"[Walker discovery] budget_exhausted=true budget_sec={DISCOVERY_BUDGET_SEC}")
            break
        recent = code in recent_codes
        row = base.make_item(code, rec or {}, stamp, recent=recent)
        if row is None:
            rejected_title += 1
            continue

        if cached_archive_dead(guard, code, recent, now):
            cached_dead_skipped += 1
            rejected_missav_404 += 1
            continue

        num = code.split("-")[-1]
        existence = guard.probe(num)
        verified += 1
        if existence.get("status") == "dead":
            rejected_missav_404 += 1
            continue
        if existence.get("status") != "alive":
            deferred_unverified += 1
            if existence.get("reason") == "access_limited":
                break
            continue

        mg.apply_live_url(row, existence)
        items.append(row)
        existing.add(code)
        added.append(code)
        if recent:
            recent_added += 1
        else:
            archive_added += 1

    guard.save()
    elapsed_sec = round(time.monotonic() - started, 1)

    if added or relinked:
        m.sanitize_item_titles(items)
        payload["items"] = items
        payload["updated_at"] = stamp
        m.save_json(m.DATA_FILE, payload)

    if added:
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
    report["walker_missav_verified"] = verified
    report["walker_rejected_missav_404"] = rejected_missav_404
    report["walker_cached_dead_skipped"] = cached_dead_skipped
    report["walker_deferred_unverified"] = deferred_unverified
    report["walker_budget_exhausted"] = budget_exhausted
    report["walker_budget_sec"] = DISCOVERY_BUDGET_SEC
    report["walker_elapsed_sec"] = elapsed_sec
    report["primary_links_repaired"] = relinked
    report["total_after"] = len(items)
    m.save_json(m.CRAWL_FILE, crawl)

    print("[Walker discovery] " + m.json.dumps({
        "recent_pages": recent_pages,
        "recent_records": len(recent_records),
        "cache_records": len(cache),
        "unseen_before": len(unseen),
        "verified_on_missav": verified,
        "cached_dead_skipped": cached_dead_skipped,
        "rejected_missav_404": rejected_missav_404,
        "deferred_unverified": deferred_unverified,
        "added": len(added),
        "recent_added": recent_added,
        "archive_added": archive_added,
        "rejected_unresolved_title": rejected_title,
        "budget_exhausted": budget_exhausted,
        "budget_sec": DISCOVERY_BUDGET_SEC,
        "elapsed_sec": elapsed_sec,
        "primary_links_repaired": relinked,
        "remaining_unseen_estimate": max(0, len(unseen) - len(added) - rejected_title - rejected_missav_404),
        "catalog_total": len(items),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
