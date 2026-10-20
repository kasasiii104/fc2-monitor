#!/usr/bin/env python3
import argparse
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import requests

import fc2_monitor as m

STATE_FILE = Path(os.environ.get("MISSAV_GUARD_STATE_FILE", "docs/missav_guard_state.json"))
AUDIT_LIMIT = max(1, int(os.environ.get("MISSAV_404_AUDIT_LIMIT", "250")))
ALIVE_TTL_SEC = max(3600, int(os.environ.get("MISSAV_ALIVE_TTL_SEC", str(7 * 86400))))
DEAD_TTL_SEC = max(3600, int(os.environ.get("MISSAV_DEAD_TTL_SEC", str(24 * 3600))))
UNKNOWN_TTL_SEC = max(300, int(os.environ.get("MISSAV_UNKNOWN_TTL_SEC", str(3600))))


def _num(value):
    text = str(value or "")
    match = re.search(r"(\d{5,8})", text)
    return match.group(1) if match else ""


def _code(item):
    code = str((item or {}).get("code") or "")
    num = _num((item or {}).get("code_num") or code)
    return (f"FC2-PPV-{num}", num) if num else (code, "")


def _correct_detail_destination(url, num):
    try:
        path = urlsplit(url or "").path
    except ValueError:
        return False
    path = re.sub(r"^/dm\d+(?=/)", "", path, count=1)
    return bool(re.fullmatch(rf"/(?:ja/)?fc2-ppv-{re.escape(num)}/?", path, re.I))


def _existence_request(url):
    """Check only response status/destination; never download the detail HTML body."""
    if m.source_in_cooldown(url):
        return {"status": 0, "final_url": url, "access_limited": True, "error": "source_cooldown"}
    headers = {**m.HEADERS, "Range": "bytes=0-0", "Accept": "text/html,*/*;q=0.8"}
    try:
        with requests.get(url, headers=headers, timeout=(4, 8), allow_redirects=True, stream=True) as res:
            status = int(res.status_code or 0)
            challenge = (res.headers.get("cf-mitigated") or "").lower() == "challenge"
            m.record_source_denial(url, status, challenge)
            return {
                "status": status,
                "final_url": str(res.url or url),
                "access_limited": bool(status in (401, 403, 429) or challenge),
                "error": None,
            }
    except requests.RequestException as exc:
        return {"status": 0, "final_url": url, "access_limited": False, "error": type(exc).__name__}


def apply_live_url(item, result):
    """Point the card/source link at the MissAV mirror actually verified alive."""
    live_url = str((result or {}).get("live_url") or "")
    if not live_url or (result or {}).get("status") != "alive":
        return False
    changed = False
    if item.get("url") != live_url:
        item["url"] = live_url
        changed = True
    sources = dict(item.get("sources") or {})
    if sources.get("MissAV") != live_url:
        sources["MissAV"] = live_url
        item["sources"] = sources
        changed = True
    return changed


class Guard:
    def __init__(self):
        raw = m.load_json(STATE_FILE, {"checks": {}})
        self.state = raw if isinstance(raw, dict) else {"checks": {}}
        self.checks = self.state.get("checks") if isinstance(self.state.get("checks"), dict) else {}
        self.state["checks"] = self.checks
        self.dirty = False

    def _ttl(self, status):
        if status == "alive":
            return ALIVE_TTL_SEC
        if status == "dead":
            return DEAD_TTL_SEC
        return UNKNOWN_TTL_SEC

    def probe(self, num, force=False):
        num = _num(num)
        code = f"FC2-PPV-{num}" if num else ""
        if not num:
            return {"status": "unknown", "reason": "invalid_code", "checked_at": m.now_ts()}
        previous = self.checks.get(code) if isinstance(self.checks.get(code), dict) else {}
        age = m.now_ts() - int(previous.get("checked_at") or 0)
        if previous and not force and age < self._ttl(previous.get("status")):
            return previous

        urls = m.missav_detail_urls(num)
        explicit_missing = 0
        attempts = 0
        result = None
        for url in urls:
            info = _existence_request(url)
            attempts += 1
            status = int(info.get("status") or 0)
            final_url = str(info.get("final_url") or url)
            if status in (200, 206) and _correct_detail_destination(final_url, num):
                result = {
                    "status": "alive",
                    "reason": "detail_200",
                    "http_status": status,
                    "live_url": final_url,
                }
                break
            if status in (404, 410):
                explicit_missing += 1
                continue
            if info.get("access_limited"):
                result = {"status": "unknown", "reason": "access_limited", "http_status": status}
                break
            if status in (200, 206):
                result = {"status": "unknown", "reason": "unexpected_destination", "http_status": status}
                break
            # One mirror can be temporarily unavailable. Try the remaining mirrors,
            # but classify as dead only if all configured hosts explicitly say 404/410.

        if result is None:
            if attempts and explicit_missing == attempts == len(urls):
                result = {"status": "dead", "reason": "all_missav_hosts_404", "http_status": 404}
            else:
                result = {"status": "unknown", "reason": "fetch_failed", "http_status": 0}
        result.update({"checked_at": m.now_ts(), "attempts": attempts})
        self.checks[code] = result
        self.dirty = True
        return result

    def save(self):
        if not self.dirty:
            return
        # Keep only bounded, useful existence evidence. 40k covers the current catalog
        # with room for future discoveries without letting state grow forever.
        rows = sorted(self.checks.items(), key=lambda pair: int((pair[1] or {}).get("checked_at") or 0), reverse=True)
        self.state["checks"] = dict(rows[:40000])
        self.state["updated_at"] = m.now_ts()
        m.save_json(STATE_FILE, self.state)
        self.checks = self.state["checks"]
        self.dirty = False


