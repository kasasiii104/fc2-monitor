#!/usr/bin/env python3
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
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "80"))
KEEP_ITEMS = int(os.environ.get("KEEP_ITEMS", "0"))
PAGES = int(os.environ.get("PAGES", "8"))
BACKFILL_PAGES = int(os.environ.get("BACKFILL_PAGES", "6"))
INCLUDE_KEYWORDS = [x.strip() for x in os.environ.get("INCLUDE_KEYWORDS", "").split(",") if x.strip()]
EXCLUDE_KEYWORDS = [x.strip() for x in os.environ.get("EXCLUDE_KEYWORDS", "").split(",") if x.strip()]

JST = timezone(timedelta(hours=9))


def now_jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


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


def send_telegram(text: str, photo: str | None = None) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram未設定のため通知スキップ")
        return
    if photo:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        res = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "photo": photo,
                "caption": text[:1024],
            },
            timeout=20,
        )
        if res.ok:
            return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    res = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    res.raise_for_status()


def fetch_soup(url: str) -> BeautifulSoup:
    res = requests.get(url, headers=HEADERS, timeout=30)
    res.raise_for_status()
    return BeautifulSoup(res.text, "html.parser")


DURATION_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")


def clean_title(text: str, code_num: str) -> str:
    title = re.sub(r"\s+", " ", (text or "")).strip()
    title = re.sub(r"^\d{1,2}:\d{2}(?::\d{2})?\s*", "", title)
    title = title.replace(f"FC2-PPV-{code_num}", "").replace(f"FC2PPV {code_num}", "")
    title = title.replace(f"FC2PPV-{code_num}", "").strip(" -|/")
    return title[:180]


def is_better_title(new: str, old: str) -> bool:
    if not new:
        return False
    if DURATION_RE.match(new):
        return False
    if not old or DURATION_RE.match(old) or old.startswith("FC2-PPV-"):
        return True
    return len(new) > len(old)


