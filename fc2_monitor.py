#!/usr/bin/env python3
import html
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

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
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "80"))
KEEP_ITEMS = int(os.environ.get("KEEP_ITEMS", "0"))
PAGES = int(os.environ.get("PAGES", "8"))
BACKFILL_PAGES = int(os.environ.get("BACKFILL_PAGES", "6"))
VIEW_FETCH_LIMIT = int(os.environ.get("VIEW_FETCH_LIMIT", "40"))
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


def parse_views(text: str):
    for pat in [
        r"([\d,]+)\s*(?:views|view|Views)",
        r"(?:視聴回数|再生回数|回再生|視聴)\s*[:：]?\s*([\d,]+)",
        r"([\d,]+)\s*(?:回再生|回視聴)",
        r"([\d,]+)\s*(?:次播放|播放|次視聴)",
        r"([\d,]+)\s*(?:people wanted|wanted|想看)",
    ]:
        match = re.search(pat, text or "", flags=re.I)
        if match:
            try:
                value = int(match.group(1).replace(",", ""))
                if 0 < value < 100000000:
                    return value
            except ValueError:
                pass
    return None


def extra_sources(code_num: str) -> dict:
    return {
        "Supjav": f"https://supjav.com/ja/?s=FC2PPV+{code_num}",
        "JavDB": f"https://javdb.com/search?q=FC2-PPV-{code_num}&f=all",
        "FC2検索": f"https://adult.contents.fc2.com/search/?q={code_num}",
        "123AV": f"https://123av.com/ja/search?keyword=FC2-PPV-{code_num}",
        "JavFC2": f"https://javfc2.xyz/search?q={code_num}",
    }


def fill_missing_views(items):
    fetched = 0
    for item in items:
        if item.get("views") or fetched >= VIEW_FETCH_LIMIT:
            continue
        code_num = item.get("code_num") or str(item.get("code", "")).split("-")[-1]
        sources = dict(item.get("sources") or {})
        sources.update(extra_sources(code_num))
        urls = []
        for key in ("MissAV", "Supjav", "JavDB", "123AV", "JavFC2"):
            if sources.get(key) and sources[key] not in urls:
                urls.append(sources[key])
        views = None
        last_err = None
        for url in urls:
            try:
                views = parse_views(fetch_soup(url).get_text(" ", strip=True))
                if views:
                    item["views"] = views
                    fetched += 1
                    print(f"再生数: {item['code']} = {views} ({url})")
                    break
            except Exception as e:
                last_err = e
        if not views and last_err:
            print(f"再生数取得失敗 {item.get('code')}: {last_err}")


def enrich(code_num, title, source, url, duration="", views=None):
    slug = f"fc2-ppv-{code_num}"
    return {
        "code": f"FC2-PPV-{code_num}",
        "code_num": code_num,
        "title": title or f"FC2-PPV-{code_num}",
        "source": source,
        "url": url,
        "duration": duration,
        "views": views,
        "thumb": f"https://fourhoi.com/{slug}/cover-n.jpg",
        "preview": f"https://fourhoi.com/{slug}/preview.mp4",
    }


def scrape_missav(pages=None):
    collected = {}
    for page in (pages or list(range(1, PAGES + 1))):
        url = MISSAV_URL if page == 1 else f"{MISSAV_URL}?page={page}"
        try:
            soup = fetch_soup(url)
        except Exception as e:
            print(f"MissAV {page}ページ失敗: {e}")
            break
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
            current = collected.get(code_num)
            if not current:
                collected[code_num] = enrich(code_num, title, "MissAV", full_url, parse_duration(around), parse_views(around))
            else:
                if is_better_title(title, current["title"]):
                    current["title"], current["url"] = title, full_url
                if parse_duration(around) and not current.get("duration"):
                    current["duration"] = parse_duration(around)
                if parse_views(around) and not current.get("views"):
                    current["views"] = parse_views(around)
        print(f"MissAV {page}ページ: {len(collected) - before}件追加 / 合計{len(collected)}")
        if len(collected) == before:
            break
    return list(collected.values())


def scrape_supjav(pages=None):
    items, seen = [], set()
    for page in (pages or list(range(1, PAGES + 1))):
        url = SUPJAV_URL if page == 1 else f"{SUPJAV_URL.rstrip('/')}/page/{page}"
        try:
            soup = fetch_soup(url)
        except Exception as e:
            print(f"Supjav {page}ページ失敗: {e}")
            break
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
            items.append(enrich(code_num, clean_title(a.get("title") or a.get_text(" ", strip=True), code_num), "Supjav", full_url, parse_duration(around), parse_views(around)))
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
    out = []
    for code in order:
        merged[code]["source_label"] = " / ".join(merged[code]["sources"].keys())
        out.append(merged[code])
    return out


