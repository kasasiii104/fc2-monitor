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
TARGET_URL = os.environ.get("TARGET_URL", "https://missav.live/ja/search/fc2-ppv")
DATA_FILE = Path(os.environ.get("DATA_FILE", "docs/data.json"))
HISTORY_FILE = Path(os.environ.get("HISTORY_FILE", "docs/notified_ids.json"))
HTML_FILE = Path(os.environ.get("HTML_FILE", "docs/index.html"))
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "40"))
KEEP_ITEMS = int(os.environ.get("KEEP_ITEMS", "200"))

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


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram未設定のため通知スキップ")
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


def get_latest_videos() -> list[dict]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja,en;q=0.8",
    }
    res = requests.get(TARGET_URL, headers=headers, timeout=30)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")

    videos = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        match = re.search(r"/fc2-ppv-(\d+)", href, flags=re.I)
        if not match:
            continue
        code = f"FC2-PPV-{match.group(1)}"
        if code in seen:
            continue
        seen.add(code)
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 8:
            parent = a.find_parent()
            title = parent.get_text(" ", strip=True)[:180] if parent else code
        slug = f"fc2-ppv-{match.group(1)}"
        videos.append({
            "code": code,
            "title": title,
            "url": href if href.startswith("http") else urljoin("https://missav.live", href),
            "thumb": f"https://fourhoi.com/{slug}/cover-n.jpg",
        })
        if len(videos) >= MAX_ITEMS:
            break
    return videos


def render_html(items: list[dict], updated_at: str, new_count: int) -> str:
    cards = []
    for item in items:
        badge = '<span class="badge new">NEW</span>' if item.get("is_new") else ""
        thumb = item.get("thumb") or ""
        cards.append(
            f"""
            <a class="card" href="{item['url']}" target="_blank" rel="noopener">
              <div class="thumb-wrap">
                <img src="{thumb}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.display='none'">
                {badge}
              </div>
              <div class="body">
                <span class="code">{item['code']}</span>
                <p class="title">{item['title']}</p>
                <p class="meta">初回確認: {item.get('first_seen', '-')}</p>
              </div>
            </a>
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
    main {{
      padding: 12px;
      display: grid;
      gap: 10px;
    }}
    .card {{
      display: grid;
      grid-template-columns: 112px 1fr;
      gap: 12px;
      text-decoration: none;
      color: inherit;
      background: var(--card);
      border-radius: 16px;
      padding: 10px;
      overflow: hidden;
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
  </header>
  <main id="list">
    {cards_html}
  </main>
  <script>
    const q = document.getElementById('q');
    const cards = [...document.querySelectorAll('.card')];
    q.addEventListener('input', () => {{
      const keyword = q.value.trim().toLowerCase();
      cards.forEach(card => {{
        card.style.display = card.textContent.toLowerCase().includes(keyword) ? '' : 'none';
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
        send_telegram(f"監視エラー: ページ取得に失敗しました\\n{e}")
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

    merged = merged[:KEEP_ITEMS]
    save_json(DATA_FILE, {"updated_at": stamp, "items": merged})
    save_json(HISTORY_FILE, {"updated_at": stamp, "ids": sorted(known)})
    HTML_FILE.parent.mkdir(parents=True, exist_ok=True)
    HTML_FILE.write_text(render_html(merged, stamp, len(new_videos)), encoding="utf-8")

    if first_run:
        preview = "\\n".join(f"- {v['code']}" for v in latest[:8])
        send_telegram(
            "監視を開始しました。\\n"
            "今後の新着だけ通知します。\\n\\n"
            f"現在の最新:\\n{preview}"
        )
        print("初回保存完了")
        return 0

    if not new_videos:
        print("新着なし")
        return 0

    for video in reversed(new_videos):
        send_telegram(
            "【新着 FC2-PPV】\\n\\n"
            f"{video['code']}\\n"
            f"{video['title']}\\n\\n"
            f"{video['url']}"
        )
        print(f"通知: {video['code']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
