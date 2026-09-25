# 企業DBの拡張と実ニュースの限定検証

2026-09-25更新: 監視9社の事業資料を追加し、確認済み事業は15社・16関係になりました。本文の6社は初回拡張時点の記録です。最新の関連性判定と操作は[関連性改善ガイド](DISCOVERY_QUALITY.md)を参照してください。

## データの役割

[JPX公式の東証上場銘柄一覧](https://www.jpx.co.jp/markets/statistics-equities/misc/01.html)は無料で取得できる月末の一覧です。銘柄コード・名称・33業種・市場区分を利用し、プライム/スタンダード/グロースの内国4文字銘柄を対象とします。ETF、REIT、外国株、種類株などの5文字コードは対象外です。日本の全取引所や月中の新規上場を網羅するものではありません。

一覧から主要事業を推測して確定することはありません。業種→産業タグの対応はcompany_catalog.pyのSECTORSにある明示ルールです。例えば電気機器・機械から半導体への候補は広い「推定」で、個別企業が半導体事業を行う証拠にはなりません。推定だけの企業は企業別AI送信を保留し、事業の確認を要求します。

主要事業・役割・関連産業は既存のcompanies-importで別途登録します。公式URL、短い原文引用、確認日時を持つ確認済み情報と、不明・推定を分離します。主要事業未確認を空想で補いません。新しい企業名と既存の名前が異なる場合（全角/半角差を除く）は、古い事業情報を自動継承せず再確認扱いです。

## Windows: 一覧の更新

```bat
.\.venv\Scripts\python.exe -m pip install -r requirements-operations.txt
.\.venv\Scripts\python.exe company_catalog.py import-jpx --file data/company_sources/jpx_20260923T151959.xlsx
.\.venv\Scripts\python.exe news_discovery.py companies-import --file config/companies_verified_extra.json
.\.venv\Scripts\python.exe company_catalog.py status
```

上のxlsxは今回保存したファイルです。次回はJPX公式ページから最新ファイルをダウンロードし、--fileをそのパスへ置き換えます。CLIは自動ダウンロードやAPI呼出しをしません。.xlsx/.xlsと同じ日本語列名のUTF-8 CSVを読み込めます。CSVは必ず完全な一覧を使用し、一部だけのファイルを月次一覧として取り込まないでください。

今回取得した一覧は2026-08-31基準で、対象3,700社でした。原本はdata/company_sourcesに保存し、DBにはURL、一覧基準日、取得後の登録時刻、原本SHA-256、全件スナップショットを追記します。同一ファイルの再登録は冪等、古い基準日の上書きは拒否します。前回一覧から消えた銘柄を新しい候補集合に残さず、過去の一覧と企業改訂は保持します。当月中の上場/廃止は別途確認が必要です。

## 主要事業・関連産業の追加

```bat
.\.venv\Scripts\python.exe company_catalog.py template --symbol 7203 --output data/company_7203_review.json
```

未使用の出力名を指定してください。JSONを編集し、business、aliases、relationsを公式資料で確認した内容に更新します。relationsのtopic/roleはcompany_graph.pyの定義から選び、state=確認済みにはsource・quote・verified_atが必要です。数値比率が不明ならnullのままにします。updated_atも確認時点へ更新し、資料本文やLLMの推測を出典確認の代わりにしないでください。

```bat
.\.venv\Scripts\python.exe news_discovery.py companies-import --file data/company_7203_review.json
```

今回追加した3社の出典は[INPEX事業案内](https://www.inpex.com/business/)、[アドバンテスト企業情報](https://www.advantest.com/ja/about/)、[三井不動産事業紹介](https://www.mitsuifudosan.co.jp/business/)です。既存3社と合わせ確認済み事業関係は6社。現時点で1,172社は業種由来の推定だけです。その他の企業も名称検索と公式資料の追加登録ができます。6社以外の主要事業を確認済みにしたわけではありません。

## ローカル検索と送信件数

1回の処理開始時に産業タグの転置索引と企業名・別名の文字検索索引を作成します。各記事は該当タグと文字一致の企業だけを取り出します。全企業の情報をLLMへ送りません。既存のcompanies/discoverの呼出し互換性を維持しています。

discovery.max_candidates（既定5）は企業別AIの上位候補数、max_calls（既定3）は発見処理の1回のAPI上限。max_saved_candidates（既定50、最大1000）は1記事で保存する候補数です。候補には全一致数と保存上限を記録し、切り捨てを網羅的な探索と誤認しないようにします。名称直接一致、確認済み関係を優先します。業種だけの候補には企業別APIを使いません。

## 実ニュース3件の検証（API通信なし）

```bat
.\.venv\Scripts\python.exe discovery_check.py prepare --folder data/news_check_next --limit 3
.\.venv\Scripts\python.exe discovery_check.py dry-run --folder data/news_check_next
.\.venv\Scripts\python.exe discovery_check.py report --folder data/news_check_next
```

prepareは既存の保存済みニュースから鮮度とタグの多様性を優先して選びます。最大10件です。空なら先に既存RSS収集を行ってください。元記事・取得/公表日時・企業改訂の一覧をmanifest.jsonに固定し、config.jsonはdry_run=true、network_enabled=false、対象記事ID限定、max_calls=1に設定します。別のdiscovery.sqlite3を使い、運用中の候補DBと混ぜません。記事内容が変わったり7日を過ぎて見つからなくなった場合、また企業DBが変わった場合は再準備を要求します。

event_requests.jsonで第1段階に送る内容を確認できます。expected.jsonは人が別途記入する期待値で、API入力には含めません。topicsは期待するタグ配列、symbolsは保存される候補のコード配列、impactsはコード→短期の好悪材料、reviewerは確認者を記入します。これは人の仮説との一致検証であり、上昇確率の正解や収益性の検証ではありません。

reportは保存済みの第1段階・企業別解析結果を読み、分類/候補/影響/状態をvalidation.json、チャート・MA等との統合結果をevening.jsonとHTMLへ保存します。AI未解析、企業情報不足、入力変更、人手期待値未確認、一致、不一致を区別します。AI通信はありません。

後日、有料実解析を自分で開始する場合に限り、生成したconfig.jsonの予算・対象・モデルを確認し、network_enabled=trueとdry_run=falseへ明示変更し、環境変数OPENAI_API_KEYを設定したうえで既存news_discovery.py run --config 対象/config.jsonを実行できます。初期max_calls=1のため第1段階の後で停止し、次回にキャッシュを利用して企業別へ進みます。この操作は課金対象で、今回は実行していません。古い記事の公表日時を書き換えて鮮度ゲートを通さないでください。

## 検証範囲

実ニュース3件からローカル検索→候補保存→HTML/JSON表示を実行しました。今回の100件は記事×銘柄候補で、100銘柄を買う意味ではありません。実LLMの解析精度は未検証と表示します。AI応答後の産業抽出→企業別好悪→翌営業日表示はモック統合テストで確認します。

最終確認は全161テスト成功（既存151＋今回10）。新企業の候補キー重複を件数照合で検出し、企業改訂キーに銘柄情報を含めて修正しました。回帰テストと実ニュース再実行で、保存100件・表示100件・識別子100種類・発注可能0件を確認しています。既存監視銘柄10件の分析も同じレポートに表示します。修正前の試行フォルダーは削除せず保持し、確認済み結果はdata/discovery_check_20260924_v2以下です。

実注文、有料APIの自動実行、監視対象への自動追加はありません。株価反応・コスト・リスクを確認する既存研究戦略と仮想口座の設計を維持します。新候補の価格履歴がなければチャートは保留されます。実ニュースの妥当性確認、主要事業の継続拡充、日本株全体の網羅性、実AI精度の評価は今後の作業です。
