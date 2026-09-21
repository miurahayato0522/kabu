from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from news_documents import register, snapshot
from news_ai import analyze, open_cache, parse_response, make_request, main
from news_market import build


class BodyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'documents.db'
        self.names = {'7203': 'トヨタ自動車'}
        self.body = 'テスト資料。トヨタ自動車の2026年の売上予想は100億円です。'
        self.args = (self.path, self.body, 'テスト資料', 'https://example.com/test', ['7203'])

    def test_duplicate_preserves_receipt_and_revision_keeps_history(self):
        self.assertTrue(register(*self.args)[1])
        original = snapshot(self.path)[0]
        self.assertFalse(register(*self.args)[1])
        self.assertEqual(snapshot(self.path)[0], original)
        register(self.path, self.body + '訂正。', *self.args[2:])
        self.assertEqual(len(snapshot(self.path)), 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM documents').fetchone()[0], 2)
        for kw in ({'published_at': '2026-09-21T10:00:00'}, {'kind': 'invalid'}):
            with self.assertRaises(ValueError):
                register(*self.args, **kw)

    def test_validation_cache_and_market_integration(self):
        register(*self.args, published_at='2026-09-21T09:00:00+09:00')
        article = snapshot(self.path)[0]
        result = dict(summary='テスト資料の売上予想', category='決算', related_symbols=['7203'],
                      evidence=['売上予想は100億円'], unknowns=[], facts=['売上予想の記載'],
                      relations=[dict(symbol='7203', relation='直接', reason='企業名の明記')],
                      numbers=[dict(label='売上予想', value='100', unit='億円', period='2026年', quote='2026年の売上予想は100億円')])
        def response():
            return dict(status='completed', output=[dict(type='message', content=[dict(type='output_text', text=json.dumps(result))])])
        self.assertEqual(parse_response(response(), article, self.names), result)
        self.assertTrue(make_request(article, self.names)['text']['format']['strict'])
        cache = Path(self.temp.name) / 'ai.db'
        with closing(open_cache(cache)) as db:
            caller = Mock(return_value=response())
            self.assertEqual(analyze(db, article, self.names, 'secret', caller=caller), 'ok')
            self.assertEqual(analyze(db, article, self.names, 'secret', caller=caller), 'cached:ok')
            caller.assert_called_once()
        result['numbers'][0]['value'] = '200'
        with self.assertRaises(ValueError):
            parse_response(response(), article, self.names)
        result['numbers'][0]['value'] = '100'
        result['evidence'] = ['本文にない引用']
        with self.assertRaises(ValueError):
            parse_response(response(), article, self.names)
        prices = Path(self.temp.name) / 'prices.db'
        with closing(sqlite3.connect(prices)) as db, db:
            db.execute('CREATE TABLE daily_prices (code TEXT,day TEXT,fetched_at TEXT,payload TEXT,source TEXT)')
            db.execute('INSERT INTO daily_prices VALUES (?,?,?,?,?)', ('72030','2026-09-18',article['observed_at'],'{}','yahoo_reconstructed_v1'))
        with patch('news_market.indicators', side_effect=ValueError('テスト：日足不足')):
            report = build(cache, prices)
        item = report['items'][0]
        self.assertEqual(item['analysis_version'], 'body-v1')
        self.assertGreaterEqual(item['decision_at'], article['body_received_at'])
        self.assertFalse(any('旧AI形式' in reason for reason in item['reasons']))

    def test_preview_does_not_send_body(self):
        register(*self.args)
        config = Path(self.temp.name) / 'names.json'
        config.write_text(json.dumps({'symbols': self.names}), encoding='utf-8')
        with patch('news_ai.call_openai') as api, patch('news_ai.getpass.getpass') as key:
            self.assertEqual(main(['preview', '--body', '--documents-db', str(self.path), '--config', str(config)]), 0)
            api.assert_not_called()
            key.assert_not_called()


if __name__ == '__main__':
    unittest.main()