def get_latest_videos():
    state = load_json(CRAWL_FILE, {"missav_page": 3, "supjav_page": 3})
    missav_pages = [1, 2] + list(range(int(state.get("missav_page", 3)), int(state.get("missav_page", 3)) + BACKFILL_PAGES))
    supjav_pages = [1, 2] + list(range(int(state.get("supjav_page", 3)), int(state.get("supjav_page", 3)) + BACKFILL_PAGES))
    found, errors = [], []
    try:
        items = scrape_missav(missav_pages)
        print(f"MissAV: {len(items)}件")
        found.append(items)
        state["missav_page"] = missav_pages[-1] + 1
    except Exception as e:
        errors.append(f"MissAV: {e}")
    try:
        items = scrape_supjav(supjav_pages)
        print(f"Supjav: {len(items)}件")
        found.append(items)
        state["supjav_page"] = supjav_pages[-1] + 1
    except Exception as e:
        errors.append(f"Supjav: {e}")
    save_json(CRAWL_FILE, state)
    videos = merge_videos(found)
    if not videos and errors:
        raise RuntimeError(" / ".join(errors))
    return videos


def calc_trend(points, current, hours, now):
    if current is None or not points:
        return None
    target = now - hours * 3600
    older = [p for p in points if isinstance(p.get("t"), (int, float)) and p["t"] <= now - hours * 1800]
    if not older:
        return None
    chosen = min(older, key=lambda p: abs(p["t"] - target))
    prev = chosen.get("v")
    if not isinstance(prev, int):
        return None
    return max(0, current - prev)


def update_views_history(items):
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
        if isinstance(views, int):
            if not points or points[-1].get("v") != views or now - int(points[-1].get("t") or 0) > 3 * 3600:
                points.append({"t": now, "v": views})
            points = points[-40:]
        item["trend_6h"] = calc_trend(points[:-1], views if isinstance(views, int) else None, 6, now)
        item["trend_24h"] = calc_trend(points[:-1], views if isinstance(views, int) else None, 24, now)
        store[code] = {"points": points, "trend_6h": item["trend_6h"], "trend_24h": item["trend_24h"]}
    save_json(VIEWS_HISTORY_FILE, {"updated_at": now_jst(), "items": store})


