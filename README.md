# kabuステーション 株価収集基盤

## ニュース・仮想売買の継続運転

`python kabu_system.py run --config config/paper.json` を追加しました。初期状態は **DRY_RUN・ネットワーク無効**。設定変更後はRSS定期収集、公式資料取得、予算付き好悪材料解析、価格取得、統合判断、仮想口座更新を1プロセスで実行できます。

```bat
python -m pip install -r requirements-operations.txt
python kabu_system.py run --config config/paper.json --once
python kabu_system.py run-status --config config/paper.json
```

**実市場の企業行動確認フィードは未接続です。** 確認済みデータがあれば翌日へ持越し、未確認なら停止します。導入、月額500円目安の予算、API有効化、企業行動登録、停止・再開は [継続運転ガイド](docs/CONTINUOUS_OPERATIONS.md) を参照してください。既存のLightGBMとMA戦略は維持しています。

## 統合研究CLI（2026-09追加）

既存コマンドを残したまま、`kabu_system.py` に日足LightGBM・ニュース併用判断・共有資金のバックテスト・保存済みtickによる仮想口座を追加しました。実注文機能はありません。企業行動の照合は確認済みデータの手動登録に対応し、自動取得は未接続です。詳細な実装範囲・制限・評価結果は [開発ロードマップ](docs/DEVELOPMENT_ROADMAP.md) を参照してください。

以下はプロジェクトフォルダで `.venv` を有効にしたWindowsのコマンドです。研究用コマンドは外部APIを呼びません。取得CLIの単独利用も従来どおり可能です。新しい `run` のネットワーク動作は上記ガイドの設定で制御します。

```bat
python -m pip install -r requirements-ai.txt
python kabu_system.py normalize --prices-db data/yahoo.sqlite3 --output data/normalized.sqlite3
python kabu_system.py train --prices-db data/yahoo.sqlite3 --output data/chart_model
python kabu_system.py evaluate --prices-db data/chart_model/normalized.sqlite3 --model data/chart_model
python kabu_system.py compare --prices-db data/chart_model/normalized.sqlite3 --model data/chart_model --news-db data/news_ai.sqlite3
python kabu_system.py predict --prices-db data/yahoo.sqlite3 --model data/chart_model
```

`train` は新規のモデルフォルダを指定します。既に存在する場合は上書きせず停止するので別名にしてください。既定は `symbols_10.json` の10銘柄、予測対象5営業日。`--symbols 7203 8306`、`--horizon 10` で変更できます。予測値は小数の騰落率（0.01=1%）で、上昇確率や売買利益ではありません。学習・検証・テストを時系列で分割し、将来ラベルの期間重複を除外します。

評価/比較は学習時と同じ銘柄・スナップショットを使います。学習時に `--symbols` を指定した場合、評価時にも同じ指定が必要です。株価DBを更新しても、モデルフォルダの `normalized.sqlite3` から再現できます。新しいデータでの予測は `predict` で別に実行します。

`compare` はMA基準、AチャートAI、Bニュースイベント、C併用を同じ初期資金・期間・コストで計算し、JSON・比較PNG・銘柄別売買PNGを `data/system_日時/` に保存します。Bの予測対象はイベント時点だけです。Aの対象日はニュース有無で減らしません。ニュース解析の完了前には材料を使えないため、最近取得したニュースで過去数年を埋めることはできません。

共有口座の既定は合計1,000万円、1回100株、1銘柄100万円・総投資500万円までです。従来の「100万円×10個の独立口座」とは違います。変更例:

```bat
python kabu_system.py compare --prices-db data/chart_model/normalized.sqlite3 --model data/chart_model --cash 10000000 --per-symbol-limit 1000000 --total-limit 5000000 --max-holdings 10 --daily-loss-limit 100000 --daily-order-limit 20 --quantity 100 --fee 0 --slippage-bps 5 --spread-bps 0
```

ニュース併用の学習:

