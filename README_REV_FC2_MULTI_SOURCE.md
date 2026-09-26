# FC2 Monitor multi-source revision

既存構成を極力維持した統合版。

- MissAV: 最新 + 過去12ページ/実行 + `/ja/fc2` カタログ
- JavDB: 独立した新作/過去作発見元。最新1-2ページ + 過去6ページ/実行。HTTP 200正常ページのみ採用
- Supjav: 補助。最新1-3ページのみ。403/Cloudflare等はスキップ
- 同一FC2番号: 従来どおり1件へmerge
- MissAVランキング: 未確認作品をランキングだけで新規生成しない
- FC2 Content Market: 既存作品を24件/実行ずつ巡回し、200＋番号一致時だけ平均評価・レビュー件数を補完。7日後に再確認可能
- UI: 既存カード構造を維持し、取得できた作品だけ `FC2公式 ★x.x ・ レビュー n件` を1行表示。メニューからFC2公式へ移動可能
- 403/429/503/Cloudflare challenge: 突破しない

環境変数で調整可能:
- BACKFILL_PAGES=12
- JAVDB_BACKFILL_PAGES=6
- FC2_MARKET_FETCH_LIMIT=24
- FC2_MARKET_REFRESH_SEC=604800
- JAVDB_URL=https://javdb.com/search?q=FC2&f=all

注意: JavDB側の公開検索HTML/ページング仕様が変わった場合は、その実行では0件またはスキップになります。既存データは削除しません。
