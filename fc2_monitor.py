#!/usr/bin/env python3
import html
import hashlib
import shutil
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit, quote_plus, parse_qs

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
DISCOVERY_RETRY_SEC = 12 * 3600
SOURCE_RETRY_AT = {}
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
H_WALKER_CACHE_KEY = "hwalker_title_cache"
H_WALKER_CACHE_LIMIT = 20000
H_WALKER_REFRESH_SEC = int(os.environ.get("H_WALKER_REFRESH_SEC", str(6 * 3600)))
CONTINUOUS_BACKFILL_CURSOR_KEY = "metadata_backfill_v2_cursor"  # legacy; no longer used for selection
CONTINUOUS_BACKFILL_STATS_KEY = "metadata_backfill_v3_stats"
BACKFILL_ATTEMPTS_KEY = "metadata_backfill_v3_attempts"
BACKFILL_RETRY_BASE_SEC = int(os.environ.get("BACKFILL_RETRY_BASE_SEC", str(12 * 3600)))
BACKFILL_RETRY_MAX_SEC = int(os.environ.get("BACKFILL_RETRY_MAX_SEC", str(3 * 86400)))
BACKFILL_OFFICIAL_LIMIT = int(os.environ.get("BACKFILL_OFFICIAL_LIMIT", "500"))
FC2_SAMPLE_FETCH_LIMIT = int(os.environ.get("FC2_SAMPLE_FETCH_LIMIT", "24"))
PREVIEW_REFRESH_LIMIT = int(os.environ.get("PREVIEW_REFRESH_LIMIT", "500"))
PREVIEW_REFRESH_SEC = int(os.environ.get("PREVIEW_REFRESH_SEC", str(18 * 3600)))
SAMPLE_RETRY_MAX_SEC = 3 * 86400
PREVIEW_REFRESH_CURSOR_KEY = "preview_refresh_v1_cursor"
PREVIEW_REFRESH_STATS_KEY = "preview_refresh_v1_stats"
THUMB_BACKFILL_LIMIT = int(os.environ.get("THUMB_BACKFILL_LIMIT", "200"))
THUMB_BACKFILL_ATTEMPTS_KEY = "thumbnail_backfill_v1_attempted_at"
THUMB_BACKFILL_STATS_KEY = "thumbnail_backfill_v1_stats"
VIEW_FETCH_LIMIT = int(os.environ.get("VIEW_FETCH_LIMIT", "50"))
TITLE_FETCH_LIMIT = int(os.environ.get("TITLE_FETCH_LIMIT", "500"))
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
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         suffix=".tmp", delete=False) as f:
            temporary = Path(f.name)
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o644)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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


def discovery_source(url):
    host = (urlsplit(url).hostname or "").lower()
    for source, hosts in {"missav": ("missav.ws", "missav.live", "missav.ai"),
                          "javdb": ("javdb.com",), "supjav": ("supjav.com",)}.items():
        if any(host == value or host.endswith("." + value) for value in hosts):
            return source
    return ""


def source_in_cooldown(url):
    return int(SOURCE_RETRY_AT.get(discovery_source(url)) or 0) > now_ts()


def record_source_denial(url, status, challenge=False):
    source = discovery_source(url)
    if source and (status in (401, 403, 429) or challenge):
        SOURCE_RETRY_AT[source] = now_ts() + DISCOVERY_RETRY_SEC


def save_discovery_cooldowns():
    """Persist denials even when collection fails, without advancing cursors."""
    state = load_json(CRAWL_FILE, {})
    if (state.get("source_retry_at") or {}) != SOURCE_RETRY_AT:
        state["source_retry_at"] = dict(SOURCE_RETRY_AT)
        save_json(CRAWL_FILE, state)


def fetch_soup(url: str):
    if source_in_cooldown(url):
        response = requests.Response()
        response.status_code = 403
        raise requests.HTTPError("source cooldown; no request made", response=response)
    res = requests.get(url, headers=HEADERS, timeout=30)
    record_source_denial(url, res.status_code)
    res.raise_for_status()
    return BeautifulSoup(res.text, "html.parser")


def fetch_page(url: str) -> dict:
    if source_in_cooldown(url):
        return {"ok": False, "status": 0, "final_url": url, "soup": None,
                "size": 0, "title": "", "cloudflare": False, "error": "source_cooldown"}
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
            title_l.startswith("just a moment")
            or "<title>just a moment" in low[:4000]
        )
        if res.status_code == 200 and real_title and len(text) > 20000:
            challenge = False
        record_source_denial(url, res.status_code, challenge)
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


def title_without_site_suffix(text: str) -> str:
    title = html.unescape(re.sub(r"\s+", " ", (text or "")).strip())
    # MissAV's Japanese site description is page chrome, not a translated title.
    return re.sub(r"\s*[-–—|｜]\s*MissAV\b.*$", "", title, flags=re.I).strip()


def clean_title(text: str, code_num: str) -> str:
    title = title_without_site_suffix(text)
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


CHINESE_TITLE_HINTS = re.compile(
    r"(?:無碼|无码|中文字幕|中字|中文|視頻|视频|線上|线上|觀看|观看|下載|下载|"
    r"這個|这个|這部|这部|女孩們|女孩们|美女們|美女们|人妻們|人妻们|推荐|"
    r"處女|处女|调教|內射|做愛|做爱|自拍|約會|约会|"
    r"露臉|露脸|藥劑師|药剂师|大腦|大脑|傳教士|传教士|"
    r"第一次拍照|限價|限价|僅限|仅限|支持新人|完整出場|完整出场|"
    r"小姐姐|銷售|销售|群組|群组|諮詢|咨询|嬌小|娇小|"
    r"毛茸茸|女大學生|未經|未经|審查|审查)"
)
# Shared kanji (後, 長, 時, 体, 結果, 数量, ...) are not language evidence.
# These simplified/traditional forms differ from ordinary Japanese spelling.
CHINESE_TITLE_CHARS = re.compile(
    r"[这這們们歲岁药剂與东腦脑错傳传过萬體發发为來还會讓让從从裡"
    r"开當兩两對对时說说给种麼么视频线觀观载處处调爱约个码價价僅仅"
    r"请銷销賣卖询經经审學脸臉]"
)
JAPANESE_TITLE_HINTS = re.compile(
    r"(?:限定|素人|人妻|女子|大学|美人|美少女|巨乳|中出し|顔射|潮吹き|"
    r"初撮り|個人撮影|無修正|生ハメ|援交|熟女|痴女|作品|特典|販売)"
)

def looks_chinese_title(text: str) -> bool:
    title = title_without_site_suffix(text)
    if not title:
        return False
    # Kana is useful Japanese evidence, but Chinese text can contain a
    # Japanese product/person fragment (e.g. Vtuber names). Strong Chinese
    # phrases must win before accepting kana.
    if CHINESE_TITLE_HINTS.search(title) or CHINESE_TITLE_CHARS.search(title):
        return True
    return False

def title_score(text: str):
    title = title_without_site_suffix(text)
    if not title or is_invalid_title(title) or DURATION_RE.match(title) or title.startswith("FC2-PPV-"):
        return (0, 0)
    if looks_chinese_title(title):
        return (0, len(title))
    has_kana = bool(re.search(r"[ぁ-んァ-ヶ]", title))
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", title))
    japanese_kanji_only = bool(has_cjk and JAPANESE_TITLE_HINTS.search(title))
    return (3 if (has_kana or japanese_kanji_only) else (1 if has_cjk else 0), len(title))


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
    return value if 0 <= value < 100000000 else None


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
        if value is not None:
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
    # Do not return a merely CJK-looking fallback. A title repair succeeds
    # only when the candidate passes the Japanese-title threshold.
    return ""


def extract_search_title(soup, code_num: str) -> str:
    """Search-page headings describe the query, not the requested work."""
    for link in soup.find_all("a", href=True):
        href = link.get("href") or ""
        if re.search(r"[?&](?:s|q|keyword)=", href):
            continue
        raw = link.get("title") or link.get_text(" ", strip=True)
        matches = CODE_RE.findall(raw + " " + (link.get("href") or ""))
        if code_num not in matches or any(num != code_num for num in matches):
            continue
        title = clean_title(raw, code_num)
        if not needs_jp_title(title):
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
            if info.get("status") in (401, 403, 429) or info.get("cloudflare") or info.get("error") == "source_cooldown":
                break
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