```bat
python kabu_system.py train --kind combined --news-db data/news_ai.sqlite3 --output data/combined_model
```

学習に使える独立イベントが足りないと `SKIPPED` と理由を返し、モデルは作りません。初期条件は100件以上ですが、十分な学習品質を保証する数字ではありません。確認済みの収集稼働期間がある場合のみ `--coverage-start` / `--coverage-end` にタイムゾーン付きISO日時を渡せます。未指定では「ニュースが存在しない」と断定せず、情報不足として併用ルールの新規買いを抑制します。Cを学習済み結合モデルへ切り替える際は `compare --combined-model data/combined_model` を追加します（Aと同じ日付境界が必要）。

前向き仮想口座（別ターミナルでkabu価格収集を動かしてから、まず1銘柄で確認）:

```bat
python kabu_collector.py watch --symbols 7203
```

別ターミナルで、最新の完了日足を保存した後に実行:

```bat
python kabu_system.py paper-step --symbols 7203 --ticks-db data/production.sqlite3 --prices-db data/yahoo.sqlite3 --ledger data/system_paper.sqlite3
python kabu_system.py paper-report --ledger data/system_paper.sqlite3
```

`--model` 未指定のpaperは従来の5/20クロスです。モデルを使う場合は `--model data/chart_model` を追加し、新しい台帳を指定してください。1回目で候補を保存し、その後の `paper-step` で判断後の新しい価格があれば仮想約定します。この単発コマンドは自動常駐ではありません。市場時刻・受信時刻が60秒超古い場合、日足が直前営業日まで揃っていない場合、通信切断検知時は停止。翌日への持越しには確認済み企業行動DBを `--actions-db` で指定します。未指定で保有を持ち越すと停止します。稼働時間外の古い価格での実行は成功扱いにしません。

手動で新規買いだけを停止（Windows CMD）:

```bat
type nul > data\STOP_NEW_TRADES
```

再開時はこの停止用ファイルを手動で削除します。既存のデータベースを消さないでください。売却候補の処理は停止ファイルの対象外です。

ニュースAPI費用の上限を指定する場合は `budget.example.json` を `data/news_budget.json` へコピーし、`daily_usd` と `rates_per_million` に利用モデルの最新の入力/出力単価（USD/100万tokens）を設定して、既存の `news_ai.py run ... --budget-file data/news_budget.json` に渡します。初期設定は予算0・単価未設定で送信できません。実装テストでOpenAIは呼んでいません。

旧CLI互換のため予算指定は任意です。未指定の呼出しには日予算上限がありません。共通設定ファイル/台帳を使うと別キャッシュでもUTC日ごとに送信前見積りを予約し、入力/出力tokensと推定費用を記録します。不明な使用量は予約を保持します。請求額そのものではなく、キャッシュ割引は計算しない保守的見積りです。`preview` はAPI・予算台帳を変更しません。

オフラインテスト:

```bat
python -m unittest discover -s tests -q
```

今回の確認済み成果物: `data/system_chart_model_v1`、`data/system_comparison_v1`（ローカルのみ、Git対象外）。これは機能確認の初回研究結果です。モデルの有効性・運用可能性を確認したものではありません。

Python 3.11以降向け。注文機能はありません。API認証、東証の時価・板情報取得、WebSocket配信の保存に対応します。元の証券会社サンプルは変更していません。

## 最初のセットアップ（PowerShell）

