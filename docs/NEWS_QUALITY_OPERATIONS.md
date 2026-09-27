# ニュース品質・企業行動レビュー・翌営業日レポート（2026-09-27）

この機能は研究用です。ニュースの好悪材料、チャートの予測リターン、売買判断は別の項目です。ニュースで見つけた企業は分析候補に保存するだけで、監視対象や仮想売買の対象へ自動追加しません。実注文はありません。

## 何を改善したか

保存済み記事を、AIに送る前にローカルで `company_direct`（企業固有）、`industry`（業界）、`macro`（市場全体）、`noise_or_reference`（参照・ノイズ）へ分類します。監視外でも、ローカル企業DBの十分に長い正式名称・別名が見出しにある場合だけ企業候補として認識します。全社をLLMへ送る方式ではありません。

株価情報、企業情報、基準価額、掲示板、検索/ランキング等の静的ページは、決算・提携等のイベント語がないときに除外します。公表日時不明/期限超過、低い企業関連性、重複イベントも理由を残して除外します。企業名の偶然一致（例: 日立市）と、子会社名だけによる親会社への材料化を避けます。

重複は、本文が同一なら同一日内でまとめます。見出しだけでは、二社名と提携表現が完全に対応する限定例だけをまとめます。中止・解消・検討・噂・予定・再開・拡大はまとめません。重複記事があっても材料の強さを加算しません。

キューは新鮮な企業固有、業界、確認済みの間接的なマクロ、その他の順に優先します。既存キャッシュは送信せず、除外理由と履歴は保持します。リスク制御の既存安全停止は緩和していません。

## 読み取り中心の確認

プロジェクト直下の PowerShell で実行します。これらはAPI送信・注文を行いません。

```bat
.\.venv\Scripts\python.exe kabu_system.py run-status --config config/paper.json
.\.venv\Scripts\python.exe news_discovery.py summary --config config/paper.json --limit 10
.\.venv\Scripts\python.exe news_quality.py status --config config/paper.json --limit 10
.\.venv\Scripts\python.exe news_discovery.py api-preview --config config/paper.json --limit 3
.\.venv\Scripts\python.exe corporate_review.py status --config config/paper.json --observations-db data/action_review/observations.sqlite3
.\.venv\Scripts\python.exe kabu_system.py evening-report --config config/paper.json --output data/report_quality_check_YYYYMMDD
```

`evening-report` は未使用の出力フォルダーを指定してください。HTMLは短い運用ビューで、`report.json` には全記事・除外理由・候補・入力・判断を残します。`NO_TRADE` 相当の表示は、価格不足/日足未確定、ニュース待ち、企業行動未確認、市場時間外、安全条件、リスク制限、買い候補/売りシグナルなしを分けます。表示理由は既存MA、LightGBM、仮想売買、リスク条件を変更しません。

## 企業行動レビュー

`corporate_review.py status` はYahoo観測、確認済み台帳、JPX/TDnet/IRの未確認状態を分けて出します。Yahooで候補が0件でも `REVIEW_REQUIRED` のままです。自動で `none`、効力日、訂正完了を登録しません。

JPXの[権利落等一覧](https://www.jpx.co.jp/listing/others/ex-rights/)、[TDnetの有料API案内](https://www.jpx.co.jp/markets/paid-info-listing/tdnet/)、[J-Quants Proの分割データ仕様](https://jpx.gitbook.io/j-quants-pro-ja/api-reference/corporate_action/stock_split)はいずれも今回の「全企業行動が存在しない」保証には不足します。公式IRを含め、人が根拠、訂正、効力日、権利落日を確認して既存の確認済み台帳へ登録する必要があります。

## 新鮮な1記事だけの将来の有料検証

今は実行しません。まず品質一覧で新鮮な `company_direct` または十分な確認済み関係を持つ `industry` の記事IDを選びます。次は**プレビューだけ**です。

```bat
.\.venv\Scripts\python.exe news_trial.py preview --config config/paper.json --article-id ARTICLE_ID --cache data/news_trial/ARTICLE_ID.sqlite3 --max-calls 1
```

実送信は、別途利用者が明示的に許可し、`dry_run=false`、通信有効、正しいモデル単価・日次/月次予算、`OPENAI_API_KEY` を安全に設定してからです。継続運転用の設定をこの1件試験に転用せず、試験用の設定/キャッシュを用い、既存の予算台帳は共有して上限を回避しないでください。実行コマンドはその許可時にのみ案内します。

## 性能の確認記録

隔離したDBで同一入力を比較した結果、旧レポートは約241秒、株価DB読込3,719回、HTML約22MBでした。共有スナップショットと予測メモ化後は約29秒、読込2回、HTML約34KBでした。銘柄コードで突合した終値、MA、既存のMA/チャート判断、発見候補判断は一致しました。計測値はPC、DBサイズ、グラフ生成により変動します。

同じ隔離キューへ品質分類を適用した時点では pending は115件から9件になりました。これは稼働中DBへ書き込まずに測定した値です。`news_quality.py status` のRSS全件集計と、ランタイムの重要語キュー件数は母集団が異なります。
