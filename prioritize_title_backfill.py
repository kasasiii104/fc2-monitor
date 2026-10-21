#!/usr/bin/env python3
"""Promote due unresolved-title records into the next official FC2 backfill batch.

The main metadata queue also contains rating/review-only work. This helper does
not bypass retry cooldowns; it only marks title-repair records that are already
due as untried so the existing 500-request official batch selects them first.
"""

import re

import fc2_monitor as m

PROMOTE_LIMIT = max(1, m.BACKFILL_OFFICIAL_LIMIT)
CODE_ONLY_RE = re.compile(r"^FC2-PPV-\d{6,8}$", re.I)


def code_only(item):
    title = (item.get("title") or "").strip()
    code = (item.get("code") or "").strip()
    return not title or title == code or bool(CODE_ONLY_RE.fullmatch(title))


def priority(item):
    source_title = item.get("source_title") or ""
    chinese_origin = m.looks_chinese_title(source_title) or m.looks_chinese_title(item.get("title") or "")
    return (
        0 if chinese_origin or code_only(item) else 1,
        -int(item.get("code_num") or str(item.get("code", "")).split("-")[-1] or 0),
    )


def main():
    payload = m.load_json(m.DATA_FILE, {"items": []})
    items = payload.get("items") or []
    state = m.load_fc2_market_state(items)
    attempts = state[m.BACKFILL_ATTEMPTS_KEY]
    now = m.now_ts()

    due = []
    for item in items:
        code = item.get("code") or ""
        rec = attempts.get(code) or {}
        if not code or item.get("fc2_market_not_found") or rec.get("status") == "not_found":
            continue
        if not m.needs_jp_title(item.get("title", "")):
            continue
        if rec.get("last_attempt") and not m.fc2_market_due(item, state, now):
            continue
        due.append(item)

    due.sort(key=priority)
    promoted = due[:PROMOTE_LIMIT]
    chinese_or_code = 0
    for item in promoted:
        code = item.get("code") or ""
        rec = attempts.get(code)
        if not isinstance(rec, dict):
            rec = {}
            attempts[code] = rec
        if priority(item)[0] == 0:
            chinese_or_code += 1
        # Keep status/failure history and next_retry_at. These records are only
        # selected here when their existing retry window is already due.
        rec["last_attempt"] = 0

    state[m.BACKFILL_ATTEMPTS_KEY] = attempts
    m.save_json(m.FETCH_STATE_FILE, state)
    print(
        f"[Title priority] due={len(due)} promoted={len(promoted)} "
        f"chinese_or_code={chinese_or_code} limit={PROMOTE_LIMIT}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
