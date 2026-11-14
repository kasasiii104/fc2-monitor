#!/usr/bin/env python3
"""Repair bare FC2-PPV number titles before the general historical backlog.

Official FC2 article access can be eKYC-blocked on GitHub Actions. This lane first
uses already-saved canonical candidates and the Walker cache, then checks Japanese
MissAV detail pages. MissAV page chrome or ambiguous non-kana text is never
accepted as a title.
"""

import re
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

import fc2_monitor as m
import walker_title_repair as walker

LIMIT = 40
RETRY_SEC = 4 * 3600
STATE_KEY = "code_only_title_repair_v1"
CODE_ONLY_RE = re.compile(r"^(?:FC2[-_ ]?PPV[-_ ]?)?\d{5,8}$", re.I)
KANA_RE = re.compile(r"[ぁ-んァ-ヶ]")
GENERIC_PAGE_RE = re.compile(
    r"(?:パスワードを忘れ|パスワード|ログイン|サインイン|新規登録|会員登録|無料登録|"
    r"検索結果|検索する|forgot\s+password|reset\s+password|sign\s*in|log\s*in|register|"
    r"access\s+denied|just\s+a\s+moment|cloudflare)",
    re.I,
)


def is_code_only(item):
    title = (item.get("title") or "").strip()
    code = (item.get("code") or "").strip()
    return not title or title == code or bool(CODE_ONLY_RE.fullmatch(title))


def is_generic_page_title(title):
    trim_chars = " \t\r\n-|｜–—:：・.…!！"
    value = " ".join(str(title or "").split()).strip(trim_chars)
    site_names = {"missav", "fc2", "fc2-ppv", "javdb", "fc2cmadb", "fc2ウォーカー"}
    separators = list(re.finditer(r"\s*[|｜–—:：・-]\s*", value))
    # Prefer the longest site name so FC2-PPV's hyphen is not a boundary.
    separators.sort(key=lambda match: max(
        len(value[:match.start()]) if value[:match.start()].casefold() in site_names else 0,
        len(value[match.end():]) if value[match.end():].casefold() in site_names else 0,
    ), reverse=True)
    for separator in separators:
        first, last = value[:separator.start()], value[separator.end():]
        if first.casefold() in site_names:
            value = last.strip(trim_chars)
            break
        if last.casefold() in site_names:
            value = first.strip(trim_chars)
            break
    if not value:
        return False
    return bool(GENERIC_PAGE_RE.fullmatch(value)) or value.casefold() in {
        "missav", "fc2", "fc2-ppv", "home", "search"
    }


def valid_candidate(raw, item):
    code = item.get("code") or ""
    num = str(item.get("code_num") or code.split("-")[-1])
    title = m.clean_title(raw or "", num)
    if not title or m.is_invalid_title(title) or m.looks_chinese_title(title):
        return ""
    if CODE_ONLY_RE.fullmatch(title) or is_generic_page_title(title):
        return ""
    return title


def candidate_from_html(text, item):
    soup = BeautifulSoup(text or "", "html.parser")
    raw_candidates = []
    for node in (
        soup.find("meta", attrs={"property": "og:title"}),
        soup.find("meta", attrs={"name": "twitter:title"}),
    ):
        if node and node.get("content"):
            raw_candidates.append(node.get("content"))
    if soup.title:
        raw_candidates.append(soup.title.get_text(" ", strip=True))
    for h in soup.find_all(["h1"], limit=3):
        raw_candidates.append(h.get_text(" ", strip=True))

    usable = []
    for raw in raw_candidates:
        title = valid_candidate(raw, item)
        # MissAV can return HTTP-200 login/chrome pages at a work URL. For this
        # untrusted fallback lane, require Japanese kana. Kanji-only canonical
        # titles must come from trusted Walker/official/saved metadata instead.
        if title and KANA_RE.search(title):
            usable.append(title)
    if not usable:
        return ""
    usable.sort(key=len, reverse=True)
    return usable[0]


def fetch_title(item, state=None):
    num = str(item.get("code_num") or str(item.get("code", "")).split("-")[-1])
    if state is None:
        state = m.load_json(m.FETCH_STATE_FILE, {})
    urls = []
    for url in (
        item.get("url") or "",
        f"https://missav.ws/ja/fc2-ppv-{num}",
        f"https://missav.live/ja/fc2-ppv-{num}",
        f"https://missav.ai/ja/fc2-ppv-{num}",
    ):
        if urlsplit(url).hostname == m.FC2_MARKET_HOST and m.fc2_market_blocked(state, m.now_ts()):
            continue
        if url and url not in urls:
            urls.append(url)
    with m.failure_summary() as failures:
        for url in urls:
            if m.run_deadline_reached("code-only request", 1):
                return None, None
            try:
                res = requests.get(url, headers=m.HEADERS, timeout=12, allow_redirects=True)
                if urlsplit(url).hostname == m.FC2_MARKET_HOST and res.status_code in (403, 429):
                    m.record_fc2_host_stop(state, res.status_code, res.headers.get("Retry-After"), m.now_ts())
                    m.log_fc2_carryover(state, 1)
                    continue
                text = res.text or ""
                if res.status_code != 200 or len(text) < 1500:
                    continue
                final_path = (urlsplit(str(res.url or url)).path or "").lower()
                if f"fc2-ppv-{num}" not in final_path and num not in text:
                    continue
                title = candidate_from_html(text, item)
                if title:
                    return title, str(res.url or url)
            except Exception as exc:
                failures[("code-only title", type(exc).__name__)] += 1
                continue
    return "", ""


