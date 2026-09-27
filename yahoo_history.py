"""検証用Yahoo日足。J-Quantsと別DBに保存。未調整価格は分割履歴から復元した近似値。"""
import argparse
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import time
import tempfile
import shutil

from jquants_history import ROOT, DEFAULT_DB as JQUANTS_DB, HistoryError, symbol_code

DEFAULT_DB = ROOT / 'data' / 'yahoo.sqlite3'
SOURCE = 'yahoo_reconstructed_v1'
JST = timezone(timedelta(hours=9))


def fetch_frame(code, start, today):
    import certifi
    import yfinance as yf
    from curl_cffi import requests
    # Windowsのcurlが日本語パスのCAファイルを開けない場合にもTLS検証を維持。
    with tempfile.TemporaryDirectory(prefix='stock_yahoo_ca_') as directory:
        bundle = Path(directory) / 'ca.pem'
        shutil.copyfile(certifi.where(), bundle)
        with requests.Session(impersonate='chrome', verify=str(bundle)) as session:
            return yf.Ticker(code[:4] + '.T', session=session).history(
                start=start.isoformat(), end=(today + timedelta(days=1)).isoformat(), interval='1d',
                auto_adjust=False, back_adjust=False, actions=True, repair=False)


def convert(frame, code, start, end):
    if frame.empty or frame.index.tz is None:
        raise HistoryError('日足が空、またはタイムゾーン不明です')
    data = frame.copy()
    data.index = data.index.tz_convert('Asia/Tokyo')
    days = [stamp.date() for stamp in data.index]
    if days != sorted(set(days)):
        raise HistoryError('日付の重複・逆行があります')
    cumulative, rows = 1.0, []
    # YahooのOHLCはauto_adjust=Falseでも分割調整済み。
    # 最新日までの分割を使い、過去の実価格単位に戻してから期間を切る。
    for stamp, row in reversed(list(data.iterrows())):
        split = float(row['Stock Splits'])
        if not math.isfinite(split) or split < 0:
            raise HistoryError('分割情報が不正です')
        day = stamp.date()
        if start <= day < end:
            values = {key: float(row[column]) * cumulative for key, column in
                      [('O','Open'), ('H','High'), ('L','Low'), ('C','Close')]}
            volume = float(row['Volume']) / cumulative
            invalid=[k for k,v in values.items() if not math.isfinite(v) or v<=0]
            if not math.isfinite(volume) or volume<=0: invalid.append('Vo')
            if invalid:
                raise HistoryError(f'{day}: invalid_ohlcv fields={",".join(invalid)}; zero/missing/nonpositive; no imputation')
            if not values['L']<=min(values['O'],values['C'])<=max(values['O'],values['C'])<=values['H']:
                raise HistoryError(f'{day}: inconsistent_ohlc')
            rows.append(dict(values, Date=day.isoformat(), Code=code, Vo=volume,
                             AdjFactor=1 / split if split else 1,
                             ExRT=('1' if split > 1 else '2') if split and split != 1 else '0',
                             DataSource=SOURCE, YahooTicker=code[:4] + '.T',
                             PriceBasis='split_reconstructed_not_exchange_original'))
        if split:
            cumulative *= split
            if not math.isfinite(cumulative) or cumulative<=0:
                raise HistoryError('Invalid cumulative split factor')
    if not rows:
        raise HistoryError('指定期間に日足がありません')
    return list(reversed(rows))


def save(path, code, rows):
    path = Path(path).resolve()
    if path == JQUANTS_DB.resolve():
        raise HistoryError('J-QuantsのDBには保存できません')
    if not rows or any(r['Code'] != code or r['DataSource'] != SOURCE for r in rows):
        raise HistoryError('保存データが不正です')
    for row in rows:
        if any(type(row.get(k)) not in (int,float) or not math.isfinite(row[k]) or row[k]<=0
               for k in ('O','H','L','C','Vo','AdjFactor')):
            raise HistoryError('Invalid OHLCV/split factor at save boundary')
        if not row['L']<=min(row['O'],row['C'])<=max(row['O'],row['C'])<=row['H']:
            raise HistoryError('Inconsistent OHLC at save boundary')
        date.fromisoformat(row['Date'])
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('''CREATE TABLE IF NOT EXISTS daily_prices (
            code TEXT NOT NULL,day TEXT NOT NULL,source TEXT NOT NULL,fetched_at TEXT NOT NULL,
            payload TEXT NOT NULL,PRIMARY KEY(code,day,source))''')
        if any(r[0] != SOURCE for r in db.execute('SELECT DISTINCT source FROM daily_prices')):
            raise HistoryError('別の取得元のDBへは保存できません')
        existing={r[0] for r in db.execute('SELECT day FROM daily_prices WHERE code=?',(code,))}
        if not existing.issubset({r['Date'] for r in rows}):
            raise HistoryError('Incomplete replacement would remove saved dates; preserved existing data')
        # 古い分割基準のデータを混ぜないため、銘柄ごと取得範囲全体を置換。
        db.execute('DELETE FROM daily_prices WHERE code=?', (code,))
        db.executemany('INSERT INTO daily_prices VALUES (?,?,?,?,?)', [
            (code,r['Date'],SOURCE,datetime.now(timezone.utc).isoformat(),json.dumps(r,allow_nan=False)) for r in rows])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbol', type=symbol_code)
    parser.add_argument('--symbols-file', type=Path, default=ROOT / 'symbols_10.json')
    parser.add_argument('--from', dest='start', type=date.fromisoformat, default=date(2024, 6, 27))
    parser.add_argument('--to', dest='end', type=date.fromisoformat, help='終了日を含む。当日は保存しません')
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    args = parser.parse_args(argv)
    try:
        import yfinance as yf
        today = datetime.now(JST).date()
        end = min(args.end + timedelta(days=1), today) if args.end else today
        if args.start >= end:
            raise HistoryError('開始日・終了日を確認してください')
        codes = [args.symbol] if args.symbol else [symbol_code(str(c)) for c in
            json.loads(args.symbols_file.read_text(encoding='utf-8-sig'))['symbols']]
        if not codes or len(codes) != len(set(codes)):
            raise HistoryError('銘柄一覧が空または重複しています')
        yf.set_tz_cache_location(str(ROOT / 'data' / 'yfinance_cache'))
        failures = 0
        for i, code in enumerate(codes):
            if i:
                time.sleep(2)
            try:
                # 過去の終了日を指定しても、復元に必要な分割履歴は現在まで取得。
                frame = fetch_frame(code, args.start, today)
                rows = convert(frame, code, args.start, end)
                save(args.db, code, rows)
                print(f"{code}: 保存{len(rows)}日 / {rows[0]['Date']} ～ {rows[-1]['Date']}", flush=True)
            except Exception as exc:
                failures += 1
                print(f'{code}: 取得・保存失敗 / {type(exc).__name__}: {exc}', flush=True)
        print('Yahoo検証用・分割から復元した価格。配当調整なし。当日足は除外。')
        print('保存先:', args.db.resolve())
        return 1 if failures else 0
    except (ImportError, OSError, ValueError, KeyError, HistoryError) as exc:
        print('停止:', exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
