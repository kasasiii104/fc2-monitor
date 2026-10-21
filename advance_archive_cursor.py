#!/usr/bin/env python3
"""Advance a stalled MissAV archive cursor past a confirmed duplicate page.

The main crawler deliberately stops when an archive page returns the exact same
FC2-code fingerprint as a page already processed in the same/previous crawl.
That protects against a clamped or shifted listing, but an isolated duplicate
must not pin the archive cursor forever. This helper advances exactly one page,
records the skipped page for audit/revisit, and leaves all other failure modes
untouched.
"""

import fc2_monitor as m

MAX_RECORDED_SKIPS = 200


def advance(state):
    discovery = state.get("last_discovery") if isinstance(state, dict) else None
    discovery = discovery if isinstance(discovery, dict) else {}
    sources = discovery.get("sources") if isinstance(discovery.get("sources"), dict) else {}
    report = sources.get("missav") if isinstance(sources.get("missav"), dict) else {}

    if report.get("status") != "repeated_page":
        return False, "not_repeated"

    try:
        stopped = int(report.get("stopped_page") or 0)
        current = int(state.get("missav_page") or 0)
    except (TypeError, ValueError):
        return False, "invalid_state"
    if stopped < 1 or current < 1 or stopped != current:
        return False, "cursor_mismatch"

    entry = {
        "page": stopped,
        "at": m.now_jst(),
        "reason": "duplicate_fingerprint",
    }
    pages = state.get("missav_repeated_pages")
    pages = pages if isinstance(pages, list) else []
    pages = [row for row in pages if isinstance(row, dict) and int(row.get("page") or 0) != stopped]
    pages.append(entry)
    state["missav_repeated_pages"] = pages[-MAX_RECORDED_SKIPS:]
    state["missav_page"] = stopped + 1
    return True, "advanced"


def main():
    state = m.load_json(m.CRAWL_FILE, {})
    changed, reason = advance(state)
    if changed:
        m.save_json(m.CRAWL_FILE, state)
    print(f"[Archive cursor] changed={changed} reason={reason} next_page={state.get('missav_page', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
