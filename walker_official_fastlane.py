#!/usr/bin/env python3
"""Fast, authoritative archive discovery from the Walker cache.

The normal discovery lane verifies MissAV first and is intentionally cautious.
This lane targets Walker records that are still absent from the catalog and
checks FC2 official directly in small concurrent batches. Only FC2-confirmed
articles are added, so throughput can increase without publishing unverified
Walker rows or dead MissAV links.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import re
import time

import aggressive_discovery as base
import fc2_monitor as m

CHECK_LIMIT = max(1, int(os.environ.get("WALKER_OFFICIAL_FAST_LIMIT", "240")))
WORKERS = max(1, min(8, int(os.environ.get("WALKER_OFFICIAL_FAST_WORKERS", "4"))))
BATCH_SIZE = max(WORKERS, int(os.environ.get("WALKER_OFFICIAL_FAST_BATCH", "24")))
NOT_FOUND_RETRY_SEC = max(86400, int(os.environ.get("WALKER_OFFICIAL_NOT_FOUND_RETRY_SEC", str(30 * 86400))))
FAILED_RETRY_SEC = max(3600, int(os.environ.get("WALKER_OFFICIAL_FAILED_RETRY_SEC", str(12 * 3600))))
CHECK_KEY = "walker_official_discovery_checks"


def due(checks, code, now):
    rec = checks.get(code) if isinstance(checks.get(code), dict) else {}
    checked = int(rec.get("checked_at") or 0)
    status = rec.get("status") or ""
    delay = NOT_FOUND_RETRY_SEC if status == "not_found" else FAILED_RETRY_SEC
    return not checked or now - checked >= delay


def fetch_one(code, row):
    num = code.split("-")[-1]
    try:
        meta = m.fetch_fc2_market(num) or {"outcome": "failed", "reason": "empty_response"}
    except Exception as exc:
        meta = {"outcome": "failed", "reason": type(exc).__name__}
    return code, row, num, meta


def restore_official_primary_links(items):
    """Undo generic MissAV relinking for rows confirmed only by FC2 official."""
    changed = 0
    for item in items:
        if item.get("source") != "FC2公式":
            continue
        code = item.get("code") or ""
        num = str(item.get("code_num") or code.split("-")[-1])
        if not re.fullmatch(r"\d{5,8}", num):
            continue
        sources = dict(item.get("sources") or {})
        official = item.get("fc2_market_url") or sources.get("FC2公式") or f"https://adult.contents.fc2.com/article/{num}/?lang=ja"
        if item.get("url") != official:
            item["url"] = official
            changed += 1
        if sources.get("FC2公式") != official:
            sources["FC2公式"] = official
            item["sources"] = sources
            changed += 1
    return changed


def main():
    started = time.monotonic()
    payload = m.load_json(m.DATA_FILE, {"items": []})
    items = payload.get("items") if isinstance(payload, dict) else []
    items = items if isinstance(items, list) else []
    existing = {item.get("code") for item in items if item.get("code")}
    state = m.load_json(m.FETCH_STATE_FILE, {})
    cache = state.get(m.H_WALKER_CACHE_KEY)
    cache = cache if isinstance(cache, dict) else {}
    checks = state.get(CHECK_KEY)
    checks = checks if isinstance(checks, dict) else {}
    now = m.now_ts()
    stamp = m.now_jst()

    relinked = restore_official_primary_links(items)
    unseen = [(code, rec) for code, rec in cache.items() if code not in existing]
    unseen.sort(key=lambda pair: (
        -int((pair[1] or {}).get("checked_at") or 0),
        -int(pair[0].split("-")[-1]) if pair[0].split("-")[-1].isdigit() else 0,
    ))

    candidates = []
    rejected_title = retry_waiting = 0
    for code, rec in unseen:
        if len(candidates) >= CHECK_LIMIT:
            break
        if not due(checks, code, now):
            retry_waiting += 1
            continue
        row = base.make_item(code, rec or {}, stamp, recent=False)
        if row is None:
            rejected_title += 1
            continue
        candidates.append((code, row))

    added = []
    checked = success = not_found = failed = blocked = 0
    stop = False
    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="fc2-official") as pool:
        for offset in range(0, len(candidates), BATCH_SIZE):
            if stop:
                break
            batch = candidates[offset:offset + BATCH_SIZE]
            futures = [pool.submit(fetch_one, code, row) for code, row in batch]
            for future in as_completed(futures):
                code, row, num, meta = future.result()
                checked += 1
                outcome = meta.get("outcome") or "failed"
                checks[code] = {
                    "checked_at": now,
                    "status": "alive" if outcome == "success" else "not_found" if outcome == "not_found" else "failed",
                    "reason": meta.get("reason") or meta.get("blocked") or "",
                }
                if outcome == "success":
                    m.apply_fc2_market_metadata(row, meta, now)
                    official = meta.get("fc2_market_url") or f"https://adult.contents.fc2.com/article/{num}/?lang=ja"
                    row.setdefault("sources", {})["FC2公式"] = official
                    row["url"] = official
                    row["source"] = "FC2公式"
                    row["source_label"] = "FC2公式 / FC2ウォーカー"
                    row["is_new"] = False
                    row["first_seen"] = ""
                    row["last_seen"] = ""
                    items.append(row)
                    existing.add(code)
                    added.append(code)
                    success += 1
                elif outcome == "not_found":
                    not_found += 1
                elif outcome == "blocked":
                    blocked += 1
                    stop = True
                else:
                    failed += 1
            # Stop quickly if FC2 starts denying the batch instead of making the
            # remaining requests more aggressive.
            if blocked:
                stop = True

    # Keep shared evidence bounded and reusable by efficient_discovery.py.
    checks = {
        code: rec for code, rec in checks.items()
        if isinstance(rec, dict) and now - int(rec.get("checked_at") or 0) < NOT_FOUND_RETRY_SEC
    }
    state[CHECK_KEY] = checks
    state["walker_official_fastlane_stats"] = {
        "last_run": now,
        "cache_records": len(cache),
        "unseen_before": len(unseen),
        "candidates": len(candidates),
        "checked": checked,
        "success": success,
        "not_found": not_found,
        "failed": failed,
        "blocked": blocked,
        "added": len(added),
        "retry_waiting": retry_waiting,
        "rejected_unresolved_title": rejected_title,
        "workers": WORKERS,
        "elapsed_sec": round(time.monotonic() - started, 1),
    }
    m.save_json(m.FETCH_STATE_FILE, state)

    if added or relinked:
        m.sanitize_item_titles(items)
        payload["items"] = items
        payload["updated_at"] = stamp
        m.save_json(m.DATA_FILE, payload)

    if added:
        history = m.load_json(m.HISTORY_FILE, {"ids": []})
        ids = set(history.get("ids") or [])
        ids.update(added)
        m.save_json(m.HISTORY_FILE, {"updated_at": stamp, "ids": sorted(ids)})

    crawl = m.load_json(m.CRAWL_FILE, {})
    report = crawl.setdefault("last_discovery", {})
    report["walker_official_fastlane_added"] = len(added)
    report["walker_official_fastlane_checked"] = checked
    report["walker_official_fastlane_success"] = success
    report["walker_official_fastlane_failed"] = failed
    report["walker_official_fastlane_not_found"] = not_found
    report["walker_official_fastlane_relinked"] = relinked
    report["total_after"] = len(items)
    m.save_json(m.CRAWL_FILE, crawl)

    print("[Walker official fastlane] " + m.json.dumps(state["walker_official_fastlane_stats"], ensure_ascii=False))
    print(f"[Walker official fastlane] total_after={len(items)} relinked={relinked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