```powershell
cd 'C:\Users\nanan\OneDrive\ドキュメント\ChatGPT\株自動取引'
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 1. 接続確認・1回取得

kabuステーションを起動・ログインし、APIを有効にしてください。

```powershell
.\.venv\Scripts\python.exe kabu_collector.py quote --symbols 7203
```

APIパスワードを求められたら、このPC上で入力してEnterを押します。入力文字は表示されません。パスワード・トークンはファイルへ保存しません。トークンは起動時に1回発行します。別ツールの既存トークンが無効になる点に注意してください。

認証成功の後、銘柄名・現在値・価格時刻・出来高を表示し、SQLiteへ保存します。市場が閉じている場合は前回の価格やnullが返ることがあります。価格時刻を必ず確認してください。

## 2. リアルタイム記録

```powershell
.\.venv\Scripts\python.exe kabu_collector.py watch --symbols 7203
```

複数銘柄の例（銘柄の推奨ではありません）:

```powershell
.\.venv\Scripts\python.exe kabu_collector.py watch --symbols 7203 8306
```

Ctrl+Cで終了します。最初に現在値を取得し、続いて銘柄登録・配信接続を行います。この間の更新を含め、全約定の完全な記録は保証しません。PUSH配信は更新時に届き、昼休み・引け後などは届きません。無配信だけでは休場と通信停滞を区別できません。

既定は本番の**価格情報取得**（18080）です。検証環境を使う場合は `--environment verification` を追加します（18081）。検証環境での応答確認は戦略の収益性を試す仮想売買とは別です。

登録上限は他ツール分も含め50銘柄です。本プログラムは既存登録を全解除せず、終了時にも登録を残します。再接続時は対象銘柄を再登録します。

## 保存内容

既定の保存先はこのフォルダ内の `data/production.sqlite3`。検証環境は `data/verification.sqlite3` へ分けます。`--db` で任意の保存先も指定できます。1プロセスでの利用を想定します。

`events` テーブル:

|列|内容|
|---|---|
|id|記録順の連番|
|received_at|PCでの受信・記録時刻（UTC）|
|environment|production / verification|
|source|snapshot / push / connected / disconnected / stopped|
|symbol|銘柄コード（接続イベントは空）|
|payload|APIのJSON。価格時刻、累積出来高、板情報などを保持|

1件ごとにコミットします。データは追記されます。出来高は累積値なので、そのまま足し合わせないでください。Bid/Askの意味は一般的な命名から推測せず公式仕様に従ってください。

OneDrive配下で稼働中のDBを他PCから同時使用しないでください。コピー・バックアップは記録停止後に行ってください。本格運用では同期対象外の保存先が適しています。

## エラー・切断

- REST通信は10秒でタイムアウトします。認証・登録エラーでは停止します。
- WebSocketの切断は待ち時間を増やしながら最大5回再試行します。接続が60秒以上維持された場合は試行回数をリセットします。
- 切断を検知した時刻を保存します。切断中の配信データは補完しません。接続ログから完全な欠損範囲を特定できるわけではありません。
- 保存失敗・不正なJSONでは停止します。PCのスリープやkabuステーションのログアウト中は収集できません。
- kabuステーションの再起動・ログアウト・他ツールのトークン再発行後は、本プログラムも再起動してください。

## 次の開発

### 10銘柄 × 各100万円で検証する

`symbols_10.json` の `symbols` に対象コードを設定します。初期値は比較用の10銘柄例です。銘柄選択の偏りがあり、市場全体を代表するものでも投資推奨でもありません。件数は変更できます。4文字と5文字で同じコードを重複指定すると停止します。

1. 一括取得（APIキー入力は1回、銘柄間は15秒待ちます。通常数分程度）:

```powershell
.\.venv\Scripts\python.exe multi_backtest.py fetch
```

2. 取得成功が10/10になったら、一括検証:

```powershell
.\.venv\Scripts\python.exe multi_backtest.py run
```

資金は**1銘柄100万円、10銘柄で1,000万円**です。各銘柄は独立し、別銘柄の利益を使いません。従来どおり100株ずつの売買なので100万円全額を投資する方式ではありません。`--cash` は1銘柄当たりの資金、`--quantity` は各銘柄共通の1回の株数です。5日/20日移動平均、手数料0円、片道5bpsの初期条件も従来と同じです。

日付範囲は各銘柄の共通期間へそろえます。`--from YYYY-MM-DD --to YYYY-MM-DD` でさらに限定できます。株式分割・併合に対応しているため、通常は期間短縮は不要です。明示的に最後の調整後の期間だけを検証する場合は次を使えます:

```powershell
.\.venv\Scripts\python.exe multi_backtest.py run --after-actions
```

このオプションは分割処理の実装ではありません。共通期間内の全銘柄で最後の `AdjFactor != 1` の日より後へ、全銘柄の開始日を移動します。元の開始日・実際の検証期間をレポートに残します。欠損・非取引日の問題を回避するものではありません。短縮後にデータ不足なら停止します。切り取った期間の結果を元の2年分の結果として扱わないでください。

出力先 `data/multi_run_日時/`:

- `report.md`: 銘柄別の損益・収益率・最大下落率・売買件数・保有継続との比較、失敗理由。
- `summary.png`: 銘柄別損益、各資産推移、合計資産と保有継続との比較。
- `summary.json`: 全体の数値と日次資産推移。
- `backtest_銘柄コード.json`: 個別の詳細。`plot_backtest.py --result ファイルパス` でも可視化できます。

取得失敗時は既存DBのその銘柄のデータを保持します。取得結果 `data/multi_fetch_日時/fetch_status.json` を確認し、失敗したまま古いデータで検証しないでください。`run` はネット接続せずDBのみを使うため、取得が最新かどうかは判定しません。

未取得・検証失敗銘柄はレポートに理由を出し、残りは個別検証します。ただし、**全銘柄成功かつ日付行が完全一致しない限り、合計結果は出しません**。欠けた銘柄を現金100万円として扱ったり、欠けた日の価格を補ったりはしません。保有継続の初日購入が資金不足なら、その比較のみ不可と表示します。保有継続比較の条件は単一銘柄版と共通です。

### グラフで売買と資産を確認

```powershell
.\.venv\Scripts\python.exe plot_backtest.py --result data/backtest_20260919T062722117907.json
```

`--result` に可視化するバックテストJSONを指定します。株価と売買位置、資産比較、最高値からの下落率、往復取引ごとの損益をPNGとPDFに出力します。`data/charts_日時/` 内の `report.md` と `comparison.json` に比較結果も保存します。既存出力フォルダを上書きしません。

比較対象は期間初日の始値で同じ株数を購入し、残金を現金で保持します。購入コスト条件は戦略と共通。最終日の売却は行わず終値評価します。移動平均ルールの助走期間と保有継続の投資開始日は異なります。配当・税金なし、全量約定を仮定した比較です。DBのデータが取得し直し等で変わった場合はハッシュ不一致で停止するのでバックテストも再実行してください。

### J-Quantsの日足取得と単純な売買シミュレーション

新しいライブラリのインストールやkabuステーションの起動は不要です。`jquants_history.py` を実行し、**J-QuantsのAPIキー**をPC上で入力してください（kabuステーションのAPIパスワードとは別です）。キーの表示・保存は行いません。`api.txt` などのファイルも自動では読みません。

```powershell
.\.venv\Scripts\python.exe jquants_history.py --symbol 7203
```

取得可能な全期間をページの続きも含めて取得し、`data/historical.sqlite3` に保存します。無料プランの遅延や取得期間はサービスの権限に従います。成功時に実際の取得期間と日数を表示します。特定の年数や直近までの取得は保証しません。APIがHTTP 403を返す場合は契約・APIキー・取得権限を確認してください。429なら時間を置いて再実行してください。ページ間は15秒待機します。

保存には `daily_prices` テーブルを使い、元のAPI応答（調整前後の価格・調整係数等）、取得日時、取得元を保存します。全ページ取得後、この銘柄の以前のデータを今回の取得範囲に置換します。取得に失敗した場合は既存データを変更しません。銘柄・日付の重複は防ぎます。取引所カレンダーとの照合は未実装です。

取得に成功したら、以下で仮想売買できます。

```powershell
.\.venv\Scripts\python.exe daily_backtest.py --symbol 7203
```

初期設定:

- 仮想資金100万円、1回100株、単一銘柄の現物買いのみ。これは計算例であり実運用金額の推奨ではありません。
- 終値の5日移動平均が20日移動平均を下から上へ交差したら、翌データ日の始値で買います。上から下へ交差したら翌データ日の始値で売ります。
- 最初に両移動平均が計算可能になった日には売買せず、交差を待ちます。保有は最大100株、追加購入なし。資金不足時の買いは見送り、次の交差まで再試行しません。
- 片道手数料0円は仮の初期値です。`--fee 100` などで1注文当たりの固定手数料を設定できます。
- 売買それぞれで5bps（0.05%）不利な価格にずらします。`--slippage-bps` で変更可能です。
- 最終日に残った保有は終値で評価し、強制売却しません。最終日の売買シグナルは翌日データがないため執行しません。
- 日次終値での資産推移・最大下落率、確定/含み損益、売買履歴を日時付きJSONに保存します。入力データのハッシュとパラメータも記録します。
- 約定・資産評価には未調整価格を使います。`AdjFactor` と `ExRT` で分割・併合を確認し、権利落ち日の始値約定前に保有株数を係数で割ります。現金と総取得原価は変えません。初日の購入には初日の分割を重ねて適用しません。
- 移動平均は、その日までに判明した係数で過去の終値を当日の株価単位へ補正します。将来の調整済み株価を売買判定には使いません。新規購入は指定株数（既定100株）、売却は分割後の保有全株です。保有継続比較も分割に応じて株数を変更します。
- 単一銘柄グラフの株価・移動平均・売買マーカーは、表示専用に期間最終日の株価単位へ換算します。JSONの約定価格・株数は当時の値です。`corporate_actions` に日付・係数・調整前後の保有株数を記録します。
- 調整係数の欠損、種類不明の調整、権利割当、保有株に端株・単元未満株が生じる分割・併合、価格欠損・無約定日は停止します。配当、端株清算、権利行使は再現しません。

調整係数の定義: https://jpx-jquants.com/en/spec/eq-bars-daily
- `--from 2025-01-01 --to 2025-12-31` のように検証期間を限定できます。移動平均の助走期間もこの範囲内に含まれます。
- 配当・税金、売買停止、値幅制限、流動性・呼値による約定不能は再現しません。実際の運用成績の保証ではありません。

データ仕様: https://jpx-jquants.com/spec/eq-bars-daily

この取得処理の実接続は、ユーザーがAPIキーを入力して行います。開発テストは模擬API応答と架空の日足による検証です。

### 夜間に進めるオフライン分析

追加した `kabu_analysis.py` はAPIに接続しません。パスワードやkabuステーションの起動は不要です。

模擬データを初めて作成する場合（既存ファイルには上書き・追記しません）:

```powershell
.\.venv\Scripts\python.exe kabu_analysis.py demo
```

`data/demo.sqlite3` に架空の2日分、各10分間・5秒間隔、計240件を作ります。environmentも `demo` とし、本番データとは分離します。同じコマンドの再実行でFileExistsErrorが出るのは既存データ保護のためです。別ファイルなら `--db data/demo2.sqlite3` を指定できます。

収集状況と観測1分足を確認:

```powershell
.\.venv\Scripts\python.exe kabu_analysis.py status --environment demo
.\.venv\Scripts\python.exe kabu_analysis.py bars --environment demo
```

模擬データは240件・観測1分足20件になります。画面には直近10本を表示します。全件をJSONで保存するには `--output data/demo_bars.json` を追加します。既存出力は上書きしないため、再出力時は別名を指定してください。

本番データにも同じ処理を使えます（DBは読み取り専用で開きます）:

```powershell
.\.venv\Scripts\python.exe kabu_analysis.py status
.\.venv\Scripts\python.exe kabu_analysis.py bars --output data/production_bars.json
```

#### 集計の意味と制約

- 記録件数、受信期間、価格時刻の範囲、価格・出来高・時刻の不正/欠損を表示します。空DBは0件になります。DB不存在はエラーです。
- `snapshot` は診断対象ですが、古い単発取得値が混入しないよう足には使いません。`push` のCurrentPriceTimeを日本時間の分単位に区切り、観測価格のOHLC（始値・高値・安値・終値）を作ります。
- 時刻が逆行する価格は集計から除外し、同じ銘柄の直前と価格・価格時刻・出来高・出来高時刻が一致する更新は重複として除外します。板だけの更新もこの重複件数に含まれます。
- `observed_volume` は、同じ分内で時刻が順行する累積出来高の増分だけです。最初の累積値をその分の出来高にしません。分の境界をまたぐ増分、日替わり、接続境界、出来高の減少は加算しません。**正確な1分間出来高ではなく、確認できた部分の量です。0でも売買がなかったとは限りません。**
- JSONの `flags` に `no_volume_baseline`（基準なし）、`volume_crosses_minute`（分境界の差分不明）、`volume_reset`（累積値減少）などを記録します。
- PUSHは全約定データではないため、すべての足に `sampled_prices_not_official_ohlcv` を付けます。正式な取引所1分足との一致を保証しません。未受信の分は埋めず、休日・昼休み・通信欠損も自動判別しません。最後の足や収集開始・終了の足は一部だけの可能性があります。
- この集計はバッチ処理であり、生成時点で判明した情報を含みます。将来バックテストを実装するときは受信時刻と判断可能時刻も考慮し、未来の情報が混ざらない設計が必要です。

模擬データによる確認は実配信の検証・予測精度・収益性の証明ではありません。

まず取引時間中に1銘柄で取得・保存を確認します。その後、データ品質確認、1分足集計、過去データでの戦略検証、AI予測、仮想売買の順に追加します。現在のプログラムにはAI・仮想売買・発注はありません。

## オフラインテスト

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

公式仕様: https://kabucom.github.io/kabusapi/reference/index.html

公式PUSH配信: https://kabucom.github.io/kabusapi/ptal/push.html
# ニュース収集（Windows / Ubuntu共通）

## ニュースとチャートを結び付ける（API通信・注文なし）

```powershell
.\.venv\Scripts\python.exe news_market.py
# 判断時点ですでに取得済みの日足だけに限定する場合
.\.venv\Scripts\python.exe news_market.py --mode recorded
```

既定はYahooの `data/yahoo.sqlite3` と保存済みAI結果を使います。J-Quantsへ切り替える場合は `--prices-db data/historical.sqlite3`。出力は `data/news_market_日時/report.md` と `context.json`。元DBは変更しません。

既定の `replay` は後日取得した価格も用いる過去データ再構成であり、当時リアルタイムで実行した記録ではありません。AIの成功結果を記事ごとに最新1件選択。判断時刻はAI分析完了・初回取得・見出し観測の最も遅い時刻とし、日本時間の当日足と未来足を除外します。`recorded` では株価のDB取得日時も判断時刻以前に限定し、不足なら見送ります。日足DBは取得時に置換されるため、過去時点の取得履歴を完全に復元するものではありません。

終値、5/20本移動平均、1/5本騰落率、直前20本平均に対する出来高比を記録します。窓内の分割・併合は価格と出来高を最新単位に補正します。価格日・使用した21本・取得時刻・入力ハッシュ・AI結果をJSONに残します。

確認候補の仮条件は「AI形式v2で直接関連、公開日時あり・未来でなく7日以内、日足21本以上・最新足が7暦日以内、終値とMA5がMA20より上、出来高比1以上」です。これは本文を人が確認する優先候補を抽出する便宜的条件で、売買シグナルでも有効性を検証済みの戦略でもありません。満たさない項目は見送り理由として保存します。取引日カレンダー照合、本文検証、株価予測、仮想注文の執行はまだ行いません。

## Yahoo日足で直近期間を試す（J-Quantsは維持）

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-yahoo.txt
.\.venv\Scripts\python.exe yahoo_history.py
.\.venv\Scripts\python.exe multi_backtest.py run --db data/yahoo.sqlite3
```

