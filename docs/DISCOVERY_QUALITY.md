# 関連性・表示・少数実ニュース検証

## 今回の変更

前回の未コミットの企業マスター拡張を引き継ぎました。現在のローカル登録は3,700社、公式資料の事業関係がある企業15社（監視10＋当初追加2＋INPEX/アドバンテスト/三井不動産）です。確認済み情報は登録した事業の範囲に限り、多角事業の全体や特定案件への参画を保証しません。残る3,685社は詳細事業が未確認です。旧社名は過去の改訂に保持し、確認できた別名はaliasesで管理します。

AIと半導体のタグを分離しました。企業名記載、事業/製品記載、供給網・経済条件の間接候補、同一産業のみ、関連不明の5区分を追加。ローカル一次選別であり、キーワードを業績好悪の確定には使いません。同一産業だけ・法人同一性が曖昧・事業根拠未確認の候補は企業別AI送信を保留します。NTTデータ等の名称延長を親会社そのものと無条件に扱いません。ただし一般的な法人同定や全グループ関係の解決は未実装です。

原文を正規化した完全一致・同一公表日の別媒体記事をローカルでまとめ、出典をduplicate_sourcesに保持します。表現が違う同一事件の自動同定は未実装です。統合判断でも一致する同方向材料を重複評価せず、矛盾する好悪は残して悪材料抑制を適用します。材料件数による加点や確率生成はありません。

古いニュースは解析済みでも既定24時間を超えると現在の材料から除外し、HISTORICAL_OR_UNDATED_NEWSで表示します。公表・取得・解析完了を別々に扱い、日時を書き換えません。24時間はanalysis_max_age_hoursで指定する既存設定です。

## Windowsの操作

```bat
.\.venv\Scripts\python.exe -m pip install -r requirements-operations.txt
.\.venv\Scripts\python.exe news_discovery.py companies-import --file config/companies_watch_verified.json
.\.venv\Scripts\python.exe company_catalog.py status
.\.venv\Scripts\python.exe news_discovery.py run --dry-run --human
.\.venv\Scripts\python.exe news_discovery.py summary --limit 10
.\.venv\Scripts\python.exe news_discovery.py summary --symbol 7203
.\.venv\Scripts\python.exe news_discovery.py summary --industry semiconductor --relation supply_chain
.\.venv\Scripts\python.exe news_discovery.py summary --status ok --direction ポジティブ --watch new
.\.venv\Scripts\python.exe news_discovery.py summary --name トヨタ --since 2026-09-24T00:00:00+09:00 --until 2026-09-26T00:00:00+09:00
.\.venv\Scripts\python.exe news_discovery.py list --symbol 7203 --json
```

summaryは初期10件、最大100件。--relationはcompany/product/supply_chain/industry/unknown、--watchはexisting/newです。除外・要確認件数は未解析数と重複します。新規ニュース数はrunの今回初登録分です。キーやAPI応答全文は概要表示しません。

**互換性:** 従来のpreview/list/runの既定JSONは維持します。人向けにはsummary、api-preview、または--humanを指定してください。--jsonは明示的な機械出力です。runのJSONは従来項目にnews/new_newsを追加しました。日本語は端末側でもUTF-8（必要なら`chcp 65001`と`set PYTHONUTF8=1`）を使ってください。

## 1記事の送信前確認

```bat
.\.venv\Scripts\python.exe news_discovery.py api-preview --limit 3
.\.venv\Scripts\python.exe news_discovery.py api-preview --news-id ここを表示された完全なIDに置換
.\.venv\Scripts\python.exe news_discovery.py api-preview --url "ここを保存済み記事URLに置換" --json
```

正確に一致する1記事だけを選びます。不明なID/URLや期限外で見つからない記事なら停止します。タイトル・URL・公表日時・候補数・詳細解析候補数・モデル・既知の未キャッシュ依頼数・max_calls・入力概算枠・出力上限・USD見積り・日次/月次残額を表示します。API送信と予算予約はしません。

入力概算は既存予算エンジンと同じUTF-8バイト数+4096で、トークナイザーの正確な件数ではありません。産業解析前には企業候補と第2段階のプロンプトが変わり得るため、表示費用は既知依頼分のみで総費用の確定値ではありません。第1段階後に再度プレビューしてください。料金はbudget_fileの利用者設定を読み、未設定のモデルは費用不明・送信不可です。実請求の保証ではありません。

## 実ニュースの隔離検証

