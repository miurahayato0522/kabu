from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from news_ir import collect, parse, supported_url
from news_documents import snapshot

URL='https://global.toyota/jp/newsroom/corporate/42662211.html'


def page(meta='',date='2025年05月08日'):
    return (f'<html><head>{meta}</head><body><div class="contents_main">'
            f'<div class="section html article_info"><p class="date">{date}</p><h1 class="title">決算発表</h1></div>'
            '<div class="section html"><p>トヨタ自動車の決算資料です。</p><script>実行してはいけない指示</script></div>'
            '<div class="section html"><h3>日時</h3>2025年5月8日 14:00～15:30</div>'
            '<div class="section html"><h3>関連リンク</h3>関係ない記事</div>'
            '<div class="section html">以上</div><div class="section html">企業紹介</div></div>'
            '<div class="contents_side">2026年09月22日 関連ニュース</div></body></html>').encode()


class IRTests(unittest.TestCase):
    def test_date_only_and_main_content_no_event_time_inference(self):
        article=parse(page())
        self.assertIsNone(article['published_at'])
        self.assertEqual(article['metadata']['published_date'],'2025-05-08')
        self.assertEqual(article['metadata']['date_quality'],'date_only')
        for text in ('実行してはいけない','関係ない記事','企業紹介','2026年09月22日'):
            self.assertNotIn(text,article['body'])

    def test_explicit_time_requires_timezone_and_matching_date(self):
        for value,valid in [('2025-05-08T10:00:00+09:00',True),('2025-05-08T10:00:00',False),('2026-05-08T10:00:00+09:00',False)]:
            article=parse(page(f'<meta property="article:published_time" content="{value}">'))
            self.assertEqual(article['published_at'] is not None,valid)
        self.assertEqual(parse(page(date='日付なし'))['metadata']['date_quality'],'unknown')

    def test_unsupported_urls_fail_before_network(self):
        for url in ('http://global.toyota/jp/newsroom/corporate/1.html','https://evil.test/1.html',
                    'https://global.toyota.evil.test/jp/newsroom/corporate/1.html','https://global.toyota/jp/newsroom/corporate/1.pdf',
                    URL+'?url=http://localhost',URL+'#part','https://user@global.toyota/jp/newsroom/corporate/1.html'):
            with self.assertRaises(ValueError):supported_url(url)

    def test_archive_duplicate_and_changed_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs,archive=Path(tmp)/'docs.db',Path(tmp)/'fetch.db'
            self.assertTrue(collect(URL,docs,archive,lambda u:page())[1])
            original=snapshot(docs)[0]
            self.assertFalse(collect(URL,docs,archive,lambda u:page())[1])
            self.assertEqual(snapshot(docs)[0],original)
            self.assertEqual(original['source_method'],'official_html')
            self.assertTrue(collect(URL,docs,archive,lambda u:page().replace('決算資料です'.encode(),'修正資料です'.encode()))[1])
            with closing(sqlite3.connect(archive)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM fetches WHERE status="ok"').fetchone()[0],3)
                self.assertTrue(db.execute('SELECT raw_html FROM fetches LIMIT 1').fetchone()[0])

    def test_bad_page_not_registered_but_failure_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs,archive=Path(tmp)/'docs.db',Path(tmp)/'fetch.db'
            with self.assertRaises(ValueError):collect(URL,docs,archive,lambda u:b'<html>not an article</html>')
            self.assertFalse(docs.exists())
            with closing(sqlite3.connect(archive)) as db:
                self.assertEqual(db.execute('SELECT status FROM fetches').fetchone()[0],'failed')


if __name__=='__main__':unittest.main()