def json_for_script(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def public_item(item):
    sources = dict(item.get("sources") or {})
    code_num = item.get("code_num") or str(item.get("code", "")).split("-")[-1]
    for name, url in extra_sources(code_num).items():
        sources.setdefault(name, url)
    return {
        "code": item.get("code", ""),
        "code_num": str(code_num),
        "title": item.get("title") or item.get("code", ""),
        "url": item.get("url") or sources.get("MissAV") or "",
        "thumb": item.get("thumb") or "",
        "preview": item.get("preview") or "",
        "duration": item.get("duration") or "",
        "duration_sec": duration_seconds(item.get("duration") or ""),
        "views": item.get("views") if isinstance(item.get("views"), int) else 0,
        "first_seen": item.get("first_seen") or "",
        "last_seen": item.get("last_seen") or "",
        "is_new": bool(item.get("is_new")),
        "sources": sources,
        "source_label": item.get("source_label") or " / ".join(sources.keys()),
        "trend_6h": item.get("trend_6h") if isinstance(item.get("trend_6h"), int) else None,
        "trend_24h": item.get("trend_24h") if isinstance(item.get("trend_24h"), int) else None,
    }


def render_html(items, updated_at, new_count):
    return (
        HTML_TEMPLATE.replace("__UPDATED_AT__", html.escape(updated_at))
        .replace("__NEW_COUNT__", str(new_count))
        .replace("__ITEM_COUNT__", str(len(items)))
        .replace("__ITEMS_JSON__", json_for_script([public_item(x) for x in items]))
    )


HTML_TEMPLATE = r"""<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>FC2-PPV</title>
<style>
:root{color-scheme:dark;--bg:#0f0f0f;--text:#f1f1f1;--muted:#aaa;--line:#272727;--chip:#272727;--bar:#f00}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:80;background:#0f0f0f;border-bottom:1px solid var(--line);padding:calc(8px + env(safe-area-inset-top)) 12px 0}
.top{display:flex;align-items:center;gap:10px}h1{margin:0;font-size:16px}
.count{color:var(--muted);font-size:11px;margin-left:auto;max-width:55%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.search-wrap{position:relative;flex:1}
.search-row{display:none;gap:8px;align-items:center}
.search-row.on,.page-search .search-row{display:flex}
#q{width:100%;border:1px solid #303030;border-radius:20px;padding:12px 14px;background:#121212;color:#fff;font-size:16px;outline:none}
.icon-btn{border:0;background:#272727;color:#fff;border-radius:18px;min-height:44px;padding:0 14px}
.suggest{display:none;position:fixed;left:0;right:0;bottom:calc(56px + env(safe-area-inset-bottom));background:#1a1a1a;border-top:1px solid #333;max-height:45vh;overflow:auto;z-index:95}
.suggest.on{display:block}
.sug{display:flex;gap:10px;align-items:center;width:100%;border:0;background:transparent;color:#fff;text-align:left;min-height:48px;padding:10px 12px}
.sug img{width:72px;height:40px;object-fit:cover;border-radius:6px;background:#000}
.chips,.sorts,.rail{display:flex;gap:8px;overflow-x:auto;-webkit-overflow-scrolling:touch;touch-action:pan-x;scrollbar-width:none}
.chips::-webkit-scrollbar,.rail::-webkit-scrollbar,.sorts::-webkit-scrollbar{display:none}
.chips{padding:0 0 10px}
.lib-tabs{display:none;padding:0 12px 8px}.lib-tabs.on{display:flex}
.chips button,.sorts button{flex:0 0 auto;border:0;border-radius:8px;min-height:36px;padding:8px 14px;background:var(--chip);color:#fff}
.chips button.on,.sorts button.on{background:#f1f1f1;color:#111}
.side{display:none}main{padding:0 0 calc(92px + env(safe-area-inset-bottom))}
.section h2{margin:0 12px 8px;font-size:16px}
.rail{padding:0 12px 12px;overscroll-behavior-x:contain}
.grid{display:grid;grid-template-columns:1fr;gap:14px}
@media(min-width:700px){.grid{grid-template-columns:repeat(2,minmax(0,1fr));padding:0 12px}}
@media(min-width:900px){.nav{display:none}.search-row{display:flex}.side{display:flex;flex-direction:column;position:fixed;left:0;top:118px;bottom:0;width:216px;padding:12px 10px;gap:4px;border-right:1px solid var(--line);background:#0f0f0f}.side button{border:0;background:transparent;color:#fff;text-align:left;border-radius:10px;padding:10px 14px}.side button.on{background:#272727}.side hr{border:0;border-top:1px solid #222;margin:8px 6px}header{padding-left:24px;padding-right:24px}main{margin-left:216px;padding:12px 20px 32px}.suggest{position:absolute;left:0;right:0;top:48px;bottom:auto;max-height:70vh;border:1px solid #333;border-radius:12px}}
@media(min-width:1000px){.grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(min-width:1400px){.grid{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media(min-width:1800px){.grid{grid-template-columns:repeat(5,minmax(0,1fr))}}
.card{min-width:220px}.rail .card{width:240px;flex:0 0 auto}
.thumb-wrap{position:relative;display:block;width:100%;aspect-ratio:16/9;padding:0;border:0;background:#000;overflow:hidden;border-radius:12px}
.thumb-wrap img,.thumb-wrap video{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.thumb-wrap video{opacity:0}.thumb-wrap.playing video{opacity:1}
.badge.new{position:absolute;top:8px;left:8px;z-index:2;background:#3ddc84;color:#073;font-size:11px;font-weight:700;padding:2px 6px;border-radius:4px}
.rank{position:absolute;top:8px;left:8px;z-index:2;background:rgba(0,0,0,.8);color:#fff;font-size:16px;font-weight:800;padding:2px 7px;border-radius:6px}
.rank.top{color:#ffd54a}
.time{position:absolute;right:8px;bottom:14px;z-index:2;background:rgba(0,0,0,.85);color:#fff;font-size:12px;padding:2px 6px;border-radius:4px}
.prog{position:absolute;left:0;right:0;bottom:0;height:3px;background:#333;z-index:3}.prog i{display:block;height:100%;background:var(--bar);width:0}
.body{position:relative;padding:10px 28px 8px 2px}
.title{margin:0 0 4px;font-size:15px;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;color:#fff;text-decoration:none}
.subline,.meta{margin:0;color:var(--muted);font-size:12px}
.more{position:absolute;top:2px;right:0;border:0;background:transparent;color:#aaa;font-size:22px;width:44px;height:44px}
.list-head{display:flex;align-items:center;gap:8px;padding:8px 12px;flex-wrap:wrap}
.nav{position:fixed;left:0;right:0;bottom:0;z-index:80;display:flex;background:#0f0f0f;border-top:1px solid #222;padding:4px 0 calc(6px + env(safe-area-inset-bottom))}
.nav button{flex:1;border:0;background:transparent;color:#888;font-size:10px;min-height:48px}
.nav button.on{color:#fff}
.sheet-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:90}.sheet-bg.on{display:block}
.panel,.menu{display:none;position:fixed;left:0;right:0;bottom:0;z-index:100;background:#212121;border-radius:16px 16px 0 0;padding:10px 14px calc(16px + env(safe-area-inset-bottom));max-height:78vh;overflow:auto}
.panel.on,.menu.on{display:block}
.panel:before,.menu:before{content:"";display:block;width:36px;height:4px;border-radius:4px;background:#555;margin:4px auto 10px}
.panel h3{margin:12px 0 6px;font-size:13px;color:#aaa}
.panel button{margin:0 6px 6px 0;border:0;border-radius:8px;min-height:40px;padding:8px 12px;background:#333;color:#fff}
.panel button.on{background:#f1f1f1;color:#111}
.menu button{display:block;width:100%;border:0;background:transparent;color:#fff;text-align:left;min-height:48px;padding:12px;font-size:16px}
.empty{color:var(--muted);padding:30px 12px;text-align:center}.trend{color:#3ddc84}
</style></head>
<body>
<header>
  <div class="top"><h1>FC2-PPV</h1><span class="count">更新 __UPDATED_AT__ ・ 新着 __NEW_COUNT__ ・ __ITEM_COUNT__件</span></div>
  <div class="search-row"><div class="search-wrap"><input id="q" type="search" placeholder="番号・タイトルで検索" autocomplete="off"><div id="suggest" class="suggest"></div></div><button class="icon-btn" id="filterBtn" type="button">絞り込み</button></div>
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
  <div id="libTabs" class="lib-tabs chips"><button type="button" data-lib="later">後で見る</button><button type="button" data-lib="saved">保存済み</button></div>
  <div id="shelves"></div>
  <div class="list-head"><h2 id="listTitle">すべての作品</h2><span id="resultCount"></span>
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
  <button data-f="vmin" data-v="0">すべて</button><button data-f="vmin" data-v="10000">1万以上</button><button data-f="vmin" data-v="100000">10万以上</button><button data-f="vmin" data-v="500000">50万以上</button>
  <h3>動画時間</h3>
  <button data-f="dur" data-v="">すべて</button><button data-f="dur" data-v="lt30">30分未満</button><button data-f="dur" data-v="30-60">30～60分</button><button data-f="dur" data-v="60-120">60～120分</button><button data-f="dur" data-v="gte120">120分以上</button>
  <h3>検出日</h3>
  <button data-f="seen" data-v="">すべて</button><button data-f="seen" data-v="today">今日</button><button data-f="seen" data-v="24h">24時間</button><button data-f="seen" data-v="7d">7日</button><button data-f="seen" data-v="30d">30日</button>
  <h3>ソース数</h3>
  <button data-f="src" data-v="0">すべて</button><button data-f="src" data-v="1">1以上</button><button data-f="src" data-v="2">2以上</button><button data-f="src" data-v="3">3以上</button>
</div>
<div id="menu" class="menu"></div>
<script id="data" type="application/json">__ITEMS_JSON__</script>
<script>
const ITEMS=JSON.parse(document.getElementById('data').textContent);
const qEl=document.getElementById('q'),sugEl=document.getElementById('suggest'),grid=document.getElementById('grid'),shelves=document.getElementById('shelves'),resultCount=document.getElementById('resultCount'),listTitle=document.getElementById('listTitle'),panel=document.getElementById('panel'),menu=document.getElementById('menu'),sheetBg=document.getElementById('sheetBg');
const loadSet=k=>new Set(JSON.parse(localStorage.getItem(k)||'[]'));
const saveSet=(k,s)=>localStorage.setItem(k,JSON.stringify([...s]));
const favs=loadSet('fc2favs'),watched=loadSet('fc2watched'),later=loadSet('fc2watchlater');
const progress=JSON.parse(localStorage.getItem('fc2progress')||'{}');
let searches=JSON.parse(localStorage.getItem('fc2searchhist')||'[]');
let page='home',chip='all',sort='new',riseWin='24h',filters={vmin:0,dur:'',seen:'',src:0},hoverTimer=null;
const playingSet=new Set();
const byCode=Object.fromEntries(ITEMS.map(x=>[x.code,x]));
const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const digits=s=>String(s||'').replace(/\D/g,'');
function viewsLabel(n){n=Number(n||0);if(!n)return '';if(n<10000)return n.toLocaleString()+'回視聴';const v=n/10000;return (v>=100?String(Math.round(v)):String(Math.round(v*10)/10).replace(/\.0$/,''))+'万回視聴';}
function parseSeen(s){if(!s)return 0;const t=Date.parse(String(s).replace(' ','T')+'+09:00');return isNaN(t)?0:t;}
function relTime(s){const t=parseSeen(s);if(!t)return '';const d=Math.max(0,Date.now()-t),m=Math.floor(d/60000),h=Math.floor(d/3600000),day=Math.floor(d/86400000);if(m<1)return 'たった今';if(m<60)return m+'分前';if(h<24)return h+'時間前';if(day<7)return day+'日前';const dt=new Date(t+9*3600000);return dt.getUTCFullYear()+'/'+String(dt.getUTCMonth()+1).padStart(2,'0')+'/'+String(dt.getUTCDate()).padStart(2,'0');}
const trendOf=it=>riseWin==='6h'?it.trend_6h:it.trend_24h;
function matchQuery(it,q){q=(q||'').trim();if(!q)return true;const num=digits(q),hay=(it.code+' '+it.code_num+' '+it.title+' '+it.source_label).toLowerCase();if(/^\d+$/.test(q.replace(/\s/g,''))||/^fc2[-_ ]?ppv[-_ ]?\d+$/i.test(q.replace(/\s/g,'')))return it.code_num.includes(num)||digits(it.code).endsWith(num);return hay.includes(q.toLowerCase())||(num&&it.code_num.includes(num));}
function matchFilters(it){
  if((it.views||0)<Number(filters.vmin||0))return false;
  const sec=it.duration_sec||0;
  if(filters.dur==='lt30'&&!(sec&&sec<1800))return false;
  if(filters.dur==='30-60'&&!(sec>=1800&&sec<3600))return false;
  if(filters.dur==='60-120'&&!(sec>=3600&&sec<7200))return false;
  if(filters.dur==='gte120'&&!(sec>=7200))return false;
  const age=Date.now()-parseSeen(it.first_seen);
  if(filters.seen==='today'&&(it.first_seen||'').slice(0,10)!==new Date(Date.now()+9*3600000).toISOString().slice(0,10))return false;
  if(filters.seen==='24h'&&age>86400000)return false;
  if(filters.seen==='7d'&&age>7*86400000)return false;
  if(filters.seen==='30d'&&age>30*86400000)return false;
  if(Number(filters.src||0)&&Object.keys(it.sources||{}).length<Number(filters.src))return false;
  if(chip==='new'&&!it.is_new)return false;
  if(chip==='rising'&&!(trendOf(it)>0))return false;
  if(chip==='popular'&&!(it.views>0))return false;
  if(chip==='today'&&(it.first_seen||'').slice(0,10)!==new Date(Date.now()+9*3600000).toISOString().slice(0,10))return false;
  if(chip==='week'&&age>7*86400000)return false;
  if(chip==='views10'&&(it.views||0)<100000)return false;
  if(chip==='dur60'&&(it.duration_sec||0)<3600)return false;
  if(chip==='saved'&&!favs.has(it.code))return false;
  if(page==='new'&&!it.is_new&&age>2*86400000)return false;
  if(page==='rising'&&!(trendOf(it)>0))return false;
  if(page==='popular'&&!(it.views>0))return false;
  if(page==='history'&&!watched.has(it.code))return false;
  if(page==='later'&&!later.has(it.code))return false;
  if(page==='saved'&&!favs.has(it.code))return false;
  return true;
}
function sortItems(arr){
  const copy=arr.slice();
  copy.sort((a,b)=>sort==='old'?(a.first_seen||'').localeCompare(b.first_seen||''):sort==='views'?(b.views||0)-(a.views||0):sort==='rise'?(trendOf(b)||-1)-(trendOf(a)||-1):sort==='long'?(b.duration_sec||0)-(a.duration_sec||0):sort==='short'?(a.duration_sec||0)-(b.duration_sec||0):(b.first_seen||'').localeCompare(a.first_seen||''));
  if(page==='rising'||chip==='rising')copy.sort((a,b)=>(trendOf(b)||-1)-(trendOf(a)||-1));
  if(page==='popular'||chip==='popular')copy.sort((a,b)=>(b.views||0)-(a.views||0));
  const q=qEl.value.trim();
  if(/^\d+$/.test(q))copy.sort((a,b)=>(b.code_num===q?1:0)-(a.code_num===q?1:0));
  return copy;
}
function cardHTML(it,rank){
  const prog=Number(progress[it.code]||0),tr=trendOf(it);
  const extra=(page==='rising'||chip==='rising')&&tr?`<p class="meta trend">${riseWin} +${tr.toLocaleString()}</p>`:'';
  return `<article class="card" data-code="${it.code}"><button class="thumb-wrap" type="button" data-code="${it.code}"><img src="${it.thumb}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.opacity=0"><video muted loop playsinline preload="none" poster="${it.thumb}"></video>${rank?`<span class="rank${rank<=3?' top':''}">#${rank}</span>`:(it.is_new?'<span class="badge new">NEW</span>':'')}<span class="time">${it.duration||''}</span><span class="prog"><i style="width:${Math.min(100,prog*100)}%"></i></span></button><div class="body"><a class="title open" href="${esc(it.sources.MissAV||it.url||'#')}" target="_blank" rel="noopener">${esc(it.title)}</a><p class="subline">${esc(it.code)}</p><p class="meta">${[viewsLabel(it.views),relTime(it.first_seen)].filter(Boolean).join(' ・ ')}</p>${extra}<p class="meta">${esc(it.source_label||'')}</p><button class="more" type="button" data-more="${esc(it.code)}">⋮</button></div></article>`;
}
document.addEventListener('click',e=>{
  const more=e.target.closest('.more');
  if(more){e.preventDefault();e.stopPropagation();openMenu(more.dataset.more,e);return;}
  const open=e.target.closest('.open');
  if(open){const card=open.closest('.card');if(card){watched.add(card.dataset.code);saveSet('fc2watched',watched);}return;}
  const thumb=e.target.closest('.thumb-wrap');
  if(thumb){e.preventDefault();thumb.classList.contains('playing')?stopPreview(thumb):startPreview(thumb);}
});
document.addEventListener('mouseover',e=>{
  const w=e.target.closest('.thumb-wrap');
  if(!w||!window.matchMedia('(hover:hover) and (pointer:fine)').matches)return;
  clearTimeout(hoverTimer);hoverTimer=setTimeout(()=>startPreview(w),600);
});
document.addEventListener('mouseout',e=>{
  const w=e.target.closest('.thumb-wrap');
  if(!w||w.contains(e.relatedTarget))return;
  clearTimeout(hoverTimer);
  if(window.matchMedia('(hover:hover) and (pointer:fine)').matches)stopPreview(w);
});
function startPreview(w){
  const it=byCode[w.dataset.code];if(!it||!it.preview)return;
  const v=w.querySelector('video');if(!v.getAttribute('src'))v.src=it.preview;
  const p=Number(progress[it.code]||0);w.classList.add('playing');playingSet.add(w);
  if(playingSet.size>4){const oldest=[...playingSet].find(x=>x!==w);if(oldest)stopPreview(oldest);}
  const play=async()=>{try{await v.play();if(p>0&&p<.95){try{v.currentTime=p*(v.duration||0);}catch(e){}}}catch(e){}};
  v.onloadedmetadata=play;
  v.ontimeupdate=()=>{if(!v.duration)return;const r=v.currentTime/v.duration;progress[it.code]=r;localStorage.setItem('fc2progress',JSON.stringify(progress));const bar=w.querySelector('.prog i');if(bar)bar.style.width=Math.min(100,r*100)+'%';if(r>.9){watched.add(it.code);saveSet('fc2watched',watched);}};
  play();
}
function stopPreview(w){if(!w)return;const v=w.querySelector('video');v.pause();v.removeAttribute('src');v.load();w.classList.remove('playing');playingSet.delete(w);}
function railHTML(title,arr,ranked,go){if(!arr.length)return '';return `<section class="section"><h2${go?` data-go="${go}"`:''}>${title}</h2><div class="rail">${arr.map((it,i)=>cardHTML(it,ranked?i+1:0)).join('')}</div></section>`;}
function render(){
  const query=qEl.value.trim();
  document.body.classList.toggle('page-search',page==='search');
  document.querySelector('.search-row').classList.toggle('on',page==='search');
  document.getElementById('libTabs').classList.toggle('on',page==='later'||page==='saved'||page==='library');
  document.querySelectorAll('[data-lib]').forEach(b=>b.classList.toggle('on',page===b.dataset.lib));
  const showHome=page==='home'&&!query&&chip==='all'&&!filters.vmin&&!filters.dur&&!filters.seen&&!filters.src;
  if(showHome){
    const rise=ITEMS.filter(x=>(x.trend_24h||0)>0).sort((a,b)=>(b.trend_24h||0)-(a.trend_24h||0)).slice(0,10);
    const pop=ITEMS.filter(x=>x.views>0).sort((a,b)=>b.views-a.views).slice(0,10);
    const news=ITEMS.filter(x=>x.is_new).slice(0,10);
    const recent=ITEMS.filter(x=>watched.has(x.code)).slice(0,10);
    const saved=ITEMS.filter(x=>favs.has(x.code)).slice(0,10);
    const weekPop=ITEMS.filter(x=>x.views>0&&Date.now()-parseSeen(x.first_seen)<=7*86400000).sort((a,b)=>b.views-a.views).slice(0,10);
    shelves.innerHTML=railHTML('急上昇 TOP10',rise,true,'rising')+railHTML('人気 TOP10',pop,true,'popular')+railHTML('新着',news,false,'new')+(weekPop.length?railHTML('今週の人気',weekPop,true,'popular'):'')+railHTML('最近見た作品',recent,false,'history')+railHTML('保存済み',saved,false,'saved');
  }else shelves.innerHTML=page==='rising'?`<div class="chips" style="padding:8px 12px"><button type="button" data-rise="6h"${riseWin==='6h'?' class="on"':''}>6時間</button><button type="button" data-rise="24h"${riseWin==='24h'?' class="on"':''}>24時間</button></div>`:'';
  const titles={home:'すべての作品',rising:'急上昇',popular:'人気ランキング',new:'新着',search:'検索',history:'履歴',later:'後で見る',saved:'保存済み'};
  listTitle.textContent=query?'検索結果':(titles[page]||'すべての作品');
  let list=sortItems(ITEMS.filter(it=>matchQuery(it,query)&&matchFilters(it)));
  if(page==='popular'||page==='rising')list=list.slice(0,100);
  resultCount.textContent=(query?'検索結果 ':'')+list.length+'件';
  grid.innerHTML=list.length?list.map((it,i)=>cardHTML(it,(page==='popular'||page==='rising')?i+1:0)).join(''):'<p class="empty">該当する作品がありません。</p>';
  document.querySelectorAll('[data-go]').forEach(b=>b.addEventListener('click',()=>{page=b.dataset.go;if(page==='rising')chip='rising';if(page==='popular')chip='popular';render();}));
  document.querySelectorAll('[data-rise]').forEach(b=>b.addEventListener('click',()=>{riseWin=b.dataset.rise;render();}));
  document.querySelectorAll('[data-page]').forEach(b=>b.classList.toggle('on',b.dataset.page===page||((page==='later'||page==='saved')&&b.dataset.page==='library')));
  document.querySelectorAll('[data-chip]').forEach(b=>b.classList.toggle('on',b.dataset.chip===chip));
  document.querySelectorAll('[data-sort]').forEach(b=>b.classList.toggle('on',b.dataset.sort===sort));
}
function addSearch(term){term=(term||'').trim();if(!term)return;searches=[term,...searches.filter(x=>x!==term)].slice(0,10);localStorage.setItem('fc2searchhist',JSON.stringify(searches));}
function showSuggest(){
  const query=qEl.value.trim();let html='';
  if(!query)html=searches.map(s=>`<button class="sug" data-q="${esc(s)}"><span>${esc(s)}</span><b class="sug-del" data-del="${esc(s)}">×</b></button>`).join('')+(searches.length?'<button class="sug" id="clearHist">検索履歴をすべて削除</button>':'<div class="sug"><span>最近の検索はありません</span></div>');
  else{const num=digits(query);const scored=ITEMS.map(it=>{let score=0;if(it.code_num===num)score+=100;else if(num&&it.code_num.includes(num))score+=50;if(it.title.toLowerCase().includes(query.toLowerCase()))score+=10;return{it,score};}).filter(x=>x.score>0).sort((a,b)=>b.score-a.score).slice(0,8);html=scored.map(x=>`<button class="sug" data-code="${esc(x.it.code)}"><img src="${esc(x.it.thumb)}" alt=""><div><b>${esc(x.it.code)}</b><span>${esc(x.it.title)}</span></div></button>`).join('')||'<div class="sug"><span>候補なし</span></div>';}
  sugEl.innerHTML=html;sugEl.classList.add('on');
  sugEl.querySelectorAll('[data-q]').forEach(b=>b.addEventListener('click',e=>{if(e.target.dataset.del){searches=searches.filter(x=>x!==e.target.dataset.del);localStorage.setItem('fc2searchhist',JSON.stringify(searches));showSuggest();return;}qEl.value=b.dataset.q;addSearch(b.dataset.q);sugEl.classList.remove('on');render();}));
  sugEl.querySelectorAll('[data-code]').forEach(b=>b.addEventListener('click',()=>{const it=byCode[b.dataset.code];qEl.value=it.code;addSearch(it.code);sugEl.classList.remove('on');render();}));
  const clr=document.getElementById('clearHist');if(clr)clr.addEventListener('click',()=>{searches=[];localStorage.setItem('fc2searchhist','[]');showSuggest();});
}
function openSheet(el){el.classList.add('on');sheetBg.classList.add('on');}
function closeSheets(){panel.classList.remove('on');menu.classList.remove('on');sheetBg.classList.remove('on');}
function openMenu(code,ev){
  const it=byCode[code];
  menu.innerHTML=`<button data-act="later">${later.has(code)?'後で見るから外す':'後で見る'}</button><button data-act="save">${favs.has(code)?'保存を解除':'保存'}</button><button data-act="watched">${watched.has(code)?'視聴済みを解除':'視聴済みにする'}</button><button data-act="unwatch">履歴から削除</button>${Object.entries(it.sources||{}).map(([n,u])=>`<button data-url="${esc(u)}">${esc(n)}</button>`).join('')}`;
  openSheet(menu);
  menu.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{
    if(b.dataset.url){watched.add(code);saveSet('fc2watched',watched);window.open(b.dataset.url,'_blank');}
    if(b.dataset.act==='later'){later.has(code)?later.delete(code):later.add(code);saveSet('fc2watchlater',later);}
    if(b.dataset.act==='save'){favs.has(code)?favs.delete(code):favs.add(code);saveSet('fc2favs',favs);}
    if(b.dataset.act==='watched'){watched.has(code)?watched.delete(code):watched.add(code);saveSet('fc2watched',watched);}
    if(b.dataset.act==='unwatch'){watched.delete(code);saveSet('fc2watched',watched);}
    closeSheets();render();
  }));
}
qEl.addEventListener('input',()=>{render();showSuggest();});
qEl.addEventListener('focus',showSuggest);
qEl.addEventListener('keydown',e=>{if(e.key==='Enter'){addSearch(qEl.value);sugEl.classList.remove('on');}});
document.addEventListener('click',e=>{if(!e.target.closest('.search-wrap'))sugEl.classList.remove('on');if(!e.target.closest('.menu')&&!e.target.closest('.more')&&!e.target.closest('.panel')&&!e.target.closest('#filterBtn'))closeSheets();});
document.getElementById('filterBtn').addEventListener('click',()=>openSheet(panel));
sheetBg.addEventListener('click',closeSheets);
document.querySelectorAll('[data-page]').forEach(b=>b.addEventListener('click',()=>{page=b.dataset.page;if(page==='rising')chip='rising';if(page==='popular')chip='popular';if(page==='new')chip='new';if(page==='home')chip='all';if(page==='search'){chip='all';setTimeout(()=>qEl.focus(),50);}if(page==='library')page='later';render();}));
document.querySelectorAll('[data-lib]').forEach(b=>b.addEventListener('click',()=>{page=b.dataset.lib;render();}));
document.querySelectorAll('[data-chip]').forEach(b=>b.addEventListener('click',()=>{chip=b.dataset.chip;page='home';render();}));
document.querySelectorAll('[data-sort]').forEach(b=>b.addEventListener('click',()=>{sort=b.dataset.sort;render();}));
panel.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{filters[b.dataset.f]=isNaN(Number(b.dataset.v))?b.dataset.v:Number(b.dataset.v);panel.querySelectorAll('[data-f="'+b.dataset.f+'"]').forEach(x=>x.classList.toggle('on',x===b));render();}));
render();
</script>
</body></html>
"""


def main() -> int:
    print(f"[{now_jst()}] チェック開始")
    try:
        latest = get_latest_videos()
    except Exception as e:
        send_telegram(f"監視エラー: ページ取得に失敗しました\n{e}")
        return 1
    if not latest:
        send_telegram("監視エラー: 動画リストを抽出できませんでした。")
        return 1
    fill_missing_views(latest)
    history = load_json(HISTORY_FILE, {"ids": []})
    known = set(history.get("ids", []))
    existing = load_json(DATA_FILE, {"items": []})
    existing_map = {item["code"]: item for item in existing.get("items", [])}
    first_run = not known
    new_videos, merged, stamp = [], [], now_jst()
    for video in latest:
        old = existing_map.get(video["code"], {})
        is_new = video["code"] not in known and not first_run
        item = {**video, "first_seen": old.get("first_seen", stamp), "last_seen": stamp, "is_new": is_new}
        if old.get("title") and not is_better_title(item.get("title", ""), old.get("title", "")):
            item["title"] = old["title"]
        item["views"] = video.get("views") or old.get("views")
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
    update_views_history(merged)
    save_json(DATA_FILE, {"updated_at": stamp, "items": merged})
    save_json(HISTORY_FILE, {"updated_at": stamp, "ids": sorted(known)})
    HTML_FILE.parent.mkdir(parents=True, exist_ok=True)
    HTML_FILE.write_text(render_html(merged, stamp, len(new_videos)), encoding="utf-8")
    if first_run:
        send_telegram("監視を開始しました。\n今後の新着だけ通知します。")
        return 0

    def allowed(video):
        text = f"{video.get('code','')} {video.get('title','')}"
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
        lines = [f"{n}: {u}" for n, u in (video.get("sources") or {video.get("source", "Link"): video.get("url")}).items()]
        extra = []
        if video.get("duration"):
            extra.append(f"時間: {video['duration']}")
        if video.get("views"):
            extra.append(f"再生: {video['views']:,}")
        send_telegram("【新着 FC2-PPV】\n\n" + f"{video['code']}\n{video['title']}\n" + ((" / ".join(extra) + "\n\n") if extra else "\n") + "\n".join(lines), photo=video.get("thumb"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