1銘柄のみなら `yahoo_history.py --symbol 7203`。`--from 2026-07-01 --to 2026-09-18` で期間指定できます。既定は2024-06-27から前日まで。日本時間の当日は未確定の可能性があるため常に除外します。移動平均の計算には9月以前の助走期間も必要です。

J-Quantsの取得コマンド・既定DB `historical.sqlite3` は維持します。Yahooは `yahoo.sqlite3` に保存し、sourceも別に記録します。既存シミュレーターは `--db` 指定先の取得元を判定します。取得元混在DBは拒否します。Yahooの取得失敗時にJ-Quantsへ自動切替はしません。

yfinanceはYahoo公式の契約APIではなく、個人の研究・検証用です。利用条件は提供元で確認してください。欠損・取得制限・遅延があり得ます。YahooのOHLCは `auto_adjust=False` でも株式分割調整済みなので、取得時点までの分割履歴を使って過去の価格と出来高を復元します。`Adj Close` は使わず、配当調整・配当損益は含めません。復元値は丸めや分割履歴の欠落に左右され、取引所の当時の原値と一致する保証はありません。J-Quants移行時は重なる期間で比較してください。

銘柄ごと今回の取得範囲で置換します。短い期間を指定するとその銘柄の以前の広い範囲は残りません。空データ・欠損は保存せず既存値を維持します。`yfinance_cache` はライブラリ用キャッシュです。元ライブラリ: https://github.com/ranaroussi/yfinance

