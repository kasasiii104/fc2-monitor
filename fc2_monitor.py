#!/usr/bin/env python3
import html
import hashlib
import shutil
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
FC2CMADB_URL = os.environ.get("FC2CMADB_URL", "https://fc2cmadb.com").rstrip("/")
FC2CMADB_FETCH_LIMIT = int(os.environ.get("FC2CMADB_FETCH_LIMIT", "24"))
H_WALKER_URL = os.environ.get("H_WALKER_URL", "https://fc2cm.h-walker.net").rstrip("/")
H_WALKER_PAGES = int(os.environ.get("H_WALKER_PAGES", "20"))
H_WALKER_CURSOR_KEY = "hwalker_next_url"
H_WALKER_STATS_KEY = "hwalker_crawl_stats"
H_WALKER_REFRESH_SEC = int(os.environ.get("H_WALKER_REFRESH_SEC", str(6 * 3600)))
CONTINUOUS_BACKFILL_CURSOR_KEY = "metadata_backfill_v2_cursor"  # legacy; no longer used for selection
CONTINUOUS_BACKFILL_STATS_KEY = "metadata_backfill_v3_stats"
BACKFILL_ATTEMPTS_KEY = "metadata_backfill_v3_attempts"
BACKFILL_RETRY_BASE_SEC = int(os.environ.get("BACKFILL_RETRY_BASE_SEC", str(12 * 3600)))
BACKFILL_RETRY_MAX_SEC = int(os.environ.get("BACKFILL_RETRY_MAX_SEC", str(3 * 86400)))
BACKFILL_OFFICIAL_LIMIT = int(os.environ.get("BACKFILL_OFFICIAL_LIMIT", "500"))
FC2_SAMPLE_FETCH_LIMIT = int(os.environ.get("FC2_SAMPLE_FETCH_LIMIT", "24"))
THUMB_BACKFILL_LIMIT = int(os.environ.get("THUMB_BACKFILL_LIMIT", "200"))
THUMB_BACKFILL_ATTEMPTS_KEY = "thumbnail_backfill_v1_attempted_at"
THUMB_BACKFILL_STATS_KEY = "thumbnail_backfill_v1_stats"
VIEW_FETCH_LIMIT = int(os.environ.get("VIEW_FETCH_LIMIT", "50"))
TITLE_FETCH_LIMIT = int(os.environ.get("TITLE_FETCH_LIMIT", "30"))
VIEW_REFRESH_SEC = int(os.environ.get("VIEW_REFRESH_SEC", str(8 * 3600)))
FAIL_SKIP_SEC = 12 * 3600
INCLUDE_KEYWORDS = [x.strip() for x in os.environ.get("INCLUDE_KEYWORDS", "").split(",") if x.strip()]
EXCLUDE_KEYWORDS = [x.strip() for x in os.environ.get("EXCLUDE_KEYWORDS", "").split(",") if x.strip()]
JST = timezone(timedelta(hours=9))
DURATION_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
INVALID_TITLE_SNIPPETS = (
    "お探しの商品が見つかりませんでした",
    "お探しの商品は見つかりませんでした",
    "商品が見つかりませんでした",
    "商品は見つかりませんでした",
    "product not found",
    "item not found",
    "page not found",
    "404 not found",
)


