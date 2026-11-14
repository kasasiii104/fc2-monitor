#!/usr/bin/env python3
import argparse
from copy import deepcopy
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
MISSAV_RETRY_LIMIT = max(0, int(os.environ.get("MISSAV_RETRY_LIMIT", "40")))
MISSAV_RETRY_TTL_SEC = max(0, int(os.environ.get("MISSAV_RETRY_TTL_SEC", str(14 * 86400))))
RETRY_QUEUE_MAX = 2000


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
    if m.run_deadline_reached("MissAV request", 1):
        return {"status": 0, "final_url": url, "error": "run_deadline"}
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


def is_official_confirmed(item):
    return item.get("source") == "FC2公式" or (
        int(item.get("fc2_market_last_success") or 0) > 0
        and not item.get("fc2_market_not_found")
    )


def invalidate_missav_link(item):
    sources = dict(item.get("sources") or {})
    _, num = _code(item)
    official_url = (item.get("fc2_market_url") or sources.get("FC2公式")
                    or f"https://adult.contents.fc2.com/article/{num}/?lang=ja")
    missav_url = m.missav_detail_urls(num)[0]
    sources["MissAV"] = missav_url
    sources["FC2公式"] = official_url
    item.update(url=missav_url, sources=sources, missav_dead=True,
                missav_dead_checked_at=m.now_ts())


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
    for key in ("missav_dead", "missav_dead_checked_at"):
        if key in item:
            del item[key]
            changed = True
    return changed


class Guard:
    def __init__(self):
        raw = m.load_json(STATE_FILE, {"checks": {}}, allow_non_dict=True)
        self.state = raw if isinstance(raw, dict) else {"checks": {}}
        self.checks = self.state.get("checks") if isinstance(self.state.get("checks"), dict) else {}
        self.state["checks"] = self.checks
        raw_queue = self.state.get("retry_queue") if isinstance(self.state.get("retry_queue"), dict) else {}
        # Drop malformed rows individually so one bad entry cannot crash the guarded run.
        self.retry_queue = {}
        for code, row in raw_queue.items():
            if (not re.fullmatch(r"FC2-PPV-[0-9]{5,8}", code)
                    or not isinstance(row, dict) or not isinstance(row.get("item"), dict)
                    or type(row.get("queued_at")) is not int
                    or type(row.get("attempts")) is not int):
                continue
            item = row["item"]
            # The snapshot is re-injected into latest, so its own code must match the key
            # and the fields used by link handling / main's merge must have usable types.
            if item.get("code") != code or _code(item)[0] != code:
                continue
            for key in ("title", "url", "source", "thumb", "duration"):
                if key in item and not isinstance(item[key], str):
                    item[key] = ""
            sources = item.get("sources")
            item["sources"] = ({k: v for k, v in sources.items() if isinstance(k, str) and isinstance(v, str)}
                               if isinstance(sources, dict) else {})
            if not isinstance(row.get("discovery_kind"), str):
                row["discovery_kind"] = "recent"
            if "last_tried_at" in row and type(row["last_tried_at"]) is not int:
                del row["last_tried_at"]
            self.retry_queue[code] = row
        self.state["retry_queue"] = self.retry_queue
        self.dirty = False
        self.recovered = set()

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
            if m.run_deadline_reached("MissAV probe", 1):
                return {"status": "unknown", "reason": "run_deadline"}
            info = _existence_request(url)
            if info.get("error") == "run_deadline":
                return {"status": "unknown", "reason": "run_deadline"}
            attempts += 1
            status = int(info.get("status") or 0)
            final_url = str(info.get("final_url") or url)
            if info.get("access_limited"):
                result = {"status": "unknown", "reason": "access_limited", "http_status": status}
                break
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


