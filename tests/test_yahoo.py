from contextlib import closing
from datetime import date
from pathlib import Path
import sqlite3
import tempfile
import unittest
try:
    import pandas as pd
except ImportError:
    pd = None
from yahoo_history import convert, save, SOURCE
from jquants_history import load_daily, HistoryError


@unittest.skipIf(pd is None, 'Yahoo追加依存なし')
class YahooTests(unittest.TestCase):
    def test_split_restores_price_volume_and_ex_date(self):
        frame = pd.DataFrame({'Open':[20,20,21], 'High':[20,20,21], 'Low':[20,20,21],
                              'Close':[20,20,21], 'Volume':[500,500,600], 'Stock Splits':[0,5,0]},
                             index=pd.date_range('2026-09-01', periods=3, tz='Asia/Tokyo'))
        rows = convert(frame, '72030', date(2026,9,1), date(2026,9,3))
        self.assertEqual(rows[0]['C'], 100)
        self.assertEqual(rows[0]['Vo'], 100)
        self.assertEqual(rows[1]['C'], 20)
        self.assertEqual(rows[1]['AdjFactor'], .2)
        self.assertEqual(rows[1]['ExRT'], '1')
        # 終了日より後に分割があっても復元に含む。
        self.assertEqual(convert(frame,'72030',date(2026,9,1),date(2026,9,2))[0]['C'],100)

    def test_source_isolation_and_loader(self):
        row = {'Code':'72030','Date':'2026-09-01','DataSource':SOURCE,
               'O':100,'H':100,'L':100,'C':100,'Vo':1000,'AdjFactor':1}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.db'
            save(path,'72030',[row])
            self.assertEqual(load_daily(path,'72030'),[row])
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("INSERT INTO daily_prices VALUES ('83060','2026-09-01','jquants_v2','now','{}')")
            with self.assertRaises(HistoryError):
                save(path,'72030',[row])
            with self.assertRaises(HistoryError):
                load_daily(path,'72030')

    def test_empty_missing_and_duplicate_stop(self):
        with self.assertRaises(HistoryError):
            convert(pd.DataFrame(),'72030',date(2026,9,1),date(2026,9,3))
        frame = pd.DataFrame({'Open':[float('nan')], 'High':[20], 'Low':[20], 'Close':[20],
                              'Volume':[100], 'Stock Splits':[0]},
                             index=pd.date_range('2026-09-01',periods=1,tz='Asia/Tokyo'))
        with self.assertRaises(HistoryError):
            convert(frame,'72030',date(2026,9,1),date(2026,9,3))