## OpenAI APIで少量の見出しを分析

追加ライブラリ不要。既定は動作確認向けの `gpt-5-nano`、確認候補を最新観測順に最大3件です。ChatGPT月額とは別にAPIの課金設定とキーが必要です。

分析形式 `headline-v2` では、見出しに記載された事実、各候補銘柄との関係（直接・間接・不明・無関係）、根拠、本文で確認する項目を分離します。確認項目の空欄や配信元名だけの根拠は拒否します。企業関係はAIの推定であり、検査が意味の正しさを保証するものではありません。過去のv1結果も一覧表示できます。今回のモデル・形式変更後は同じ記事も新規分析として課金されます。

```powershell
# 送信対象を確認（通信・課金・APIキー不要）
.\.venv\Scripts\python.exe news_ai.py preview --limit 3
# まず1件だけ送信。APIキーは非表示入力、またはOPENAI_API_KEY環境変数
.\.venv\Scripts\python.exe news_ai.py run --limit 1
# 保存済み結果を読む（API通信なし）
.\.venv\Scripts\python.exe news_ai.py list
```

`--limit 3` で3件、`--symbol 7203` で銘柄を絞れます。1回上限20件。比較用に `--model gpt-5-mini` に変更可能です（モデル変更は別の分析として課金されます）。キーはチャットやファイルに貼らず、入力を求められたときに端末へ入力してください。`api.txt` や `.env` は読みません。