def parse_duration(text: str) -> str:
    match = re.search(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b", text or "")
    return match.group(1) if match else ""


def parse_views(text: str) -> int | None:
    match = re.search(r"([\d,]+)\s*(?:views|view|回再生|視聴|Views)", text or "", flags=re.I)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def enrich(code_num: str, title: str, source: str, url: str, duration: str = "", views: int | None = None) -> dict:
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


def scrape_missav(pages: list[int] | None = None) -> list[dict]:
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
            href = a["href"]
            match = re.search(r"/fc2-ppv-(\d+)", href, flags=re.I)
            if not match:
                continue
            code_num = match.group(1)
            full_url = href if href.startswith("http") else urljoin("https://missav.live", href)
            img = a.find("img")
            raw = " ".join([
                a.get("title") or "",
                (img.get("alt") if img else "") or "",
                a.get_text(" ", strip=True),
            ])
            title = clean_title(raw, code_num)
            around = " ".join([
                raw,
                a.parent.get_text(" ", strip=True) if a.parent else "",
            ])
            duration = parse_duration(around)
            views = parse_views(around)
            current = collected.get(code_num)
            if not current:
                collected[code_num] = enrich(
                    code_num,
                    title or f"FC2-PPV-{code_num}",
                    "MissAV",
                    full_url,
                    duration=duration,
                    views=views,
                )
            else:
                if is_better_title(title, current["title"]):
                    current["title"] = title
                    current["url"] = full_url
                if duration and not current.get("duration"):
                    current["duration"] = duration
                if views and not current.get("views"):
                    current["views"] = views
        print(f"MissAV {page}ページ: {len(collected) - before}件追加 / 合計{len(collected)}")
        if len(collected) == before:
            break
    return list(collected.values())


def scrape_supjav(pages: list[int] | None = None) -> list[dict]:
    items = []
    seen = set()
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
            if code_num in seen:
                continue
            href = a["href"]
            if href.startswith("#") or "category" in href:
                continue
            seen.add(code_num)
            title = clean_title(a.get("title") or a.get_text(" ", strip=True), code_num)
            full_url = href if href.startswith("http") else urljoin("https://supjav.com", href)
            around = " ".join([text, a.parent.get_text(" ", strip=True) if a.parent else ""])
            items.append(enrich(
                code_num,
                title or f"FC2-PPV-{code_num}",
                "Supjav",
                full_url,
                duration=parse_duration(around),
                views=parse_views(around),
            ))
        print(f"Supjav {page}ページ: {len(seen) - before}件追加 / 合計{len(seen)}")
        if len(seen) == before:
            break
    return items


def merge_videos(groups: list[list[dict]]) -> list[dict]:
    merged = {}
    order = []
    for group in groups:
        for item in group:
            code = item["code"]
            if code not in merged:
                merged[code] = {
                    **item,
                    "sources": {item["source"]: item["url"]},
                }
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
    result = []
    for code in order:
        item = merged[code]
        item["source_label"] = " / ".join(item["sources"].keys())
        result.append(item)
    return result


def next_pages(start: int, count: int) -> list[int]:
    return list(range(start, start + count))


def get_latest_videos() -> list[dict]:
    state = load_json(CRAWL_FILE, {"missav_page": 3, "supjav_page": 3})
    missav_pages = [1, 2] + next_pages(int(state.get("missav_page", 3)), BACKFILL_PAGES)
    supjav_pages = [1, 2] + next_pages(int(state.get("supjav_page", 3)), BACKFILL_PAGES)
    found = []
    errors = []
    try:
        items = scrape_missav(missav_pages)
        print(f"MissAV: {len(items)}件 pages={missav_pages}")
        found.append(items)
        state["missav_page"] = missav_pages[-1] + 1
    except Exception as e:
        print(f"MissAV 取得失敗: {e}", file=sys.stderr)
        errors.append(f"MissAV: {e}")
    try:
        items = scrape_supjav(supjav_pages)
        print(f"Supjav: {len(items)}件 pages={supjav_pages}")
        found.append(items)
        state["supjav_page"] = supjav_pages[-1] + 1
    except Exception as e:
        print(f"Supjav 取得失敗: {e}", file=sys.stderr)
        errors.append(f"Supjav: {e}")
    save_json(CRAWL_FILE, state)
    videos = merge_videos(found)
    if not videos and errors:
        raise RuntimeError(" / ".join(errors))
    return videos


def render_html(items: list[dict], updated_at: str, new_count: int) -> str:
    cards = []
    for item in items:
        badge = '<span class="badge new">NEW</span>' if item.get("is_new") else ""
        thumb = item.get("thumb") or ""
        preview = item.get("preview") or ""
        sources = item.get("sources") or {item.get("source", "Link"): item.get("url", "#")}
        code_num = item.get("code_num") or item["code"].split("-")[-1]
        sources.setdefault("JavDB", f"https://javdb.com/search?q=FC2-PPV-{code_num}&f=all")
        sources.setdefault("FC2検索", f"https://adult.contents.fc2.com/search/?q={code_num}")
        sources.setdefault("123AV", f"https://123av.com/ja/search?keyword=FC2-PPV-{code_num}")
        sources.setdefault("JavFC2", f"https://javfc2.xyz/search?q={code_num}")
        duration = item.get("duration") or "-"
        views = item.get("views")
        views_label = f"{views:,}回" if isinstance(views, int) else "-"
        source_links = " ".join(
            f'<a href="{url}" target="_blank" rel="noopener">{name}</a>'
            for name, url in sources.items()
        )
        seen = item.get("first_seen", "")
        cards.append(
            f"""
            <article class="card" data-code="{item['code']}" data-seen="{seen}" data-new="{1 if item.get('is_new') else 0}" data-views="{item.get('views') or 0}" data-duration="{duration}">
              <button class="thumb-wrap" type="button" data-preview="{preview}" aria-label="プレビュー再生">
                <img src="{thumb}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.display='none'">
                <video muted loop playsinline preload="none" poster="{thumb}"></video>
                {badge}
              </button>
              <div class="body">
                <div class="row">
                  <span class="code">{item['code']}</span>
                  <button class="fav" type="button" data-code="{item['code']}">☆</button>
                </div>
                <p class="title">{item['title']}</p>
                <p class="meta">時間: {duration} ／ 再生: {views_label}</p>
                <p class="meta">初回確認: {item.get('first_seen', '-')} ／ {item.get('source_label', item.get('source', ''))}</p>
                <p class="links">{source_links}</p>
              </div>
            </article>
            """
        )
    cards_html = "\n".join(cards) if cards else '<p class="empty">まだデータがありません。</p>'
    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FC2-PPV 新着モニター</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0f1115;
      --card: #1a1f29;
      --text: #f3f5f7;
      --muted: #9aa3b2;
      --accent: #7dd3fc;
      --new: #34d399;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    header {{
      position: sticky;
      top: 0;
      padding: 16px 16px 12px;
      background: rgba(15,17,21,.92);
      backdrop-filter: blur(10px);
      border-bottom: 1px solid #2a3140;
    }}
    h1 {{ margin: 0; font-size: 18px; }}
    .sub {{ margin: 6px 0 12px; color: var(--muted); font-size: 13px; }}
    input {{
      width: 100%;
      border: 0;
      border-radius: 12px;
      padding: 12px 14px;
      background: #11161f;
      color: var(--text);
      font-size: 16px;
    }}
    .toolbar {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }}
    .toolbar button {{
      border: 0;
      border-radius: 999px;
      padding: 8px 12px;
      background: #11161f;
      color: var(--text);
      font-size: 13px;
    }}
    .toolbar button.on {{
      background: #243044;
      color: var(--accent);
    }}
    .row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
    }}
    .fav {{
      border: 0;
      background: transparent;
      color: var(--muted);
      font-size: 18px;
    }}
    .fav.on {{
      color: #fbbf24;
    }}
    main {{
      padding: 12px;
      display: grid;
      gap: 10px;
    }}
    .card {{
      display: grid;
      grid-template-columns: 128px 1fr;
      gap: 12px;
      color: inherit;
      background: var(--card);
      border-radius: 16px;
      padding: 10px;
      overflow: hidden;
    }}
    .thumb-wrap {{
      border: 0;
      padding: 0;
      cursor: pointer;
    }}
    .thumb-wrap video {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      object-fit: cover;
      opacity: 0;
      pointer-events: none;
    }}
    .thumb-wrap.playing video {{
      opacity: 1;
    }}
    .links a {{
      display: inline-block;
      margin-right: 8px;
      color: var(--accent);
      font-size: 13px;
    }}
    .thumb-wrap {{
      position: relative;
      width: 112px;
      height: 84px;
      border-radius: 12px;
      overflow: hidden;
      background: #11161f;
    }}
    .thumb-wrap img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }}
    .thumb-wrap .badge {{
      position: absolute;
      top: 6px;
      left: 6px;
    }}
    .body {{
      min-width: 0;
    }}
    .code {{ color: var(--accent); font-weight: 700; font-size: 13px; }}
    .badge.new {{
      background: rgba(52,211,153,.15);
      color: var(--new);
      font-size: 11px;
      padding: 3px 8px;
      border-radius: 999px;
    }}
    .title {{ margin: 8px 0 6px; font-size: 15px; line-height: 1.45; }}
    .meta {{ margin: 0; color: var(--muted); font-size: 12px; }}
    .empty {{ color: var(--muted); text-align: center; padding: 40px 0; }}
  </style>