RANK_PERIODS = {"day": "today_views", "week": "weekly_views", "month": "monthly_views", "total": "views"}
RANK_STATE_KEY = "missav_rankings_v2"
VIEW_ATTEMPTS_KEY = "views_refresh_v2_attempts"
VIEW_STATS_KEY = "views_refresh_v2_stats"


def page_access_limited(info):
    return bool(info.get("status") in (401, 403, 429) or info.get("cloudflare")
                or info.get("error") == "source_cooldown")


def scrape_missav_fc2_rankings() -> dict:
    """Return one independently verified snapshot/outcome per period."""
    hosts = ["https://missav.ws", "https://missav.live", "https://missav.ai"]
    periods = {}
    for period, sort in RANK_PERIODS.items():
        outcome = {"status": "fetch_failed", "reason": "unavailable"}
        for host in hosts:
            url = f"{host}/ja/fc2?sort={sort}"
            if source_in_cooldown(url):
                outcome = {"status": "access_limited", "reason": "source_cooldown"}
                break
            page = fetch_page(url)
            if page_access_limited(page):
                record_source_denial(url, page.get("status"), page.get("cloudflare", False))
                outcome = {"status": "access_limited", "reason": "access_denied"}
                break
            if not page.get("ok") or page.get("soup") is None or page.get("status") != 200:
                continue
            try:
                final = urlsplit(page.get("final_url") or url)
            except ValueError:
                outcome = {"status": "invalid_response", "reason": "invalid_destination"}
                continue
            if (discovery_source(final.geturl()) != "missav" or final.path.rstrip("/") != "/ja/fc2"
                    or parse_qs(final.query).get("sort") != [sort]):
                outcome = {"status": "invalid_response", "reason": "sort_redirect"}
                continue
            codes = []
            for link in page["soup"].find_all("a", href=True):
                try:
                    target = urlsplit(urljoin(url, link["href"]))
                except ValueError:
                    continue
                match = re.fullmatch(r"/(?:ja/)?fc2-ppv-(\d{6,8})/?", target.path, re.I)
                if not match or discovery_source(target.geturl()) != "missav":
                    continue
                code = f"FC2-PPV-{match.group(1)}"
                if code not in codes:
                    codes.append(code)
            if codes:
                outcome = {"status": "ok", "codes": codes}
                break
            outcome = {"status": "invalid_response", "reason": "no_ranked_works"}
        if outcome["status"] == "access_limited":
            outcome["next_retry_at"] = int(SOURCE_RETRY_AT.get("missav") or 0)
        periods[period] = outcome
        print(f"[MissAV FC2 rank {period}] status={outcome['status']} count={len(outcome.get('codes', []))}")
    return {"version": 2, "periods": periods}


def scrape_missav_rankings() -> dict:
    return scrape_missav_fc2_rankings()


def valid_rank_codes(codes):
    return (isinstance(codes, list) and 0 < len(codes) <= 500
            and all(isinstance(code, str) and re.fullmatch(r"FC2-PPV-\d{6,8}", code) for code in codes)
            and len(set(codes)) == len(codes))


def reconcile_saved_ranks(items, state):
    """The period snapshot, not stale per-item annotations, is authoritative."""
    snapshots = state.get(RANK_STATE_KEY)
    if not isinstance(snapshots, dict):
        return 0  # Legacy input is retained until its first explicit audit.
    changed = 0
    for period in RANK_PERIODS:
        rec = snapshots.get(period) if isinstance(snapshots.get(period), dict) else {}
        codes = rec.get("codes", []) if rec.get("last_success_at") else []
        if not valid_rank_codes(codes):
            codes = []
        order = {code: i + 1 for i, code in enumerate(codes)}
        field = "missav_rank_" + period
        for item in items:
            rank = order.get(item.get("code"))
            if item.get(field) != rank:
                changed += 1
            item[field] = rank
    for item in items:
        item.pop("missav_rank_updated_at", None)
    return changed


def apply_missav_ranks(items, ranks, stamp):
    state = load_json(FETCH_STATE_FILE, {})
    snapshots = state.get(RANK_STATE_KEY)
    if not isinstance(snapshots, dict):
        snapshots = state[RANK_STATE_KEY] = {}
    periods = ranks.get("periods") if ranks.get("version") == 2 else None
    if periods is None:
        # Compatibility for older offline callers/tests; absent periods failed.
        periods = {}
        for period in RANK_PERIODS:
            entries = [(value.get(period), code) for code, value in ranks.items()
                       if isinstance(value, dict) and isinstance(value.get(period), int) and value[period] > 0]
            entries.sort()
            valid = entries and [rank for rank, _ in entries] == list(range(1, len(entries) + 1))
            periods[period] = {"status": "ok", "codes": [code for _, code in entries]} if valid else {"status": "fetch_failed"}
    if not isinstance(periods, dict):
        periods = {}
    known = {item.get("code") for item in items}
    for period in RANK_PERIODS:
        result = periods.get(period) if isinstance(periods.get(period), dict) else {"status": "fetch_failed"}
        previous = snapshots.get(period) if isinstance(snapshots.get(period), dict) else {}
        codes = result.get("codes") or []
        valid = result.get("status") == "ok" and valid_rank_codes(codes)
        if valid:
            snapshots[period] = {"status": "ok", "checked_at": stamp, "last_success_at": stamp,
                                 "codes": codes, "count": len(codes),
                                 "listed_count": sum(code in known for code in codes)}
            state["missav_rank_last_success"] = stamp
        else:
            snapshots[period] = {**previous, "status": (result.get("status") or "fetch_failed") if result.get("status") != "ok" else "invalid_response",
                                 "checked_at": stamp, "reason": result.get("reason") or "unverified_response",
                                 "next_retry_at": result.get("next_retry_at") or 0}
    reconcile_saved_ranks(items, state)
    save_json(FETCH_STATE_FILE, state)


def supjav_work_views(soup, code_num):
    """Read only a card/article identified as this exact work, never related views."""
    if soup is None:
        return None
    candidates = soup.select("article, .post, .item, .video-item, .card, .entry")
    # Some themes use a heading inside a div rather than an article element.
    for heading in soup.find_all(["h1", "h2", "h3"]):
        if set(CODE_RE.findall(heading.get_text(" ", strip=True))) == {str(code_num)}:
            candidates.append(heading.parent)
    for node in candidates:
        # Remove related sections from a copy, leaving the original soup reusable.
        scoped = BeautifulSoup(str(node), "html.parser")
        for other in scoped.select("aside, .related, .related-posts, .related-videos, .comments, #comments, nav, script, style"):
            other.decompose()
        text = scoped.get_text(" ", strip=True)
        identifiers = set(CODE_RE.findall(text + " " + " ".join(a.get("href", "") for a in scoped.find_all("a"))))
        if identifiers != {str(code_num)}:
            continue
        value = parse_supjav_card_views(scoped)
        if value is not None:
            return value
    return None


def supjav_detail_url(item):
    url = (item.get("sources") or {}).get("Supjav") or ""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme == "https" and parsed.hostname in ("supjav.com", "www.supjav.com")
                and not parsed.username and not parsed.password and not parsed.query
                and re.fullmatch(r"/(?:ja/)?\d+\.html/?", parsed.path)):
            return url
    except ValueError:
        pass
    number = str(item.get("code_num") or item.get("code", "").split("-")[-1])
    return f"https://supjav.com/ja/?s=FC2PPV+{number}" if re.fullmatch(r"\d{6,8}", number) else ""