- OpenAIへ送るのは見出しと候補銘柄名・コード、固定の分析指示です。本文・Web検索・実注文は扱いません。初回は見出しの分類・根拠抽出の試験です。指示、タイトル内の攻撃的な命令、企業名の誤認なども含め結果を人が確認してください。
- 結果・入力・入力ハッシュ・モデル・分析開始終了時刻・トークン使用量は `data/news_ai.sqlite3` に保存。`list` で直近20件を表示します。買い推奨や上昇確率は生成しません。根拠の引用が見出し内にあるかとコードの範囲を検査しますが、内容の真偽や要約全体の正確性を保証するものではありません。
- 同じ記事・入力・モデル・指示は再送しません。`run --limit 1` の後の `run --limit 3` は、既存1件を再利用して残りを分析します。候補は実行時のDBから選ぶため、新規記事があれば対象が変わります。
- 出力上限は推論込み2,000トークン。失敗・拒否・不正応答・途中終了も記録し、残りの送信を止めます。通信エラーや中断では課金済みの可能性があるため、同じ依頼の自動再送はしません。`failed` / `started` が残ったら `list` で確認してください。上限不足でも勝手に増額・再試行しません。
- `store:false` でAPI側のレスポンス保存を無効にしています。OpenAI側での全てのログ保持がなくなるという意味ではありません。費用はAPI管理画面が正で、保存したusageは見積もりの材料です。APIキーは保存・表示しません。