</head>
<body>
  <header>
    <h1>FC2-PPV 新着モニター</h1>
    <p class="sub">更新: {updated_at} / 今回の新着 {new_count}件</p>
    <input id="q" type="search" placeholder="番号やタイトルで検索">
    <div class="toolbar">
      <button type="button" data-filter="all" class="on">すべて</button>
      <button type="button" data-filter="new">NEW</button>
      <button type="button" data-filter="fav">お気に入り</button>
      <button type="button" data-filter="watched">視聴履歴</button>
      <button type="button" data-filter="today">今日</button>
      <button type="button" data-filter="yesterday">昨日</button>
      <button type="button" data-filter="week">1週間</button>
      <button type="button" data-sort="new">新しい順</button>
      <button type="button" data-sort="views">再生数順</button>
    </div>
  </header>
  <main id="list">
    {cards_html}
  </main>
  <script>
    const q = document.getElementById('q');
    const list = document.getElementById('list');
    const cards = [...document.querySelectorAll('.card')];
    const favs = new Set(JSON.parse(localStorage.getItem('fc2favs') || '[]'));
    const watched = new Set(JSON.parse(localStorage.getItem('fc2watched') || '[]'));
    let filter = 'all';
    const saveFavs = () => localStorage.setItem('fc2favs', JSON.stringify([...favs]));
    const saveWatched = () => localStorage.setItem('fc2watched', JSON.stringify([...watched]));
    const ymd = (d) => d.toISOString().slice(0, 10);
    const now = new Date();
    const today = ymd(new Date(now.getTime() + 9 * 3600 * 1000));
    const yesterdayDate = new Date(now.getTime() + 9 * 3600 * 1000);
    yesterdayDate.setUTCDate(yesterdayDate.getUTCDate() - 1);
    const yesterday = ymd(yesterdayDate);
    const weekAgo = new Date(now.getTime() + 9 * 3600 * 1000);
    weekAgo.setUTCDate(weekAgo.getUTCDate() - 7);
    const weekStart = ymd(weekAgo);
    const apply = () => {{
      const keyword = q.value.trim().toLowerCase();
      cards.forEach(card => {{
        const text = card.textContent.toLowerCase();
        const isFav = favs.has(card.dataset.code);
        const isNew = card.dataset.new === '1';
        const isWatched = watched.has(card.dataset.code);
        const seenDay = (card.dataset.seen || '').slice(0, 10);
        let ok = text.includes(keyword);
        if (filter === 'new') ok = ok && isNew;
        if (filter === 'fav') ok = ok && isFav;
        if (filter === 'watched') ok = ok && isWatched;
        if (filter === 'today') ok = ok && seenDay === today;
        if (filter === 'yesterday') ok = ok && seenDay === yesterday;
        if (filter === 'week') ok = ok && seenDay >= weekStart;
        card.style.display = ok ? '' : 'none';
        const btn = card.querySelector('.fav');
        btn.classList.toggle('on', isFav);
        btn.textContent = isFav ? '★' : '☆';
      }});
    }};
    q.addEventListener('input', apply);
    document.querySelectorAll('[data-filter]').forEach(btn => {{
      btn.addEventListener('click', () => {{
        filter = btn.dataset.filter;
        document.querySelectorAll('[data-filter]').forEach(b => b.classList.toggle('on', b === btn));
        apply();
      }});
    }});
    document.querySelector('[data-sort="new"]').addEventListener('click', () => {{
      cards.sort((a, b) => (b.dataset.seen || '').localeCompare(a.dataset.seen || ''));
      cards.forEach(card => list.appendChild(card));
    }});
    document.querySelector('[data-sort="views"]').addEventListener('click', () => {{
      cards.sort((a, b) => Number(b.dataset.views || 0) - Number(a.dataset.views || 0));
      cards.forEach(card => list.appendChild(card));
    }});
    document.querySelectorAll('.links a').forEach(a => {{
      a.addEventListener('click', () => {{
        const card = a.closest('.card');
        if (!card) return;
        watched.add(card.dataset.code);
        saveWatched();
      }});
    }});
    document.querySelectorAll('.fav').forEach(btn => {{
      btn.addEventListener('click', () => {{
        const code = btn.dataset.code;
        if (favs.has(code)) favs.delete(code);
        else favs.add(code);
        saveFavs();
        apply();
      }});
    }});
    apply();

    let current = null;
    const stopPreview = (wrap) => {{
      const video = wrap.querySelector('video');
      video.pause();
      video.removeAttribute('src');
      video.load();
      wrap.classList.remove('playing');
    }};
    const playPreview = async (wrap) => {{
      const video = wrap.querySelector('video');
      const src = wrap.dataset.preview;
      if (!src) return;
      if (current && current !== wrap) stopPreview(current);
      if (!video.src) video.src = src;
      wrap.classList.add('playing');
      current = wrap;
      try {{ await video.play(); }} catch (e) {{}}
    }};
    document.querySelectorAll('.thumb-wrap').forEach(wrap => {{
      wrap.addEventListener('pointerenter', () => playPreview(wrap));
      wrap.addEventListener('pointerleave', () => stopPreview(wrap));
      wrap.addEventListener('click', (e) => {{
        e.preventDefault();
        if (wrap.classList.contains('playing')) stopPreview(wrap);
        else playPreview(wrap);
      }});
    }});
  </script>
