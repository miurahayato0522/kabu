# 本文の取り込みと少量のAI分析

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

出力はbody-v1という固定JSON形式です。要約、分類、記載事項、企業関係、根拠、不明点に加え、
数値を項目名・値・単位・対象期間・本文引用として保存します。本文引用の存在と数値文字列を検査しますが、
単位や期間の解釈・真偽まで保証するものではありません。出典URLと本文の対応も利用者が確認してください。
既存のheadline-v2と分離してキャッシュするため、見出しを分析済みでも本文分析には料金がかかります。

この段階ではスコア・上昇確率・注文を生成しません。ニュースから抽出した情報と価格条件を並べて確認します。
後からスコアを追加する場合も、AIの主観的な点数と、将来リターンで検証した確率は区別します。
news_marketのreplayは事後取得した価格を使う確認用。recordedは価格取得日時も制限します。
将来の成績検証では、実際に情報を取得・分析できた後の価格から評価する必要があります。