公式仕様: https://developers.openai.com/api/docs/guides/structured-outputs

## 取得後の分類・候補確認（無料、AI未使用）

```powershell
.\.venv\Scripts\python.exe news_triage.py
.\.venv\Scripts\python.exe news_triage.py --symbol 7203
```

保存済みニュースの最新観測見出しをキーワードで分類し、`data/news_triage_日時/report.html` と `triage.json` を作成します。HTMLはブラウザで開き、表示切替で「確認候補」「要確認」「保留」を選べます。元ニュースDBは変更しません。出力先は毎回新規作成します。

材料キーワードと検索銘柄名の文字一致が両方ある記事を「確認候補」、キーワードだけある記事を「要確認」、キーワードがない記事を「保留」とします。根拠語、関連候補銘柄、公開・初回取得・見出し観測時刻を表示します。全件を残すため、除外による見逃しも確認できます。AIによる判断・重要度の確率・好悪材料・売買シグナルではありません。略称・子会社・否定表現などは誤判定し得ます。本文確認が必要です。

JSONには分類ルール・銘柄設定・版番号・入力ハッシュ・分析日時を保存します。分類した時刻は今であり、過去の公開時刻に分類・売買できたことを示しません。同一事件の別記事は未統合です。新しいニュースは先に `news_collector.py fetch` で取得してください。OpenAI API接続と自動定期実行は未実装です。