def fill_missing_views(items) -> None:
    """Refresh historical works fairly, with actual per-work attempts and cooldowns."""
    state = load_json(FETCH_STATE_FILE, {})
    attempts = state.setdefault(VIEW_ATTEMPTS_KEY, {})
    now, updated = now_ts(), set()
    stats = {"checked_at": now_jst(), "status": "ok", "requests": 0, "tried": 0,
             "updated": 0, "failed": 0, "next_retry_at": 0}
    for item in items:
        item["views_updated"] = False

    def request(url):
        if source_in_cooldown(url):
            stats["status"] = "access_limited"
            return None
        stats["requests"] += 1
        info = fetch_page(url)
        if page_access_limited(info):
            record_source_denial(url, info.get("status"), info.get("cloudflare", False))
            stats["status"] = "access_limited"
            return None
        try:
            final_source = discovery_source(info.get("final_url") or url)
        except ValueError:
            final_source = ""
        if (not info.get("ok") or info.get("soup") is None
                or final_source != "supjav"):
            stats["failed"] += 1
            return None
        return info["soup"]

    def record_success(item, value):
        code = item["code"]
        item.update(views=value, views_source="Supjav", last_views_fetch=now,
                    views_checked_at=now, last_views_attempt=now, views_updated=True)
        attempts[code] = {"last_attempt": now, "last_success": now, "failures": 0,
                          "next_retry_at": now + VIEW_REFRESH_SEC}
        updated.add(code)

    if source_in_cooldown(SUPJAV_URL):
        stats["status"] = "access_limited"
    else:
        by_code = {x["code"]: x for x in items}
        for page in (1, 2, 3):
            url = SUPJAV_URL if page == 1 else f"{SUPJAV_URL.rstrip('/')}/page/{page}"
            soup = request(url)
            if soup is not None:
                codes = set(CODE_RE.findall(soup.get_text(" ", strip=True)))
                for num in codes:
                    item = by_code.get("FC2-PPV-" + num)
                    value = supjav_work_views(soup, num) if item else None
                    if value is not None:
                        record_success(item, value)
            if stats["status"] == "access_limited":
                break
        # Do not inherit last_views_attempt: older code incorrectly set it on every
        # catalog entry even when it had not requested that work.
        queue = sorted((x for x in items if x["code"] not in updated
                        and now >= int((attempts.get(x["code"]) or {}).get("next_retry_at") or 0)),
                       key=lambda x: (int((attempts.get(x["code"]) or {}).get("last_attempt") or 0),
                                      not bool(x.get("is_new")), x.get("first_seen") or "", x["code"]))
        for item in queue:
            if stats["status"] == "access_limited" or stats["tried"] >= VIEW_FETCH_LIMIT or stats["failed"] >= 3:
                break
            url = supjav_detail_url(item)
            if not url:
                continue
            rec = attempts.get(item["code"]) or {}
            item["last_views_attempt"] = now
            stats["tried"] += 1
            soup = request(url)
            value = supjav_work_views(soup, item.get("code_num") or item["code"].split("-")[-1])
            if value is not None:
                record_success(item, value)
            else:
                failures = int(rec.get("failures") or 0) + 1
                delay = DISCOVERY_RETRY_SEC if stats["status"] == "access_limited" else min(3 * 86400, FAIL_SKIP_SEC * 2 ** min(failures - 1, 3))
                attempts[item["code"]] = {**rec, "last_attempt": now, "failures": failures,
                                         "next_retry_at": now + delay}
    stats["updated"] = len(updated)
    stats["known_views"] = sum(isinstance(x.get("views"), int) and x.get("views_source") == "Supjav" for x in items)
    stats["pending"] = sum(now >= int((attempts.get(x["code"]) or {}).get("next_retry_at") or 0) for x in items)
    stats["never_checked"] = sum(x["code"] not in attempts for x in items)
    stats["last_success_at"] = now_jst() if updated else (state.get(VIEW_STATS_KEY) or {}).get("last_success_at")
    if stats["status"] == "access_limited":
        stats["next_retry_at"] = int(SOURCE_RETRY_AT.get("supjav") or 0)
    elif stats["failed"]:
        stats["status"] = "partial" if updated else "fetch_failed"
    elif not updated:
        stats["status"] = "no_measurements" if stats["requests"] else "idle"
    state[VIEW_STATS_KEY] = stats
    save_json(FETCH_STATE_FILE, state)
    print("[Views refresh] " + json.dumps(stats, ensure_ascii=False))


def refresh_japanese_titles(items) -> None:
    state = load_json(FETCH_STATE_FILE, {})
    fails = state.get("title_fail") if isinstance(state.get("title_fail"), dict) else {}
    blocked = state.get("title_hosts_blocked") if isinstance(state.get("title_hosts_blocked"), dict) else {}
    host_errors = {}
    now = now_ts()
    updated = tried = new_tried = historical_tried = requests_made = 0
    targets = [x for x in items if needs_jp_title(x.get("title", ""))]
    new_targets = [x for x in targets if x.get("is_new")]
    historical = [x for x in targets if not x.get("is_new")]
    # A positional cursor skips records whenever repaired titles leave the
    # queue. Prefer never-tried records, then the oldest failed attempt.
    historical.sort(key=lambda x: (int(fails.get(x.get("code")) or 0),
                                    x.get("first_seen") or "", x.get("code") or ""))
    ordered = new_targets + historical
    for item in ordered:
        code = item.get("code") or ""
        last_fail = int(fails.get(code) or 0)
        if last_fail and now - last_fail < FAIL_SKIP_SEC:
            continue
        if not item.get("is_new") and historical_tried >= TITLE_FETCH_LIMIT:
            break
        code_num = item.get("code_num") or str(code).split("-")[-1]
        sources = [("MissAV", url) for url in missav_detail_urls(code_num)] + [
            ("Supjav", f"https://supjav.com/ja/?s=FC2PPV+{code_num}"),
            ("123AV", f"https://123av.com/ja/search?keyword=FC2-PPV-{code_num}"),
        ]
        attempted = False
        for source, url in sources:
            host = urlsplit(url).netloc
            if now - int(blocked.get(host) or 0) < FAIL_SKIP_SEC or host_errors.get(host, 0) >= 3:
                continue
            attempted = True
            requests_made += 1
            try:
                soup = fetch_soup(url)
                host_errors[host] = 0
                found = (extract_search_title(soup, code_num) if source != "MissAV"
                         else extract_page_title(soup, code_num))
                if found and not needs_jp_title(found) and is_better_title(found, item.get("title", "")):
                    print(f"[Japanese title] code={code} source={source}")
                    item["title"] = found
                    item["title_source"] = source
                    updated += 1
                    fails.pop(code, None)
                    break
                if found and title_score(found)[0] >= 3:
                    fails.pop(code, None)
                    break
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status in (403, 429):
                    blocked[host] = now
                    print(f"[Japanese titles] host={host} blocked={status}")
                elif (status and status >= 500) or isinstance(e, (requests.Timeout, requests.ConnectionError)):
                    host_errors[host] = host_errors.get(host, 0) + 1
                    if host_errors[host] >= 3:
                        print(f"[Japanese titles] host={host} unavailable=true stop_for_run=true")
        if attempted:
            tried += 1
            if item.get("is_new"):
                new_tried += 1
            else:
                historical_tried += 1
        if attempted and needs_jp_title(item.get("title", "")):
            fails[code] = now
    pending = [x for x in items if needs_jp_title(x.get("title", ""))]
    pending_codes = {x.get("code") for x in pending}
    state["title_fail"] = {code: stamp for code, stamp in fails.items() if code in pending_codes}
    state["title_hosts_blocked"] = blocked
    state.pop("title_cursor", None)
    stats = {"last_run": now, "pending_before": len(targets), "pending_after": len(pending),
             "new_pending": sum(bool(x.get("is_new")) for x in pending),
             "tried": tried, "new_tried": new_tried, "historical_tried": historical_tried,
             "requests": requests_made, "title_updated": updated,
             "untried": sum(not fails.get(x.get("code")) for x in pending)}
    state["japanese_title_stats"] = stats
    save_json(FETCH_STATE_FILE, state)
    print(f"[Japanese titles] stats={stats}")


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
            break
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


