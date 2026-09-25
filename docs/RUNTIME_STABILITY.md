# 日足更新・企業行動・継続運転（2026-09-26）

## 調査結果と今回の変更

運転中のBotを停止せず、runtime.sqlite3等は読み取り専用で確認しました。既存のconfig/paper.json、ニュース、仮想口座、日足DBは変更していません。運転中プロセスには修正がすべて反映されたとは限らないため、下記の停止・バックアップ・再起動が必要です。

再現したHistoryErrorはyahoo_history.convertのOHLCV検証です。2026-09-26 01時台JSTのYahoo取得結果では監視10銘柄すべての2026-09-25のCloseがNaNでした。例えば7203はOpen=2990, High=3003, Low=2979, Close=NaN, Volume=21084000。取得期間は2024-06-27から2026-09-26（APIの終了境界は翌日）。HTTP接続や銘柄コード変換でなく、取得後の欠損検出で停止しました。元のログには詳細がないので00:30の応答そのものとの同一性までは証明できません。

既存コードは最初の銘柄で例外が出ると全体を中断していました。今は取得・検証・保存を銘柄別に分離し、ok / partial / failedと個別のstage、理由、期間、保存済最終日、再試行予定を記録します。外部例外の本文は認証情報等の混入を避けて非表示、ローカル検証は日付と問題フィールドを表示します。既定の再試行は設定のdaily間隔（現在6時間）で、失敗を高速ループしません。

価格欠損、出来高0/負値、OHLC大小関係不整合、不正な分割情報は停止します。出来高0が売買不成立を意味することもありますが、現行分析は扱えないため正常な足としません。欠損補完、当日未確定足の早期保存、HTTP制限回避はしません。16時までは当日足を除外します。必要な直近営業日の足がない応答も成功扱いしません。既存日付が欠落した全期間置換は拒否し、既存正常データを保持します。分割の復元は従来どおり取得範囲全体を使います。

実データはdata/runtime_check_20260926に隔離しました。

| 銘柄 | 25日までの検証 | 24日までに明示限定した保存件数 | 保存最終日 |
|---|---|---:|---|
|7203|25日終値欠損・保存停止|546|2026-09-24|
|8306|同上|546|2026-09-24|
|6758|同上|546|2026-09-24|
|6501|同上|546|2026-09-24|
|8058|同上|546|2026-09-24|
|7201|同上|546|2026-09-24|
|4502|同上|546|2026-09-24|
|2914|同上|546|2026-09-24|
|9432|同上|546|2026-09-24|
|9984|同上|546|2026-09-24|

daily.jsonは全件失敗の結果、through24.jsonとthrough24.sqlite3は明示限定した成功結果、*_raw.csvは取得応答です。25日の欠損を削除して更新成功扱いにはしていません。稼働用DBは以前の9月18日のままです。9月28日の判断には9月25日の正常な足が必要で、24日までの隔離DBを差し替えて運転しないでください。

## 企業行動：取得と確認を分離

以下は2026-09-26確認の公式情報。契約・通信課金は実行していません。

