import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock
from contextlib import closing

from news_collector import parse_feed, connect, save, collect, read_news


def rss(title='業績修正', stamp='Fri, 18 Sep 2026 06:00:00 GMT'):
    return f'''<rss><channel><item><title>{title}</title>
      <link>https://example.com/news/1</link><guid>item1</guid>
      <pubDate>{stamp}</pubDate><source>企業発表</source></item></channel></rss>'''.encode()


class NewsTests(unittest.TestCase):
    def test_dates_and_invalid_item(self):
        rows, rejected = parse_feed(rss())
        self.assertEqual(rows[0]['published_at'], '2026-09-18T06:00:00+00:00')
        self.assertEqual(rejected, 0)
        self.assertIsNone(parse_feed(rss(stamp='bad'))[0][0]['published_at'])
        self.assertEqual(parse_feed(rss(stamp=''))[0][0]['date_quality'], 'missing')
        self.assertEqual(parse_feed(rss().replace(b'https://example.com/news/1', b'javascript:bad'))[1], 1)

    def test_non_rss_and_entities_rejected(self):
        for payload in (b'<html/>', b'<!DOCTYPE rss><rss><channel/></rss>', b'broken'):
            with self.assertRaises(Exception):
                parse_feed(payload)

    def test_repeat_cross_symbol_and_revision_preserve_first_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'news.db'
            with closing(connect(path)) as db, db:
                rows, _ = parse_feed(rss())
                self.assertEqual(save(db, rows, '7203', 'トヨタ', '2026-09-19T00:00:00+00:00'), 1)
                self.assertEqual(save(db, rows, '7203', 'トヨタ', '2026-09-20T00:00:00+00:00'), 0)
                self.assertEqual(save(db, rows, '8306', 'UFJ', '2026-09-20T00:00:00+00:00'), 0)
                revised, _ = parse_feed(rss(title='訂正された発表'))
                save(db, revised, '7203', 'トヨタ', '2026-09-21T00:00:00+00:00')
                self.assertEqual(db.execute('SELECT count(*) FROM observations').fetchone()[0], 2)
            row = read_news(path, '8306')[0]
            self.assertEqual(row['title'], '業績修正')
            self.assertEqual(row['first_seen_at'], '2026-09-19T00:00:00+00:00')
            self.assertEqual(row['last_seen_at'], '2026-09-21T00:00:00+00:00')
            self.assertEqual(row['body_status'], 'not_fetched')
            self.assertEqual(read_news(path, '9984'), [])

    def test_partial_failure_logged_and_next_feed_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'news.db'
            fetch = Mock(side_effect=[OSError('offline'), rss()])
            self.assertEqual(collect(path, {'7203': 'トヨタ', '8306': 'UFJ'}, fetch, Mock()), 1)
            with closing(connect(path)) as db:
                self.assertEqual(db.execute('SELECT status FROM fetch_runs ORDER BY id').fetchall(), [('failed',), ('ok',)])
            self.assertEqual(len(read_news(path)), 1)


if __name__ == '__main__':
    unittest.main()