</body>
</html>
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
    new_videos = []
    merged = []
    stamp = now_jst()

    for video in latest:
        old = existing_map.get(video["code"], {})
        is_new = video["code"] not in known and not first_run
        item = {
            **video,
            "first_seen": old.get("first_seen", stamp),
            "last_seen": stamp,
            "is_new": is_new,
            "duration": video.get("duration") or old.get("duration", ""),
            "views": video.get("views") or old.get("views"),
        }
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
    save_json(DATA_FILE, {"updated_at": stamp, "items": merged})
    save_json(HISTORY_FILE, {"updated_at": stamp, "ids": sorted(known)})
    HTML_FILE.parent.mkdir(parents=True, exist_ok=True)
    HTML_FILE.write_text(render_html(merged, stamp, len(new_videos)), encoding="utf-8")

    if first_run:
        preview = "\n".join(f"- {v['code']}" for v in latest[:8])
        send_telegram(
            "監視を開始しました。\n"
            "今後の新着だけ通知します。\n\n"
            f"現在の最新:\n{preview}"
        )
        print("初回保存完了")
        return 0

    def allowed(video: dict) -> bool:
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
        source_lines = []
        for name, url in (video.get("sources") or {video.get("source", "Link"): video.get("url")}).items():
            source_lines.append(f"{name}: {url}")
        extra = []
        if video.get("duration"):
            extra.append(f"時間: {video['duration']}")
        if video.get("views"):
            extra.append(f"再生: {video['views']:,}")
        send_telegram(
            "【新着 FC2-PPV】\n\n"
            f"{video['code']}\n"
            f"{video['title']}\n"
            + (" / ".join(extra) + "\n\n" if extra else "\n")
            + "\n".join(source_lines),
            photo=video.get("thumb"),
        )
        print(f"通知: {video['code']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
