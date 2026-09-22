# 実資料に基づく小規模な検証

real_news_cases.json に本文・出典URL・ページ・公表日・期待値をまとめています。
入力は公式PDFの表の必要部分を転記・文章化したものです。PDF全文抽出やニュース収集の評価ではありません。
期待値はCodexが出典から作った草案で、ユーザーの確認済みとは扱いません。APIからの抽出結果を期待値にコピーしないでください。

## コピペ用コマンド

プロジェクトの(.venv)の画面で実行します。prepareは登録済みなら重複登録しません。

```bat
python news_eval.py prepare
python news_ai.py preview --body --documents-db data/news_eval_documents.sqlite3 --cache data/news_eval_ai.sqlite3 --model gpt-5-mini --limit 3
python news_ai.py run --body --documents-db data/news_eval_documents.sqlite3 --cache data/news_eval_ai.sqlite3 --model gpt-5-mini --limit 3
python news_eval.py report
```

runだけがAPI通信・課金を伴います。失敗するとその後の送信は停止します。
reportはAPI通信なし。表示されるreport.htmlをブラウザで開くと比較表を確認できます。
未分析・失敗・不一致も全ケースに表示します。失敗後に過去の成功を使って合格扱いにはしません。
モデルは既定gpt-5-mini。別モデルの比較はrunとreport両方で--modelをそろえます。
元の本文・期待値はAPI入力に一緒に送信せず、本文だけを送ります。

## 人による期待値確認

出典PDFの指定ページで企業、指標、期間、単位、連結/単体と前回・今回の数値を確認してください。
確認後に限り、例えば次を実行します。自分の名前・識別名に置き換えてください。

```bat
python news_eval.py review --case toyota_up_20250205 --reviewer "確認者名"
python news_eval.py review --case nissan_down_20240725 --reviewer "確認者名"
python news_eval.py review --case nissan_loss_20251030 --reviewer "確認者名"
python news_eval.py report
```

確認日時とケース全体のハッシュを別DBに記録します。本文・出典・期待値の変更は確認記録を無効にします。
一致しても、人の確認前は「暫定一致（期待値未確認）」です。確認記録はレビュー者の申告であり認証機能ではありません。

## 評価範囲

- 企業関係、指標、対象期間、円換算、前後の組合せ、差額、修正率、増減区分を確認。
- 金額は完全一致、率は0.000001パーセントポイントの誤差以内、文字列は完全一致。
- 日産の赤字縮小ケースでは前回が負のため修正率nullを期待します。
- 連結/単体は現行AI出力に専用欄がなく自動採点の対象外。資料と引用を人が確認します。
- 追加の数値・計算、数値欠落、重複は不一致となる場合があります。不一致を隠すための期待値変更は避けてください。
- 3件・2社のみ。未知の資料への汎化、銘柄選定、売買成績は評価しません。
- 2024～2025年の資料を現在取り込むため、過去時点で利用可能だったとは扱えません。
- 単体テストは人工応答で検証ロジックを確認します。実APIの分析成績はrun後に初めて分かります。
