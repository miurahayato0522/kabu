from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock
from system_budget import DailyBudget
from system_data import encode
from system_news import NewsStore
from news_ai import open_cache,analyze


class NewsIntegrationTests(unittest.TestCase):
    def test_time_gating_and_missing_distinguished_from_absence(self):
        store = NewsStore()
        f, ids = store.features('72030','2025-01-01T16:00:00+09:00')
        self.assertEqual((f['news_present'],f['news_missing']), (0,1))
        store.coverage = ('2025-01-01T00:00:00+09:00','2025-01-02T00:00:00+09:00')
        f, ids = store.features('72030','2025-01-01T16:00:00+09:00')
        self.assertEqual((f['news_present'],f['news_missing']), (0,0))
        item = dict(news_id='a',event_id='e',source={'symbols':['7203']},
            first_seen_at='2025-01-01T15:00:00+09:00',started_at='2025-01-01T15:01:00+09:00',
            available_at='2025-01-01T17:00:00+09:00',published_at='2025-01-01T14:00:00+09:00',
            status='ok',result={},numbers=None,quality=[])
        store.items = [item]
        self.assertEqual(store.features('72030','2025-01-01T16:00:00+09:00')[0]['news_missing'],1)
        self.assertEqual(store.features('72030','2025-01-01T18:00:00+09:00')[1],['e'])
        store.items.append(dict(item,news_id='b'))
        self.assertEqual(store.features('72030','2025-01-01T18:00:00+09:00')[0]['news_count'],1)

    def test_budget_reservation_unknown_usage_and_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)/'budget.json'
            config.write_text(encode(dict(ledger='cost.db',daily_usd=.01,rates_per_million={'fake':[1,1]})),encoding='utf-8')
            b = DailyBudget(config)
            payload = {'model':'fake','max_output_tokens':100}
            b.reserve('a',payload)
            with self.assertRaises(ValueError):
                b.reserve('a',payload)
            b.reserve('b',payload)
            with self.assertRaises(ValueError):
                b.reserve('c',payload)
            b.settle('a',payload,{'input_tokens':10,'output_tokens':10})
            b.reserve('c',payload)
            with closing(sqlite3.connect(b.path)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM api_costs').fetchone()[0],3)

    def test_budget_reject_never_calls_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=Path(tmp)/'budget.json'
            config.write_text(encode(dict(ledger='cost.db',daily_usd=0,rates_per_million={'gpt-5-nano':[1,1]})),encoding='utf-8')
            caller=Mock()
            with closing(open_cache(Path(tmp)/'ai.db')) as db:
                with self.assertRaises(ValueError):
                    analyze(db,{'id':'a','title':'トヨタ 決算','symbols':['7203']},{'7203':'トヨタ'},'not-a-key',caller=caller,budget=DailyBudget(config))
                caller.assert_not_called()
                self.assertEqual(db.execute('SELECT count(*) FROM ai_analyses').fetchone()[0],0)


if __name__ == '__main__':
    unittest.main()
