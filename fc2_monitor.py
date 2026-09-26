#!/usr/bin/env python3
import html
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, quote_plus

import requests
from bs4 import BeautifulSoup

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
MISSAV_URL = os.environ.get("MISSAV_URL", "https://missav.live/ja/search/fc2-ppv")
SUPJAV_URL = os.environ.get("SUPJAV_URL", "https://supjav.com/ja/category/maker/fc2ppv")
CODE_RE = re.compile(r"FC2[-_ ]?PPV[-_ ]?(\d{6,8})", re.I)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.8",
}
DATA_FILE = Path(os.environ.get("DATA_FILE", "docs/data.json"))
HISTORY_FILE = Path(os.environ.get("HISTORY_FILE", "docs/notified_ids.json"))
HTML_FILE = Path(os.environ.get("HTML_FILE", "docs/index.html"))
CRAWL_FILE = Path(os.environ.get("CRAWL_FILE", "docs/crawl_state.json"))
VIEWS_HISTORY_FILE = Path(os.environ.get("VIEWS_HISTORY_FILE", "docs/views_history.json"))
FETCH_STATE_FILE = Path(os.environ.get("FETCH_STATE_FILE", "docs/fetch_state.json"))
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "80"))
KEEP_ITEMS = int(os.environ.get("KEEP_ITEMS", "0"))
PAGES = int(os.environ.get("PAGES", "8"))
BACKFILL_PAGES = int(os.environ.get("BACKFILL_PAGES", "12"))
JAVDB_URL = os.environ.get("JAVDB_URL", "https://javdb.com/search?q=FC2&f=all")
JAVDB_BACKFILL_PAGES = int(os.environ.get("JAVDB_BACKFILL_PAGES", "6"))
FC2_MARKET_FETCH_LIMIT = int(os.environ.get("FC2_MARKET_FETCH_LIMIT", "24"))
FC2_MARKET_REFRESH_SEC = int(os.environ.get("FC2_MARKET_REFRESH_SEC", str(7 * 86400)))
VIEW_FETCH_LIMIT = int(os.environ.get("VIEW_FETCH_LIMIT", "50"))
TITLE_FETCH_LIMIT = int(os.environ.get("TITLE_FETCH_LIMIT", "30"))
VIEW_REFRESH_SEC = int(os.environ.get("VIEW_REFRESH_SEC", str(8 * 3600)))
FAIL_SKIP_SEC = 12 * 3600
INCLUDE_KEYWORDS = [x.strip() for x in os.environ.get("INCLUDE_KEYWORDS", "").split(",") if x.strip()]
EXCLUDE_KEYWORDS = [x.strip() for x in os.environ.get("EXCLUDE_KEYWORDS", "").split(",") if x.strip()]
JST = timezone(timedelta(hours=9))
DURATION_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")


def now_jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def now_ts() -> int:
    return int(datetime.now(JST).timestamp())


def parse_jst(text: str):
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=JST)
        except ValueError:
            continue
    return None


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def send_telegram(text: str, photo=None) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram未設定のため通知スキップ")
        return
    if photo:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        res = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "photo": photo, "caption": text[:1024]}, timeout=20)
        if res.ok:
            return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    res = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": False}, timeout=20)
    res.raise_for_status()


def fetch_soup(url: str):
    res = requests.get(url, headers=HEADERS, timeout=30)
    res.raise_for_status()
    return BeautifulSoup(res.text, "html.parser")