def listing_pagination(soup):
    """Read the publisher's pagination, not an assumed page count."""
    current, maximum = None, None
    for node in soup.find_all("input"):
        value, limit = str(node.get("value") or ""), str(node.get("max") or "")
        context = node.parent.get_text(" ", strip=True) if node.parent else ""
        fraction = re.search(r"/\s*([\d,]+)\b", context)
        if limit.isdigit() and int(limit) > 0:
            maximum = int(limit)
        elif fraction and len(context) < 100:
            maximum = int(fraction.group(1).replace(",", ""))
        if (limit.isdigit() or fraction or node.get("type") == "number") and value.isdigit():
            current = int(value)
    active = soup.select_one('[aria-current="page"]')
    if active and active.get_text(strip=True).isdigit():
        current = int(active.get_text(strip=True))
    last = soup.find("a", rel="last", href=True)
    if last:
        raw = parse_qs(urlsplit(last["href"]).query).get("page", [""])[0]
        if raw.isdigit():
            maximum = int(raw)
    # The live publisher uses numbered links and rel=next/prev, with no
    # numeric input or rel=last. A far-end pair after a gap in the forward
    # window exposes the limit; a contiguous local window alone does not.
    numbered, adjacent = set(), set()
    for link in soup.find_all("a", href=True):
        raw = parse_qs(urlsplit(link["href"]).query).get("page", [""])[0]
        if not raw.isdigit() or int(raw) < 1:
            continue
        value = int(raw)
        label = link.get_text(strip=True).replace(",", "")
        if label.isdigit() and int(label) == value:
            numbered.add(value)
        if "next" in (link.get("rel") or []):
            adjacent.add(value - 1)
        if "prev" in (link.get("rel") or []):
            adjacent.add(value + 1)
    if current is None and len(adjacent) == 1:
        current = adjacent.pop()
    if maximum is None and current is not None:
        forward = sorted(value for value in numbered if value > current)
        if (len(forward) >= 3 and forward[-1] == forward[-2] + 1
                and any(right > left + 1 for left, right in zip(forward, forward[1:]))):
            maximum = forward[-1]
    return {"current_page": current, "max_page": maximum}


def scrape_missav_catalog(pages=None, completed_pages=None, report=None):
    """Validate page identity and limits before marking any archive page complete."""
    report = report if report is not None else {}
    report.setdefault("pages", [])
    report.setdefault("status", "ok")
    collected = {}
    fingerprints = set()
    previous_archive = report.get("last_archive") or {}
    head_fingerprint = None
    bases = ["https://missav.ws/ja/fc2", "https://missav.live/ja/fc2", "https://missav.ai/ja/fc2"]
    for page_no in (pages or [1, 2]):
        if report.get("max_page") and page_no > report["max_page"]:
            report.update(status="page_limit", stopped_page=page_no)
            break
        soup = None
        final = ""
        for base in bases:
            url = base if page_no == 1 else f"{base}?page={page_no}"
            info = fetch_page(url)
            print(f"[MissAV /ja/fc2] page={page_no} status={info.get('status')} cloudflare={info.get('cloudflare')}")
            if info.get("ok") and info.get("status") == 200 and not info.get("cloudflare"):
                soup, final = info.get("soup"), info.get("final_url") or base
                break
            if info.get("status") in (401, 403, 429) or info.get("cloudflare") or info.get("error") == "source_cooldown":
                report.update(status="access_limited", stopped_page=page_no)
                break  # Do not switch domains to get around an access denial.
        if soup is None:
            if report["status"] == "ok":
                report.update(status="fetch_failed", stopped_page=page_no)
            break
        pagination = listing_pagination(soup)
        if pagination.get("max_page"):
            report["max_page"] = pagination["max_page"]
        actual = pagination.get("current_page")
        redirected_page = parse_qs(urlsplit(final).query).get("page", [None])[0]
        if actual is None and redirected_page and redirected_page.isdigit():
            actual = int(redirected_page)
        if (actual is not None and actual != page_no) or (report.get("max_page") and page_no > report["max_page"]):
            report.update(status="page_mismatch", stopped_page=page_no, actual_page=actual)
            break
        fingerprint_codes = sorted(set(m.group(1) for a in soup.find_all("a", href=True)
            if (m := re.search(r"/fc2-ppv-(\d{6,8})(?:\b|/)", a["href"], re.I))))
        fingerprint = hashlib.sha256(",".join(fingerprint_codes).encode()).hexdigest()[:16]
        if page_no == 1:
            head_fingerprint = fingerprint
        if fingerprint_codes and fingerprint in fingerprints:
            report.update(status="repeated_page", stopped_page=page_no)
            break
        if (page_no > max(2, PAGES) and page_no > int(previous_archive.get("page") or 0)
                and previous_archive.get("fingerprint") == fingerprint
                and (not head_fingerprint or not previous_archive.get("head_fingerprint")
                     or previous_archive["head_fingerprint"] == head_fingerprint)):
            report.update(status="repeated_page", stopped_page=page_no)
            break
        if fingerprint_codes:
            fingerprints.add(fingerprint)
        before = len(collected)
        recognized = False
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
            recognized = True
            num = m.group(1)
            full = href if href.startswith("http") else urljoin(final, href)
            title = clean_title(raw, num)
            cur = collected.get(num)
            if not cur:
                collected[num] = enrich(num, title or f"FC2-PPV-{num}", "MissAV", full)
                collected[num]["discovery_kind"] = "recent" if page_no <= max(2, PAGES) else "archive"
            elif is_better_title(title, cur.get("title", "")):
                cur["title"], cur["url"] = title, full
        print(f"[MissAV /ja/fc2] page={page_no} +{len(collected)-before} total={len(collected)}")
        if recognized and completed_pages is not None:
            completed_pages.add(page_no)
        report["pages"].append({"page": page_no, "current_page": actual, "items": len(fingerprint_codes),
                                "fingerprint": fingerprint, "added_to_batch": len(collected)-before})
        if recognized and page_no > max(2, PAGES):
            report["last_archive"] = {"page": page_no, "fingerprint": fingerprint,
                                      "head_fingerprint": head_fingerprint}
        if not recognized:
            report.update(status="unrecognized", stopped_page=page_no)
            break
    return list(collected.values())


def scrape_javdb(pages=None, completed_pages=None, report=None):
    """Independent discovery; access denials stop the source's current batch."""
    collected = {}
    for page_no in (pages or [1, 2]):
        sep = "&" if "?" in JAVDB_URL else "?"
        url = JAVDB_URL if page_no == 1 else f"{JAVDB_URL}{sep}page={page_no}"
        info = fetch_page(url)
        print(f"[JavDB] page={page_no} status={info.get('status')} cloudflare={info.get('cloudflare')} size={info.get('size')}")
        if not (info.get("ok") and info.get("status") == 200 and not info.get("cloudflare") and info.get("soup")):
            if report is not None:
                report.update(status="access_limited" if info.get("status") in (401, 403, 429) or info.get("cloudflare") or info.get("error") == "source_cooldown" else "fetch_failed", stopped_page=page_no)
            if info.get("status") in (401, 403, 429) or info.get("cloudflare") or info.get("error") == "source_cooldown":
                break
            continue
        soup = info["soup"]
        before = len(collected)
        recognized = False
        for a in soup.find_all("a", href=True):
            href = a.get("href") or ""
            parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
            raw = " ".join([a.get("title") or "", a.get_text(" ", strip=True), parent_text, href])
            m = CODE_RE.search(raw)
            if not m:
                continue
            recognized = True
            num = m.group(1)
            full = href if href.startswith("http") else urljoin(info.get("final_url") or "https://javdb.com/", href)
            title = clean_title(raw, num)
            cur = collected.get(num)
            if not cur:
                collected[num] = enrich(num, title or f"FC2-PPV-{num}", "JavDB", full)
                collected[num]["discovery_kind"] = "recent" if page_no <= 2 else "archive"
            elif is_better_title(title, cur.get("title", "")):
                cur["title"], cur["url"] = title, full
        print(f"[JavDB] page={page_no} +{len(collected)-before} total={len(collected)}")
        if recognized and completed_pages is not None:
            completed_pages.add(page_no)
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