def filter_unseen(videos, existing_codes, guard):
    kept = []
    rejected_404 = rejected_unverified = relinked = 0
    for item in videos:
        code, num = _code(item)
        if not num or code in existing_codes:
            kept.append(item)
            continue
        result = guard.probe(num)
        if result.get("status") == "alive":
            relinked += int(apply_live_url(item, result))
            kept.append(item)
        elif result.get("status") == "dead":
            rejected_404 += 1
            print(f"[MissAV guard] reject_new={code} reason=404")
        else:
            # New works are fail-closed: if MissAV cannot be verified now, wait for a
            # later crawl instead of publishing a potentially dead primary link.
            rejected_unverified += 1
            print(f"[MissAV guard] defer_new={code} reason={result.get('reason')}")
    print(f"[MissAV guard] new_checked={len(videos)} kept={len(kept)} rejected_404={rejected_404} deferred={rejected_unverified} relinked={relinked}")
    return kept


def prune_saved(guard=None, limit=AUDIT_LIMIT):
    guard = guard or Guard()
    payload = m.load_json(m.DATA_FILE, {"items": []})
    items = payload.get("items") if isinstance(payload, dict) else []
    items = items if isinstance(items, list) else []
    if not items:
        guard.save()
        return {"checked": 0, "removed": 0, "relinked": 0, "remaining": 0}

    def last_checked(item):
        code, _ = _code(item)
        rec = guard.checks.get(code) if isinstance(guard.checks.get(code), dict) else {}
        return int(rec.get("checked_at") or 0)

    candidates = sorted((item for item in items if _code(item)[1]), key=last_checked)
    removed = set()
    checked = 0
    unknown = 0
    relinked = 0
    for item in candidates[:max(1, limit)]:
        code, num = _code(item)
        result = guard.probe(num, force=True)
        checked += 1
        if result.get("status") == "dead":
            removed.add(code)
            print(f"[MissAV guard] remove_saved={code} reason=404")
        elif result.get("status") == "alive":
            relinked += int(apply_live_url(item, result))
        elif result.get("status") == "unknown":
            unknown += 1
            # Once the source is denying access, further checks in this run would not
            # provide evidence and could only add load.
            if result.get("reason") == "access_limited":
                break

    if removed or relinked:
        payload["items"] = [item for item in items if item.get("code") not in removed]
        payload["updated_at"] = m.now_jst()
        m.save_json(m.DATA_FILE, payload)

    if removed:
        history = m.load_json(m.HISTORY_FILE, {"ids": []})
        ids = [code for code in (history.get("ids") or []) if code not in removed]
        history["ids"] = ids
        history["updated_at"] = m.now_jst()
        m.save_json(m.HISTORY_FILE, history)

    guard.state["last_audit"] = {
        "at": m.now_ts(), "checked": checked, "removed": len(removed),
        "relinked": relinked, "unknown": unknown, "catalog_before": len(items),
        "catalog_after": len(items) - len(removed),
    }
    guard.dirty = True
    guard.save()
    print(f"[MissAV guard] audit_checked={checked} removed={len(removed)} relinked={relinked} unknown={unknown} remaining={len(items)-len(removed)}")
    return {"checked": checked, "removed": len(removed), "relinked": relinked,
            "remaining": len(items) - len(removed)}


def run_guarded_monitor():
    guard = Guard()
    prune_saved(guard)
    current = m.load_json(m.DATA_FILE, {"items": []})
    existing_codes = {item.get("code") for item in (current.get("items") or []) if item.get("code")}
    original = m.get_latest_videos

    def guarded_latest():
        videos, crawl_state = original()
        filtered = filter_unseen(videos, existing_codes, guard)
        guard.save()
        return filtered, crawl_state

    m.get_latest_videos = guarded_latest
    try:
        return m.main()
    finally:
        m.get_latest_videos = original
        guard.save()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prune-only", action="store_true")
    args = parser.parse_args()
    if args.prune_only:
        prune_saved()
        return 0
    return run_guarded_monitor()


if __name__ == "__main__":
    raise SystemExit(main())