def fetch_page(url: str) -> dict:
    try:
        res = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
        text = res.text or ""
        try:
            soup = BeautifulSoup(text, "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
        except Exception:
            soup = None
            title = ""
        low = text.lower()
        title_l = (title or "").lower()
        real_title = bool(title) and "just a moment" not in title_l
        challenge = (
            res.status_code in (403, 503)
            or title_l.startswith("just a moment")
            or "<title>just a moment" in low[:4000]
        )
        if res.status_code == 200 and real_title and len(text) > 20000:
            challenge = False
        return {
            "ok": res.status_code == 200 and not challenge and len(text) > 2000,
            "status": res.status_code,
            "final_url": str(res.url),
            "text": text,
            "soup": soup,
            "size": len(text),
            "title": title,
            "cloudflare": challenge,
            "error": None,
        }
    except Exception as e:
        return {
            "ok": False, "status": 0, "final_url": url, "text": "", "soup": None,
            "size": 0, "title": "", "cloudflare": False, "error": str(e),
        }


def clean_title(text: str, code_num: str) -> str:
    title = re.sub(r"\s+", " ", (text or "")).strip()
    title = re.sub(r"^\d{1,2}:\d{2}(?::\d{2})?\s*", "", title)
    title = title.replace(f"FC2-PPV-{code_num}", "").replace(f"FC2PPV {code_num}", "")
    title = title.replace(f"FC2PPV-{code_num}", "").strip(" -|/")
    return title[:180]


def title_score(text: str):
    title = text or ""
    if not title or DURATION_RE.match(title) or title.startswith("FC2-PPV-"):
        return (0, 0)
    has_ja = 1 if re.search(r"[ぁ-んァ-ン]", title) else 0
    has_cjk = 1 if re.search(r"[\u4e00-\u9fff]", title) else 0
    return (3 if has_ja else (1 if has_cjk else 0), len(title))


def is_better_title(new: str, old: str) -> bool:
    return bool(new) and title_score(new) > title_score(old)


def parse_duration(text: str) -> str:
    match = re.search(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b", text or "")
    return match.group(1) if match else ""


def duration_seconds(text: str) -> int:
    parts = [int(x) for x in re.findall(r"\d+", text or "")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return 0


def parse_compact_count(raw: str):
    text = (raw or "").replace(",", "").strip()
    match = re.match(r"^([\d.]+)\s*(万|億|k|m)?$", text, flags=re.I)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    unit = (match.group(2) or "").lower()
    if unit == "万":
        value *= 10000
    elif unit == "億":
        value *= 100000000
    elif unit == "k":
        value *= 1000
    elif unit == "m":
        value *= 1000000
    value = int(value)
    return value if 0 < value < 100000000 else None


VIEW_TEXT_PATTERNS = [
    r"([\d,.]+)\s*(万|億|k|m)?\s*(?:views?|view_count|play_count|pv)\b",
    r"(?:視聴回数|再生回数|再生数|回再生|視聴数)\s*[:：]?\s*([\d,.]+)\s*(万|億|k|m)?",
    r"([\d,.]+)\s*(万|億)?\s*(?:回再生|回視聴)",
    r"([\d,.]+)\s*(?:次播放|次視聴|播放)",
    r"([\d,.]+)\s*(万|億|k|m)?\s*(?:PV|pv)\b",
]
VIEW_JSON_PATTERNS = [
    r'"(?:view_count|play_count|views)"\s*:\s*"?([\d,.]+)"?',
    r"'(?:view_count|play_count|views)'\s*:\s*'?([\d,.]+)'?",
    r'data-(?:views|view-count|play-count|view_count)="([\d,.]+)"',
]


def parse_views(text: str):
    blob = text or ""
    for pat in VIEW_TEXT_PATTERNS + VIEW_JSON_PATTERNS:
        match = re.search(pat, blob, flags=re.I)
        if not match:
            continue
        if re.search(r"wanted|想看", match.group(0), flags=re.I):
            continue
        raw = match.group(1)
        unit = match.group(2) if match.lastindex and match.lastindex >= 2 else ""
        value = parse_compact_count(raw + (unit or ""))
        if value:
            return value
    return None


def extract_views_from_html(soup, text: str = ""):
    blob = text or ""
    if soup is not None:
        blob = " ".join([soup.get_text(" ", strip=True), str(soup)])
        for tag in soup.find_all(attrs=True):
            for key, val in list(tag.attrs.items()):
                if re.search(r"view|play", str(key), flags=re.I) and not re.search(r"preview|viewport", str(key), flags=re.I):
                    blob += f" {key}={val}"
        for script in soup.find_all("script"):
            body = script.string or script.get_text() or ""
            if "ld+json" in (script.get("type") or "").lower() or re.search(r"view_count|play_count|\"views\"", body):
                blob += " " + body
    return parse_views(blob)


def extract_supjav_views(soup, text: str = ""):
    return extract_views_from_html(soup, text)


def needs_jp_title(title: str) -> bool:
    return title_score(title or "")[0] < 3


def missav_detail_urls(code_num: str):
    slug = f"fc2-ppv-{code_num}"
    return [f"https://missav.ws/ja/{slug}", f"https://missav.live/ja/{slug}", f"https://missav.ai/ja/{slug}"]


def extra_sources(code_num: str) -> dict:
    return {
        "MissAV": f"https://missav.ws/ja/fc2-ppv-{code_num}",
        "Supjav": f"https://supjav.com/ja/?s=FC2PPV+{code_num}",
        "JavDB": f"https://javdb.com/search?q=FC2-PPV-{code_num}&f=all",
        "FC2検索": f"https://adult.contents.fc2.com/search/?q={code_num}",
        "123AV": f"https://123av.com/ja/search?keyword=FC2-PPV-{code_num}",
        "JavFC2": f"https://javfc2.xyz/search?q={code_num}",
    }


def extract_page_title(soup, code_num: str) -> str:
    parts = []
    for tag in (soup.find("meta", attrs={"property": "og:title"}), soup.find("meta", attrs={"name": "twitter:title"})):
        if tag and tag.get("content"):
            parts.append(tag["content"])
    if soup.title and soup.title.get_text():
        parts.append(soup.title.get_text(" ", strip=True))
    h1 = soup.find("h1")
    if h1:
        parts.append(h1.get_text(" ", strip=True))
    for raw in parts:
        title = clean_title(raw, code_num)
        if title_score(title)[0] >= 3:
            return title
    for raw in parts:
        title = clean_title(raw, code_num)
        if title_score(title)[0] > 0:
            return title
    return ""


def rotate_items(items, cursor: int):
    if not items:
        return []
    start = cursor % len(items)
    return items[start:] + items[:start]


def parse_supjav_card_views(node):
    if node is None:
        return None
    blobs = []
    if hasattr(node, "select"):
        for sel in (".meta", ".pv", ".views", ".stats", ".post-meta", ".entry-meta", ".info"):
            for el in node.select(sel):
                blobs.append(el.get_text(" ", strip=True))
        blobs.append(node.get_text(" ", strip=True))
        blobs.append(str(node)[:4000])
    return extract_supjav_views(node if hasattr(node, "find_all") else None, " ".join(blobs))


def harvest_supjav_views(pages=None) -> dict:
    found = {}
    debug_left = 3
    for page in (pages or [1, 2, 3]):
        url = SUPJAV_URL if page == 1 else f"{SUPJAV_URL.rstrip('/')}/page/{page}"
        info = fetch_page(url)
        extra = " keeping_previous_views=true" if info.get("cloudflare") or info.get("status") == 403 else ""
        print(f"[Supjav] page={page} status={info.get('status')} title={(info.get('title') or '')[:60]!r} cloudflare={info.get('cloudflare')}{extra}")
        if not info.get("ok") or not info.get("soup"):
            continue
        soup = info["soup"]
        cards = []
        for sel in ("article", ".post", ".item", ".video-item", ".card", ".entry"):
            cards.extend(soup.select(sel))
        if not cards:
            cards = soup.find_all("a", href=True)
        page_found = page_miss = 0
        seen = set()
        for card in cards:
            text = card.get_text(" ", strip=True) if hasattr(card, "get_text") else ""
            hrefs = []
            if getattr(card, "get", None) and card.get("href"):
                hrefs.append(card.get("href") or "")
            if hasattr(card, "find_all"):
                hrefs.extend(a.get("href", "") for a in card.find_all("a", href=True))
            match = CODE_RE.search(text + " " + " ".join(hrefs))
            if not match:
                continue
            code = f"FC2-PPV-{match.group(1)}"
            if code in seen:
                continue
            seen.add(code)
            views = parse_supjav_card_views(card)
            if views:
                found[code] = views
                page_found += 1
            else:
                page_miss += 1
                if debug_left > 0:
                    print("[Supjav views debug]", code, re.sub(r"\s+", " ", text)[:500])
                    debug_left -= 1
        print(f"[Supjav] status={info.get('status')} items={len(seen)} views_found={page_found} views_missing={page_miss}")
    return found


def scrape_missav_fc2_rankings() -> dict:
    periods = {"day": "today_views", "week": "weekly_views", "month": "monthly_views", "total": "views"}
    hosts = ["https://missav.ws", "https://missav.live", "https://missav.ai"]
    result = {}
    for period, sort in periods.items():
        got = False
        for host in hosts:
            page = fetch_page(f"{host}/ja/fc2?sort={sort}")
            soup = page.get("soup")
            usable = page.get("ok") or (page.get("status") == 200 and (page.get("size") or 0) > 20000)
            if not usable or soup is None:
                print(f"[MissAV FC2 rank {period}] status={page.get('status')} title={(page.get('title') or '')[:50]!r} cloudflare={page.get('cloudflare')}")
                continue
            rank = 0
            seen = set()
            tops = []
            for a in soup.find_all("a", href=True):
                href = a.get("href") or ""
                text = a.get_text(" ", strip=True)
                blob = href + " " + text + " " + (a.get("title") or "")
                if any(x in href.lower() for x in ("/search", "/login", "/genres", "/makers", "/actresses", "language", "locale")):
                    continue
                if "繁體" in text or "简体" in text or text in ("日本語", "English", "繁體中文"):
                    continue
                match = re.search(r"fc2[-_ ]?ppv[-_ ]?(\d{6,8})", blob, flags=re.I)
                if not match:
                    continue
                code = f"FC2-PPV-{match.group(1)}"
                if code in seen:
                    continue
                seen.add(code)
                rank += 1
                result.setdefault(code, {})[period] = rank
                if rank <= 5:
                    tops.append(f"#{rank} {code}")
            print(f"[MissAV FC2 rank {period}] status={page.get('status')} fc2_found={rank} " + " ".join(tops[:3]))
            if rank:
                got = True
                break
        if not got:
            print(f"[MissAV FC2 rank {period}] failed")
    return result


def scrape_missav_rankings() -> dict:
    return scrape_missav_fc2_rankings()


def apply_missav_ranks(items, ranks, stamp):
    state = load_json(FETCH_STATE_FILE, {})
    if not ranks:
        print("[MissAV rank] keep previous ranks")
        return
    state["missav_rank_last_success"] = stamp
    save_json(FETCH_STATE_FILE, state)
    known = {item.get("code") for item in items}
    for item in items:
        rec = ranks.get(item.get("code") or "") or {}
        if not rec:
            continue
        if rec.get("day"):
            item["missav_rank_day"] = rec.get("day")
        if rec.get("week"):
            item["missav_rank_week"] = rec.get("week")
        if rec.get("month"):
            item["missav_rank_month"] = rec.get("month")
        if rec.get("total"):
            item["missav_rank_total"] = rec.get("total")
        item["missav_rank_updated_at"] = stamp
    # Do not create unchecked items from ranking pages. Rankings only annotate known items.



def fill_missing_views(items) -> None:
    harvested = harvest_supjav_views([1, 2, 3])
    now = now_ts()
    fetched = 0
    for item in items:
        item["views_updated"] = False
        code = item.get("code") or ""
        new_views = harvested.get(code)
        if isinstance(new_views, int):
            item["views"] = new_views
            item["views_source"] = "Supjav"
            item["last_views_fetch"] = now
            item["views_checked_at"] = now
            item["last_views_attempt"] = now
            item["views_updated"] = True
            fetched += 1
            print(f"再生数: {code} = {new_views} (Supjav)")
        else:
            item["last_views_attempt"] = now
    print(f"[Supjav] updated={fetched} harvested={len(harvested)}")


def refresh_japanese_titles(items) -> None:
    state = load_json(FETCH_STATE_FILE, {})
    fails = state.get("title_fail") if isinstance(state.get("title_fail"), dict) else {}
    cursor = int(state.get("title_cursor") or 0)
    now = now_ts()
    updated = tried = 0
    targets = [x for x in items if needs_jp_title(x.get("title", ""))]
    for item in rotate_items(targets, cursor):
        if updated >= TITLE_FETCH_LIMIT or tried >= TITLE_FETCH_LIMIT * 3:
            break
        code = item.get("code") or ""
        last_fail = int(fails.get(code) or 0)
        if last_fail and now - last_fail < FAIL_SKIP_SEC:
            continue
        tried += 1
        code_num = item.get("code_num") or str(code).split("-")[-1]
        urls = missav_detail_urls(code_num) + [
            f"https://supjav.com/ja/?s=FC2PPV+{code_num}",
            f"https://123av.com/ja/search?keyword=FC2-PPV-{code_num}",
        ]
        found, last_err = "", None
        for url in urls:
            try:
                soup = fetch_soup(url)
                found = extract_page_title(soup, code_num)
                if found and is_better_title(found, item.get("title", "")):
                    print(f"タイトル更新: {code} -> {found[:40]}")
                    item["title"] = found
                    updated += 1
                    fails.pop(code, None)
                    break
                if found and title_score(found)[0] >= 3:
                    fails.pop(code, None)
                    break
            except Exception as e:
                last_err = e
        if needs_jp_title(item.get("title", "")):
            fails[code] = now
            if last_err:
                print(f"タイトル取得失敗 {code}: {last_err}")
    state["title_fail"] = fails
    state["title_cursor"] = (cursor + max(tried, 1)) % max(len(targets), 1)
    save_json(FETCH_STATE_FILE, state)


def enrich(code_num, title, source, url, duration="", views=None):
    slug = f"fc2-ppv-{code_num}"
    return {
        "code": f"FC2-PPV-{code_num}", "code_num": code_num,
        "title": title or f"FC2-PPV-{code_num}", "source": source, "url": url,
        "duration": duration, "views": views,
        "thumb": f"https://fourhoi.com/{slug}/cover-n.jpg",
        "preview": f"https://fourhoi.com/{slug}/preview.mp4",
    }


def scrape_missav(pages=None):
    collected = {}
    for page in (pages or list(range(1, PAGES + 1))):
        url = MISSAV_URL if page == 1 else f"{MISSAV_URL}?page={page}"
        info = fetch_page(url)
        if not (info.get("ok") and info.get("status") == 200 and not info.get("cloudflare") and info.get("soup")):
            print(f"MissAV {page}ページスキップ: status={info.get('status')} cloudflare={info.get('cloudflare')}")
            continue
        soup = info["soup"]
        before = len(collected)
        for a in soup.find_all("a", href=True):
            match = re.search(r"/fc2-ppv-(\d+)", a["href"], flags=re.I)
            if not match:
                continue
            code_num = match.group(1)
            full_url = a["href"] if a["href"].startswith("http") else urljoin("https://missav.live", a["href"])
            img = a.find("img")
            raw = " ".join([a.get("title") or "", (img.get("alt") if img else "") or "", a.get_text(" ", strip=True)])
            title = clean_title(raw, code_num)
            around = " ".join([raw, a.parent.get_text(" ", strip=True) if a.parent else ""])
            duration = parse_duration(around)
            current = collected.get(code_num)
            if not current:
                collected[code_num] = enrich(code_num, title or f"FC2-PPV-{code_num}", "MissAV", full_url, duration, None)
            else:
                if is_better_title(title, current["title"]):
                    current["title"] = title
                    current["url"] = full_url
                if duration and not current.get("duration"):
                    current["duration"] = duration
        print(f"MissAV {page}ページ: {len(collected) - before}件追加 / 合計{len(collected)}")
        if len(collected) == before:
            break
    return list(collected.values())


def scrape_missav_catalog(pages=None):
    """MissAV /ja/fc2 catalog. Only usable HTTP-200 pages are parsed."""
    collected = {}
    bases = ["https://missav.ws/ja/fc2", "https://missav.live/ja/fc2", "https://missav.ai/ja/fc2"]
    for page_no in (pages or [1, 2]):
        soup = None
        final = ""
        for base in bases:
            url = base if page_no == 1 else f"{base}?page={page_no}"
            info = fetch_page(url)
            print(f"[MissAV /ja/fc2] page={page_no} status={info.get('status')} cloudflare={info.get('cloudflare')}")
            if info.get("ok") and info.get("status") == 200 and not info.get("cloudflare"):
                soup, final = info.get("soup"), info.get("final_url") or base
                break
        if soup is None:
            continue
        before = len(collected)
        for a in soup.find_all("a", href=True):
            href = a.get("href") or ""
            img = a.find("img")
            raw = " ".join([href, a.get("title") or "", (img.get("alt") if img else "") or "", a.get_text(" ", strip=True)])
            m = CODE_RE.search(raw) or re.search(r"/fc2-ppv-(\d{6,8})", href, re.I)
            if not m:
                continue
            num = m.group(1)
            full = href if href.startswith("http") else urljoin(final, href)
            title = clean_title(raw, num)
            cur = collected.get(num)
            if not cur:
                collected[num] = enrich(num, title or f"FC2-PPV-{num}", "MissAV", full)
            elif is_better_title(title, cur.get("title", "")):
                cur["title"], cur["url"] = title, full
        print(f"[MissAV /ja/fc2] page={page_no} +{len(collected)-before} total={len(collected)}")
    return list(collected.values())


def scrape_javdb(pages=None):
    """Independent JavDB discovery. 403/challenge/non-200 pages are simply skipped."""
    collected = {}
    for page_no in (pages or [1, 2]):
        sep = "&" if "?" in JAVDB_URL else "?"
        url = JAVDB_URL if page_no == 1 else f"{JAVDB_URL}{sep}page={page_no}"
        info = fetch_page(url)
        print(f"[JavDB] page={page_no} status={info.get('status')} cloudflare={info.get('cloudflare')} size={info.get('size')}")
        if not (info.get("ok") and info.get("status") == 200 and not info.get("cloudflare") and info.get("soup")):
            continue
        soup = info["soup"]
        before = len(collected)
        for a in soup.find_all("a", href=True):
            href = a.get("href") or ""
            parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
            raw = " ".join([a.get("title") or "", a.get_text(" ", strip=True), parent_text, href])
            m = CODE_RE.search(raw)
            if not m:
                continue
            num = m.group(1)
            full = href if href.startswith("http") else urljoin(info.get("final_url") or "https://javdb.com/", href)
            title = clean_title(raw, num)
            cur = collected.get(num)
            if not cur:
                collected[num] = enrich(num, title or f"FC2-PPV-{num}", "JavDB", full)
            elif is_better_title(title, cur.get("title", "")):
                cur["title"], cur["url"] = title, full
        print(f"[JavDB] page={page_no} +{len(collected)-before} total={len(collected)}")
    return list(collected.values())



def clean_fc2_market_title(raw: str, code_num: str) -> str:
    """Extract the seller's product title from FC2 Content Market's Japanese page."""
    title = html.unescape(re.sub(r"\s+", " ", (raw or "")).strip())
    if not title:
        return ""
    title = re.sub(
        rf"^\s*FC2[-_ ]?PPV[-_ ]?{re.escape(str(code_num))}\s*",
        "",
        title,
        flags=re.I,
    )
    title = re.sub(
        r"\s*[\|\-–—]\s*FC2(?:コンテンツマーケット| Content Market).*$",
        "",
        title,
        flags=re.I,
    )
    title = re.sub(r"\s*FC2コンテンツマーケット\s*$", "", title, flags=re.I)
    title = title.strip(" -|–—")
    # Reject obvious page chrome / non-title headings.
    if not title or title in {
        "FC2コンテンツマーケット", "FC2 Content Market",
        "商品レビュー", "商品説明", "販売者情報",
    }:
        return ""
    return title[:180]


def extract_fc2_market_title(soup, code_num: str) -> str:
    """Prefer FC2's own Japanese product-title metadata/headings."""
    candidates = []
    for tag in (
        soup.find("meta", attrs={"property": "og:title"}),
        soup.find("meta", attrs={"name": "twitter:title"}),
    ):
        if tag and tag.get("content"):
            candidates.append(tag.get("content"))
    if soup.title and soup.title.get_text():
        candidates.append(soup.title.get_text(" ", strip=True))
    # Product heading is usually near the top. Keep this as a fallback.
    for tag in soup.find_all(["h1", "h2", "h3"], limit=12):
        raw = tag.get_text(" ", strip=True)
        if raw:
            candidates.append(raw)

    best = ""
    for raw in candidates:
        candidate = clean_fc2_market_title(raw, code_num)
        if not candidate:
            continue
        # Avoid headings that are clearly site chrome.
        if re.search(r"商品レビュー|商品説明|販売者情報|サンプル画像|サンプル動画|対応デバイス", candidate):
            continue
        # Prefer a title containing Japanese kana. If none does, keep the first
        # plausible official title as fc2_title but do not necessarily replace
        # an existing title with it later.
        if re.search(r"[ぁ-んァ-ン]", candidate):
            return candidate
        if not best and title_score(candidate)[0] > 0:
            best = candidate
    return best


def fetch_fc2_market(code_num):
    url = f"https://adult.contents.fc2.com/article/{code_num}/?lang=ja"
    info = fetch_page(url)
    print(f"[FC2 Market] FC2-PPV-{code_num} status={info.get('status')} cloudflare={info.get('cloudflare')}")
    if not (info.get("ok") and info.get("status") == 200 and not info.get("cloudflare") and info.get("soup")):
        return None
    soup = info["soup"]
    text = soup.get_text(" ", strip=True)
    if str(code_num) not in ((info.get("text") or "") + " " + text):
        return None
    rating = None
    count = None
    for pat in (r"Average Rating\s*([0-5](?:\.\d+)?)", r"平均評価\s*[:：]?\s*([0-5](?:\.\d+)?)"):
        m = re.search(pat, text, re.I)
        if m:
            rating = float(m.group(1)); break
    for pat in (r"Product Review\s*[\(（]\s*(\d+)\s*[\)）]", r"商品レビュー\s*[\(（]\s*(\d+)\s*[\)）]", r"レビュー\s*(\d+)\s*件"):
        m = re.search(pat, text, re.I)
        if m:
            count = int(m.group(1)); break
    fc2_title = extract_fc2_market_title(soup, code_num)
    return {
        "fc2_market_url": info.get("final_url") or url,
        "fc2_rating": rating,
        "fc2_review_count": count,
        "fc2_title": fc2_title,
    }


def enrich_fc2_market(items):
    state = load_json(FETCH_STATE_FILE, {})
    checked = state.get("fc2_market_checked") if isinstance(state.get("fc2_market_checked"), dict) else {}
    failed = state.get("fc2_market_failed") if isinstance(state.get("fc2_market_failed"), dict) else {}
    cursor = int(state.get("fc2_market_cursor") or 0)
    now = now_ts()
    if not items:
        return
    rotated = items[cursor % len(items):] + items[:cursor % len(items)]
    title_targets = [
        x for x in rotated
        if not x.get("fc2_title") and needs_jp_title(x.get("title", ""))
    ]
    title_target_codes = {x.get("code") for x in title_targets}
    ordered = title_targets + [x for x in rotated if x.get("code") not in title_target_codes]
    tried = updated = title_updated = 0
    for item in ordered:
        if tried >= FC2_MARKET_FETCH_LIMIT:
            break
        code = item.get("code") or ""
        needs_official_title = not item.get("fc2_title") and needs_jp_title(item.get("title", ""))
        if not needs_official_title and now - int(checked.get(code) or 0) < FC2_MARKET_REFRESH_SEC:
            continue
        if now - int(failed.get(code) or 0) < FAIL_SKIP_SEC:
            continue
        tried += 1
        num = item.get("code_num") or code.split("-")[-1]
        meta = fetch_fc2_market(num)
        if not meta:
            failed[code] = now
            continue
        item["fc2_market_url"] = meta["fc2_market_url"]
        official_title = (meta.get("fc2_title") or "").strip()
        if official_title:
            item["fc2_title"] = official_title
            # Replace low-quality/non-Japanese titles only when FC2's own title
            # is clearly Japanese. Existing good Japanese titles are left alone.
            if needs_jp_title(item.get("title", "")) and re.search(r"[ぁ-んァ-ン]", official_title):
                if item.get("title") != official_title:
                    print(f"FC2公式タイトル更新: {code} -> {official_title[:60]}")
                    item["title"] = official_title
                    item["title_source"] = "FC2公式"
                    title_updated += 1
        if meta.get("fc2_rating") is not None:
            item["fc2_rating"] = meta["fc2_rating"]
        if meta.get("fc2_review_count") is not None:
            item["fc2_review_count"] = meta["fc2_review_count"]
        item["fc2_market_checked_at"] = now
        checked[code] = now
        failed.pop(code, None)
        updated += 1
    state["fc2_market_cursor"] = (cursor + max(tried, 1)) % max(len(items), 1)
    state["fc2_market_checked"] = checked
    state["fc2_market_failed"] = failed
    save_json(FETCH_STATE_FILE, state)
    print(f"[FC2 Market] tried={tried} updated={updated} title_updated={title_updated}")


def scrape_supjav(pages=None):
    items, seen = [], set()
    for page in (pages or list(range(1, PAGES + 1))):
        url = SUPJAV_URL if page == 1 else f"{SUPJAV_URL.rstrip('/')}/page/{page}"
        info = fetch_page(url)
        if not (info.get("ok") and info.get("status") == 200 and not info.get("cloudflare") and info.get("soup")):
            print(f"Supjav {page}ページスキップ: status={info.get('status')} cloudflare={info.get('cloudflare')}")
            continue
        soup = info["soup"]
        before = len(seen)
        for a in soup.find_all("a", href=True):
            text = " ".join([a.get("title") or "", a.get_text(" ", strip=True)])
            match = CODE_RE.search(text) or CODE_RE.search(a.get("href", ""))
            if not match:
                continue
            code_num = match.group(1)
            href = a["href"]
            if code_num in seen or href.startswith("#") or "/category/" in href or "/maker/" in href:
                continue
            seen.add(code_num)
            full_url = href if href.startswith("http") else urljoin("https://supjav.com", href)
            around = " ".join([text, a.parent.get_text(" ", strip=True) if a.parent else ""])
            views = parse_supjav_card_views(a.parent) or (extract_supjav_views(a.parent, around) if a.parent else parse_views(around))
            row = enrich(code_num, clean_title(a.get("title") or a.get_text(" ", strip=True), code_num), "Supjav", full_url, parse_duration(around), views)
            if views:
                row["views_source"] = "Supjav"
            items.append(row)
        print(f"Supjav {page}ページ: {len(seen) - before}件追加 / 合計{len(seen)}")
        if len(seen) == before:
            break
    return items


def merge_videos(groups):
    merged, order = {}, []
    for group in groups:
        for item in group:
            code = item["code"]
            if code not in merged:
                merged[code] = {**item, "sources": {item["source"]: item["url"]}}
                order.append(code)
            else:
                merged[code]["sources"][item["source"]] = item["url"]
                if item["source"] == "MissAV":
                    merged[code]["url"] = item["url"]
                if is_better_title(item["title"], merged[code]["title"]):
                    merged[code]["title"] = item["title"]
                if item.get("duration") and not merged[code].get("duration"):
                    merged[code]["duration"] = item["duration"]
                if item.get("views") and not merged[code].get("views"):
                    merged[code]["views"] = item["views"]
                    if item.get("views_source"):
                        merged[code]["views_source"] = item["views_source"]
    out = []
    for code in order:
        merged[code]["source_label"] = " / ".join(merged[code]["sources"].keys())
        out.append(merged[code])
    return out


def get_latest_videos():
    state = load_json(CRAWL_FILE, {"missav_page": 3, "supjav_page": 3, "javdb_page": 3})
    mp = int(state.get("missav_page", 3))
    jp = int(state.get("javdb_page", 3))
    missav_pages = [1, 2] + list(range(mp, mp + BACKFILL_PAGES))
    javdb_pages = [1, 2] + list(range(jp, jp + JAVDB_BACKFILL_PAGES))
    # Supjav is an optional helper; keep its request volume small.
    supjav_pages = [1, 2, 3]
    found, errors = [], []
    try:
        items = scrape_missav(missav_pages)
        catalog = scrape_missav_catalog(missav_pages)
        print(f"MissAV: search={len(items)} catalog={len(catalog)}")
        found.extend([items, catalog])
        state["missav_page"] = missav_pages[-1] + 1
    except Exception as e:
        errors.append(f"MissAV: {e}")
    try:
        items = scrape_javdb(javdb_pages)
        print(f"JavDB: {len(items)}件")
        found.append(items)
        state["javdb_page"] = javdb_pages[-1] + 1
    except Exception as e:
        errors.append(f"JavDB: {e}")
    try:
        items = scrape_supjav(supjav_pages)
        print(f"Supjav: {len(items)}件")
        found.append(items)
    except Exception as e:
        print(f"Supjav補助スキップ: {e}")
    save_json(CRAWL_FILE, state)
    videos = merge_videos(found)
    if not videos and errors:
        raise RuntimeError(" / ".join(errors))
    return videos


def calc_trend(points, current, hours, now):
    if not isinstance(current, int) or not points:
        return None
    target = now - hours * 3600
    if hours <= 6:
        oldest_ok, newest_ok = now - 8 * 3600, now - 4 * 3600
    else:
        oldest_ok, newest_ok = now - 30 * 3600, now - 18 * 3600
    window = [p for p in points if isinstance(p.get("t"), (int, float)) and isinstance(p.get("v"), int) and oldest_ok <= p["t"] <= newest_ok]
    if not window:
        return None
    prev = min(window, key=lambda p: abs(p["t"] - target)).get("v")
    if not isinstance(prev, int) or current < prev:
        return None
    return current - prev


def update_views_history(items) -> None:
    hist = load_json(VIEWS_HISTORY_FILE, {"items": {}})
    store = hist.get("items") if isinstance(hist, dict) else {}
    if not isinstance(store, dict):
        store = {}
    now = now_ts()
    for item in items:
        code = item.get("code")
        views = item.get("views")
        rec = store.get(code) or {"points": []}
        points = rec.get("points") if isinstance(rec.get("points"), list) else []
        if isinstance(views, int) and item.get("views_updated") and item.get("views_source") == "Supjav":
            if not points or points[-1].get("v") != views or now - int(points[-1].get("t") or 0) > 3 * 3600:
                points.append({"t": now, "v": views})
            points = points[-40:]
        can_trend = item.get("views_source") == "Supjav" and isinstance(views, int) and len(points) >= 2
        item["trend_6h"] = calc_trend(points[:-1], views, 6, now) if can_trend else None
        item["trend_24h"] = calc_trend(points[:-1], views, 24, now) if can_trend else None
        store[code] = {"points": points, "trend_6h": item["trend_6h"], "trend_24h": item["trend_24h"]}
    save_json(VIEWS_HISTORY_FILE, {"updated_at": now_jst(), "items": store})


def json_for_script(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def public_item(item):
    sources = dict(item.get("sources") or {})
    code_num = item.get("code_num") or str(item.get("code", "")).split("-")[-1]
    search_links = extra_sources(code_num)
    return {
        "code": item.get("code", ""),
        "code_num": str(code_num),
        "title": item.get("title") or item.get("code", ""),
        "url": item.get("url") or sources.get("MissAV") or search_links.get("MissAV") or "",
        "thumb": item.get("thumb") or "",
        "preview": item.get("preview") or "",
        "duration": item.get("duration") or "",
        "duration_sec": duration_seconds(item.get("duration") or ""),
        "views": item.get("views") if isinstance(item.get("views"), int) else 0,
        "views_source": item.get("views_source") or "",
        "first_seen": item.get("first_seen") or "",
        "last_seen": item.get("last_seen") or "",
        "is_new": bool(item.get("is_new")),
        "sources": sources,
        "search_links": search_links,
        "source_label": item.get("source_label") or " / ".join(sources.keys()),
        "trend_6h": item.get("trend_6h") if isinstance(item.get("trend_6h"), int) else None,
        "trend_24h": item.get("trend_24h") if isinstance(item.get("trend_24h"), int) else None,
        "missav_rank_day": item.get("missav_rank_day") if isinstance(item.get("missav_rank_day"), int) else None,
        "missav_rank_week": item.get("missav_rank_week") if isinstance(item.get("missav_rank_week"), int) else None,
        "missav_rank_month": item.get("missav_rank_month") if isinstance(item.get("missav_rank_month"), int) else None,
        "missav_rank_total": item.get("missav_rank_total") if isinstance(item.get("missav_rank_total"), int) else None,
        "missav_rank_updated_at": item.get("missav_rank_updated_at") or "",
        "fc2_market_url": item.get("fc2_market_url") or "",
        "fc2_title": item.get("fc2_title") or "",
        "title_source": item.get("title_source") or "",
        "fc2_rating": item.get("fc2_rating") if isinstance(item.get("fc2_rating"), (int, float)) else None,
        "fc2_review_count": item.get("fc2_review_count") if isinstance(item.get("fc2_review_count"), int) else None,
        "fc2_market_checked_at": item.get("fc2_market_checked_at") or 0,
    }


def render_html(items, updated_at, new_count):
    return (
        HTML_TEMPLATE.replace("__UPDATED_AT__", html.escape(updated_at))
        .replace("__NEW_COUNT__", str(new_count))
        .replace("__ITEM_COUNT__", str(len(items)))
        .replace("__ITEMS_JSON__", json_for_script([public_item(x) for x in items]))
    )

HTML_TEMPLATE = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<title>FC2-PPV</title>
<style>
:root { color-scheme:dark; --bg:#0f0f0f; --card:#0f0f0f; --text:#f1f1f1; --muted:#aaa; --line:#272727; --chip:#272727; --accent:#3ea6ff; --new:#3ddc84; --bar:#f00; }
* { box-sizing:border-box; }
html,body { margin:0; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
header { position:sticky; top:0; z-index:80; background:#0f0f0f; border-bottom:1px solid var(--line); padding:calc(8px + env(safe-area-inset-top)) 12px 0; }
.top { display:flex; align-items:center; gap:10px; }
h1 { margin:0; font-size:16px; }
.count { color:var(--muted); font-size:11px; margin-left:auto; white-space:nowrap; max-width:55%; overflow:hidden; text-overflow:ellipsis; }
.search-wrap { position:relative; flex:1; }
.search-row { display:none; gap:8px; align-items:center; }
.search-row.on, .page-search .search-row { display:flex; }
#q { width:100%; border:1px solid #303030; border-radius:20px; padding:12px 14px; background:#121212; color:var(--text); font-size:16px; outline:none; }
.icon-btn { border:0; background:#272727; color:#fff; border-radius:18px; min-height:44px; padding:0 14px; font-size:13px; white-space:nowrap; }
.suggest { display:none; position:fixed; left:0; right:0; bottom:calc(56px + env(safe-area-inset-bottom)); top:auto; background:#1a1a1a; border-top:1px solid #333; overflow:auto; max-height:45vh; z-index:95; }
.suggest.on { display:block; }
.sug { display:flex; gap:10px; align-items:center; width:100%; border:0; background:transparent; color:#fff; text-align:left; min-height:48px; padding:10px 12px; }
.sug img { width:72px; height:40px; object-fit:cover; border-radius:6px; background:#000; }
.sug b { display:block; font-size:12px; color:#3ea6ff; }
.sug span { display:block; font-size:12px; color:#ddd; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.chips { display:flex; gap:8px; overflow-x:auto; padding:0 0 10px; -webkit-overflow-scrolling:touch; touch-action:pan-x; scrollbar-width:none; }
.chips::-webkit-scrollbar, .rail::-webkit-scrollbar, .sorts::-webkit-scrollbar { display:none; }
.lib-tabs { display:none; padding:0 12px 8px; gap:8px; }
.lib-tabs.on { display:flex; }
.chips button, .sorts button { flex:0 0 auto; border:0; border-radius:8px; min-height:36px; padding:8px 14px; background:var(--chip); color:#fff; font-size:13px; }
.chips button.on, .sorts button.on { background:#f1f1f1; color:#0f0f0f; }
.side { display:none; }
main { padding:0 0 calc(92px + env(safe-area-inset-bottom)); }
.section { padding:8px 0 4px; }
.section h2 { margin:0 12px 8px; font-size:16px; }
.rail { display:flex; gap:12px; overflow-x:auto; padding:0 12px 12px; -webkit-overflow-scrolling:touch; touch-action:pan-x; overscroll-behavior-x:contain; }
.grid { display:grid; grid-template-columns:1fr; gap:14px; padding:0 0 16px; }
@media (min-width:700px) { .grid { grid-template-columns:repeat(2,minmax(0,1fr)); padding:0 12px; } }
@media (min-width:900px) {
  .nav { display:none; }
  .search-row { display:flex; }
  .side { display:flex; flex-direction:column; position:fixed; left:0; top:118px; bottom:0; width:216px; padding:12px 10px; gap:4px; border-right:1px solid var(--line); background:#0f0f0f; }
  .side button { border:0; background:transparent; color:#f1f1f1; text-align:left; border-radius:10px; padding:10px 14px; font-size:14px; }
  .side button.on { background:#272727; }
  .side hr { border:0; border-top:1px solid #222; margin:8px 6px; }
  header { padding-left:24px; padding-right:24px; }
  main { margin-left:216px; padding:12px 20px 32px; }
  .search-wrap { margin:8px 0; max-width:720px; }
  .suggest { position:absolute; left:0; right:0; top:48px; max-height:70vh; border:1px solid #333; border-radius:12px; }
}
@media (min-width:1000px) { .grid { grid-template-columns:repeat(3,minmax(0,1fr)); } }
@media (min-width:1400px) { .grid { grid-template-columns:repeat(4,minmax(0,1fr)); } }
@media (min-width:1800px) { .grid { grid-template-columns:repeat(5,minmax(0,1fr)); } }
.card { background:var(--card); min-width:220px; }
.rail .card { width:240px; flex:0 0 auto; }
.thumb-wrap { position:relative; display:block; width:100%; aspect-ratio:16/9; padding:0; border:0; background:#000; overflow:hidden; border-radius:12px; }
.thumb-wrap img, .thumb-wrap video { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }
.thumb-wrap video { opacity:0; pointer-events:none; }
.thumb-wrap.playing video { opacity:1; }
.badge.new { position:absolute; top:8px; left:8px; z-index:2; background:#3ddc84; color:#073; font-size:11px; font-weight:700; padding:2px 6px; border-radius:4px; }
.rank { position:absolute; top:8px; left:8px; z-index:2; background:rgba(0,0,0,.8); color:#fff; font-size:16px; font-weight:800; padding:2px 7px; border-radius:6px; }
.rank.top { color:#ffd54a; }
.time { position:absolute; right:8px; bottom:14px; z-index:2; background:rgba(0,0,0,.85); color:#fff; font-size:12px; padding:2px 6px; border-radius:4px; }
.prog { position:absolute; left:0; right:0; bottom:0; height:3px; background:#333; z-index:3; }
.prog i { display:block; height:100%; background:var(--bar); width:0; }
.body { position:relative; padding:10px 28px 8px 2px; }
.title { margin:0 0 4px; font-size:15px; line-height:1.35; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; color:#fff; text-decoration:none; }
.subline,.meta { margin:0; color:var(--muted); font-size:12px; }
.more { position:absolute; top:2px; right:0; border:0; background:transparent; color:#aaa; font-size:22px; width:44px; height:44px; }
.list-head { display:flex; align-items:center; gap:8px; padding:8px 12px; flex-wrap:wrap; }
.list-head h2 { margin:0; font-size:16px; }
.sorts { display:flex; gap:6px; overflow-x:auto; }
.nav { position:fixed; left:0; right:0; bottom:0; z-index:80; display:flex; background:#0f0f0f; border-top:1px solid #222; padding:4px 0 calc(6px + env(safe-area-inset-bottom)); }
.nav button { flex:1; border:0; background:transparent; color:#888; font-size:10px; min-height:48px; padding:6px 0; }
.nav button.on { color:#fff; }
.sheet-bg { display:none; position:fixed; inset:0; background:rgba(0,0,0,.55); z-index:90; }
.sheet-bg.on { display:block; }
.panel, .menu { display:none; position:fixed; left:0; right:0; bottom:0; z-index:100; background:#212121; border-radius:16px 16px 0 0; padding:10px 14px calc(16px + env(safe-area-inset-bottom)); max-height:78vh; overflow:auto; }
.panel.on, .menu.on { display:block; }
.panel:before, .menu:before { content:""; display:block; width:36px; height:4px; border-radius:4px; background:#555; margin:4px auto 10px; }
.panel h3 { margin:12px 0 6px; font-size:13px; color:#aaa; }
.panel button { margin:0 6px 6px 0; border:0; border-radius:8px; min-height:40px; padding:8px 12px; background:#333; color:#fff; }
.panel button.on { background:#f1f1f1; color:#111; }
.menu button { display:block; width:100%; border:0; background:transparent; color:#fff; text-align:left; min-height:48px; padding:12px; font-size:16px; }
.empty { color:var(--muted); padding:30px 12px; text-align:center; }
.trend { color:#3ddc84; }
a { color:inherit; }
</style>
</head>
<body>
<header>
  <div class="top">
    <h1>FC2-PPV</h1>
    <span class="count">更新 __UPDATED_AT__ ・ 新着 __NEW_COUNT__ ・ __ITEM_COUNT__件</span>
  </div>
  <div class="search-row">
    <div class="search-wrap">
      <input id="q" type="search" placeholder="番号・タイトルで検索" autocomplete="off">
      <div id="suggest" class="suggest"></div>
    </div>
    <button class="icon-btn" id="filterBtn" type="button">絞り込み</button>
  </div>
  <div class="chips" id="chips">
    <button type="button" data-chip="all" class="on">すべて</button>
    <button type="button" data-chip="new">新着</button>
    <button type="button" data-chip="rising">急上昇</button>
    <button type="button" data-chip="popular">人気</button>
    <button type="button" data-chip="today">今日</button>
    <button type="button" data-chip="week">1週間</button>
    <button type="button" data-chip="views10">10万回以上</button>
    <button type="button" data-chip="dur60">60分以上</button>
    <button type="button" data-chip="saved">保存済み</button>
  </div>
</header>
<aside class="side">
  <button type="button" data-page="home" class="on">ホーム</button>
  <button type="button" data-page="rising">急上昇</button>
  <button type="button" data-page="popular">人気ランキング</button>
  <button type="button" data-page="new">新着</button>
  <hr>
  <button type="button" data-page="history">履歴</button>
  <button type="button" data-page="later">後で見る</button>
  <button type="button" data-page="saved">保存済み</button>
</aside>
<main>
  <div id="libTabs" class="lib-tabs chips">
    <button type="button" data-lib="later">後で見る</button>
    <button type="button" data-lib="saved">保存済み</button>
  </div>
  <div id="shelves"></div>
  <div class="list-head">
    <h2 id="listTitle">すべての作品</h2>
    <span id="resultCount"></span>
    <div class="sorts" id="sorts">
      <button type="button" data-sort="new" class="on">新しい順</button>
      <button type="button" data-sort="old">古い順</button>
      <button type="button" data-sort="views">再生数順</button>
      <button type="button" data-sort="rise">急上昇順</button>
      <button type="button" data-sort="long">長い順</button>
      <button type="button" data-sort="short">短い順</button>
    </div>
  </div>
  <div id="grid" class="grid"></div>
</main>
<nav class="nav">
  <button type="button" data-page="home" class="on">ホーム</button>
  <button type="button" data-page="rising">急上昇</button>
  <button type="button" data-page="search">検索</button>
  <button type="button" data-page="history">履歴</button>
  <button type="button" data-page="library">ライブラリ</button>
</nav>
<div id="sheetBg" class="sheet-bg"></div>
<div id="panel" class="panel">
  <h3>再生数</h3>
  <button data-f="vmin" data-v="0">すべて</button>
  <button data-f="vmin" data-v="10000">1万以上</button>
  <button data-f="vmin" data-v="100000">10万以上</button>
  <button data-f="vmin" data-v="500000">50万以上</button>
  <h3>動画時間</h3>
  <button data-f="dur" data-v="">すべて</button>
  <button data-f="dur" data-v="lt30">30分未満</button>
  <button data-f="dur" data-v="30-60">30～60分</button>
  <button data-f="dur" data-v="60-120">60～120分</button>
  <button data-f="dur" data-v="gte120">120分以上</button>
  <h3>検出日</h3>
  <button data-f="seen" data-v="">すべて</button>
  <button data-f="seen" data-v="today">今日</button>
  <button data-f="seen" data-v="24h">24時間</button>
  <button data-f="seen" data-v="7d">7日</button>
  <button data-f="seen" data-v="30d">30日</button>
  <h3>ソース数</h3>
  <button data-f="src" data-v="0">すべて</button>
  <button data-f="src" data-v="1">1以上</button>
  <button data-f="src" data-v="2">2以上</button>
  <button data-f="src" data-v="3">3以上</button>
</div>
<div id="menu" class="menu"></div>
<script id="data" type="application/json">__ITEMS_JSON__</script>
<script>
const ITEMS = JSON.parse(document.getElementById('data').textContent);
const qEl = document.getElementById('q');
const sugEl = document.getElementById('suggest');
const grid = document.getElementById('grid');
const shelves = document.getElementById('shelves');
const resultCount = document.getElementById('resultCount');
const listTitle = document.getElementById('listTitle');
const panel = document.getElementById('panel');
const menu = document.getElementById('menu');
const loadSet = (k) => new Set(JSON.parse(localStorage.getItem(k) || '[]'));
const saveSet = (k, set) => localStorage.setItem(k, JSON.stringify([...set]));
const favs = loadSet('fc2favs');
const watched = loadSet('fc2watched');
const later = loadSet('fc2watchlater');
const watchTimes = JSON.parse(localStorage.getItem('fc2watchtimes') || '{}');
const progress = JSON.parse(localStorage.getItem('fc2progress') || '{}');
function markWatched(code){
  watched.add(code); watchTimes[code] = Date.now();
  saveSet('fc2watched', watched);
  localStorage.setItem('fc2watchtimes', JSON.stringify(watchTimes));
}
function unmarkWatched(code){
  watched.delete(code); delete watchTimes[code];
  saveSet('fc2watched', watched);
  localStorage.setItem('fc2watchtimes', JSON.stringify(watchTimes));
}
let searches = JSON.parse(localStorage.getItem('fc2searchhist') || '[]');
let page = 'home', chip = 'all', sort = 'new', riseWin = '24h';
let filters = { vmin: 0, dur: '', seen: '', src: 0 };
let hoverTimer = null;
const playingSet = new Set();
const byCode = Object.fromEntries(ITEMS.map(x => [x.code, x]));
function esc(s){ return String(s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function fc2num(s){
  const text = String(s||'');
  const tagged = text.match(/fc2[-_\s]*ppv[-_\s]*(\d{6,8})/i);
  if(tagged) return tagged[1];
  const only = text.match(/(\d{6,8})/);
  return only ? only[1] : '';
}
function viewsLabel(n){
  n = Number(n||0);
  if(!n) return '';
  if(n < 10000) return n.toLocaleString() + '回視聴';
  const v = n / 10000;
  return (v >= 100 ? String(Math.round(v)) : String(Math.round(v*10)/10).replace(/\.0$/,'')) + '万回視聴';
}
function parseSeen(s){
  if(!s) return 0;
  const t = Date.parse(String(s).replace(' ','T')+'+09:00');
  return isNaN(t) ? 0 : t;
}
function relTime(s){
  const t = parseSeen(s);
  if(!t) return '';
  const diff = Math.max(0, Date.now() - t);
  const m = Math.floor(diff/60000), h = Math.floor(diff/3600000), d = Math.floor(diff/86400000);
  if(m < 1) return 'たった今';
  if(m < 60) return m + '分前';
  if(h < 24) return h + '時間前';
  if(d < 7) return d + '日前';
  const dt = new Date(t + 9*3600*1000);
  return dt.getUTCFullYear() + '/' + String(dt.getUTCMonth()+1).padStart(2,'0') + '/' + String(dt.getUTCDate()).padStart(2,'0');
}
function trendOf(it){ return riseWin === '6h' ? it.trend_6h : it.trend_24h; }
function sourceCount(it){ return Object.keys(it.sources||{}).length; }
function isRecentNew(it){ const t = parseSeen(it.first_seen); return !!t && (Date.now() - t) <= 48*3600000; }
function matchQuery(it, query){
  const raw = (query||'').trim();
  if(!raw) return true;
  const num = fc2num(raw);
  const hay = (it.code + ' ' + it.code_num + ' ' + it.title + ' ' + it.source_label).toLowerCase();
  if(num) return it.code_num === num || it.code_num.includes(num);
  return hay.includes(raw.toLowerCase());
}
function matchFilters(it){
  if((it.views||0) < Number(filters.vmin||0)) return false;
  const sec = it.duration_sec||0;
  if(filters.dur === 'lt30' && !(sec && sec < 1800)) return false;
  if(filters.dur === '30-60' && !(sec >= 1800 && sec < 3600)) return false;
  if(filters.dur === '60-120' && !(sec >= 3600 && sec < 7200)) return false;
  if(filters.dur === 'gte120' && !(sec >= 7200)) return false;
  const t = parseSeen(it.first_seen);
  const age = Date.now() - t;
  if(filters.seen === 'today' && (it.first_seen||'').slice(0,10) !== new Date(Date.now()+9*3600000).toISOString().slice(0,10)) return false;
  if(filters.seen === '24h' && age > 86400000) return false;
  if(filters.seen === '7d' && age > 7*86400000) return false;
  if(filters.seen === '30d' && age > 30*86400000) return false;
  if(Number(filters.src||0) && sourceCount(it) < Number(filters.src)) return false;
  if(chip === 'new' && !isRecentNew(it) && !it.is_new) return false;
  if(chip === 'rising' && !(trendOf(it) > 0 && it.views_source==='Supjav')) return false;
  if(chip === 'popular' && !(it.views > 0 && it.views_source==='Supjav')) return false;
  if(chip === 'today' && (it.first_seen||'').slice(0,10) !== new Date(Date.now()+9*3600000).toISOString().slice(0,10)) return false;
  if(chip === 'week' && age > 7*86400000) return false;
  if(chip === 'views10' && (it.views||0) < 100000) return false;
  if(chip === 'dur60' && (it.duration_sec||0) < 3600) return false;
  if(chip === 'saved' && !favs.has(it.code)) return false;
  if(page === 'new' && !isRecentNew(it) && !it.is_new) return false;
  if(page === 'rising' && !(trendOf(it) > 0 && it.views_source==='Supjav')) return false;
  if(page === 'popular' && !(it.views > 0 && it.views_source==='Supjav')) return false;
  if(page === 'history' && !watched.has(it.code)) return false;
  if(page === 'later' && !later.has(it.code)) return false;
  if(page === 'saved' && !favs.has(it.code)) return false;
  if(page === 'library' && !(watched.has(it.code) || later.has(it.code) || favs.has(it.code))) return false;
  return true;
}
function sortItems(arr){
  const copy = arr.slice();
  copy.sort((a,b)=>{
    if(sort==='old') return (a.first_seen||'').localeCompare(b.first_seen||'');
    if(sort==='views') return (b.views||0) - (a.views||0);
    if(sort==='rise') return (trendOf(b)||-1) - (trendOf(a)||-1);
    if(sort==='long') return (b.duration_sec||0) - (a.duration_sec||0);
    if(sort==='short') return (a.duration_sec||0) - (b.duration_sec||0);
    return (b.first_seen||'').localeCompare(a.first_seen||'');
  });
  if(page==='rising' || chip==='rising') copy.sort((a,b)=>(trendOf(b)||-1)-(trendOf(a)||-1));
  if(page==='popular' || chip==='popular') copy.sort((a,b)=>(b.views||0)-(a.views||0));
  if(page==='history') copy.sort((a,b)=>(watchTimes[b.code]||0)-(watchTimes[a.code]||0));
  const q = fc2num(qEl.value.trim());
  if(q) copy.sort((a,b)=>(b.code_num===q?1:0)-(a.code_num===q?1:0));
  return copy;
}
function cardHTML(it, rank){
  const prog = Number(progress[it.code]||0);
  const badge = it.is_new ? '<span class="badge new">NEW</span>' : '';
  const rankEl = rank ? `<span class="rank${rank<=3?' top':''}">#${rank}</span>` : badge;
  const tr = trendOf(it);
  const extra = (page==='rising' || chip==='rising') && tr ? `<p class="meta trend">${riseWin} +${tr.toLocaleString()}</p>` : '';
  const ranks = [];
  if(it.missav_rank_day) ranks.push('今日 #' + it.missav_rank_day);
  else if(it.missav_rank_week) ranks.push('週間 #' + it.missav_rank_week);
  else if(it.missav_rank_month) ranks.push('月間 #' + it.missav_rank_month);
  else if(it.missav_rank_total) ranks.push('累計 #' + it.missav_rank_total);
  const rankLine = ranks.length ? `<p class="meta trend">MissAV ${ranks.join(' / ')}</p>` : '';
  const fc2pop = (it.fc2_rating!=null || it.fc2_review_count!=null) ? `<p class="meta">FC2公式 ${it.fc2_rating!=null?'★'+it.fc2_rating:''}${it.fc2_review_count!=null?' ・ レビュー '+it.fc2_review_count.toLocaleString()+'件':''}</p>` : '';
  const multi = (it.missav_rank_day && it.missav_rank_day<=10 && (it.trend_24h||0)>0) ? '<p class="meta">複数サイトで人気</p>' : '';
  return `<article class="card" data-code="${it.code}">
    <button class="thumb-wrap" type="button" data-code="${it.code}" aria-label="プレビュー">
      <img src="${it.thumb}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.opacity=0">
      <video muted loop playsinline preload="none" poster="${it.thumb}" referrerpolicy="no-referrer"></video>
      ${rankEl}
      <span class="time">${it.duration||''}</span>
      <span class="prog"><i style="width:${Math.min(100,prog*100)}%"></i></span>
    </button>
    <div class="body">
      <div class="title">${esc(it.title)}</div>
      <p class="subline">${esc(it.code)}</p>
      <p class="meta">${[viewsLabel(it.views)+(it.views&&it.views_source==='Supjav'?' ・ Supjav':''), relTime(it.first_seen)].filter(Boolean).join(' ・ ')}</p>
      ${rankLine}${fc2pop}${multi}${extra}
      <p class="meta">${esc(it.source_label||'')}</p>
      <button class="more" type="button" data-more="${esc(it.code)}">⋮</button>
    </div>
  </article>`;
}
document.addEventListener('click', e=>{
  const more = e.target.closest('.more');
  if(more){ e.preventDefault(); e.stopPropagation(); openMenu(more.dataset.more); return; }
  const open = e.target.closest('.open');
  if(open){ const card=open.closest('.card'); if(card) markWatched(card.dataset.code); return; }
  const thumb = e.target.closest('.thumb-wrap');
  if(thumb){ e.preventDefault(); thumb.classList.contains('playing') ? stopPreview(thumb) : startPreview(thumb); }
});
document.addEventListener('mouseover', e=>{
  const w = e.target.closest('.thumb-wrap');
  if(!w || !window.matchMedia('(hover:hover) and (pointer:fine)').matches) return;
  clearTimeout(hoverTimer);
  hoverTimer = setTimeout(()=>startPreview(w), 600);
});
document.addEventListener('mouseout', e=>{
  const w = e.target.closest('.thumb-wrap');
  if(!w || w.contains(e.relatedTarget)) return;
  clearTimeout(hoverTimer);
  if(window.matchMedia('(hover:hover) and (pointer:fine)').matches) stopPreview(w);
});
function startPreview(w){
  const it = byCode[w.dataset.code];
  if(!it || !it.preview) return;
  const v = w.querySelector('video');
  v.setAttribute('referrerpolicy', 'no-referrer');
  v.referrerPolicy = 'no-referrer';
  if(!v.getAttribute('src')) v.src = it.preview;
  const p = Number(progress[it.code]||0);
  w.classList.add('playing');
  playingSet.add(w);
  if(playingSet.size > 4){
    const oldest = [...playingSet].find(x => x !== w);
    if(oldest) stopPreview(oldest);
  }
  const play = async ()=>{
    try { await v.play(); if(p > 0 && p < 0.95) { try { v.currentTime = p * (v.duration||0); } catch(e){} } } catch(e) {}
  };
  v.onloadedmetadata = play;
  v.ontimeupdate = ()=>{
    if(!v.duration) return;
    const ratio = v.currentTime / v.duration;
    progress[it.code] = ratio;
    localStorage.setItem('fc2progress', JSON.stringify(progress));
    const bar = w.querySelector('.prog i');
    if(bar) bar.style.width = Math.min(100, ratio*100) + '%';
    if(ratio > 0.9) markWatched(it.code);
  };
  play();
}
function stopPreview(w){
  if(!w) return;
  const v = w.querySelector('video');
  v.pause(); v.removeAttribute('src'); v.load();
  w.classList.remove('playing'); playingSet.delete(w);
}
function railHTML(title, arr, ranked, go){
  if(!arr.length) return '';
  return `<section class="section"><h2${go?` data-go="${go}" style="cursor:pointer"`:''}>${title}</h2><div class="rail">${arr.map((it,i)=>cardHTML(it, ranked?i+1:0)).join('')}</div></section>`;
}
function currentList(){ return sortItems(ITEMS.filter(it => matchQuery(it, qEl.value.trim()) && matchFilters(it))); }
function render(){
  const query = qEl.value.trim();
  document.body.classList.toggle('page-search', page==='search');
  document.querySelector('.search-row').classList.toggle('on', page==='search');
  document.getElementById('libTabs').classList.toggle('on', page==='library' || page==='later' || page==='saved');
  const showHome = page==='home' && !query && chip==='all' && !filters.vmin && !filters.dur && !filters.seen && !filters.src;
  if(showHome){
    const day = ITEMS.filter(x => x.missav_rank_day).sort((a,b)=>a.missav_rank_day-b.missav_rank_day).slice(0,10);
    const week = ITEMS.filter(x => x.missav_rank_week).sort((a,b)=>a.missav_rank_week-b.missav_rank_week).slice(0,10);
    const month = ITEMS.filter(x => x.missav_rank_month).sort((a,b)=>a.missav_rank_month-b.missav_rank_month).slice(0,10);
    const total = ITEMS.filter(x => x.missav_rank_total).sort((a,b)=>a.missav_rank_total-b.missav_rank_total).slice(0,10);
    const rise = ITEMS.filter(x => (x.trend_24h||0) > 0 && x.views_source==='Supjav').sort((a,b)=>(b.trend_24h||0)-(a.trend_24h||0)).slice(0,10);
    const pop = ITEMS.filter(x => x.views>0 && x.views_source==='Supjav').sort((a,b)=>b.views-a.views).slice(0,10);
    const news = ITEMS.filter(x => isRecentNew(x) || x.is_new).slice(0,10);
    const recent = ITEMS.filter(x => watched.has(x.code)).sort((a,b)=>(watchTimes[b.code]||0)-(watchTimes[a.code]||0)).slice(0,10);
    const saved = ITEMS.filter(x => favs.has(x.code)).slice(0,10);
    shelves.innerHTML = railHTML('MissAV 今日の人気', day, true, 'rising')
      + railHTML('MissAV 週間人気', week, true, 'rising')
      + railHTML('MissAV 月間人気', month, true, 'rising')
      + railHTML('MissAV 累計人気', total, true, 'rising')
      + railHTML('Supjav 急上昇', rise, true, 'rising')
      + railHTML('Supjav 総再生数', pop, true, 'popular')
      + railHTML('新着', news, false, 'new')
      + railHTML('最近見た作品', recent, false, 'history')
      + railHTML('保存済み', saved, false, 'saved');
  } else {
    shelves.innerHTML = (page==='rising') ? `<div class="chips" style="padding:8px 12px"><button type="button" data-rise="6h"${riseWin==='6h'?' class="on"':''}>6時間</button><button type="button" data-rise="24h"${riseWin==='24h'?' class="on"':''}>24時間</button></div>` : '';
  }
  const titles = {home:'すべての作品', rising:'急上昇', popular:'人気ランキング', new:'新着', search:'検索', history:'履歴', later:'後で見る', saved:'保存済み', library:'ライブラリ'};
  listTitle.textContent = query ? '検索結果' : (titles[page]||'すべての作品');
  let list = currentList();
  if(page==='popular' || page==='rising') list = list.slice(0,100);
  resultCount.textContent = (query ? '検索結果 ' : '') + list.length + '件';
  grid.innerHTML = list.length ? list.map((it,i)=>cardHTML(it, (page==='popular'||page==='rising')?i+1:0)).join('') : '<p class="empty">該当する作品がありません。</p>';
  document.querySelectorAll('[data-go]').forEach(b=>b.addEventListener('click',()=>{ page=b.dataset.go; if(page==='rising') chip='rising'; if(page==='popular') chip='popular'; render(); }));
  document.querySelectorAll('[data-rise]').forEach(b=>b.addEventListener('click',()=>{ riseWin=b.dataset.rise; render(); }));
  document.querySelectorAll('[data-page]').forEach(b=>b.classList.toggle('on', b.dataset.page===page || (page==='library' && b.dataset.page==='library')));
  document.querySelectorAll('[data-chip]').forEach(b=>b.classList.toggle('on', b.dataset.chip===chip));
  document.querySelectorAll('[data-sort]').forEach(b=>b.classList.toggle('on', b.dataset.sort===sort));
}
function addSearch(term){
  term = (term||'').trim();
  if(!term) return;
  searches = [term, ...searches.filter(x=>x!==term)].slice(0,10);
  localStorage.setItem('fc2searchhist', JSON.stringify(searches));
}
function showSuggest(){
  const query = qEl.value.trim();
  let html = '';
  if(!query){
    html = searches.map(s=>`<button class="sug" data-q="${s}"><span>${s}</span><b class="sug-del" data-del="${s}">×</b></button>`).join('')
      + (searches.length ? '<button class="sug" id="clearHist">検索履歴をすべて削除</button>' : '<div class="sug"><span>最近の検索はありません</span></div>');
  } else {
    const num = fc2num(query);
    const scored = ITEMS.map(it=>{
      let score = 0;
      if(it.code_num === num) score += 100;
      else if(num && it.code_num.includes(num)) score += 50;
      if(it.title.toLowerCase().includes(query.toLowerCase())) score += 10;
      return {it, score};
    }).filter(x=>x.score>0).sort((a,b)=>b.score-a.score).slice(0,8);
    html = scored.map(x=>`<button class="sug" data-code="${esc(x.it.code)}"><img src="${esc(x.it.thumb)}" alt=""><div><b>${esc(x.it.code)}</b><span>${esc(x.it.title)}</span></div></button>`).join('') || '<div class="sug"><span>候補なし</span></div>';
  }
  sugEl.innerHTML = html;
  sugEl.classList.add('on');
  sugEl.querySelectorAll('[data-q]').forEach(b=>b.addEventListener('click', e=>{
    if(e.target.dataset.del){ searches = searches.filter(x=>x!==e.target.dataset.del); localStorage.setItem('fc2searchhist', JSON.stringify(searches)); showSuggest(); return; }
    qEl.value = b.dataset.q; addSearch(b.dataset.q); sugEl.classList.remove('on'); render();
  }));
  sugEl.querySelectorAll('[data-code]').forEach(b=>b.addEventListener('click', ()=>{
    const it = byCode[b.dataset.code];
    qEl.value = it.code; addSearch(it.code); sugEl.classList.remove('on'); render();
  }));
  const clr = document.getElementById('clearHist');
  if(clr) clr.addEventListener('click', ()=>{ searches=[]; localStorage.setItem('fc2searchhist','[]'); showSuggest(); });
}
function openMenu(code){
  const it = byCode[code];
  const ranks = [];
  if(it.missav_rank_day) ranks.push('MissAV 今日 #' + it.missav_rank_day);
  if(it.missav_rank_week) ranks.push('週間 #' + it.missav_rank_week);
  if(it.missav_rank_month) ranks.push('月間 #' + it.missav_rank_month);
  if(it.missav_rank_total) ranks.push('累計 #' + it.missav_rank_total);
  menu.innerHTML = `
    ${ranks.length?`<button disabled>${esc(ranks.join(' / '))}</button>`:''}
    <button data-act="later">${later.has(code)?'後で見るから外す':'後で見る'}</button>
    <button data-act="save">${favs.has(code)?'保存を解除':'保存'}</button>
    <button data-act="watched">${watched.has(code)?'視聴済みを解除':'視聴済みにする'}</button>
    <button data-act="unwatch">履歴から削除</button>
    ${it.fc2_market_url?`<button class="ext" data-url="${esc(it.fc2_market_url)}">FC2公式</button>`:''}
    ${Object.entries(it.sources||{}).map(([n,u])=>`<button class="ext" data-url="${esc(u)}">${esc(n)}</button>`).join('')}
    ${Object.entries(it.search_links||{}).filter(([n])=>!(it.sources||{})[n]).map(([n,u])=>`<button class="ext" data-url="${esc(u)}">${esc(n)}検索</button>`).join('')}`;
  openSheet(menu);
  menu.querySelectorAll('button[data-act],button[data-url]').forEach(b=>b.addEventListener('click', ()=>{
    if(b.dataset.url){ markWatched(code); window.open(b.dataset.url, '_blank'); }
    if(b.dataset.act==='later'){ later.has(code)?later.delete(code):later.add(code); saveSet('fc2watchlater', later); }
    if(b.dataset.act==='save'){ favs.has(code)?favs.delete(code):favs.add(code); saveSet('fc2favs', favs); }
    if(b.dataset.act==='watched'){ watched.has(code)?unmarkWatched(code):markWatched(code); }
    if(b.dataset.act==='unwatch'){ unmarkWatched(code); }
    menu.classList.remove('on'); document.getElementById('sheetBg').classList.remove('on'); render();
  }));
}
qEl.addEventListener('input', ()=>{ render(); showSuggest(); });
qEl.addEventListener('focus', showSuggest);
qEl.addEventListener('keydown', e=>{ if(e.key==='Enter'){ addSearch(qEl.value); sugEl.classList.remove('on'); }});
document.addEventListener('click', e=>{
  if(!e.target.closest('.search-wrap')) sugEl.classList.remove('on');
  if(!e.target.closest('.menu') && !e.target.closest('.more') && !e.target.closest('.panel') && !e.target.closest('#filterBtn')){
    panel.classList.remove('on'); menu.classList.remove('on');
    document.getElementById('sheetBg').classList.remove('on');
  }
});
const sheetBg = document.getElementById('sheetBg');
function openSheet(el){ el.classList.add('on'); sheetBg.classList.add('on'); }
document.getElementById('filterBtn').addEventListener('click', ()=>openSheet(panel));
sheetBg.addEventListener('click', ()=>{ panel.classList.remove('on'); menu.classList.remove('on'); sheetBg.classList.remove('on'); });
document.querySelectorAll('[data-page]').forEach(b=>b.addEventListener('click', ()=>{
  page=b.dataset.page;
  if(page==='rising') chip='rising';
  if(page==='popular') chip='popular';
  if(page==='new') chip='new';
  if(page==='home') chip='all';
  if(page==='search'){ chip='all'; document.querySelector('.search-row').classList.add('on'); setTimeout(()=>qEl.focus(), 50); }
  if(page==='library') page='later';
  render();
}));
document.querySelectorAll('[data-lib]').forEach(b=>b.addEventListener('click', ()=>{ page=b.dataset.lib; render(); }));
document.querySelectorAll('[data-chip]').forEach(b=>b.addEventListener('click', ()=>{ chip=b.dataset.chip; page='home'; render(); }));
document.querySelectorAll('[data-sort]').forEach(b=>b.addEventListener('click', ()=>{ sort=b.dataset.sort; render(); }));
panel.querySelectorAll('button').forEach(b=>b.addEventListener('click', ()=>{
  filters[b.dataset.f] = isNaN(Number(b.dataset.v)) ? b.dataset.v : Number(b.dataset.v);
  panel.querySelectorAll('[data-f="'+b.dataset.f+'"]').forEach(x=>x.classList.toggle('on', x===b));
  render();
}));
render();
</script>
</body></html>
"""


def main() -> int:
    print(f"[{now_jst()}] チェック開始")
    try:
        latest = get_latest_videos()
    except Exception as e:
        print(f"取得失敗: {e}", file=sys.stderr)
        send_telegram(f"監視エラー: ページ取得に失敗しました\n{e}")
        return 1
    if not latest:
        send_telegram("監視エラー: 動画リストを抽出できませんでした。")
        return 1

    history = load_json(HISTORY_FILE, {"ids": []})
    known = set(history.get("ids", []))
    existing = load_json(DATA_FILE, {"items": []})
    existing_map = {item["code"]: item for item in existing.get("items", [])}
    first_run = not known
    new_videos, merged = [], []
    stamp = now_jst()

    for video in latest:
        old = existing_map.get(video["code"], {})
        is_new = video["code"] not in known and not first_run
        item = {**video, "first_seen": old.get("first_seen", stamp), "last_seen": stamp, "is_new": is_new}
        if old.get("title") and not is_better_title(item.get("title", ""), old.get("title", "")):
            item["title"] = old["title"]
        item["views"] = video.get("views") or old.get("views")
        item["views_source"] = video.get("views_source") or old.get("views_source") or ""
        item["views_checked_at"] = video.get("views_checked_at") or old.get("views_checked_at") or 0
        item["last_views_fetch"] = video.get("last_views_fetch") or old.get("last_views_fetch") or 0
        item["last_views_attempt"] = video.get("last_views_attempt") or old.get("last_views_attempt") or 0
        item["missav_rank_day"] = old.get("missav_rank_day")
        item["missav_rank_week"] = old.get("missav_rank_week")
        item["missav_rank_month"] = old.get("missav_rank_month")
        item["missav_rank_total"] = old.get("missav_rank_total")
        item["missav_rank_updated_at"] = old.get("missav_rank_updated_at") or ""
        item["fc2_market_url"] = old.get("fc2_market_url") or ""
        item["fc2_rating"] = old.get("fc2_rating")
        item["fc2_review_count"] = old.get("fc2_review_count")
        item["fc2_market_checked_at"] = old.get("fc2_market_checked_at") or 0
        item["fc2_title"] = old.get("fc2_title") or ""
        item["title_source"] = old.get("title_source") or item.get("title_source") or ""
        item["duration"] = video.get("duration") or old.get("duration", "")
        if old.get("sources"):
            item["sources"] = {**old.get("sources", {}), **item.get("sources", {})}
        merged.append(item)
        if is_new:
            new_videos.append(item)
        known.add(video["code"])

    latest_codes = {v["code"] for v in latest}
    for item in existing.get("items", []):
        if item["code"] not in latest_codes:
            item["is_new"] = False
            merged.append(item)
    if KEEP_ITEMS > 0:
        merged = merged[:KEEP_ITEMS]

    refresh_japanese_titles(merged)
    fill_missing_views(merged)
    enrich_fc2_market(merged)
    apply_missav_ranks(merged, scrape_missav_rankings(), stamp)
    update_views_history(merged)
    save_json(DATA_FILE, {"updated_at": stamp, "items": merged})
    save_json(HISTORY_FILE, {"updated_at": stamp, "ids": sorted(known)})
    HTML_FILE.parent.mkdir(parents=True, exist_ok=True)
    HTML_FILE.write_text(render_html(merged, stamp, len(new_videos)), encoding="utf-8")

    if first_run:
        send_telegram("監視を開始しました。\n今後の新着だけ通知します。\n\n現在の最新:\n" + "\n".join(f"- {v['code']}" for v in latest[:8]))
        print("初回保存完了")
        return 0

    def allowed(video):
        text = f"{video.get('code', '')} {video.get('title', '')}"
        if EXCLUDE_KEYWORDS and any(k.lower() in text.lower() for k in EXCLUDE_KEYWORDS):
            return False
        if INCLUDE_KEYWORDS and not any(k.lower() in text.lower() for k in INCLUDE_KEYWORDS):
            return False
        return True

    new_videos = [v for v in new_videos if allowed(v)]
    if not new_videos:
        print("新着なし")
        return 0
    for video in reversed(new_videos):
        source_lines = [f"{n}: {u}" for n, u in (video.get("sources") or {video.get("source", "Link"): video.get("url")}).items()]
        extra = []
        if video.get("duration"):
            extra.append(f"時間: {video['duration']}")
        if video.get("views"):
            extra.append(f"再生: {video['views']:,}")
        send_telegram(
            "【新着 FC2-PPV】\n\n" + f"{video['code']}\n{video['title']}\n" + (" / ".join(extra) + "\n\n" if extra else "\n") + "\n".join(source_lines),
            photo=video.get("thumb"),
        )
        print(f"通知: {video['code']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