def sanitize_item_titles(items) -> int:
    """Clean saved titles and restore known Japanese originals without fetching."""
    changed = 0
    for item in items:
        before = item.copy()
        code = item.get("code") or ""
        num = item.get("code_num") or code.split("-")[-1]
        had_invalid = is_invalid_title(item.get("title", "")) or is_invalid_title(item.get("fc2_title", ""))
        cleaned = clean_title(item.get("title", ""), num)
        # Keep the source text for recovery even while the visible title is
        # pending. A language heuristic must never destroy saved metadata.
        if looks_chinese_title(cleaned):
            item["source_title"] = cleaned
        elif not cleaned:
            recovered = clean_title(item.get("source_title", ""), num)
            if recovered and not needs_jp_title(recovered):
                cleaned = recovered
        item["title"] = (code or f"FC2-PPV-{num}") if looks_chinese_title(cleaned) else (cleaned or code or f"FC2-PPV-{num}")
        official_title = clean_title(item.get("fc2_title", ""), num)
        if "fc2_title" in item:
            item["fc2_title"] = official_title
        if had_invalid:
            item["fc2_market_not_found"] = True
            item["fc2_market_url"] = ""
            if item.get("title_source") == "FC2公式":
                item["title_source"] = ""
        if official_title and not needs_jp_title(official_title):
            item["title"] = official_title
            item["title_source"] = "FC2公式"
        changed += item != before
    return changed


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
    return title if not needs_jp_title(title) else ""


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
        label = re.sub(r"\s+", "", a.get_text(" ", strip=True))
        if "次の50件" in label or label.startswith("次の"):
            nxt = urljoin(current_url, a.get("href") or "")
            if urlsplit(nxt).netloc == urlsplit(H_WALKER_URL).netloc:
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
        if not (info.get("status") == 200 and not info.get("cloudflare") and info.get("soup") is not None):
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
                m = re.search(r"(?:[?&])aid=(\d{5,8})(?:&|$)|/article/(\d{5,8})(?:/|$|[?#])", href)
                if m and urlsplit(href).hostname == "adult.contents.fc2.com":
                    product = link
                    num = m.group(1) or m.group(2)
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
            cell_texts = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)).strip() for c in cells]
            rating = None
            review_count = None
            rating_idx = -1
            for idx, txt in enumerate(cell_texts):
                if re.fullmatch(r"[0-5](?:\.\d{1,2})", txt):
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

        if not page_found:
            # A challenge, changed layout or empty response is not a completed
            # page. Retry this position instead of skipping its records.
            print(f"[FC2 Walker] unrecognized_page=true url={url}")
            next_url = url
            break
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
    cache = state.get(H_WALKER_CACHE_KEY)
    cache = cache if isinstance(cache, dict) else {}
    start_url = state.get(H_WALKER_CURSOR_KEY) or (H_WALKER_URL + "/")
    records, pages_done = {}, 0
    if last and now - last < H_WALKER_REFRESH_SEC:
        print(f"[FC2 Walker] skip fresh=true age={now-last}s")
    else:
        records, next_url, pages_done = scrape_hwalker_market(start_url=start_url)
        if records:
            state[H_WALKER_CURSOR_KEY] = next_url or (H_WALKER_URL + "/")
            state["hwalker_last_success"] = now
            for code, record in records.items():
                cache[code] = {**record, "checked_at": now}
            cache = dict(sorted(cache.items(), key=lambda kv: kv[1].get("checked_at", 0),
                                reverse=True)[:H_WALKER_CACHE_LIMIT])
        else:
            print("[FC2 Walker] harvested=0 keep_previous=true")
    state[H_WALKER_CACHE_KEY] = cache

    matched = rating_updated = review_updated = title_updated = 0
    for item in items:
        code = item.get("code") or ""
        rec = cache.get(code)
        if not rec:
            continue
        matched += 1
        checked_at = int(rec.get("checked_at") or now)
        item["hwalker_checked_at"] = checked_at
        item["hwalker_url"] = rec.get("source_url") or H_WALKER_URL
        title = clean_title(rec.get("title") or "", item.get("code_num") or code.split("-")[-1])
        if title and not needs_jp_title(title) and needs_jp_title(item.get("title", "")):
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
                item["fc2_market_checked_at"] = checked_at

        count = rec.get("review_count")
        if isinstance(count, int) and count >= 0 and item.get("fc2_rating_source") != "FC2公式":
            if item.get("fc2_review_count") != count:
                review_updated += 1
            item["fc2_review_count"] = count
            item["fc2_rating_source"] = "FC2ウォーカー"
            item["fc2_market_checked_at"] = checked_at

    stats = {
        "last_run": now,
        "start_url": start_url,
        "next_url": state.get(H_WALKER_CURSOR_KEY) or start_url,
        "pages": pages_done,
        "harvested": len(records),
        "cached": len(cache),
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
    title = clean_title(meta.get("fc2_title") or "", item.get("code_num") or item.get("code", "").split("-")[-1])
    if title:
        item["fc2_title"] = title
        if not needs_jp_title(title):
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


def _absolute_fc2_asset(raw) -> str:
    # Error responses can contain integers/objects in otherwise successful JSON.
    # Do not stringify them into bogus media URLs or abort the whole crawl.
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value:
        return ""
    try:
        absolute = urljoin("https://adult.contents.fc2.com/", value)
        parsed = urlsplit(absolute)
        return absolute if parsed.scheme in ("http", "https") and parsed.hostname else ""
    except ValueError:
        return ""


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

    def asset(*keys):
        return next((value for key in keys if (value := _absolute_fc2_asset(data.get(key)))), "")

    preview = asset("path", "sample_path")
    poster = asset("poster_image_path", "poster", "image")
    usable = bool(preview or poster)
    print(
        f"[FC2 sample] FC2-PPV-{code_num} status=200 "
        f"preview={bool(preview)} poster={bool(poster)}"
    )
    if not usable:
        return None
    return {"preview": preview, "poster": poster}


def sample_checked_at(item, previous_attempt=0):
    return max([int(item.get(key) or 0) for key in (
        "preview_checked_at", "fc2_sample_checked_at", "thumb_backfill_checked_at"
    )] + [int(previous_attempt or 0)])


def has_official_preview(item):
    return bool(item.get("preview")) and item.get("preview_source") == "FC2公式"


def preview_source_urls(item, additional=()):
    """Keep bounded alternatives; a metadata URL is not proof of playback."""
    saved = item.get("preview_fallbacks")
    saved = saved if isinstance(saved, list) else []
    urls = []
    for raw in [item.get("preview"), *additional, *saved]:
        if not isinstance(raw, str):
            continue
        value = raw.strip()
        try:
            parsed = urlsplit(value)
            valid = parsed.scheme in ("https", "http") and parsed.hostname and not parsed.username and not parsed.password
        except ValueError:
            valid = False
        if valid and value not in urls:
            urls.append(value)
    # This is the original provider URL already used by enrich(), not a proxy
    # or an alternate route around provider authentication. Restore it for old
    # rows whose official sample overwrote the only saved preview field.
    number = str(item.get("code_num") or str(item.get("code", "")).split("-")[-1])
    if re.fullmatch(r"\d{6,8}", number):
        legacy = f"https://fourhoi.com/fc2-ppv-{number}/preview.mp4"
        if legacy not in urls[:3]:
            urls = urls[:2] + [legacy]
    return urls[:3]


def sample_retry_due(item, now, previous_attempt=0):
    checked = sample_checked_at(item, previous_attempt)
    if not checked:
        return True
    retry_at = int(item.get("fc2_sample_retry_at") or 0)
    if retry_at:
        return now >= retry_at and now > checked
    # Migrate existing checks without treating old records as never attempted.
    delay = PREVIEW_REFRESH_SEC if has_official_preview(item) else FAIL_SKIP_SEC
    return now - checked >= delay


def sample_backfill_order(item, previous_attempt=0):
    # New discoveries must not continually overtake older, unchecked records.
    return (sample_checked_at(item, previous_attempt), item.get("first_seen") or "",
            -int(item.get("code_num") or item["code"].split("-")[-1]))


def apply_fc2_sample_result(item, meta, now):
    """Share result/cooldown across recent, preview and thumbnail acquisition."""
    item["fc2_sample_checked_at"] = now
    if meta and meta.get("preview"):
        previous = preview_source_urls(item)
        item["preview"] = meta["preview"]
        item["preview_fallbacks"] = [url for url in preview_source_urls(item, previous)
                                     if url != item["preview"]]
        item["preview_source"] = "FC2公式"
        item["fc2_sample_last_success"] = now
        item["fc2_sample_failures"] = 0
        item["fc2_sample_retry_at"] = now + PREVIEW_REFRESH_SEC
    else:
        failures = max(0, int(item.get("fc2_sample_failures") or 0)) + 1
        item["fc2_sample_failures"] = failures
        item["fc2_sample_retry_at"] = now + min(
            SAMPLE_RETRY_MAX_SEC, FAIL_SKIP_SEC * 2 ** min(failures - 1, 3)
        )
        # An unavailable response does not prove a saved sample was removed.
        # Keep the URL, but do not repeatedly spend repair slots on this item.
    if meta and meta.get("poster"):
        item["thumb"] = meta["poster"]
        item["thumb_source"] = "FC2公式"


def refresh_fc2_previews(items) -> None:
    """Recheck sample URLs across the catalog independently of thumbnail repair."""
    state = load_json(FETCH_STATE_FILE, {})
    now = now_ts()
    candidates = []
    for item in items:
        code = item.get("code") or ""
        num = str(item.get("code_num") or code.split("-")[-1])
        if not code or not num.isdigit():
            continue
        if sample_retry_due(item, now):
            candidates.append(item)
    candidates.sort(key=sample_backfill_order)
    pending = [item for item in candidates if not has_official_preview(item)]
    confirmed = [item for item in candidates if has_official_preview(item)]
    limit = max(0, PREVIEW_REFRESH_LIMIT)
    # Reserve 80% for acquisition until historical coverage catches up, while
    # retaining a refresh lane for known samples. Either lane can use spare slots.
    refresh_count = min(len(confirmed), limit // 5)
    backfill_count = min(len(pending), limit - refresh_count)
    refresh_count = min(len(confirmed), limit - backfill_count)
    batch = pending[:backfill_count] + confirmed[:refresh_count]
    tried = updated = unavailable = 0
    for item in batch:
        num = str(item.get("code_num") or str(item.get("code", "")).split("-")[-1])
        tried += 1
        meta = fetch_fc2_sample_assets(num)
        before = (item.get("preview"), item.get("preview_source"))
        apply_fc2_sample_result(item, meta, now)
        item["preview_checked_at"] = now
        if not meta or not meta.get("preview"):
            unavailable += 1
            continue
        if before != (item.get("preview"), item.get("preview_source")):
            updated += 1
    state.pop(PREVIEW_REFRESH_CURSOR_KEY, None)
    state[PREVIEW_REFRESH_STATS_KEY] = {
        "last_run": now, "candidates": len(candidates), "processed": tried,
        "updated": updated, "unavailable": unavailable,
        "backfill_processed": backfill_count, "refresh_processed": refresh_count,
        "never_checked_remaining": sum(not sample_checked_at(item) for item in items),
        "remaining_unconfirmed": sum(not has_official_preview(item) for item in items),
    }
    save_json(FETCH_STATE_FILE, state)
    print(f"[Preview refresh] stats={state[PREVIEW_REFRESH_STATS_KEY]}")


def enrich_fc2_sample_assets(items) -> None:
    now = now_ts()
    tried = updated = 0
    # Keep a small recent-item lane without re-fetching the same samples through
    # the thumbnail/preview lanes, or refreshing successful URLs every two hours.
    for item in items:
        if tried >= FC2_SAMPLE_FETCH_LIMIT:
            break
        code = item.get("code") or ""
        num = str(item.get("code_num") or code.split("-")[-1])
        if not code or not num.isdigit() or not sample_retry_due(item, now):
            continue
        tried += 1
        meta = fetch_fc2_sample_assets(num)
        apply_fc2_sample_result(item, meta, now)
        if not meta:
            continue
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
    cooldown_skipped = 0
    live_codes = set()
    for item in items:
        code = item.get("code") or ""
        num = str(item.get("code_num") or code.split("-")[-1])
        if not code or not num.isdigit():
            continue
        live_codes.add(code)
        if item.get("thumb_source") == "FC2公式":
            continue
        # Thumbnail and preview repair use the same endpoint. A separate
        # thumbnail cursor used to recheck failures from the preceding run.
        if not sample_retry_due(item, now, attempts.get(code, 0)):
            cooldown_skipped += 1
            continue
        candidates.append(item)

    candidates.sort(key=lambda item: sample_backfill_order(item, attempts.get(item["code"], 0)))
    batch = candidates[:max(0, THUMB_BACKFILL_LIMIT)]

    tried = poster_updated = preview_updated = failed = 0
    for item in batch:
        code = item.get("code") or ""
        num = str(item.get("code_num") or code.split("-")[-1])
        tried += 1
        attempts[code] = now
        meta = fetch_fc2_sample_assets(num)
        old_preview = item.get("preview")
        old_poster = (item.get("thumb"), item.get("thumb_source"))
        apply_fc2_sample_result(item, meta, now)
        item["thumb_backfill_checked_at"] = now
        if not meta:
            failed += 1
            continue
        if meta.get("preview"):
            if old_preview != item.get("preview"):
                preview_updated += 1
        if meta.get("poster"):
            if old_poster != (item.get("thumb"), item.get("thumb_source")):
                poster_updated += 1

    # Keep state bounded to records that still exist in the library.
    attempts = {code: ts for code, ts in attempts.items() if code in live_codes}
    remaining = sum(1 for item in items if item.get("thumb_source") != "FC2公式")
    stats = {
        "last_run": now,
        "candidates_before": len(candidates),
        "cooldown_skipped": cooldown_skipped,
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
            break
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
    """Propose validated discovery progress; commit it only with the catalog."""
    state = load_json(CRAWL_FILE, {"missav_page": 3, "supjav_page": 3, "javdb_page": 3})
    SOURCE_RETRY_AT.clear()
    SOURCE_RETRY_AT.update({key: int(value) for key, value in (state.get("source_retry_at") or {}).items()
                           if int(value or 0) > now_ts()})
    recent_count = max(2, PAGES)
    mp = max(recent_count + 1, int(state.get("missav_page", recent_count + 1)))
    jp = int(state.get("javdb_page", 3))
    recovery = dict(state.get("recovery_until") or {})
    missav_end = max(mp + BACKFILL_PAGES, min(mp + 60, int(recovery.get("missav_page", 0))))
    javdb_end = max(jp + JAVDB_BACKFILL_PAGES, min(jp + 30, int(recovery.get("javdb_page", 0))))
    missav_pages = list(range(1, recent_count + 1)) + list(range(mp, missav_end))
    javdb_pages = [1, 2] + list(range(jp, javdb_end))
    missav_completed, javdb_completed = set(), set()
    previous_missav = (state.get("last_discovery", {}).get("sources", {}).get("missav") or {})
    previous_archive = dict(previous_missav.get("last_archive") or {})
    if previous_archive and not previous_archive.get("head_fingerprint"):
        previous_archive["head_fingerprint"] = next((page.get("fingerprint") for page in
            previous_missav.get("pages", []) if page.get("page") == 1), None)
    reports = {"missav": {"archive_from": mp, "last_archive": previous_archive},
               "javdb": {"archive_from": jp}}
    found, errors = [], []
    try:
        # Search and the dedicated catalog have different ordering. Only the
        # catalog advances the archive cursor; search is a recent supplement.
        items = scrape_missav(list(range(1, recent_count + 1)))
        catalog = scrape_missav_catalog(missav_pages, completed_pages=missav_completed,
                                       report=reports["missav"])
        found.extend([items, catalog])
        while mp in missav_completed:
            mp += 1
        maximum = reports["missav"].get("max_page")
        if maximum and mp > maximum:
            reports["missav"].update(cycle_completed=True, restart_page=recent_count + 1)
            state["missav_cycle"] = int(state.get("missav_cycle") or 0) + 1
            state["missav_cycle_completed_at"] = now_jst()
            reports["missav"].pop("last_archive", None)
            mp = recent_count + 1
            recovery.pop("missav_page", None)
        state["missav_page"] = mp
        reports["missav"].update(search_items=len(items), catalog_items=len(catalog),
            recent_pages=sum(page <= recent_count for page in missav_completed),
            archive_pages=sum(page > recent_count for page in missav_completed), next_page=mp)
    except Exception as e:
        errors.append(f"MissAV: {e}")
        reports["missav"].update(status="error", error=str(e))
    try:
        items = scrape_javdb(javdb_pages, completed_pages=javdb_completed, report=reports["javdb"])
        found.append(items)
        while jp in javdb_completed:
            jp += 1
        state["javdb_page"] = jp
        reports["javdb"].update(items=len(items), archive_pages=sum(page > 2 for page in javdb_completed), next_page=jp)
    except Exception as e:
        errors.append(f"JavDB: {e}")
        reports["javdb"].update(status="error", error=str(e))
    try:
        items = scrape_supjav([1, 2, 3])
        found.append(items)
        reports["supjav"] = {"items": len(items), "status": "access_limited" if SOURCE_RETRY_AT.get("supjav", 0) > now_ts() else "checked"}
    except Exception as e:
        reports["supjav"] = {"status": "error", "error": str(e)}
    for key, end in list(recovery.items()):
        if int(state.get(key, 0)) >= int(end):
            recovery.pop(key)
    if recovery:
        state["recovery_until"] = recovery
    else:
        state.pop("recovery_until", None)
    state["source_retry_at"] = dict(SOURCE_RETRY_AT)
    videos = merge_videos(found)
    state["last_discovery"] = {"checked_at": now_jst(), "found_unique": len(videos), "sources": reports}
    print("[Discovery] " + json.dumps(state["last_discovery"], ensure_ascii=False))
    if not videos and errors:
        raise RuntimeError(" / ".join(errors))
    return videos, state


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
        can_trend = item.get("views_updated") and item.get("views_source") == "Supjav" and isinstance(views, int) and len(points) >= 2
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
    previews = preview_source_urls(item)
    return {
        "code": item.get("code", ""),
        "code_num": str(code_num),
        "title": item.get("title") or item.get("code", ""),
        "url": item.get("url") or sources.get("MissAV") or search_links.get("MissAV") or "",
        "thumb": item.get("thumb") or "",
        "preview": previews[0] if previews else "",
        "preview_fallbacks": previews[1:],
        "duration": item.get("duration") or "",
        "duration_sec": duration_seconds(item.get("duration") or ""),
        "views": item.get("views") if isinstance(item.get("views"), int) else None,
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


def build_update_status(items, crawl=None, state=None):
    """Small public health summary; never publish attempt maps or response bodies."""
    crawl = load_json(CRAWL_FILE, {}) if crawl is None else crawl
    state = load_json(FETCH_STATE_FILE, {}) if state is None else state
    discovery = crawl.get("last_discovery") or {}
    retries = {**(crawl.get("source_retry_at") or {}), **SOURCE_RETRY_AT}
    sources = {}
    for name in ("missav", "javdb", "supjav"):
        report = (discovery.get("sources") or {}).get(name) or {}
        retry = int(retries.get(name) or 0)
        status = report.get("status") or "unknown"
        if retry > now_ts():
            status = "access_limited"
        elif retry and status == "access_limited":
            status = "retry_due"
        sources[name] = {"status": status, "next_retry_at": retry,
                         "checked_at": discovery.get("checked_at") or ""}
    official_block = int(state.get("fc2_market_ekyc_blocked_at") or 0)
    reason = next((rec.get("reason") for rec in (state.get(BACKFILL_ATTEMPTS_KEY) or {}).values()
                   if rec.get("status") == "blocked" and rec.get("last_attempt") == official_block), "")
    official = {"status": ("access_limited" if fc2_market_blocked(state, now_ts()) else "retry_due") if official_block else "ready",
                "reason": "eKYC" if reason == "eKYC" else "access_denied" if official_block else "",
                "next_retry_at": official_block + FAIL_SKIP_SEC if official_block else 0,
                "pending": (state.get(CONTINUOUS_BACKFILL_STATS_KEY) or {}).get("remaining_candidates", 0)}
    ranks = {}
    for period in RANK_PERIODS:
        rec = (state.get(RANK_STATE_KEY) or {}).get(period) or {}
        ranks[period] = {key: rec[key] for key in ("status", "checked_at", "last_success_at", "count", "listed_count", "next_retry_at") if key in rec}
        ranks[period].setdefault("status", "unverified")
    views = state.get(VIEW_STATS_KEY) or {}
    views = {key: views[key] for key in ("status", "checked_at", "last_success_at", "tried", "updated", "known_views",
                                        "pending", "never_checked", "next_retry_at") if key in views}
    views.setdefault("status", "unknown")
    walker = state.get(H_WALKER_STATS_KEY) or {}
    return {"version": 1,
            "discovery": {"checked_at": discovery.get("checked_at") or "",
                          "recent_added": int(discovery.get("recent_added") or 0),
                          "archive_added": int(discovery.get("archive_added") or 0),
                          "next_archive_page": int(crawl.get("missav_page") or 0)},
            "sources": sources, "rankings": ranks, "views": views, "official": official,
            "fallback": {"source": "FC2ウォーカー", "rating_updated": int(walker.get("rating_updated") or 0),
                         "title_updated": int(walker.get("title_updated") or 0)}}


def catalog_payload(items, updated_at, update_status=None):
    # Render data only: do not ship crawler state, duplicate titles, or seven
    # repeated search URLs with every card. Templates are expanded on demand.
    fields = ("code", "code_num", "title", "url", "thumb", "preview", "preview_fallbacks", "duration",
              "duration_sec", "views", "views_source", "first_seen", "is_new",
              "sources", "trend_6h", "trend_24h", "missav_rank_day", "missav_rank_week",
              "missav_rank_month", "missav_rank_total", "fc2_market_url", "fc2_rating",
              "fc2_review_count", "fc2_rating_source")
    rows = []
    for item in items:
        public = public_item(item)
        rows.append({key: public[key] for key in fields if public.get(key) is not None and public.get(key) != "" and public.get(key) is not False and public.get(key) != []})
    return {"updated_at": updated_at, "items": rows, "search_templates": extra_sources("{code}"),
            "update_status": update_status or build_update_status(items)}


def render_html(items, updated_at, new_count):
    version = hashlib.sha256(b"".join((WEB_DIR / name).read_bytes() for name in ("site.css", "site.js"))).hexdigest()[:12]
    return ((WEB_DIR / "index.html").read_text(encoding="utf-8")
            .replace("__UPDATED_AT__", html.escape(updated_at))
            .replace("__NEW_COUNT__", str(new_count))
            .replace("__ITEM_COUNT__", f"{len(items):,}")
            .replace("__ASSET_VERSION__", version)
            .replace("__DATA_VERSION__", quote_plus(updated_at)))


def write_site(items, updated_at, new_count, update_status=None):
    HTML_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Data and assets are written first; Pages deploys the complete directory atomically.
    payload = catalog_payload(items, updated_at, update_status)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    payload["version"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    (HTML_FILE.parent / "catalog.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for name in ("site.css", "site.js"):
        shutil.copyfile(WEB_DIR / name, HTML_FILE.parent / name)
    HTML_FILE.write_text(render_html(items, updated_at, new_count), encoding="utf-8")
    save_json(HTML_FILE.parent / "update.json", {
        "version": payload["version"], "updated_at": updated_at,
        "item_count": len(items), "new_count": new_count,
    })


def rebuild_site():
    payload = load_json(DATA_FILE, {"items": []})
    items = payload.get("items", [])
    corrected = sanitize_item_titles(items)
    corrected += reconcile_saved_ranks(items, load_json(FETCH_STATE_FILE, {}))
    if corrected:
        save_json(DATA_FILE, payload)
        print(f"[Saved metadata repaired] changes={corrected}")
    write_site(items, payload.get("updated_at") or now_jst(), sum(bool(x.get("is_new")) for x in items))
    print(f"Site rebuilt: {len(items)} items")
    return 0


def refresh_update_metadata():
    """Repair rankings/refresh views without new discoveries or notifications."""
    payload = load_json(DATA_FILE, {"items": []})
    items = payload.get("items") or []
    if not items:
        raise RuntimeError("No saved catalog to refresh")
    crawl = load_json(CRAWL_FILE, {})
    SOURCE_RETRY_AT.clear()
    SOURCE_RETRY_AT.update({name: int(value) for name, value in (crawl.get("source_retry_at") or {}).items()
                           if int(value or 0) > now_ts()})
    apply_missav_ranks(items, scrape_missav_rankings(), now_jst())
    fill_missing_views(items)
    update_views_history(items)
    save_discovery_cooldowns()
    stamp = now_jst()
    save_json(DATA_FILE, {**payload, "updated_at": stamp, "items": items})
    write_site(items, stamp, sum(bool(x.get("is_new")) for x in items))
    print("[Update repair] " + json.dumps(build_update_status(items), ensure_ascii=False))
    return 0


def repair_saved_titles():
    """Repair the saved catalog without discoveries or notifications."""
    payload = load_json(DATA_FILE, {"items": []})
    items = payload.get("items", [])
    before = {x.get("code"): x.get("title", "") for x in items}
    recovery_path = os.environ.get("TITLE_RECOVERY_FILE", "")
    recovery = load_json(Path(recovery_path), {"items": []}) if recovery_path else {}
    originals = {x.get("code"): x for x in recovery.get("items", [])}
    restored = 0
    for item in items:
        original = originals.get(item.get("code"), {})
        candidate = clean_title(original.get("title", ""), item.get("code_num") or "")
        if needs_jp_title(item.get("title", "")) and candidate and not needs_jp_title(candidate):
            item["title"] = candidate
            item["title_source"] = original.get("title_source") or ""
            restored += 1
    load_fc2_market_state(items)
    sanitize_item_titles(items)
    run_optional_step("Japanese title cache", enrich_hwalker_market, items)
    save_json(DATA_FILE, payload)
    stats = {"last_run": now_ts(), "restored_from_snapshot": restored,
             "pending_before": sum(needs_jp_title(title) for title in before.values()),
             "pending_after": sum(needs_jp_title(x.get("title", "")) for x in items),
             "japanese_titles_changed": sum(x.get("title") != before.get(x.get("code"))
                                            and not needs_jp_title(x.get("title", "")) for x in items)}
    state = load_json(FETCH_STATE_FILE, {})
    state["japanese_title_repair_stats"] = stats
    save_json(FETCH_STATE_FILE, state)
    write_site(items, payload.get("updated_at") or now_jst(), sum(bool(x.get("is_new")) for x in items))
    print(f"[Japanese title repair] stats={stats}")
    return 0


def run_optional_step(label, action, *args, **kwargs):
    """A supplementary source or notification cannot discard discoveries."""
    try:
        action(*args, **kwargs)
        return True
    except Exception as exc:
        # Do not include response bodies or URLs (notification URLs contain secrets).
        print(f"::warning::{label} failed ({type(exc).__name__}); continuing with saved/partial metadata")
        return False


def main(discovery_only=False) -> int:
    print(f"[{now_jst()}] チェック開始")
    try:
        latest, crawl_state = get_latest_videos()
    except Exception as e:
        save_discovery_cooldowns()
        print(f"取得失敗: {e}", file=sys.stderr)
        if not discovery_only:
            run_optional_step("Error notification", send_telegram, "監視エラー: ページ取得に失敗しました")
        return 1
    save_discovery_cooldowns()
    if not latest:
        if not discovery_only:
            run_optional_step("Error notification", send_telegram, "監視エラー: 動画リストを抽出できませんでした。")
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
        is_new = video["code"] not in known and not first_run and video.get("discovery_kind") != "archive"
        item = {**video, "first_seen": old.get("first_seen", stamp), "last_seen": stamp, "is_new": is_new}
        # A longer scraped label must not replace an already acquired Japanese
        # title. Fresh official metadata can still replace it below.
        if old.get("title") and not is_invalid_title(old.get("title", "")) and (
            not needs_jp_title(old["title"]) or not is_better_title(item.get("title", ""), old["title"])
        ):
            item["title"] = old["title"]
            item["title_source"] = old.get("title_source") or ""
        item["views"] = video.get("views") if isinstance(video.get("views"), int) else old.get("views")
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
        item["title_source"] = item.get("title_source") or ""
        if old.get("source_title"):
            item["source_title"] = old["source_title"]
        item["duration"] = video.get("duration") or old.get("duration", "")
        item["thumb"] = old.get("thumb") or video.get("thumb") or ""
        item["thumb_source"] = old.get("thumb_source") or video.get("thumb_source") or ""
        item["preview"] = old.get("preview") or video.get("preview") or ""
        item["preview_source"] = old.get("preview_source") or video.get("preview_source") or ""
        item["preview_fallbacks"] = old.get("preview_fallbacks") or []
        item["preview_fallbacks"] = [url for url in preview_source_urls(item, [video.get("preview")])
                                     if url != item["preview"]]
        item["preview_checked_at"] = old.get("preview_checked_at") or 0
        item["fc2_sample_checked_at"] = old.get("fc2_sample_checked_at") or 0
        item["thumb_backfill_checked_at"] = old.get("thumb_backfill_checked_at") or 0
        for key in ("fc2_sample_last_success", "fc2_sample_failures", "fc2_sample_retry_at"):
            item[key] = old.get(key) or 0
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
    for action in (() if discovery_only else (enrich_new_fc2_market, refresh_fc2cmadb_titles,
                   run_continuous_metadata_backfill, enrich_hwalker_market,
                   refresh_japanese_titles, fill_missing_views, enrich_fc2_market,
                   enrich_fc2_sample_assets, run_thumbnail_backfill, refresh_fc2_previews)):
        run_optional_step(action.__name__, action, merged)
    if not discovery_only:
        run_optional_step("MissAV rankings", lambda: apply_missav_ranks(merged, scrape_missav_rankings(), now_jst()))
        run_optional_step("Views history", update_views_history, merged)
    additions = [item for item in latest if item["code"] not in existing_map]
    if "last_discovery" in crawl_state:
        crawl_state["last_discovery"].update(previous_total=len(existing_map), new_to_catalog=len(additions),
            recent_added=sum(item.get("discovery_kind") != "archive" for item in additions),
            archive_added=sum(item.get("discovery_kind") == "archive" for item in additions), total_after=len(merged))
    if SOURCE_RETRY_AT:
        crawl_state["source_retry_at"] = dict(SOURCE_RETRY_AT)
    save_json(DATA_FILE, {"updated_at": stamp, "items": merged})
    save_json(HISTORY_FILE, {"updated_at": stamp, "ids": sorted(known)})
    write_site(merged, stamp, len(new_videos), build_update_status(merged, crawl_state))
    # Commit the discovery position only after both data and public output exist.
    save_json(CRAWL_FILE, crawl_state)
    print(f"[Catalog saved] items={len(merged)} new={len(new_videos)} updated_at={stamp}")
    if "last_discovery" in crawl_state:
        print("[Discovery saved] " + json.dumps(crawl_state["last_discovery"], ensure_ascii=False))
    if discovery_only:
        print("Discovery-only run: metadata enrichment and notifications skipped")
        return 0

    if first_run:
        run_optional_step("Startup notification", send_telegram, "監視を開始しました。\n今後の新着だけ通知します。\n\n現在の最新:\n" + "\n".join(f"- {v['code']}" for v in latest[:8]))
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
        sent = run_optional_step("New item notification", send_telegram,
            "【新着 FC2-PPV】\n\n" + f"{video['code']}\n{video['title']}\n" + (" / ".join(extra) + "\n\n" if extra else "\n") + "\n".join(source_lines),
            photo=video.get("thumb"),
        )
        if sent:
            print(f"通知: {video['code']}")
        else:
            # Avoid repeated timeouts/rate limits delaying the Pages deployment.
            break
    return 0


def audit_discovery() -> int:
    """Read-only pagination diagnostics; never fetch video files or send notices."""
    state = load_json(CRAWL_FILE, {})
    base = "https://missav.ws/ja/fc2"
    for page in dict.fromkeys([1, 2, int(state.get("missav_page") or 3)]):
        url = base if page == 1 else f"{base}?page={page}"
        info = fetch_page(url)
        soup = info.get("soup")
        if not info.get("ok") or soup is None:
            print(json.dumps({"pagination_audit": page, "status": info.get("status"),
                              "challenge": info.get("cloudflare"), "size": info.get("size")}), flush=True)
            break
        codes = sorted(set(re.findall(r"/fc2-ppv-(\d{6,8})(?:\b|/)", str(soup), flags=re.I)))
        inputs = [{k: node.get(k) for k in ("type", "name", "value", "min", "max", "x-model")}
                  for node in soup.find_all("input")
                  if node.get("type") == "number" or node.get("x-model") or node.get("max")]
        pagination = [{"href": a.get("href"), "rel": a.get("rel")}
                      for a in soup.find_all("a", href=True) if re.search(r"[?&]page=", a["href"])]
        contexts = [node.parent.get_text(" ", strip=True)[:100]
                    for node in soup.find_all("input") if node.get("type") == "number" or node.get("x-model")]
        print(json.dumps({"pagination_audit": page, "status": info.get("status"), "final_url": info.get("final_url"),
                          "codes_count": len(codes), "fingerprint": hashlib.sha256(",".join(codes).encode()).hexdigest()[:16],
                          "inputs": inputs, "pagination": pagination, "contexts": contexts}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(rebuild_site() if "--build-site" in sys.argv else
                     refresh_update_metadata() if "--refresh-updates" in sys.argv else
                     repair_saved_titles() if "--repair-titles" in sys.argv else
                     audit_discovery() if "--audit-discovery" in sys.argv else
                     main(discovery_only="--discovery-only" in sys.argv))