| 情報源 | 分割等・日付・コード | 自動取得・費用・更新/過去 | 「なし」の確定 |
|---|---|---|---|
|[JPX配当落・権利落](https://www.jpx.co.jp/listing/others/ex-rights/)|権利落等の銘柄一覧。法的効力発生日と権利落日は区別が必要|無料公開。毎営業日13時目安、基準日が公表後2週間以内、直近4営業日分。今回スクレイパー未実装|限定範囲の一覧であり、検索0件を全企業行動なしとしない|
|[TDnet](https://www.jpx.co.jp/markets/paid-info-listing/tdnet/)|分割・併合の開示資料。効力日/比率は本文と訂正資料の確認が必要|無料閲覧と有料APIは別。APIは過去5年、更新は開示による。今回は未接続|開示の取得漏れ・訂正・対象範囲の保証が別途必要|
|[J-Quants個人向け](https://jpx-jquants.com/)|調整前後の日足を提供。価格調整だけを全権利の確認としない|API、利用可能期間・更新は契約プラン依存。既存アダプター維持|係数1だけで配当/その他権利なしとしない。今回無償プランで当日全企業行動を確認する実装なし|
|[J-Quants Pro分割](https://jpx.gitbook.io/j-quants-pro-ja/api-reference/corporate_action/stock_split)|コード、旧新比率、効力日、権利落日、訂正/削除区分|公式仕様は現時点でAPI配信なし、SFTP/Snowflake。契約サービス。東証以外の地方単独銘柄は対象外。履歴期間は契約確認が必要|分割のデータだけでは全企業行動なしを保証しない|
|[kabuステーションAPI](https://kabucom.github.io/kabusapi/reference/index.html)|現在値等の取得に使用|ログイン・API利用設定が必要。網羅的企業行動履歴フィードとしては使用していない|現在値から有無を推定しない|
|各企業公式IR|分割/併合比率・効力日は資料次第、銘柄と発行体を照合|公開資料は通常無料。更新時期/履歴保存/取得条件は各社別。統一API未実装|個別発表がないことと不存在の確認は別|
|[Yahoo / yfinance](https://ranaroussi.github.io/yfinance/)|過去の分割倍率・配当・観測日。法的効力日は得られたと扱わない|個人の研究用途、Yahoo利用条件に従う。公式IRと同等の網羅性/訂正保証なし|0件を「なし」に昇格しない|

action_candidates.pyでYahooの観測情報を専用SQLiteへ保存できます。銘柄・種類・観測日・比率/価格係数・情報源・取得日時を保存し、effective_day/verified_atはnull、statusはunconfirmed、correction_statusはunknownです。取得に失敗した銘柄は失敗と表示し、確認記録を生成しません。同じ取得レコードは重複挿入せず、再取得分は別履歴として保存します。

今回10銘柄で実取得し、6758・6501・9984の分割観測、9銘柄の配当観測を保存しました。7201は対象期間内イベント0件でしたが「なし」とは確認していません。実際の効力日・権利落日・訂正確認は未完了です。

既存corporate_actions.pyの確認済台帳はそのままです。`day`は仮想口座で価格/株数調整を適用する営業日なので、法的効力日と権利落日を無条件に同一視しないでください。`factor`は価格倍率（1株→2株なら0.5）。公式根拠を確認した記録だけを従来のJSONで登録します。値が矛盾する上書きは拒否します。配当・端株・未対応権利は停止が必要です。

台帳actions.sqlite3に対して、同じディレクトリのactions.observations.sqlite3がある場合、読み取り専用で照合します。同一銘柄・対象日の分割比率/種類の相違や配当等があれば停止します。一致しても観測結果だけから確認記録は作りません。観測日が法的効力日と違う可能性は残るため、これだけで完全な照合はできません。過去の矛盾履歴も勝手に削除せず停止します。訂正の自動解決は未実装です。

```bat
python action_candidates.py fetch --db data/action_review/observations.sqlite3
python action_candidates.py report --db data/action_review/observations.sqlite3
```

このコマンドは有料APIを使いませんがYahoo通信を行います。稼働用台帳とは別です。観測DBを運転用の照合に採用する場合は停止・バックアップ後、未使用のactions.observations.sqlite3へコピーします（既存ファイルを上書きしない）。既存確認台帳への手動登録は、根拠確認済みのファイルを用意した後のみ実行します。

```bat
python corporate_actions.py --file data/reviewed_actions.json --db data/operations/actions.sqlite3
```

全10銘柄へnoneを一括投入する例や、未確認を無効化する設定は提供しません。持越しの株数/取得単価調整・再起動後の復元・二重適用防止は既存の営業日進行と台帳の不変性で維持します。

## Windowsでの安全な反映手順

現在Botを実行しているターミナルで **Ctrl+Cを1回** 押し、`Stopped; state saved` とプロンプト復帰を確認します。タスクマネージャーでの強制終了やDB削除は不要です。他のDB更新プログラムも停止していることを確認してください。こちらから停止・再起動は行っていません。

続いて同じプロジェクトフォルダーで実行します。

```bat
python operations_backup.py --config config/paper.json
git status --short
git branch --show-current
```

バックアップは新しいdata/backups配下に作り、SQLite backup APIとintegrity_checkを使います。稼働中のランタイムロックを取得できなければ停止します。設定・主要稼働DB・存在する観測台帳・予算設定/台帳を保存し、manifest.jsonで元パスを確認できます。複数DB間の整合性のため、必ず停止後に実行してください。モデルやGit外の任意研究ファイル全体をバックアップするツールではありません。

このPCには修正済みコードがあります。専用ブランチはcodex/runtime-stabilityです。別のチェックアウトへ反映する場合はバックアップ後に以下を実行します。未コミット変更で切替に失敗したら強制せず停止してください。config/paper.jsonをcheckout/resetで上書きしないでください。

```bat
git fetch origin
git switch codex/runtime-stability
git pull --ff-only origin codex/runtime-stability
```

停止したまま日足だけ更新します（LLM/現在値/仮想注文は実行しません）。稼働ロックがあると実行できません。

```bat
python kabu_system.py daily-refresh --config config/paper.json
python kabu_system.py run-status --config config/paper.json
```

失敗が特定銘柄だけならその銘柄だけ再試行できます。実際に失敗したコードを指定してください。

```bat
python kabu_system.py daily-refresh --config config/paper.json --symbols 7203
```

daily_manualログとして記録し、監視全体の定期daily成功とは区別します。終値欠損が続くなら停止理由は解消していません。企業行動の確認状態もrun-statusで確認します。安全停止は解除せず、ニュース収集を継続するための再起動は可能です。

APIパスワードはコマンド履歴やファイルへ書かず、必要な営業日にPowerShellで次を実行して一時的なプロセス環境変数へ設定します。kabuステーションを起動・ログインし、API利用設定とAPIパスワード設定を確認してください。口座ログイン用パスワードとは別です。早朝ログアウト後は再ログインが必要です。

```powershell
$secret = Read-Host 'kabuステーションのAPIパスワード' -AsSecureString
$credential = New-Object System.Net.NetworkCredential('', $secret)
$env:KABU_API_PASSWORD = $credential.Password
python kabu_system.py run --config config/paper.json
# 上のBotをCtrl+Cで終了した後に実行
Remove-Item Env:KABU_API_PASSWORD
$credential = $null
$secret = $null
```

ニュース収集だけ継続する場合は従来どおり `python kabu_system.py run --config config/paper.json`。APIパスワードがなければ取引時間内のpricesは失敗し、仮想売買は停止します。dry_run=trueは有料ニュース解析を止める設定であり、無料データ通信や仮想売買そのものを無効化する設定ではありません。

別ターミナルで確認します。

```bat
python kabu_system.py run-status --config config/paper.json
python kabu_system.py paper-report --ledger data/operations/paper.sqlite3
```

9月28日の取引時間内にprices=ok、10銘柄の新しいCurrentPriceTime/受信時刻が揃うことを確認します。price_jobは銘柄/市場/価格/60秒以内の時刻を検証し、失敗後は次回に再認証します。市場時間外のprices未実行は正常です。通常注文は前場09:00～11:30・後場12:30～15:30のみ、保存済みtickでも時間外stepを拒否します。

paper=okの結果はexecuted / waiting_next_quote / orders_rejected / new_buys_blocked / no_signalを区別します。okは約定があったという意味ではありません。最終成功・最終失敗・次回予定・銘柄別日足をrun-statusに表示し、ハートビートだけで全体正常と判断しません。市場時間外でも以前のprices/paperエラーは注意表示に残します。

仮想売買に進むには、全監視銘柄の前営業日確定足、60秒以内の現在値、必要期間の企業行動確認、ニュース解析/収集の健全性、既存リスク条件が必要です。DRY_RUNの未解析記事が残る場合は既存設計どおり新規買いが止まります。ニュース無視やリスク解除で約定を発生させないでください。

## 隔離検証と残課題

最終テスト: `python -B -m unittest discover -s tests -q`、182件成功（既存171件＋追加11件）。新規検証は銘柄別の部分成功、欠損/不正値、取得失敗とSQL保存失敗時の既存行保持、古い応答の拒否、価格アダプター認証/鮮度、時間外停止、観測と確認の分離/矛盾停止、SQLiteバックアップを含みます。既存のソース分離テストの入力も保存境界の検証に合う正常OHLCVへ更新しています。

```bat
python daily_updates.py --db data/daily_check/yahoo.sqlite3 --report data/daily_check/result.json
python daily_updates.py --db data/daily_check/yahoo.sqlite3 --report data/daily_check/retry.json --retry-report data/daily_check/result.json
python -B -m unittest discover -s tests -q
```

daily_updates.pyは稼働用設定に登録されたDBパスを出力先にできません。テストは一時DBとモックを使用。実データ検証は上記10銘柄のYahoo取得・24日までの保存・企業行動候補取得のみです。kabu認証/株価取得、分割適用/持越し/再起動、API停止・既存MA/LightGBM/ニュース統合の回帰確認はモック/既存テストです。実市場での現在値取得・9月28日の仮想売買・25日日足の正常取得・全企業行動の自動確認・訂正解決は未完了です。