def main():
    m.initialize_run_deadline()
    if m.run_deadline_reached("repair_code_only_titles.py"):
        return 0
    payload = m.load_json(m.DATA_FILE, {"items": []})
    items = payload.get("items") if isinstance(payload, dict) else []
    if not isinstance(items, list):
        items = []
    state = m.load_json(m.FETCH_STATE_FILE, {})
    repair_state = state.get(STATE_KEY) if isinstance(state.get(STATE_KEY), dict) else {}
    attempts = repair_state.get("attempts") if isinstance(repair_state.get("attempts"), dict) else {}
    walker_cache = state.get(m.H_WALKER_CACHE_KEY)
    walker_cache = walker_cache if isinstance(walker_cache, dict) else {}
    now = m.now_ts()

    # Undo false repairs only when this lane's attempt and a repair source remain.
    # MissAV repairs containing no kana may also be Chinese or site chrome.
    rolled_back = 0
    for item in items:
        code = item.get("code") or ""
        title = item.get("title") or ""
        if not code:
            continue
        prior_repair = code in attempts and item.get("title_source") in {
            "取得元", "FC2ウォーカー", "MissAV"
        }
        if not prior_repair:
            continue
        prior_missav_attempt = item.get("title_source") == "MissAV" and code in attempts
        ambiguous_missav = prior_missav_attempt and not KANA_RE.search(title)
        if not is_generic_page_title(title) and not ambiguous_missav:
            continue
        item["title"] = code
        item["title_source"] = ""
        attempts.pop(code, None)
        rolled_back += 1

    pending = [item for item in items if is_code_only(item)]
    pending.sort(key=lambda item: (
        0 if item.get("is_new") else 1,
        -(int(item.get("code_num") or str(item.get("code", "")).split("-")[-1] or 0)),
    ))
    due = [item for item in pending if now - int(attempts.get(item.get("code") or "") or 0) >= RETRY_SEC]

    repaired = stored_repaired = walker_repaired = web_repaired = tried = 0
    examples = []
    for item in due[:LIMIT]:
        if m.run_deadline_reached("code-only repair", max(0, len(due) - tried)):
            break
        code = item.get("code") or ""
        title = valid_candidate(item.get("source_title") or "", item)
        source_url = ""
        source = "取得元"
        if title:
            stored_repaired += 1
        else:
            rec = walker_cache.get(code)
            title = walker.valid_walker_title((rec or {}).get("title"), item) if isinstance(rec, dict) else ""
            source_url = (rec or {}).get("source_url") if isinstance(rec, dict) else ""
            source = "FC2ウォーカー"
            if title:
                walker_repaired += 1
            else:
                title, source_url = fetch_title(item, state)
                source = "MissAV"
                if title:
                    web_repaired += 1
        if title is None:
            break
        tried += 1
        attempts[code] = now
        if not title:
            continue

        old = item.get("title") or code
        item["title"] = title
        item["title_source"] = source
        if source_url:
            item.setdefault("sources", {})[source] = source_url
        repaired += 1
        if len(examples) < 10:
            examples.append({"code": code, "from": old[:80], "to": title[:120], "source": source})

    if repaired or rolled_back:
        m.save_json(m.DATA_FILE, payload)
    live = {item.get("code") for item in items if item.get("code")}
    attempts = {code: ts for code, ts in attempts.items() if code in live}
    stats = {
        "last_run": now,
        "rolled_back_generic_or_ambiguous": rolled_back,
        "pending_before": len(pending),
        "due": len(due),
        "tried": tried,
        "repaired": repaired,
        "stored_repaired": stored_repaired,
        "walker_repaired": walker_repaired,
        "web_repaired": web_repaired,
        "pending_after": sum(is_code_only(item) for item in items),
        "examples": examples,
        "attempts": attempts,
    }
    state[STATE_KEY] = stats
    m.save_json(m.FETCH_STATE_FILE, state)
    print(f"[Code-only title repair] stats={{{', '.join(f'{k}={v!r}' for k, v in stats.items() if k not in ('attempts', 'examples'))}}}")
    for example in examples:
        print(f"[Code-only title repaired] {example['code']} -> {example['to'][:100]} ({example['source']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
