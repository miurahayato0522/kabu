# 公式ページの本文取得

初版はトヨタ公式サイトの日本語コーポレート記事のみです。
対応URL: https://global.toyota/jp/newsroom/corporate/数字.html
BeautifulSoupを使います。別PCでは `python -m pip install -r requirements-ir.txt` を実行してください。

## 既知の過去ページで動作確認（本番DBとは分離）

```bat
python news_ir.py https://global.toyota/jp/newsroom/corporate/42662211.html --db data/news_ir_test_documents.sqlite3 --archive data/news_ir_test_fetches.sqlite3
python news_documents.py list --db data/news_ir_test_documents.sqlite3
```

これは2025年5月8日の決算説明会ページです。業績予想の数値表そのものではありません。
日時欄の14:00は説明会の開催時刻なので、公表時刻には使用しません。
公表日は保存しますが公表時刻が不明ならpublished_atは空です。AI分析は可能でも、仮想判断のニュース条件は見送りになります。

## 新しい対応記事のURLを取得

```bat
python news_ir.py https://global.toyota/jp/newsroom/corporate/実際の番号.html
python news_ai.py preview --body --symbol 7203 --model gpt-5-mini --limit 1
python news_ai.py run --body --symbol 7203 --model gpt-5-mini --limit 1
python news_ai.py list
```

先頭のURLを実際の記事URLに置き換えます。previewでタイトルを確認してください。
news_ir.pyはAIを呼びません。runでのみAPIへ送信し、課金されます。
日付しかない記事を購入候補にするために任意の時刻を設定しないでください。

本文は既存news_documents.sqlite3に登録し、source_method=official_html・公表日・日付品質・パーサー版を保存します。
取得履歴は別のnews_ir_fetches.sqlite3にURL、開始・受信日時、HTML原本、SHA256、登録revisionを保存します。
本文や日時等が同じなら同一revisionで、初回登録日時は維持します。取得履歴は毎回追加します。
元HTMLを保存するため履歴DBは増加します。自動巡回・定期実行は未実装です。

公表時刻には記事専用article:published_timeメタデータだけを使用し、タイムゾーンと表示日付の整合性を確認します。
時刻不明や日付欠損は本文を保存して理由を表示します。記事構造の不一致、本文空、12000文字超、3MB超、通信失敗は停止します。
本文は対応領域から抽出した抜粋扱いです。関連記事、スクリプト、画像、iframeを除き、リンク先やPDFは追いません。
HTML表はテキストに展開されるため、単位や列の対応は原ページで確認してください。
公式ドメインであることは取得元の確認であり、子会社の記事が親会社自身への材料であることを保証しません。
PDF中心の決算資料を扱う機能や他社用パーサーは今後の追加対象です。

# 公式PDFのページ指定取得

トヨタ・日産の公式HTTPS PDFをページ指定で本文登録できます。PDF全体を送信せず、確認したページに
`[PDF 12ページ]` のような出典を付けます。PDFライブラリはrequirements-pdf.txtに追加済みです。

```bat
python news_pdf.py preview https://global.toyota/pages/global_toyota/ir/financial-results/2025_3q_presentation_jp.pdf --pages 12
python news_pdf.py import https://global.toyota/pages/global_toyota/ir/financial-results/2025_3q_presentation_jp.pdf --pages 12 --symbol 7203 --title "トヨタ：2025年3月期第3四半期・連結決算見通し要約" --db data/news_pdf_test_documents.sqlite3 --archive data/news_pdf_test_fetches.sqlite3
python news_documents.py list --db data/news_pdf_test_documents.sqlite3
```

previewはPDFを保存せず、指定ページの抽出テキストを表示します。importではPDF原本、SHA256、選択ページ、
取得日時、本文登録revisionを取得履歴DBへ保存します。AI送信・課金・注文はありません。

公開PDFでも空パスワード暗号化なら読み込みます。実際のパスワード保護、画像だけのPDF、本文12000文字超、
15MB超、指定ページのテキストが空の場合は登録を停止します。PDFのレイアウト抽出は表の列を保証しないため、
previewで「前回見通し」「今回見通し」「前期実績」と対象期間・単位を必ず確認してください。

PDF本文から取得できるのは公表日だけで、公表時刻は推測しません。既存の仮想判断では時刻不明としてニュース条件を見送ります。
PDFのURLはトヨタと日産の公式ドメインだけに限定しています。URLにクエリやリダイレクトは使えません。
