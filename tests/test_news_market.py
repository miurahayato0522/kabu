from datetime import datetime, timedelta, timezone
import unittest
from news_market import indicators


class MarketTests(unittest.TestCase):
    def records(self):
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        return [({'Date': (start + timedelta(days=i)).date().isoformat(),
                  'C': 100 + i, 'Vo': 100, 'AdjFactor': 1}, '2026-09-30T00:00:00+00:00')
                for i in range(31)]

    def test_future_and_same_day_excluded(self):
        records = self.records()
        asof = datetime.fromisoformat('2026-08-25T23:00:00+09:00')
        first = indicators(records, asof, 'replay')
        self.assertEqual(first['price_date'], '2026-08-24')
        records[-1][0]['C'] = 99999
        self.assertEqual(first, indicators(records, asof, 'replay'))
        self.assertEqual(first['volume_ratio_20d'], 1)

    def test_recorded_requires_prior_fetch(self):
        asof = datetime.fromisoformat('2026-09-01T00:00:00+09:00')
        with self.assertRaises(ValueError):
            indicators(self.records(), asof, 'recorded')
        available = [(r, '2026-08-31T14:00:00+00:00') for r, _ in self.records()]
        self.assertEqual(indicators(available, asof, 'recorded')['price_date'], '2026-08-31')

    def test_split_adjustment_price_and_volume(self):
        rows = self.records()
        for r, _ in rows:
            r['C'] = 100
        for r, _ in rows[20:]:
            r['C'], r['Vo'] = 20, 500
        rows[20][0].update(AdjFactor=.2, ExRT='1')
        result = indicators(rows, datetime.fromisoformat('2026-08-25T00:00:00+09:00'), 'replay')
        self.assertEqual(result['ma20'], 20)
        self.assertEqual(result['return_5d_pct'], 0)
        self.assertEqual(result['volume_ratio_20d'], 1)

    def test_missing_and_stale_prices(self):
        rows = self.records()
        asof = datetime.fromisoformat('2026-09-20T00:00:00+09:00')
        self.assertEqual(indicators(rows, asof, 'replay')['age_calendar_days'], 20)
        rows[-1][0]['C'] = None
        with self.assertRaises(ValueError):
            indicators(rows, asof, 'replay')
