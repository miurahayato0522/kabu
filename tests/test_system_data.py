import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from system_data import DailyProvider, YahooProvider, daily_groups, save_snapshot, stamp, provider
from jquants_history import save_daily
from test_history import records


class ProviderTests(unittest.TestCase):
    def test_readonly_provenance_and_append_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            src, dest = Path(folder)/'source.db', Path(folder)/'normalized.db'
            save_daily(src, '72030', records([10, 11, 12]))
            original = src.read_bytes()
            bars = DailyProvider(src).bars(['7203'])
            self.assertEqual(len(bars), 3)
            self.assertIsNone(bars[0].market_at)
            self.assertGreaterEqual(stamp(bars[0].available_at), stamp(bars[0].fetched_at))
            save_snapshot(dest, bars)
            save_snapshot(dest, bars)
            with closing(sqlite3.connect(dest)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM normalized_bars').fetchone()[0], 3)
            self.assertEqual(src.read_bytes(), original)
            self.assertEqual(provider(dest).bars(['7203']),bars)
            with self.assertRaises(ValueError):
                save_snapshot(src,bars)
            with self.assertRaises(ValueError):
                YahooProvider(src).bars(['7203'])
            with self.assertRaises(ValueError):
                DailyProvider(src).bars(['8306'])
            with self.assertRaises(ValueError):
                daily_groups(bars)  # synthetic rows include New Year's holiday


if __name__ == '__main__':
    unittest.main()
