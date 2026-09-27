# E2E仮想売買パイプライン

この手順は、日足取得からレポート保存までを接続確認するためのものです。実注文、有料APIの自動実行、運転中Botの停止は行いません。ニュースの分類条件や売買ロジックは変更しません。

## 1. まず読み取りだけで状態を確認する

```bat
.\.venv\Scripts\python.exe kabu_system.py e2e-preflight --config config/paper.json
.\.venv\Scripts\python.exe kabu_system.py e2e-preflight --config config/paper.json --json
```

表示される各段階は `daily`、`news_collection`、`news_quality`、`news_ai`、`chart`、`current_price`、`corporate_action`、`integration`、`risk`、`paper_decision`、`paper_ledger`、`report` です。`WAITING_MARKET` は市場時間外の正常な待機です。APIキーの存在だけを表示し、値は表示しません。コマンドはDB、API、注文を変更しません。

## 2. 企業行動を一銘柄だけ確認する

公式資料で対象日、銘柄、分割/併合の種類、価格倍率、訂正状況を確認した場合だけ、最小JSONを新しいファイルへ作成します。`none` は公式根拠で「対象営業日に該当する企業行動がない」と確認できた場合だけです。Yahooの観測0件から作成してはいけません。

```json
[
  {
    "symbol": "7203",
    "day": "YYYY-MM-DD",
    "kind": "none",
    "factor": 1,
    "verified_at": "YYYY-MM-DDTHH:MM:SS+09:00",
    "evidence": "https://公式資料のURL"
  }
]
```

```bat
.\.venv\Scripts\python.exe corporate_actions.py --file data/reviewed_7203.json --db data/operations/actions.sqlite3
```

既存確認と矛盾する値は拒否されます。`effective_day` と訂正状態は資料を確認し、現行台帳の `day` が仮想口座で調整する営業日であることを理解してから登録してください。保有中の未確認銘柄は口座全体を停止します。未保有の未確認銘柄はその銘柄だけ新規paper buyを止めます。

## 3. 市場時間中の現在値確認

2026-09-28の東証立会時間中に、kabuステーションへログインしてから実行します。パスワードは表示・保存しません。

```bat
.\.venv\Scripts\python.exe kabu_system.py quote-check --config config/paper.json
.\.venv\Scripts\python.exe kabu_system.py e2e-preflight --config config/paper.json
```

市場時間中はパスワードを非表示で入力するか、このターミナルだけへ `KABU_API_PASSWORD` 環境変数を設定してください。値を設定ファイル・コマンド履歴・画面へ残さないでください。`quote-check` は認証と板情報の読取りだけを行い、tick DB、注文、仮想口座を変更しません。`PASS` は、全10銘柄でSymbol一致、Exchange=1、CurrentPrice>0、CurrentPriceTimeと受信時刻が60秒以内であることを示します。市場外なら認証せず `WAITING_MARKET` を返します。

## 4. 1記事だけのOpenAI試験（後で明示実行する場合だけ）

`config/paper.json` を編集せず、[config/news_trial.json](../config/news_trial.json)を使用します。既存予算台帳 `config/budget.json` は共有し、試験キャッシュだけは分離します。単価が未設定、予算不足、APIキー未設定なら停止します。

```bat
.\.venv\Scripts\python.exe news_quality.py status --config config/paper.json --limit 10
.\.venv\Scripts\python.exe news_trial.py preview --config config/news_trial.json --article-id ARTICLE_ID --cache data/news_trial/ARTICLE_ID.sqlite3 --max-calls 1
```

プレビューで記事ID、鮮度、重複、料金、予算、`max_calls: 1` を確認してから、利用者が明示的に許可した場合だけ環境変数を設定し `run` に進みます。今回Codexは実行しません。

## 接続の意味

保存済みのAI結果は `NewsStore` が読み、`BusinessImpactStrategy` を通り、paper decision engineへ渡されます。industry記事は既存の「業界イベント解析→確認済みrelationから候補企業→企業別影響」経路を使います。企業名がないことだけで監視対象やpaper対象へ追加することはありません。

自然な判断が`NO_TRADE`でも、fresh quote、日足、ニュース、企業行動、リスク条件を通過してdecisionが保存されれば接続確認として成功です。