`news_collector.py` はGoogleニュース検索RSSの見出し・URL・配信元を取得し、`data/news.sqlite3` に保存します。追加ライブラリ・APIキーは不要です。本文取得、AI分析、注文は行いません。

```powershell
.\.venv\Scripts\python.exe news_collector.py fetch
.\.venv\Scripts\python.exe news_collector.py list --limit 20
.\.venv\Scripts\python.exe news_collector.py list --symbol 7203
.\.venv\Scripts\python.exe news_collector.py status
```

Ubuntuでは仮想環境を有効にし、先頭を `python` に置き換えます。取得は手動で1回実行して終了します。自動スケジュールは設定していません。1銘柄のみ試す場合は `fetch --symbol 7203`。検索名は `news_sources.json` で変更できます。

- 直近7日指定の検索結果を取得します。期間内の全記事取得・速報性・サービス継続は保証されません。Googleニュース検索RSSを試験用の取得元として分離しており、正式なニュース配信APIへ将来切り替えられます。
- 公開日時とBotの初回取得日時は別々にUTCで保存します。欠損・不正な公開日時は推測せず不明として保存。夜間に初めて取得した記事を、公開直後に取引できたものとして評価しないでください。
- 同じURLは1記事にまとめ、検索銘柄との対応を別テーブルに保存します。同一事件の別記事・別URLまでは統合しません。検索結果との関連付けは候補であり、記事がその会社を扱っていると確定したものではありません。
- 初回タイトルを維持し、変更内容は `observations` に取得時刻付きで保存します。本文は `body_status=not_fetched`。リンク先の記事は取得しません。RSSの内容は外部データとして扱い、そこに含まれる指示を実行しません。
- 取得結果・エラーは `fetch_runs` に記録します。取得失敗時は既存記事を保持し、他銘柄を続行します。`status` で失敗を確認できます。SQLiteファイルをWindowsとUbuntuで同時共有せず、移行時は書き込みを止めてコピーしてください。
