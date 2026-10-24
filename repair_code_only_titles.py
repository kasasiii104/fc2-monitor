#!/usr/bin/env python3
"""Repair bare FC2-PPV number titles before the general historical backlog.

Official FC2 article access can be eKYC-blocked on GitHub Actions. This lane first
uses already-saved canonical candidates and the Walker cache, then checks Japanese
MissAV detail pages. Authentication/search chrome is never accepted as a title.
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
GENERIC_PAGE_RE = re.compile(
    r"(?:パスワードを忘れ|パスワード|ログイン|サインイン|新規登録|会員登録|"
    r"検索結果|検索する|forgot\s+password|reset\s+password|sign\s*in|log\s*in|register|"
    r"access\s+denied|just\s+a\s+moment|cloudflare)",
    re.I,
)


def is_code_only(item):
    title = (item.get("title") or "").strip()
    code = (item.get("code") or "").strip()
    return not title or title == code or bool(CODE_ONLY_RE.fullmatch(title))


def is_generic_page_title(title):
    value = " ".join(str(title or "").split()).strip()
    if not value:
        return False
    return bool(GENERIC_PAGE_RE.search(value)) or value.casefold() in {
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
    for h in soup.find_all(["h1", "h2"], limit=4):
        raw_candidates.append(h.get_text(" ", strip=True))

    usable = []
    for raw in raw_candidates:
        title = valid_candidate(raw, item)
        if title:
            usable.append(title)
    if not usable:
        return ""
    # Prefer clear Japanese-script titles, then the most descriptive candidate.
    usable.sort(key=lambda x: (0 if re.search(r"[ぁ-んァ-ヶ]", x) else 1, -len(x)))
    return usable[0]


def fetch_title(item):
    num = str(item.get("code_num") or str(item.get("code", "")).split("-")[-1])
    urls = []
    for url in (
        item.get("url") or "",
        f"https://missav.ws/ja/fc2-ppv-{num}",
        f"https://missav.live/ja/fc2-ppv-{num}",
        f"https://missav.ai/ja/fc2-ppv-{num}",
    ):
        if url and url not in urls:
            urls.append(url)
    for url in urls:
        try:
            res = requests.get(url, headers=m.HEADERS, timeout=12, allow_redirects=True)
            text = res.text or ""
            if res.status_code != 200 or len(text) < 1500:
                continue
            # A login/challenge response can be HTTP 200. Require the resolved
            # URL or document itself to still identify the requested FC2 work.
            final_path = (urlsplit(str(res.url or url)).path or "").lower()
            if f"fc2-ppv-{num}" not in final_path and num not in text:
                continue
            title = candidate_from_html(text, item)
            if title:
                return title, str(res.url or url)
        except Exception:
            continue
    return "", ""


def main():
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

    # Undo any previous false repair that accidentally captured authentication
    # page chrome. Never leave a fake title in the public catalog.
    rolled_back = 0
    for item in items:
        if not is_generic_page_title(item.get("title") or ""):
            continue
        code = item.get("code") or ""
        if not code:
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
        code = item.get("code") or ""
        tried += 1
        attempts[code] = now

        # A discovery source may already have preserved a fuller title even if
        # the visible title fell back to the FC2 number. Use it before requests.
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
                title, source_url = fetch_title(item)
                source = "MissAV"
                if title:
                    web_repaired += 1
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
        "rolled_back_generic": rolled_back,
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