```bat
.\.venv\Scripts\python.exe discovery_check.py prepare --folder data/check_next --limit 3
.\.venv\Scripts\python.exe discovery_check.py dry-run --folder data/check_next
.\.venv\Scripts\python.exe discovery_check.py report --folder data/check_next
.\.venv\Scripts\python.exe discovery_check.py mock --folder data/check_next
.\.venv\Scripts\python.exe news_discovery.py api-preview --config data/check_next/config.json
```

新規フォルダーを指定します。mockは実ニュースの原文を使い、構造を検証する決め打ちの「不明」応答を既存の応答検証へ通し、チャート統合まで確認します。モデル名MOCK-no-api、MOCK_ONLY、HTML冒頭のラベルで実解析と区別し、通常のAIキャッシュへ書きません。全好悪を不明にするため、実AIの分類精度を証明するものではありません。過去記事の時刻ゲートも維持します。

今回はdata/discovery_quality_20260925で実ニュース3件・候補54件を確認しました。実AI済0、企業別結果pending54件。3記事とも公表日時が24時間より古く、現在の送信予定は0件です。モックと未解析の通常レポートは別フォルダーです。MA/保存済みLightGBMの既存接続と10監視銘柄の表示を維持し、株価履歴不足は保留します。

後日有料検証する場合は、まず新鮮な記事を既存収集機能で保存し、検証フォルダーを作り直してください。生成config.jsonの対象ID・モデル・max_calls=1・budget_fileのレートと上限を確認し、network_enabled=true/dry_run=falseを明示設定、OPENAI_API_KEYを環境変数で指定してから実行します（この段落の操作は課金対象です）。

```bat
.\.venv\Scripts\python.exe news_discovery.py run --config data/check_next/config.json --news-id 対象の完全なID --human
.\.venv\Scripts\python.exe news_discovery.py api-preview --config data/check_next/config.json
.\.venv\Scripts\python.exe discovery_check.py report --folder data/check_next
```

元のconfig/paper.jsonを検証用に変更する必要はありません。expected.jsonの期待値は人が別途記入し、モデル入力から分離しています。実ニュースの原文・引用・不明点を確認してから期待値を確定してください。

## 通常の翌営業日レポート

```bat
.\.venv\Scripts\python.exe kabu_system.py evening-report --config config/paper.json
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
```

発見候補は折りたたみ一覧にし、タイトル・銘柄・解析状態・関連性を先に表示します。展開すると事業出典、原文、短中長期の好悪、時刻、見送り理由、チャート/統合結果を確認できます。JSONは全詳細を保持します。新候補を監視/売買対象へ自動追加せず、予測リターン・資金上限・企業行動確認・重複防止を変更しません。

## 情報源と利用条件の調査（2026-09-25確認）

実装時の全体検証: `python -B -m unittest discover -s tests -q` は171テスト成功。実保存ニュース3件の無課金検証では54候補を保存し、企業別AI解析は未実行のpendingとして保持しました。対象3件は鮮度条件を超えていたため現在の売買判断には使用しません。MOCKレポートは別出力で、実ニュースに対するAI精度の検証結果ではありません。

| 取得元 | 項目・更新 | 条件・自動取得の扱い |
|---|---|---|
| [JPX月末一覧](https://www.jpx.co.jp/markets/statistics-equities/misc/01.html) | コード・社名・業種・市場、月次 | 公開ファイルのローカル取込。無料閲覧は無制限な再利用許諾を意味しない。[利用条件](https://www.jpx.co.jp/term-of-use/index.html)に従い、原本を改変せず保存。全件ファイルはGitへ再配布しない。大量自動巡回は実装しない |
| [EDINET](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0110.html) | 提出書類の事業説明・セグメント等、提出時更新 | 公式APIと[API規約](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140191.pdf)あり。キー取得とアクセス制限への対応が必要。今回は接続未実装、公式資料の確認・ローカル登録を使用 |
| [Gビズインフォ](https://content.info.gbiz.go.jp/api/index.html) | 法人名・法人番号等、項目ごとの更新 | API利用申請と規約同意が必要。株式コードと事業詳細の網羅性は別。今回は接続未実装 |
| 企業公式サイト/IR（各JSONのsource） | 事業・製品・グループ記載、随時/年度更新 | 少数の公開資料を人手で確認し短い引用を登録。各サイトについて機械的な大量取得の許諾は未確認のため自動クローラーは実装しない。更新頻度・資料の年度を明記して追加する |

個別の事業出典はconfig/companies_watch_verified.json、companies_sample.json、companies_verified_extra.jsonに保存しています。2024年や2025年の資料も含み、確認日時が新しいことと資料自体が最新であることは異なります。資料の更新・事業売却/名称変更・法人関係・市場予想との比較・異なる表現の同一事件同定は引き続き人手確認が必要です。
