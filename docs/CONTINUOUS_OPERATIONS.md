# 継続運転ガイド（Windows）

基点 `906cacbcc3fd296e56fd3bfce8a3105583d34381`、ブランチ `codex/continuous-paper-operations`。チャートAIのモデル・特徴量・学習方法、既存MAは変更していません。実注文機能はありません。

## 準備と起動

プロジェクトフォルダで実行します。

```bat
.venv\Scripts\activate
python -m pip install -r requirements-operations.txt
python kabu_system.py run --config config/paper.json --once
python kabu_system.py run-status --config config/paper.json
```

初期設定は `dry_run: true` / `network_enabled: false`。`news OFFLINE` / `news failed` は収集範囲を不明と記録する意図した動作です。市場時間外は価格/paperジョブを実行しません。新しいDBは `data/operations/` に分け、前回のDBは変更しません。

無料収集を開始するには `config/paper.json` の `network_enabled` を `true` に変更し、`dry_run` は `true` のまま、以下を起動します。

```bat
python kabu_system.py run --config config/paper.json
```

設定変更は再起動時に反映。設定が変わればジョブ予定を更新しますが、記事・解析・口座状態は消しません。同じ設定での再起動では予定時刻を保持し、直ちに大量再取得しません。

## 収集

- `symbols` が初期10銘柄。`topics` と `market_keywords` で日銀・米国経済・為替・資源・半導体等を変更できます。
- RSSは既定15分、日足Yahoo更新6時間、kabu現在値60秒。間隔は `intervals` の秒数、最小30秒。失敗時は通常間隔と300秒の長い方まで待ちます。設定間隔は取得許可・速報性を保証しません。
- kabuは既存のREST現在値取得を再利用。このプロセスが記録するsnapshotでありPUSHではありません。kabuステーション起動/ログイン/API有効化後、起動ターミナルの環境変数 `KABU_API_PASSWORD` を安全に設定してください。キーを設定JSON・ログ・Git・チャットへ書かないでください。
- 日足は `refresh_yahoo` と `history_start` で制御。当日足を除外します。通信エラー時に既存データは削除しません。正常取得時の銘柄ごとの更新は既存Yahoo保存処理と同じです。
- `trading_hours` はJST。XTKS営業日と指定時間帯の双方を満たす時だけ価格取得と仮想口座を更新します。
- 市場/業種の記事は `query_scopes` と `@market:` / `@industry:` の照合キーを保持。マクロニュースは監視企業を候補としてAIへ渡しますが、直接関連と推測して登録しません。
- URL重複に加え、同じ見出し/本文・公表時刻・配信元の完全一致を解析キューで統合し、企業の候補集合を保持します。異なる言い回しのイベントは自動統合しません。報道件数で好悪や重要度を加算しません。

## 本文・公式資料

一般記事を無差別に全文取得しません。本文がなければ見出し解析です。利用を認められる本文/抜粋は既存 `news_documents.py import` で `data/operations/documents.sqlite3` へ登録できます。

`official_documents` に明示指定した対応公式URLだけを取得できます。形式例（URLは実際の対応URLに置き換え）:

```json
[
  {"type":"toyota_html","url":"対応するトヨタ公式HTML URL"},
  {"type":"official_pdf","url":"対応する公式PDF URL","symbol":"7203","title":"資料タイトル","pages":"12"}
]
```

既存 `news_ir.py` / `news_pdf.py` のURL許可・サイズ制限を再利用します。ログイン・有料記事・取得禁止の制限は回避しません。URLごとに一度試行し、失敗/中断も自動再送しません。改訂取得は既存CLIで履歴を保持して明示実行してください。HTML内のPDF自動探索は未実装です。

同じURLの登録本文があれば見出しより優先。異なるURLの資料は別記事です。公表時刻不明の資料は分析できても売買材料としては未確認です。

## ニュースAI・月額予算

```bat
python news_ai.py preview --impact --db data/operations/news.sqlite3 --limit 3
python news_ai.py list --cache data/operations/news_ai.sqlite3
```

新形式は `headline-v3-impact` / `body-v4-impact`。旧形式・履歴は保持。新形式では同じ記事でも別解析になります。銘柄ごとの短期/中長期の好悪、重要度、理由、原文引用、原文にある場合だけの市場予想比較を保存します。企業業績/事業環境へのLLM分類であり、予測株価・上昇確率ではありません。未知の本文や比較対象は推測せず不明です。原文にない引用は検証失敗にします。

利用者が実API解析を有効化する手順:

1. `config/budget.json` の `rates_per_million` にモデル別の最新入力/出力USD単価（100万tokensあたり）を `[入力単価, 出力単価]` として設定。
2. 月額目安 `monthly_jpy: 500`、換算仮定 `jpy_per_usd: 150`、日額 `daily_usd: 0.12` を確認。150円は仮定であり実勢為替ではありません。
3. 起動ターミナルの環境変数 `OPENAI_API_KEY` を安全に設定。
4. 自分で `dry_run` を `false` に変更し、再起動。

