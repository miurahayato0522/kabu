# 本文の取り込みと少量のAI分析

## 保存済みの金額をPythonで計算する（API通信なし）

```bat
python news_numbers.py --cache data/news_ai_test_diagnostic.sqlite3
```

成功した本文分析の最新結果を記事ごとに使います。元のAI結果・DBは変更せず、元の抽出値と
計算用の円金額、差額、修正率、参照した分析IDを新しいdata/news_numbers_日時.jsonへ保存します。
標準出力にも結果を表示します。今回の架空例は100億円→120億円、差額20億円、修正率+20%です。
円・千円・万円・百万円・億円・兆円、全角数字、桁区切り、負号に対応。
概数・範囲・複合単位・外貨・曖昧な赤字符号は推測しません。
候補企業が1つ、指標・期間が一致、変更前後が一組の場合のみ比較します。
対象期間不明、同じ指標の重複、AI品質警告、非対応ラベルは確認対象です。
修正率=(変更後−変更前)/変更前×100。変更前がゼロまたは負なら率は計算せず差額と変化を表示します。
割合の表示は小数点以下2桁。計算値はDecimalの文字列で保存し、元の引用を残します。
原文の意味やAIの抽出漏れは自動保証できないので、実資料では企業・期間・連結/単体等も照合してください。
news_marketのレポートにも同じ計算を表示しますが、修正率を売買条件には使いません。

現在の分析形式はbody-v3です。以下のbody-v2の説明は数値・期間抽出を導入した時点の説明です。
body-v3では候補銘柄のコードと企業関係の件数をJSONスキーマでも制限します。
複数銘柄の場合は同一コードの重複をローカル検査で引き続き拒否します。
予想と実績の区別、原文通りの期間引用、架空資料内の企業関係について指示を強化しています。
この変更だけで意味的な正確さが保証されるわけではありません。

保存済みのテスト資料をgpt-5-miniで1件比較する場合（追加料金あり）:

```bat
python news_ai.py run --body --documents-db data/news_documents_test.sqlite3 --cache data/news_ai_test_diagnostic.sqlite3 --model gpt-5-mini --limit 1
python news_ai.py list --cache data/news_ai_test_diagnostic.sqlite3
```

以前の失敗記録は削除不要です。モデル・分析形式が変わると別の依頼として保存されます。

利用できる企業公式資料などから本文をUTF-8のテキストファイル `body.txt` に保存します。
URLからの自動取得、PDF変換はこの機能に含みません。抜粋は `excerpt`、全文は `full` として登録します。
次のタイトル・URL・日時は実際の資料に置き換えてください。公表日時不明なら `--published-at` を省略します。

```powershell
.\.venv\Scripts\python.exe news_documents.py import --text body.txt --title "実際の資料タイトル" --url "https://example.com/release" --symbols 7203 --published-at "2026-09-21T15:30:00+09:00" --kind excerpt
.\.venv\Scripts\python.exe news_documents.py list
.\.venv\Scripts\python.exe news_ai.py preview --body --limit 1
.\.venv\Scripts\python.exe news_ai.py run --body --limit 1
.\.venv\Scripts\python.exe news_ai.py list
.\.venv\Scripts\python.exe news_market.py --prices-db data/yahoo.sqlite3
```

previewまでAPI通信はありません。runで選択された本文がOpenAIへ送信され、従量料金が発生します。
既定はgpt-5-nano。本文は最大12000文字、出力上限3500トークン。長い資料は必要部分を抜粋します。
銘柄はnews_sources.jsonの登録銘柄です。対象を選ぶには `--symbol 7203` をpreview/run双方に指定できます。

本文はdata/news_documents.sqlite3、分析結果は既存のdata/news_ai.sqlite3に保存します。
同じ内容・メタデータの再登録では登録日時を更新しません。変更した資料は別の履歴として保存し、最新の履歴を分析します。
公表日時、登録日時、分析完了日時を区別し、判断時刻は分析完了・観測・登録日時の最も遅い時刻です。
RSS記事と公式資料のURLが異なれば別の記事として扱います。同一イベントの重複排除は今後の課題です。

出力はbody-v2という固定JSON形式です。要約、分類、記載事項、企業関係、根拠、不明点に加え、
数値を項目名・値・単位・対象期間・本文引用・期間の根拠引用・役割（変更前/変更後/実績/予想等）として保存します。本文引用の存在と数値文字列を検査しますが、
単位や期間の解釈・真偽まで保証するものではありません。出典URLと本文の対応も利用者が確認してください。
既存のheadline-v2と分離してキャッシュするため、見出しを分析済みでも本文分析には料金がかかります。

この段階ではスコア・上昇確率・注文を生成しません。ニュースから抽出した情報と価格条件を並べて確認します。
後からスコアを追加する場合も、AIの主観的な点数と、将来リターンで検証した確率は区別します。
news_marketのreplayは事後取得した価格を使う確認用。recordedは価格取得日時も制限します。
将来の成績検証では、実際に情報を取得・分析できた後の価格から評価する必要があります。

## 登録済みの架空テスト資料をbody-v2で再分析する

```bat
python news_ai.py preview --body --documents-db data/news_documents_test.sqlite3 --cache data/news_ai_test.sqlite3 --limit 1
python news_ai.py run --body --documents-db data/news_documents_test.sqlite3 --cache data/news_ai_test.sqlite3 --limit 1
python news_ai.py list --cache data/news_ai_test.sqlite3
```

再登録は不要。body-v1は履歴に残り、body-v2は新規分析として料金が発生します。
期待する抽出は「100/億円/変更前/2027年3月期」と「120/億円/変更後/2027年3月期」です。
品質要確認はプログラムによる点検結果で、AI自身の点数ではありません。
本文に期間表記があるのに対象期間が不明、または企業関係の理由が分類名だけ、といった応答を検出します。
期間表記の検出は限定的な日本語パターンです。警告なしでも全て正しいとは限りません。
複数期間の対応が曖昧なら自動補完せず、確認対象にします。品質警告がある分析はnews_marketで見送ります。
API応答が形式・引用検査に通った場合はokのまま、listで品質要確認を表示します。
単体テストは人工応答による検査ロジックの確認であり、モデルの精度改善は実APIでの再分析後に判断します。
