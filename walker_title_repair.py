#!/usr/bin/env python3
"""Repair unresolved FC2 titles from the trusted Content Market Walker cache."""

import re

import fc2_monitor as m

GENERIC_WALKER_TITLES = {
    "FC2コンテンツマーケット",
    "FC2 Content Market",
    "商品レビュー",
    "商品説明",
    "販売者情報",
}


def clean_current(item):
    code = item.get("code") or ""
    num = str(item.get("code_num") or code.split("-")[-1])
    return m.clean_title(item.get("title") or "", num)


def needs_walker_repair(item):
    """Chinese/code-only/unresolved titles should be replaced when Walker matches."""
    title = clean_current(item)
    if not title:
        return True
    if m.looks_chinese_title(title):
        return True
    return m.needs_jp_title(title)


def valid_walker_title(raw, item):
    code = item.get("code") or ""
    num = str(item.get("code_num") or code.split("-")[-1])
    title = m.clean_title(raw or "", num)
    if not title or m.is_invalid_title(title) or title in GENERIC_WALKER_TITLES:
        return ""
    # Walker identifies the product by its direct FC2 product link. Trust that
    # formal product title even when it is kanji-only, but never replace one
    # unresolved title with another clearly Chinese title or a bare number.
    if m.looks_chinese_title(title):
        return ""
    if re.fullmatch(r"(?:FC2[-_ ]?PPV[-_ ]?)?\d{5,8}", title, flags=re.I):
        return ""
    return title


def repair_titles(items, cache):
    stats = {
        "items": len(items),
        "pending_before": 0,
        "cache_matches": 0,
        "repaired": 0,
        "rejected_walker_title": 0,
        "pending_after": 0,
        "chinese_before": 0,
        "code_only_before": 0,
        "examples": [],
    }
    for item in items:
        if not needs_walker_repair(item):
            continue
        stats["pending_before"] += 1
        current = clean_current(item)
        if not current:
            stats["code_only_before"] += 1
        if m.looks_chinese_title(current):
            stats["chinese_before"] += 1

        code = item.get("code") or ""
        rec = cache.get(code)
        if not isinstance(rec, dict):
            continue
        stats["cache_matches"] += 1
        title = valid_walker_title(rec.get("title"), item)
        if not title:
            stats["rejected_walker_title"] += 1
            continue

        old_title = item.get("title") or code
        item["title"] = title
        item["title_source"] = "FC2ウォーカー"
        item["hwalker_url"] = rec.get("source_url") or m.H_WALKER_URL
        if rec.get("checked_at"):
            item["hwalker_checked_at"] = int(rec["checked_at"])
        stats["repaired"] += 1
        if len(stats["examples"]) < 10:
            stats["examples"].append({"code": code, "from": old_title[:80], "to": title[:120]})

    stats["pending_after"] = sum(needs_walker_repair(item) for item in items)
    return stats


def main():
    payload = m.load_json(m.DATA_FILE, {"items": []})
    items = payload.get("items") if isinstance(payload, dict) else []
    if not isinstance(items, list):
        items = []
    state = m.load_json(m.FETCH_STATE_FILE, {})
    cache = state.get(m.H_WALKER_CACHE_KEY)
    cache = cache if isinstance(cache, dict) else {}

    stats = repair_titles(items, cache)
    if stats["repaired"]:
        m.save_json(m.DATA_FILE, payload)
    state["walker_canonical_title_repair"] = {"last_run": m.now_ts(), **stats}
    m.save_json(m.FETCH_STATE_FILE, state)
    print(f"[Walker title repair] stats={stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
