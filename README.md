# FC2-PPV Monitor

MissAV の FC2-PPV 検索ページを定期チェックし、新着を Telegram に通知します。

## 必要な Secrets

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## FC2公式の個別取得状態

新着・過去作品の補完・定期更新は、共通の取得処理を通ります。
`docs/fetch_state.json` の `metadata_backfill_v3_attempts[FC2番号]` に、結果、HTTPステータス、最終試行・成功時刻、試行回数、連続失敗回数、次回試行時刻、取得経路を記録します。

- `success`: 正常アクセス。取得できた値だけを更新し、欠けている値で既存情報を消しません。
- `failed`: 通信・HTTP・ページ判定の失敗。12時間、24時間、48時間、最大72時間の間隔で再試行します。
- `not_found`: 作品なし。通常の再試行対象から外します。
- `blocked`: eKYCへの誘導。作品固有の失敗回数を増やさず、公式メタデータ取得全体を12時間停止します。

情報が揃った成功作品は7日、まだ不足がある成功作品は12時間待ってから再取得します。旧成功・失敗記録を引き継ぐため、デプロイ時に未試行扱いへ戻りません。取得結果は25アクセスごとと各処理の終了時に保存し、保存済みの成功情報は次の実行で復元できます。

サムネイル・サンプル動画APIの取得状態は、従来どおり別管理です。サンプル取得成功をタイトル・評価の取得成功として扱いません。

## 画面の編集とビルド

画面のソースは `web/index.html`、`web/site.css`、`web/site.js` です。PythonへHTMLを埋め込まず、表示用の軽量カタログを別ファイルとして生成します。カードは近くの行だけを描画し、スクロールに合わせて追加します。プレビュー動画は操作時だけ読み込みます。

```sh
pip install -r requirements.txt
python fc2_monitor.py --build-site
python -m http.server 8000 --directory docs
```

`--build-site` は保存済みデータで画面だけを再生成します。外部サイトへの取得や通知は行いません。`main`への画面・コード更新ではこのビルドを即時公開し、通常の取得は2時間ごとのスケジュールと手動実行で継続します。自動更新時も同じ画面ソースを使うため、次回取得でUIが旧版に戻ることはありません。

従来の保存済み・後で見る・履歴のブラウザ内データをそのまま使用します。人気ランキングは期間別ランキング、FC2評価、再生数を明示して切り替えます。急上昇は増加データがある作品だけを表示します。

## 回帰テスト

```sh
python -m unittest discover -s tests -v
npm install --no-save playwright@1.51.1
npx playwright install chromium
python tests/build_fixture.py test-results/fixture
node tests/browser.cjs
```

ブラウザテストは7,064件の中立なテストデータを使い、外部作品サイトやメディア配信へアクセスしません。PC・スマホのナビゲーション、検索、保存・履歴、ランキング、絞り込み、長距離スクロール、通信失敗からの復帰を確認します。
