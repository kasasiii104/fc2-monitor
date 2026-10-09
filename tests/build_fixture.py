"""Build a neutral 7,064-item catalog for browser regression tests."""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fc2_monitor as m

root = Path(sys.argv[1])
m.HTML_FILE = root / 'index.html'
now = datetime.now(m.JST)
items = []
for i in range(7064):
    num = str(8000000 + i)
    title = ['街を歩く日々の記録', '海辺の休日と小さな旅', '光の差す部屋から', 'ゆっくり過ごす午後', '旅先で見つけた風景', '週末のサンプル映像'][i % 6]
    item = dict(code=f'FC2-PPV-{num}', code_num=num, title=f'{title}｜サンプル {i:04}',
                first_seen=(now-timedelta(minutes=i*7)).strftime('%Y-%m-%d %H:%M:%S'),
                url=f'https://example.test/watch/{num}', thumb=f'https://images.example.test/{i%6}.svg',
                preview=f'https://media.example.test/{num}.mp4', duration=f'{20+i%120}:00',
                is_new=i<12, sources={'MissAV':f'https://example.test/watch/{num}', 'JavDB':f'https://example.test/catalog/{num}'},
                fc2_market_url=f'https://example.test/official/{num}',
                fc2_rating=round(3.0+(i%21)/10,1), fc2_review_count=i%73,
                fc2_rating_source='FC2公式' if i%2==0 else 'FC2ウォーカー',
                views=i*101 if i%3==0 else None,
                missav_rank_day=50-i if i<50 else None,
                missav_rank_week=80-i if i<80 else None,
                missav_rank_month=100-i if i<100 else None,
                missav_rank_total=120-i if i<120 else None,
                trend_6h=i if i<20 else None, trend_24h=i*2 if i<40 else None)
    if i==4: item['title'] = '<img src=x onerror="window.injected=true"> & サンプル'
    items.append(item)
# Preserve the production fallback shape, but keep every video URL synthetic.
# Browser tests must never request a real provider, even after primary failure.
original_sources = m.preview_source_urls
def neutral_sources(item, additional=()):
    return [url.replace('https://fourhoi.com/', 'https://media.example.test/')
            for url in original_sources(item, additional)]

stamp = now.strftime('%Y-%m-%d %H:%M:%S')
previous_stamp = (now - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
retry_at = int(now.timestamp()) + 3600
update_status = {
    'version': 1,
    'discovery': {'checked_at': stamp, 'recent_added': 0, 'archive_added': 11, 'next_archive_page': 357},
    'sources': {'missav': {'status': 'ok'}, 'supjav': {'status': 'access_limited', 'next_retry_at': retry_at}},
    'rankings': {period: {'status': 'fetch_failed' if period == 'week' else 'ok',
                         'checked_at': stamp, 'last_success_at': previous_stamp if period == 'week' else stamp,
                         'count': count, 'listed_count': count}
                 for period, count in [('day', 50), ('week', 80), ('month', 100), ('total', 120)]},
    'official': {'status': 'access_limited', 'reason': 'eKYC', 'pending': 100, 'next_retry_at': retry_at},
    'views': {'status': 'access_limited', 'tried': 0, 'updated': 0, 'known_views': 2355,
              'pending': 4709, 'never_checked': 4709, 'next_retry_at': retry_at},
    'fallback': {'source': 'FC2ウォーカー', 'rating_updated': 10},
}
with patch.object(m, 'preview_source_urls', side_effect=neutral_sources):
    m.write_site(items, stamp, 12, update_status)
print(root.resolve())