def _queue_retry(item, result, guard):
    code, _ = _code(item)
    row = guard.retry_queue.setdefault(code, {
        "item": deepcopy(item), "discovery_kind": item.get("discovery_kind") if isinstance(item.get("discovery_kind"), str) else "recent",
        "queued_at": m.now_ts(), "attempts": 0,
    })
    if result.get("reason") == "run_deadline":
        guard.dirty = True
        return
    row.update(last_tried_at=m.now_ts(), attempts=row["attempts"] + 1,
               last_reason=str(result.get("reason") or ""),
               last_status=str(result.get("status") or ""))
    guard.dirty = True


def _trim_retry_queue(guard):
    expired = 0
    rows = sorted(guard.retry_queue.items(), key=lambda pair: pair[1]["queued_at"])
    for code, row in rows:
        if m.now_ts() - row["queued_at"] > MISSAV_RETRY_TTL_SEC:
            del guard.retry_queue[code]
            expired += 1
            guard.dirty = True
            print(f"[MissAV guard] retry_expired={code}")
    overflow = max(0, len(guard.retry_queue) - RETRY_QUEUE_MAX)
    for code, _ in sorted(guard.retry_queue.items(), key=lambda pair: pair[1]["queued_at"])[:overflow]:
        del guard.retry_queue[code]
        guard.dirty = True
        print(f"[MissAV guard] retry_discarded={code} reason=queue_limit")
    return expired


def _mark_recovered(code, guard):
    guard.recovered.add(code)
    guard.retry_queue[code]["recovered_at"] = m.now_ts()
    guard.dirty = True


def filter_unseen(videos, existing_codes, guard):
    expired = _trim_retry_queue(guard)
    known = set(m.load_json(m.HISTORY_FILE, {"ids": []}).get("ids", []))

    def hold_rejected(code, item, result):
        if known and code not in known and guard.retry_queue[code]["discovery_kind"] != "archive":
            m.hold_notification(code, item, "missav_" + str(result.get("status") or "unknown"))

    # Preserve the first occurrence and the original discovery classification.
    by_code = {}
    for item in videos:
        code, _ = _code(item)
        by_code.setdefault(code, item)
    retry_checked = recovered = 0
    verified = set()
    rows = sorted(guard.retry_queue.items(),
                  key=lambda pair: pair[1].get("last_tried_at", pair[1]["queued_at"]))
    for code, row in rows:
        if code in by_code:
            by_code[code]["discovery_kind"] = row["discovery_kind"]
            continue
        item = deepcopy(row["item"])
        item["discovery_kind"] = row["discovery_kind"]
        if code in existing_codes:
            by_code[code] = item
        else:
            if retry_checked >= MISSAV_RETRY_LIMIT:
                continue
            row["last_tried_at"] = m.now_ts()
            guard.dirty = True
            result = guard.probe(_code(item)[1])
            retry_checked += 1
            if result.get("status") != "alive":
                _queue_retry(item, result, guard)
                hold_rejected(code, item, result)
                if result.get("reason") == "access_limited":
                    break
                continue
            apply_live_url(item, result)
            by_code[code] = item
            verified.add(code)

    kept = []
    rejected_404 = rejected_unverified = relinked = 0
    for code, item in by_code.items():
        if code in guard.retry_queue:
            item["discovery_kind"] = guard.retry_queue[code]["discovery_kind"]
        code, num = _code(item)
        if not num or code in existing_codes or code in verified:
            if code in guard.retry_queue:
                _mark_recovered(code, guard)
                recovered += 1
                m.release_notification(code, item)
            kept.append(item)
            continue
        if m.run_deadline_reached("MissAV filter", 1):
            _queue_retry(item, {"status": "unknown", "reason": "run_deadline"}, guard)
            continue
        if code in guard.retry_queue:
            guard.retry_queue[code]["last_tried_at"] = m.now_ts()
            guard.dirty = True
        result = guard.probe(num)
        if result.get("status") == "alive":
            relinked += int(apply_live_url(item, result))
            m.release_notification(code, item)
            kept.append(item)
            if code in guard.retry_queue:
                _mark_recovered(code, guard)
                recovered += 1
        elif result.get("status") == "dead":
            _queue_retry(item, result, guard)
            hold_rejected(code, item, result)
            rejected_404 += 1
            print(f"[MissAV guard] reject_new={code} reason=404")
        else:
            # New works stay fail-closed, but are queued for a later re-check
            # instead of being lost once the discovery cursor moves past them.
            _queue_retry(item, result, guard)
            hold_rejected(code, item, result)
            rejected_unverified += 1
            print(f"[MissAV guard] defer_new={code} reason={result.get('reason')}")
    print(f"[MissAV guard] new_checked={len(videos)} kept={len(kept)} rejected_404={rejected_404} deferred={rejected_unverified} relinked={relinked}")
    expired += _trim_retry_queue(guard)
    print(f"[MissAV guard] retry_checked={retry_checked} recovered={recovered} expired={expired} queued={len(guard.retry_queue)}")
    return kept