def is_invalid_title(text: str) -> bool:
    value = html.unescape(re.sub(r"\s+", " ", (text or ""))).strip().lower()
    return bool(value) and any(x.lower() in value for x in INVALID_TITLE_SNIPPETS)


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
    title = html.unescape(re.sub(r"\s+", " ", (text or "")).strip())
    if is_invalid_title(title):
        return ""
    # Never let source URLs/path fragments leak into the visible title.
    title = re.sub(r"https?://\S+", " ", title, flags=re.I)
    title = re.sub(r"\bwww\.\S+", " ", title, flags=re.I)
    title = re.sub(r"(?:^|\s)/(?:ja/)?fc2[-_/ ]?ppv[-_/ ]?\d{6,8}(?:\S*)?", " ", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip()
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
        "FC2CMADB": f"{FC2CMADB_URL}/articles/{code_num}",
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
            # Use href only to identify the FC2 code. Do not feed it into the
            # title candidate, otherwise relative/absolute URLs can leak into
            # the visible title.
            raw = " ".join([a.get("title") or "", (img.get("alt") if img else "") or "", a.get_text(" ", strip=True)])
            m = CODE_RE.search(href + " " + raw) or re.search(r"/fc2-ppv-(\d{6,8})", href, re.I)
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
    if not title or is_invalid_title(title) or title in {
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


def _number_value(raw):
    if raw is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(raw).replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def extract_fc2_market_rating(soup, text: str):
    """Read FC2 rating from structured data/meta first, then visible text."""
    # Schema.org / JSON-LD is more stable than translated visible labels.
    for script in soup.find_all("script"):
        body = script.string or script.get_text() or ""
        if "ratingValue" not in body and "aggregateRating" not in body:
            continue
        try:
            data = json.loads(body)
        except Exception:
            data = None
        stack = data if isinstance(data, list) else [data]
        for node in stack:
            if not isinstance(node, dict):
                continue
            agg = node.get("aggregateRating")
            if isinstance(agg, dict):
                value = _number_value(agg.get("ratingValue"))
                if value is not None and 0 <= value <= 5:
                    return round(value, 2)
        m = re.search(r'["\']ratingValue["\']\s*:\s*["\']?([0-5](?:\.\d+)?)', body, re.I)
        if m:
            return float(m.group(1))

    for attr in (
        {"itemprop": "ratingValue"},
        {"property": "ratingValue"},
        {"name": "ratingValue"},
    ):
        tag = soup.find(attrs=attr)
        if tag:
            raw = tag.get("content") or tag.get("value") or tag.get_text(" ", strip=True)
            value = _number_value(raw)
            if value is not None and 0 <= value <= 5:
                return round(value, 2)

    patterns = (
        r"(?:Average\s*Rating|平均評価|評価)\s*[:：]?\s*([0-5](?:\.\d+)?)\s*(?:/\s*5)?",
        r"([0-5](?:\.\d+)?)\s*/\s*5",
        r"5\s*点満点(?:中|で)?\s*([0-5](?:\.\d+)?)",
    )
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return float(m.group(1))
    return None


def extract_fc2_market_review_count(soup, text: str):
    """Read FC2 review/rating count from structured data/meta or visible text."""
    for script in soup.find_all("script"):
        body = script.string or script.get_text() or ""
        if not re.search(r"reviewCount|ratingCount|aggregateRating", body, re.I):
            continue
        try:
            data = json.loads(body)
        except Exception:
            data = None
        stack = data if isinstance(data, list) else [data]
        for node in stack:
            if not isinstance(node, dict):
                continue
            agg = node.get("aggregateRating")
            if isinstance(agg, dict):
                for key in ("reviewCount", "ratingCount"):
                    value = _number_value(agg.get(key))
                    if value is not None and value >= 0:
                        return int(value)
        m = re.search(r'["\'](?:reviewCount|ratingCount)["\']\s*:\s*["\']?([\d,]+)', body, re.I)
        if m:
            return int(m.group(1).replace(",", ""))

    for prop in ("reviewCount", "ratingCount"):
        tag = soup.find(attrs={"itemprop": prop}) or soup.find(attrs={"name": prop})
        if tag:
            raw = tag.get("content") or tag.get("value") or tag.get_text(" ", strip=True)
            value = _number_value(raw)
            if value is not None and value >= 0:
                return int(value)

    patterns = (
        r"Product\s*Review\s*[\(（]\s*([\d,]+)\s*[\)）]",
        r"商品レビュー\s*[\(（]\[]?\s*([\d,]+)\s*(?:件)?\s*[\)）\]]?",
        r"(?:レビュー|評価)\s*[:：]?\s*([\d,]+)\s*件",
    )
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return int(m.group(1).replace(",", ""))
    return None


def fetch_fc2_market(code_num):
    url = f"https://adult.contents.fc2.com/article/{code_num}/?lang=ja"
    info = fetch_page(url)
    print(f"[FC2 Market] FC2-PPV-{code_num} status={info.get('status')} cloudflare={info.get('cloudflare')}")
    access = {"http_status": info.get("status") or 0}
    if "redirect.fc2.com/ekyc_auth" in (info.get("final_url") or ""):
        print(f"[FC2 Market] FC2-PPV-{code_num} blocked_by=eKYC")
        return {**access, "outcome": "blocked", "blocked": "eKYC", "reason": "eKYC"}
    if info.get("status") in (404, 410):
        return {**access, "outcome": "not_found", "not_found": True}
    if not (info.get("ok") and info.get("status") == 200 and not info.get("cloudflare") and info.get("soup")):
        reason = "network_error" if info.get("error") else ("challenge" if info.get("cloudflare") else "http_or_invalid_page")
        return {**access, "outcome": "failed", "reason": reason}

    soup = info["soup"]
    text = soup.get_text(" ", strip=True)
    final_url = info.get("final_url") or url
    raw_html = info.get("text") or ""
    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if is_invalid_title(page_title) or is_invalid_title(text[:1200]):
        print(f"[FC2 Market] FC2-PPV-{code_num} not_found=True")
        return {**access, "outcome": "not_found", "not_found": True}
    fc2_title = extract_fc2_market_title(soup, code_num)

    # FC2 can omit the numeric ID from visible text/metadata even while the
    # requested article URL itself is valid. The old check rejected those
    # pages and caused "status=200" followed by updated=0.
    article_path_ok = f"/article/{code_num}" in final_url
    code_visible = str(code_num) in (raw_html + " " + text)
    if not article_path_ok and not code_visible:
        print(f"[FC2 Market] FC2-PPV-{code_num} rejected final_url={final_url}")
        return {**access, "outcome": "failed", "reason": "unexpected_page"}

    rating = extract_fc2_market_rating(soup, text)
    count = extract_fc2_market_review_count(soup, text)
    print(
        f"[FC2 Market data] FC2-PPV-{code_num} "
        f"title={bool(fc2_title)} rating={rating!r} reviews={count!r}"
    )
    return {
        **access,
        "outcome": "success",
        "fc2_market_url": final_url,
        "fc2_rating": rating,
        "fc2_review_count": count,
        "fc2_title": fc2_title,
    }



def sanitize_item_titles(items) -> None:
    """Remove URLs/path fragments and purge FC2 soft-404 titles."""
    for item in items:
        code = item.get("code") or ""
        num = item.get("code_num") or code.split("-")[-1]
        had_invalid = is_invalid_title(item.get("title", "")) or is_invalid_title(item.get("fc2_title", ""))
        cleaned = clean_title(item.get("title", ""), num)
        item["title"] = cleaned or code or f"FC2-PPV-{num}"
        if is_invalid_title(item.get("fc2_title", "")):
            item["fc2_title"] = ""
        if had_invalid:
            item["fc2_market_not_found"] = True
            item["fc2_market_url"] = ""
            if item.get("title_source") == "FC2公式":
                item["title_source"] = ""


def _fc2cmadb_payload_from_html(text: str):
    soup = BeautifulSoup(text or "", "html.parser")
    script = soup.select_one('script[data-page="app"]')
    if script:
        raw = script.string or script.get_text() or ""
        if raw.strip():
            try:
                return json.loads(html.unescape(raw))
            except Exception:
                pass
    for node in soup.select("[data-page]"):
        raw = node.get("data-page")
        if not raw or raw == "app":
            continue
        try:
            return json.loads(html.unescape(raw))
        except Exception:
            continue
    return None


def _fc2cmadb_article(payload):
    if not isinstance(payload, dict):
        return None
    props = payload.get("props")
    if isinstance(props, dict) and isinstance(props.get("article"), dict):
        return props.get("article")
    if isinstance(payload.get("article"), dict):
        return payload.get("article")
    return None


def _fc2cmadb_title_from_payload(payload, code_num: str) -> str:
    article = _fc2cmadb_article(payload)
    if not isinstance(article, dict):
        return ""
    raw = article.get("title") or article.get("name") or ""
    title = clean_title(str(raw), code_num)
    # The fallback is specifically for Japanese titles. Do not replace a title
    # with Chinese-only text from the mirror.
    return title if re.search(r"[ぁ-んァ-ン]", title) else ""


def fetch_fc2cmadb_title(code_num: str):
    url = f"{FC2CMADB_URL}/articles/{code_num}"
    base_headers = {
        **HEADERS,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Referer": f"{FC2CMADB_URL}/",
        "Cookie": "ageVerified=true",
    }

    attempts = [
        base_headers,
        {
            **base_headers,
            "Accept": "application/json,text/plain,*/*",
            "X-Inertia": "true",
            "X-Requested-With": "XMLHttpRequest",
            "X-Inertia-Partial-Component": "Articles/Show",
            "X-Inertia-Partial-Data": "article",
        },
    ]

    last_status = 0
    for headers in attempts:
        try:
            res = requests.get(url, headers=headers, timeout=25, allow_redirects=True)
            last_status = res.status_code
            if res.status_code != 200:
                continue
            payload = None
            ctype = (res.headers.get("content-type") or "").lower()
            if "json" in ctype or (res.text or "").lstrip().startswith("{"):
                try:
                    payload = res.json()
                except Exception:
                    payload = None
            if payload is None:
                payload = _fc2cmadb_payload_from_html(res.text or "")
            title = _fc2cmadb_title_from_payload(payload, code_num)
            if title:
                print(f"[FC2CMADB] FC2-PPV-{code_num} status=200 title=True")
                return {"title": title, "url": str(res.url or url)}
        except Exception as e:
            print(f"[FC2CMADB] FC2-PPV-{code_num} error={type(e).__name__}")
    print(f"[FC2CMADB] FC2-PPV-{code_num} status={last_status} title=False")
    if last_status == 403:
        return {"blocked": True, "status": 403}
    return None


def refresh_fc2cmadb_titles(items) -> None:
    state = load_json(FETCH_STATE_FILE, {})
    failed = state.get("fc2cmadb_failed") if isinstance(state.get("fc2cmadb_failed"), dict) else {}
    cursor = int(state.get("fc2cmadb_cursor") or 0)
    now = now_ts()
    blocked_at = int(state.get("fc2cmadb_blocked_at") or 0)
    if blocked_at and now - blocked_at < FAIL_SKIP_SEC:
        print(f"[FC2CMADB] host_blocked=true retry_in={FAIL_SKIP_SEC - (now - blocked_at)}s")
        return
    targets = [
        x for x in items
        if needs_jp_title(x.get("title", "")) and x.get("title_source") != "FC2公式"
    ]
    tried = updated = 0
    for item in rotate_items(targets, cursor):
        if tried >= FC2CMADB_FETCH_LIMIT:
            break
        code = item.get("code") or ""
        if now - int(failed.get(code) or 0) < FAIL_SKIP_SEC:
            continue
        tried += 1
        num = item.get("code_num") or code.split("-")[-1]
        meta = fetch_fc2cmadb_title(num)
        if meta and meta.get("blocked"):
            state["fc2cmadb_blocked_at"] = now
            print("[FC2CMADB] host_blocked=true fallback=FC2ウォーカー")
            break
        if not meta:
            failed[code] = now
            continue
        title = meta.get("title") or ""
        if title and item.get("title") != title:
            item["title"] = title
            item["title_source"] = "FC2CMADB"
            item["fc2cmadb_url"] = meta.get("url") or f"{FC2CMADB_URL}/articles/{num}"
            item["fc2cmadb_checked_at"] = now
            item.setdefault("sources", {})["FC2CMADB"] = item["fc2cmadb_url"]
            updated += 1
            print(f"FC2CMADBタイトル更新: {code} -> {title[:60]}")
        failed.pop(code, None)

    state["fc2cmadb_cursor"] = (cursor + max(tried, 1)) % max(len(targets), 1)
    state["fc2cmadb_failed"] = failed
    save_json(FETCH_STATE_FILE, state)
    print(f"[FC2CMADB] tried={tried} title_updated={updated}")



def _hwalker_count(text: str):
    raw = re.sub(r"[^0-9]", "", text or "")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 0 <= value < 10000000 else None


def _hwalker_next_url(soup, current_url: str) -> str:
    """Follow Walker's real pagination link instead of guessing its URL shape."""
    for a in soup.find_all("a", href=True):
        label = re.sub(r"\\s+", "", a.get_text(" ", strip=True))
        if "次の50件" in label or label.startswith("次の"):
            nxt = urljoin(current_url, a.get("href") or "")
            if nxt.startswith(H_WALKER_URL):
                return nxt
    return ""


def scrape_hwalker_market(start_url=None, page_limit=None):
    """Harvest Walker pages by following the site's actual next-page links."""
    found = {}
    url = start_url or (H_WALKER_URL + "/")
    limit = max(1, int(page_limit or H_WALKER_PAGES))
    visited = set()
    pages_done = 0
    next_url = url

    while next_url and pages_done < limit:
        url = next_url
        if url in visited:
            print(f"[FC2 Walker] pagination_cycle url={url}")
            next_url = ""
            break
        visited.add(url)
        info = fetch_page(url)
        if not (info.get("status") == 200 and info.get("soup") is not None):
            print(f"[FC2 Walker] page={pages_done + 1} status={info.get('status')} items=0 url={url}")
            # Keep the cursor on the failed page so a later run can retry it.
            next_url = url
            break

        soup = info["soup"]
        page_found = 0
        samples = []
        for row in soup.find_all("tr"):
            links = row.find_all("a", href=True)
            product = None
            num = ""
            for link in links:
                href = link.get("href") or ""
                m = re.search(r"(?:[?&])aid=(\\d{5,8})(?:&|$)", href)
                if m and "adult.contents.fc2.com" in href:
                    product = link
                    num = m.group(1)
                    break
            if not product or not num:
                continue

            title = clean_title(product.get_text(" ", strip=True) or product.get("title") or "", num)
            if not title:
                for link in links:
                    raw = link.get("title") or link.get_text(" ", strip=True)
                    candidate = clean_title(raw, num)
                    if candidate and len(candidate) > 6:
                        title = candidate
                        break

            cells = row.find_all(["td", "th"])
            cell_texts = [re.sub(r"\\s+", " ", c.get_text(" ", strip=True)).strip() for c in cells]
            rating = None
            review_count = None
            rating_idx = -1
            for idx, txt in enumerate(cell_texts):
                if re.fullmatch(r"[0-5](?:\\.\\d{1,2})", txt):
                    try:
                        rating = float(txt)
                        rating_idx = idx
                        break
                    except ValueError:
                        pass
            if rating_idx >= 0:
                for txt in cell_texts[rating_idx + 1:]:
                    count = _hwalker_count(txt)
                    if count is not None:
                        review_count = count
                        break

            code = f"FC2-PPV-{num}"
            found[code] = {
                "title": title,
                "rating": rating,
                "review_count": review_count,
                "source_url": url,
            }
            page_found += 1
            if len(samples) < 3:
                samples.append(f"{code}:{rating!r}/{review_count!r}")

        pages_done += 1
        candidate_next = _hwalker_next_url(soup, url)
        print(
            f"[FC2 Walker] crawl_page={pages_done} status={info.get('status')} "
            f"items={page_found} unique_total={len(found)} "
            f"samples={' '.join(samples)} next={candidate_next}"
        )
        if not candidate_next or candidate_next == url:
            next_url = ""
            break
        next_url = candidate_next

    return found, next_url, pages_done


def enrich_hwalker_market(items) -> None:
    state = load_json(FETCH_STATE_FILE, {})
    now = now_ts()
    last = int(state.get("hwalker_last_success") or 0)
    if last and now - last < H_WALKER_REFRESH_SEC:
        print(f"[FC2 Walker] skip fresh=true age={now-last}s")
        return

    start_url = state.get(H_WALKER_CURSOR_KEY) or (H_WALKER_URL + "/")
    records, next_url, pages_done = scrape_hwalker_market(start_url=start_url)
    if not records:
        print("[FC2 Walker] harvested=0 keep_previous=true")
        return

    # End of pagination means one complete pass; restart from the first page
    # on the next eligible run so ratings/reviews can refresh over time.
    state[H_WALKER_CURSOR_KEY] = next_url or (H_WALKER_URL + "/")
    state["hwalker_last_success"] = now

    matched = rating_updated = review_updated = title_updated = 0
    for item in items:
        code = item.get("code") or ""
        rec = records.get(code)
        if not rec:
            continue
        matched += 1
        item["hwalker_checked_at"] = now
        item["hwalker_url"] = rec.get("source_url") or H_WALKER_URL
        title = (rec.get("title") or "").strip()
        if title and title_score(title)[0] > 0 and needs_jp_title(item.get("title", "")):
            item["title"] = title
            item["title_source"] = "FC2ウォーカー"
            title_updated += 1

        rating = rec.get("rating")
        if isinstance(rating, (int, float)) and 0 <= rating <= 5:
            # Never downgrade metadata already verified directly from FC2.
            if item.get("fc2_rating_source") != "FC2公式":
                if item.get("fc2_rating") != rating:
                    rating_updated += 1
                item["fc2_rating"] = float(rating)
                item["fc2_rating_source"] = "FC2ウォーカー"
                item["fc2_market_checked_at"] = now

        count = rec.get("review_count")
        if isinstance(count, int) and count >= 0 and item.get("fc2_rating_source") != "FC2公式":
            if item.get("fc2_review_count") != count:
                review_updated += 1
            item["fc2_review_count"] = count
            item["fc2_rating_source"] = "FC2ウォーカー"
            item["fc2_market_checked_at"] = now

    stats = {
        "last_run": now,
        "start_url": start_url,
        "next_url": state[H_WALKER_CURSOR_KEY],
        "pages": pages_done,
        "harvested": len(records),
        "matched": matched,
        "title_updated": title_updated,
        "rating_updated": rating_updated,
        "review_updated": review_updated,
    }
    state[H_WALKER_STATS_KEY] = stats
    save_json(FETCH_STATE_FILE, state)
    print(f"[FC2 Walker] stats={stats}")

def _fc2_map(state, key):
    if not isinstance(state.get(key), dict):
        state[key] = {}
    return state[key]


def load_fc2_market_state(items):
    """One record per code for new, backfill and refresh; migrate legacy facts."""
    state = load_json(FETCH_STATE_FILE, {})
    attempts = _fc2_map(state, BACKFILL_ATTEMPTS_KEY)
    checked = _fc2_map(state, "fc2_market_checked")
    failed = _fc2_map(state, "fc2_market_failed")
    item_map = {x.get("code"): x for x in items if x.get("code")}
    for code in set(checked) | set(failed) | set(item_map):
        item = item_map.get(code, {})
        rec = attempts.get(code)
        rec = rec if isinstance(rec, dict) else {}
        success_at = max(int(checked.get(code) or 0), int(item.get("fc2_market_last_success") or 0))
        failure_at = int(failed.get(code) or 0)
        last = max(success_at, failure_at, int(item.get("fc2_market_last_attempt") or 0))
        if last > int(rec.get("last_attempt") or 0) or (not rec and item.get("fc2_market_not_found")):
            status = "not_found" if item.get("fc2_market_not_found") else (
                "failed" if failure_at >= success_at and failure_at else (
                    "success" if success_at == last and success_at else "unknown"
                )
            )
            attempts[code] = {
                **rec, "last_attempt": last, "status": status,
                "failures": max(1, int(rec.get("failures") or 0)) if status == "failed" else 0,
                "last_success": max(int(rec.get("last_success") or 0), success_at) if status != "not_found" else int(rec.get("last_success") or 0),
                "attempts": max(1, int(rec.get("attempts") or 0)),
                "last_route": "legacy", "reason": "migrated",
            }
            # A migrated timestamp supersedes any retry deadline from an older record.
            attempts[code].pop("next_retry_at", None)
    # Recover successful metadata if a previous run saved access state but was
    # interrupted before the final data.json write. No extra network attempt.
    for code, item in item_map.items():
        rec = attempts.get(code) or {}
        success_at = int(rec.get("last_success") or 0)
        item["fc2_market_last_attempt"] = max(int(item.get("fc2_market_last_attempt") or 0), int(rec.get("last_attempt") or 0))
        if rec.get("metadata") and success_at > int(item.get("fc2_market_last_success") or 0):
            apply_fc2_market_metadata(item, rec["metadata"], success_at)
        if rec.get("status") == "not_found":
            item["fc2_market_not_found"] = True
            item["fc2_market_url"] = ""
    return state


def fc2_retry_delay(failures):
    return min(BACKFILL_RETRY_MAX_SEC, BACKFILL_RETRY_BASE_SEC * 2 ** min(max(0, failures - 1), 6))


def fc2_market_blocked(state, now):
    blocked_at = int(state.get("fc2_market_ekyc_blocked_at") or 0)
    return bool(blocked_at and now - blocked_at < FAIL_SKIP_SEC)


def fc2_market_due(item, state, now):
    if item.get("fc2_market_not_found") or fc2_market_blocked(state, now):
        return False
    rec = _fc2_map(state, BACKFILL_ATTEMPTS_KEY).get(item.get("code")) or {}
    if rec.get("status") == "not_found":
        return False
    last = int(rec.get("last_attempt") or 0)
    if not last:
        return True
    delay = (BACKFILL_RETRY_BASE_SEC if _needs_fc2_metadata(item) else FC2_MARKET_REFRESH_SEC) if rec.get("status") == "success" else (
        FAIL_SKIP_SEC if rec.get("status") == "blocked" else fc2_retry_delay(int(rec.get("failures") or 1))
    )
    return now >= int(rec.get("next_retry_at") or last + delay)


def apply_fc2_market_metadata(item, meta, now):
    counts = {"title_updated": 0, "rating_updated": 0, "review_updated": 0}
    item["fc2_market_not_found"] = False
    item["fc2_market_url"] = meta.get("fc2_market_url") or item.get("fc2_market_url") or ""
    title = (meta.get("fc2_title") or "").strip()
    if title:
        item["fc2_title"] = title
        if re.search(r"[ぁ-んァ-ン]", title):
            counts["title_updated"] = int(item.get("title") != title)
            item["title"] = title
            item["title_source"] = "FC2公式"
    rating, review_count = meta.get("fc2_rating"), meta.get("fc2_review_count")
    if rating is not None:
        counts["rating_updated"] = int(item.get("fc2_rating") != rating or item.get("fc2_rating_source") != "FC2公式")
        item["fc2_rating"] = rating
        item["fc2_rating_source"] = "FC2公式"
    if review_count is not None:
        counts["review_updated"] = int(item.get("fc2_review_count") != review_count)
        item["fc2_review_count"] = review_count
        # A review count alone must not relabel a fallback rating as official.
        if rating is not None or item.get("fc2_rating") is None:
            item["fc2_rating_source"] = "FC2公式"
    item["fc2_market_checked_at"] = now
    item["fc2_market_last_success"] = now
    return counts


def record_fc2_market_result(item, meta, state, now, route):
    """Record every actual official article access, including blocks and failures.

    Failure never erases a good title/rating. Blocks are global access failures,
    not product failures, so they do not increase the per-product failure count.
    """
    code = item["code"]
    attempts = _fc2_map(state, BACKFILL_ATTEMPTS_KEY)
    checked = _fc2_map(state, "fc2_market_checked")
    failed = _fc2_map(state, "fc2_market_failed")
    previous = attempts.get(code) or {}
    meta = meta or {"outcome": "failed", "reason": "empty_response"}
    status = meta.get("outcome") or ("blocked" if meta.get("blocked") else "not_found" if meta.get("not_found") else "success")
    rec = {
        **previous, "last_attempt": now, "status": status,
        "attempts": int(previous.get("attempts") or (1 if previous.get("last_attempt") else 0)) + 1,
        "failures": int(previous.get("failures") or 0), "last_route": route,
        "http_status": meta.get("http_status"), "reason": meta.get("reason") or "",
    }
    counts = {"title_updated": 0, "rating_updated": 0, "review_updated": 0}
    item["fc2_market_last_attempt"] = now
    if status == "blocked":
        state["fc2_market_ekyc_blocked_at"] = now
        rec["next_retry_at"] = now + FAIL_SKIP_SEC
    elif status == "failed":
        rec["failures"] += 1
        rec["next_retry_at"] = now + fc2_retry_delay(rec["failures"])
        failed[code] = now
    elif status == "not_found":
        rec.update(failures=0, next_retry_at=None)
        item["fc2_market_not_found"] = True
        item["fc2_market_url"] = ""
        item["fc2_market_checked_at"] = now
        checked[code] = now
        failed.pop(code, None)
    else:
        rec.update(failures=0, last_success=now)
        counts = apply_fc2_market_metadata(item, meta, now)
        rec["metadata"] = {**(previous.get("metadata") or {}), **{k: meta[k] for k in ("fc2_market_url", "fc2_title", "fc2_rating", "fc2_review_count") if meta.get(k) is not None and meta.get(k) != ""}}
        rec["next_retry_at"] = now + (BACKFILL_RETRY_BASE_SEC if _needs_fc2_metadata(item) else FC2_MARKET_REFRESH_SEC)
        checked[code] = now
        failed.pop(code, None)
        state.pop("fc2_market_ekyc_blocked_at", None)
    attempts[code] = rec
    return {"status": status, **counts}


def attempt_fc2_market(item, state, route):
    num = str(item.get("code_num") or item.get("code", "").split("-")[-1])
    if not num.isdigit():
        return None
    try:
        meta = fetch_fc2_market(num)
    except Exception as exc:
        meta = {"outcome": "failed", "reason": type(exc).__name__}
    return record_fc2_market_result(item, meta, state, now_ts(), route)


def _fc2_batch(items, state, route, limit):
    counts = dict(tried=0, success=0, not_found=0, transient_failed=0,
                  official_blocked=False, title_updated=0, rating_updated=0, review_updated=0)
    for item in items:
        if counts["tried"] >= limit or fc2_market_blocked(state, now_ts()):
            break
        if not fc2_market_due(item, state, now_ts()):
            continue
        result = attempt_fc2_market(item, state, route)
        if result is None:
            continue
        counts["tried"] += 1
        status = result["status"]
        if status in ("success", "not_found"):
            counts[status] += 1
        elif status == "failed":
            counts["transient_failed"] += 1
        elif status == "blocked":
            counts["official_blocked"] = True
        for key in ("title_updated", "rating_updated", "review_updated"):
            counts[key] += result[key]
        # Checkpoint batches so a cancelled/failed later crawl keeps completed accesses.
        if counts["tried"] % 25 == 0 or status == "blocked":
            save_json(FETCH_STATE_FILE, state)
    counts["official_blocked"] = fc2_market_blocked(state, now_ts())
    save_json(FETCH_STATE_FILE, state)
    return counts


def enrich_new_fc2_market(items) -> None:
    targets = [x for x in items if x.get("is_new")]
    state = load_fc2_market_state(items)
    stats = _fc2_batch(targets, state, "new", len(targets))
    state["fc2_new_stats"] = {"last_run": now_ts(), "targets": len(targets), **stats}
    save_json(FETCH_STATE_FILE, state)
    print(f"[FC2 New] stats={state['fc2_new_stats']}")


def _needs_fc2_metadata(item):
    return (needs_jp_title(item.get("title", "")) or item.get("fc2_rating") is None
            or item.get("fc2_review_count") is None)


def run_continuous_metadata_backfill(items) -> None:
    state = load_fc2_market_state(items)
    attempts = state[BACKFILL_ATTEMPTS_KEY]
    now = now_ts()
    candidates = [x for x in items if not x.get("fc2_market_not_found")
                  and (attempts.get(x.get("code")) or {}).get("status") != "not_found"
                  and _needs_fc2_metadata(x)]
    candidates.sort(key=lambda x: int(x.get("code_num") or 0), reverse=True)
    def last(item):
        return int((attempts.get(item.get("code")) or {}).get("last_attempt") or 0)
    untried = [x for x in candidates if not last(x)]
    retry_ready = sorted([x for x in candidates if last(x) and fc2_market_due(x, state, now)], key=last)
    batch = (untried + retry_ready)[:max(0, BACKFILL_OFFICIAL_LIMIT)]
    stats = {
        "last_run": now, "candidates_before": len(candidates), "untried_before": len(untried),
        "retry_ready_before": len(retry_ready),
        "retry_waiting_before": len(candidates) - len(untried) - len(retry_ready),
        "selected": len(batch),
        "title_missing_before": sum(needs_jp_title(x.get("title", "")) for x in candidates),
        "rating_missing_before": sum(x.get("fc2_rating") is None for x in candidates),
        "review_missing_before": sum(x.get("fc2_review_count") is None for x in candidates),
    }
    stats.update(_fc2_batch(batch, state, "backfill", max(0, BACKFILL_OFFICIAL_LIMIT)))
    remaining = [x for x in candidates if not x.get("fc2_market_not_found") and _needs_fc2_metadata(x)]
    stats.update(
        remaining_candidates=len(remaining), remaining_untried=sum(not last(x) for x in remaining),
        remaining_failed=sum((attempts.get(x.get("code")) or {}).get("status") == "failed" for x in remaining),
    )
    known_codes = {x.get("code") for x in items}
    state[BACKFILL_ATTEMPTS_KEY] = {k: v for k, v in attempts.items() if k in known_codes}
    state.pop(CONTINUOUS_BACKFILL_CURSOR_KEY, None)
    state[CONTINUOUS_BACKFILL_STATS_KEY] = stats
    save_json(FETCH_STATE_FILE, state)
    print(f"[Backfill v3] stats={stats}")


def _absolute_fc2_asset(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return urljoin("https://adult.contents.fc2.com/", value)


def fetch_fc2_sample_assets(code_num: str):
    url = f"https://adult.contents.fc2.com/api/v2/videos/{code_num}/sample"
    headers = {
        **HEADERS,
        "Accept": "application/json,text/plain,*/*",
        "Referer": f"https://adult.contents.fc2.com/article/{code_num}/",
        "Cookie": "wei6H=1; GDPRCHECK=true",
    }
    try:
        res = requests.get(url, headers=headers, timeout=25, allow_redirects=True)
    except Exception as e:
        print(f"[FC2 sample] FC2-PPV-{code_num} error={type(e).__name__}")
        return None

    final_url = str(res.url or url)
    if res.status_code != 200 or "redirect.fc2.com/ekyc_auth" in final_url:
        print(f"[FC2 sample] FC2-PPV-{code_num} status={res.status_code} usable=False")
        return None
    try:
        data = res.json()
    except Exception:
        print(f"[FC2 sample] FC2-PPV-{code_num} status=200 json=False")
        return None
    if not isinstance(data, dict):
        return None

    preview = _absolute_fc2_asset(data.get("path") or data.get("sample_path") or "")
    poster = _absolute_fc2_asset(
        data.get("poster_image_path") or data.get("poster") or data.get("image") or ""
    )
    usable = bool(preview or poster)
    print(
        f"[FC2 sample] FC2-PPV-{code_num} status=200 "
        f"preview={bool(preview)} poster={bool(poster)}"
    )
    if not usable:
        return None
    return {"preview": preview, "poster": poster}


def enrich_fc2_sample_assets(items) -> None:
    now = now_ts()
    tried = updated = 0
    # Keep a small newest-first refresh for recent items. Historical thumbnail
    # repair is handled separately by the continuous thumbnail backfill.
    for item in items:
        if tried >= FC2_SAMPLE_FETCH_LIMIT:
            break
        code = item.get("code") or ""
        num = item.get("code_num") or code.split("-")[-1]
        if not num.isdigit():
            continue
        tried += 1
        meta = fetch_fc2_sample_assets(num)
        item["fc2_sample_checked_at"] = now
        if not meta:
            continue
        if meta.get("preview"):
            item["preview"] = meta["preview"]
            item["preview_source"] = "FC2公式"
        if meta.get("poster"):
            item["thumb"] = meta["poster"]
            item["thumb_source"] = "FC2公式"
        updated += 1
    print(f"[FC2 sample] tried={tried} updated={updated}")


def run_thumbnail_backfill(items) -> None:
    """Continuously repair historical thumbnails without disturbing previews."""
    state = load_json(FETCH_STATE_FILE, {})
    raw_attempts = state.get(THUMB_BACKFILL_ATTEMPTS_KEY, {})
    attempts = raw_attempts if isinstance(raw_attempts, dict) else {}
    now = now_ts()

    # A Fourhoi cover is only a best-effort default. Prioritize records whose
    # thumbnail has never been confirmed/replaced by the FC2 official poster.
    candidates = []
    live_codes = set()
    for item in items:
        code = item.get("code") or ""
        num = str(item.get("code_num") or code.split("-")[-1])
        if not code or not num.isdigit():
            continue
        live_codes.add(code)
        if item.get("thumb_source") == "FC2公式":
            continue
        candidates.append(item)

    # Never/least-recently attempted first. Preview-bearing records get
    # priority because those are the visible "No Image but preview works" cases.
    candidates.sort(key=lambda item: (
        0 if item.get("preview") else 1,
        int(attempts.get(item.get("code") or "", 0) or 0),
        -int(item.get("code_num") or str(item.get("code", "")).split("-")[-1]),
    ))
    batch = candidates[:THUMB_BACKFILL_LIMIT]

    tried = poster_updated = preview_updated = failed = 0
    for item in batch:
        code = item.get("code") or ""
        num = str(item.get("code_num") or code.split("-")[-1])
        tried += 1
        attempts[code] = now
        meta = fetch_fc2_sample_assets(num)
        item["fc2_sample_checked_at"] = now
        item["thumb_backfill_checked_at"] = now
        if not meta:
            failed += 1
            continue
        if meta.get("preview"):
            if item.get("preview") != meta["preview"]:
                preview_updated += 1
            item["preview"] = meta["preview"]
            item["preview_source"] = "FC2公式"
        if meta.get("poster"):
            if item.get("thumb") != meta["poster"] or item.get("thumb_source") != "FC2公式":
                poster_updated += 1
            item["thumb"] = meta["poster"]
            item["thumb_source"] = "FC2公式"

    # Keep state bounded to records that still exist in the library.
    attempts = {code: ts for code, ts in attempts.items() if code in live_codes}
    remaining = sum(1 for item in items if item.get("thumb_source") != "FC2公式")
    stats = {
        "last_run": now,
        "candidates_before": len(candidates),
        "processed": tried,
        "poster_updated": poster_updated,
        "preview_updated": preview_updated,
        "failed": failed,
        "remaining_unconfirmed": remaining,
    }
    state[THUMB_BACKFILL_ATTEMPTS_KEY] = attempts
    state[THUMB_BACKFILL_STATS_KEY] = stats
    save_json(FETCH_STATE_FILE, state)
    print(f"[Thumb backfill] stats={stats}")


def enrich_fc2_market(items):
    state = load_fc2_market_state(items)
    cursor = int(state.get("fc2_market_cursor") or 0)
    if not items:
        return
    rotated = items[cursor % len(items):] + items[:cursor % len(items)]
    ordered = sorted(rotated, key=lambda x: bool(x.get("fc2_title")))
    stats = _fc2_batch(ordered, state, "refresh", max(0, FC2_MARKET_FETCH_LIMIT))
    state["fc2_market_cursor"] = (cursor + max(stats["tried"], 1)) % len(items)
    state["fc2_refresh_stats"] = {"last_run": now_ts(), **stats}
    save_json(FETCH_STATE_FILE, state)
    print(f"[FC2 Market] stats={stats}")


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
        "fc2_market_url": "" if item.get("fc2_market_not_found") else (item.get("fc2_market_url") or ""),
        "fc2_market_not_found": bool(item.get("fc2_market_not_found")),
        "fc2_title": item.get("fc2_title") or "",
        "title_source": item.get("title_source") or "",
        "fc2_rating": item.get("fc2_rating") if isinstance(item.get("fc2_rating"), (int, float)) else None,
        "fc2_review_count": item.get("fc2_review_count") if isinstance(item.get("fc2_review_count"), int) else None,
        "fc2_rating_source": item.get("fc2_rating_source") or "",
        "hwalker_checked_at": item.get("hwalker_checked_at") or 0,
        "fc2_market_checked_at": item.get("fc2_market_checked_at") or 0,
    }


WEB_DIR = Path(__file__).resolve().parent / "web"


def catalog_payload(items, updated_at):
    # Render data only: do not ship crawler state, duplicate titles, or seven
    # repeated search URLs with every card. Templates are expanded on demand.
    fields = ("code", "code_num", "title", "url", "thumb", "preview", "duration",
              "duration_sec", "views", "views_source", "first_seen", "is_new",
              "sources", "trend_6h", "trend_24h", "missav_rank_day", "missav_rank_week",
              "missav_rank_month", "missav_rank_total", "fc2_market_url", "fc2_rating",
              "fc2_review_count", "fc2_rating_source")
    rows = []
    for item in items:
        public = public_item(item)
        rows.append({key: public[key] for key in fields if public.get(key) is not None and public.get(key) != "" and public.get(key) is not False})
    return {"updated_at": updated_at, "items": rows, "search_templates": extra_sources("{code}")}


def render_html(items, updated_at, new_count):
    version = hashlib.sha256(b"".join((WEB_DIR / name).read_bytes() for name in ("site.css", "site.js"))).hexdigest()[:12]
    return ((WEB_DIR / "index.html").read_text(encoding="utf-8")
            .replace("__UPDATED_AT__", html.escape(updated_at))
            .replace("__NEW_COUNT__", str(new_count))
            .replace("__ITEM_COUNT__", f"{len(items):,}")
            .replace("__ASSET_VERSION__", version)
            .replace("__DATA_VERSION__", quote_plus(updated_at)))


def write_site(items, updated_at, new_count):
    HTML_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Data and assets are written first; Pages deploys the complete directory atomically.
    (HTML_FILE.parent / "catalog.json").write_text(
        json.dumps(catalog_payload(items, updated_at), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for name in ("site.css", "site.js"):
        shutil.copyfile(WEB_DIR / name, HTML_FILE.parent / name)
    HTML_FILE.write_text(render_html(items, updated_at, new_count), encoding="utf-8")


def rebuild_site():
    payload = load_json(DATA_FILE, {"items": []})
    items = payload.get("items", [])
    write_site(items, payload.get("updated_at") or now_jst(), sum(bool(x.get("is_new")) for x in items))
    print(f"Site rebuilt: {len(items)} items")
    return 0


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
        if old.get("title") and not is_invalid_title(old.get("title", "")) and not is_better_title(item.get("title", ""), old.get("title", "")):
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
        item["fc2_market_not_found"] = bool(old.get("fc2_market_not_found"))
        item["fc2_rating"] = old.get("fc2_rating")
        item["fc2_review_count"] = old.get("fc2_review_count")
        item["fc2_rating_source"] = old.get("fc2_rating_source") or ""
        item["hwalker_url"] = old.get("hwalker_url") or ""
        item["hwalker_checked_at"] = old.get("hwalker_checked_at") or 0
        item["fc2_market_checked_at"] = old.get("fc2_market_checked_at") or 0
        item["fc2_market_last_success"] = old.get("fc2_market_last_success") or 0
        item["fc2_market_last_attempt"] = old.get("fc2_market_last_attempt") or 0
        item["fc2_title"] = old.get("fc2_title") or ""
        item["fc2cmadb_url"] = old.get("fc2cmadb_url") or ""
        item["fc2cmadb_checked_at"] = old.get("fc2cmadb_checked_at") or 0
        item["title_source"] = old.get("title_source") or item.get("title_source") or ""
        item["duration"] = video.get("duration") or old.get("duration", "")
        item["thumb"] = old.get("thumb") or video.get("thumb") or ""
        item["thumb_source"] = old.get("thumb_source") or video.get("thumb_source") or ""
        item["preview"] = old.get("preview") or video.get("preview") or ""
        item["preview_source"] = old.get("preview_source") or video.get("preview_source") or ""
        item["fc2_sample_checked_at"] = old.get("fc2_sample_checked_at") or 0
        item["thumb_backfill_checked_at"] = old.get("thumb_backfill_checked_at") or 0
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

    sanitize_item_titles(merged)
    # New discoveries get official Japanese title/rating/review immediately.
    enrich_new_fc2_market(merged)
    refresh_fc2cmadb_titles(merged)
    run_continuous_metadata_backfill(merged)
    enrich_hwalker_market(merged)
    refresh_japanese_titles(merged)
    fill_missing_views(merged)
    enrich_fc2_market(merged)
    run_thumbnail_backfill(merged)
    enrich_fc2_sample_assets(merged)
    apply_missav_ranks(merged, scrape_missav_rankings(), stamp)
    update_views_history(merged)
    save_json(DATA_FILE, {"updated_at": stamp, "items": merged})
    save_json(HISTORY_FILE, {"updated_at": stamp, "ids": sorted(known)})
    write_site(merged, stamp, len(new_videos))

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
    raise SystemExit(rebuild_site() if "--build-site" in sys.argv else main())