単価未設定なら送信を止めます。日/月はUTC。送信前に日次・月次の見積りを予約し、応答のtokensで推定額を更新。不明な使用量は予約を保持。予算到達時は新規API送信だけを停止し、RSS収集は継続。高額モデルへの切替・無制限再送はありません。同じ予算台帳を全呼出しに使ってください。実請求額の厳密な上限は保証しません。

単独の旧 `news_ai.py run` には `--budget-file config/budget.json` を明示してください。運転コマンドの予算は必須ですが旧CLIの予算指定は互換性のため任意です。DRY_RUNでも重要候補が未解析なら、新規買いは停止します。

## チャート・統合判断

既定はMA。LightGBMを使用する場合は `chart.strategy` を `lightgbm`、`chart.model` を保存済みモデルフォルダに変更します。予測値・期間・モデル名・データ参照を保持します。

|ケース|研究用初期ルール|
|---|---|
|A チャート買い＋好材料|買い候補を維持|
|B チャート買い＋悪材料|買い見送り|
|C ニュースなし|正常な監視範囲内ならチャート判断を維持|
|D チャート買いなし＋好材料|監視理由を記録。ニュースだけでは買わない|
|E 重要度「高」の悪材料|保有の売却候補。注文上限等が優先|
|収集/解析不明・未完了|新規買い停止。予測リターン自体は保持|

条件は `integration` で変更できます。間接材料は既定で売買に使いません。ニュース専用モデルの追加学習やニュース単独の新規買い戦略は今回追加していません。

「ニュースなし」は直近の設定対象RSS取得成功・2収集間隔以内・未完了重要候補なしという監視範囲内での判断です。全媒体の網羅を意味しません。数値や重要度を予測リターンへ加算しません。

## 持越し・企業行動確認

**当日企業行動の自動確認フィードは未接続です。** 確認済みデータを受け取るインターフェースと台帳を実装しました。今回のテスト成功は実市場の確認情報が既に揃っているという意味ではありません。

確認済みデータのJSON配列を用意します。次は項目の説明用で、実企業の確認結果として使わないでください。

```json
[{"symbol":"7203","day":"対象日YYYY-MM-DD","kind":"none","factor":1,"verified_at":"確認完了のタイムゾーン付きISO日時","evidence":"確認資料・URL等"}]
```

```bat
python corporate_actions.py --file data/confirmed_actions.json --db data/operations/actions.sqlite3
```

- `none` は発生なしを明示確認した場合だけ。日足係数1だけで自動登録しません。
- 2分割は `split` / `factor: 0.5`、2株を1株に併合は `reverse` / `factor: 2`。総原価は不変、株数と平均取得単価を換算。
- 前回口座日から当日までの全営業日・全監視銘柄の確認が必要。初日は当日分。未確認・未来の確認・未対応・端株発生なら口座全体を更新せず理由を記録。他銘柄だけ進める機能は未実装。
- 配当・税金・権利割当・端株の現金精算は未対応。該当する日は `unsupported` で止める必要があります。
- 同じ銘柄/日の矛盾する上書きを拒否。訂正資料による過去状態の再計算は未実装です。

確認が揃えば保有を復元し、分割等を一度適用して現在価格で評価します。保有開始/最終評価時刻、株数、平均取得単価、原価、現金、損益、注文/約定を保存。異常時は前回状態を保持します。

## 停止・再開・確認

Ctrl+Cで停止し、同じコマンド・設定で再開します。OSロックは異常終了でも解放され、同じruntime DBの二重起動を防ぎます。別runtime DBから同じ口座へ書き込まないでください。

```bat
python kabu_system.py run --config config/paper.json
python kabu_system.py run-status --config config/paper.json
python kabu_system.py paper-report --ledger data/operations/paper.sqlite3
```

別ターミナルで新規買いだけを停止:

```bat
type nul > data\operations\STOP_NEW_TRADES
```

再開時は停止ファイルだけを手動削除。DBは削除しません。口座のモデル・資金/リスク設定変更は同一台帳では拒否します。

`run-status` は次回予定・成否・直近20ログ。RSS詳細は `fetch_runs`、AI失敗は `news_ai.py list`、企業行動や株価エラーはpaper台帳のERRORで確認できます。日足シグナルは前営業日までの確定足を使い、実際の判断後に届く次の価格で仮想約定します。再起動時の同じ判断・ニュースイベントの重複注文を防ぎます。

処理は逐次なので、遅いRSS/PDF/API処理は価格ジョブも遅らせます。古い市場/受信時刻で評価を進めません。低遅延のPUSHワーカー化・長時間耐久試験は残課題です。

## 検証と将来のLLM

模擬ニュース/API/株価で一連の運転、再起動、持越し、分割/併合、未確認停止を検証。新形式の実OpenAI解析、実RSS/Yahoo収集、実相場の長時間運転、利益が出ることは今回確認していません。有料API・実注文はゼロです。

仕様参照: [OpenAI公式Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)。Schema準拠と意味の正しさは別です。`news_sentiment.NewsAnalyzer` のバックエンド呼出し境界を用意しました。ローカルLLMには同じ結果/envelopeを返すadapterが別途必要です。モデル取得・GPU設定はしていません。