def prune_saved(guard=None, limit=AUDIT_LIMIT):
    guard = guard or Guard()
    payload = m.load_json(m.DATA_FILE, {"items": []})
    items = payload.get("items") if isinstance(payload, dict) else []
    items = items if isinstance(items, list) else []
    if not items:
        guard.save()
        return {"checked": 0, "removed": 0, "relinked": 0, "protected": 0, "remaining": 0}

    def last_checked(item):
        code, _ = _code(item)
        rec = guard.checks.get(code) if isinstance(guard.checks.get(code), dict) else {}
        return int(rec.get("checked_at") or 0)

    candidates = sorted((item for item in items if _code(item)[1]), key=last_checked)
    pending = m.load_notify_queue()
    pending_codes = {row["code"] for row in pending}
    removed = set()
    checked = 0
    unknown = 0
    relinked = 0
    protected = 0
    for item in candidates[:max(1, limit)]:
        if m.run_deadline_reached("MissAV prune", max(0, len(candidates) - checked)):
            break
        code, num = _code(item)
        result = guard.probe(num, force=True)
        checked += 1
        if result.get("status") == "dead":
            if is_official_confirmed(item):
                invalidate_missav_link(item)
                protected += 1
                print(f"[MissAV guard] protect_saved={code} reason=official_confirmed")
            else:
                if code in pending_codes:
                    m.hold_notification(code, item, "pruned_404")
                if code in pending_codes and code not in guard.retry_queue:
                    guard.retry_queue[code] = {
                        "item": deepcopy(item), "discovery_kind": "recent",
                        "queued_at": m.now_ts(), "attempts": 0,
                        "last_reason": "pruned_404", "last_status": "dead",
                    }
                    guard.dirty = True
                    guard.save()
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

    if removed or relinked or protected:
        payload["items"] = [item for item in items if _code(item)[0] not in removed]
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
        "relinked": relinked, "protected": protected, "unknown": unknown, "catalog_before": len(items),
        "catalog_after": len(items) - len(removed),
    }
    guard.dirty = True
    guard.save()
    print(f"[MissAV guard] audit_checked={checked} removed={len(removed)} relinked={relinked} protected={protected} unknown={unknown} remaining={len(items)-len(removed)}")
    return {"checked": checked, "removed": len(removed), "relinked": relinked,
            "protected": protected, "remaining": len(items) - len(removed)}


def run_guarded_monitor():
    m.validate_state_files()
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
        history = m.load_json(m.HISTORY_FILE, {"ids": []})
        for code in guard.recovered.intersection(history.get("ids") or []):
            if code in guard.retry_queue:
                del guard.retry_queue[code]
                guard.dirty = True
        guard.save()


def main():
    m.initialize_run_deadline()
    if m.run_deadline_reached("missav_guard.py"):
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--prune-only", action="store_true")
    args = parser.parse_args()
    if args.prune_only:
        m.validate_state_files()
        prune_saved()
        return 0
    return run_guarded_monitor()


if __name__ == "__main__":
    raise SystemExit(main())
