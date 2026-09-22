from contextlib import closing
from io import BytesIO
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pypdf import PdfWriter
from news_documents import snapshot
from news_pdf import archive_fetch, extract, page_numbers, validate_url

URL = 'https://global.toyota/pages/global_toyota/ir/financial-results/example.pdf'


def pdf(*texts):
    writer = PdfWriter()
    for text in texts:
        writer.add_blank_page(width=200, height=200)
        # add_text_annotation keeps the generated test simple; text extraction is mocked below.
    out = BytesIO(); writer.write(out)
    return out.getvalue()


class PDFTests(unittest.TestCase):
    def test_url_page_and_encryption_validation(self):
        self.assertEqual(validate_url(URL), URL)
        for url in ('https://evil.test/a.pdf', 'http://global.toyota/a.pdf', URL + '?a=1',
                    'https://global.toyota/a.html', 'https://user@global.toyota/a.pdf'):
            with self.assertRaises(ValueError): validate_url(url)
        self.assertEqual(page_numbers('1,3', 3), [1,3])
        for value in ('', '0', '1,1', '4', 'one'):
            with self.assertRaises(ValueError): page_numbers(value, 3)
        writer=PdfWriter();writer.add_blank_page(width=100,height=100);writer.encrypt('secret')
        protected=BytesIO();writer.write(protected)
        with self.assertRaises(ValueError):extract(protected.getvalue(),'1')

    def test_archive_registers_selected_pages_and_preserves_history(self):
        raw = pdf('one', 'two')
        with tempfile.TemporaryDirectory() as tmp, \
             patch('news_pdf.extract', side_effect=[(2, [(1,'2025年2月5日\n資料'),(2,'営業利益 前回 43,000億円 今回 47,000億円')]), (2,[(1,'2025年2月5日\n資料'),(2,'営業利益')])]):
            docs, archive = Path(tmp)/'docs.db',Path(tmp)/'archive.db'
            first = archive_fetch(URL,docs,archive,'7203','テスト資料','1,2',lambda _:raw)
            self.assertTrue(first['inserted'])
            article=snapshot(docs)[0]
            self.assertIn('[PDF 2ページ]',article['body'])
            self.assertEqual(article['published_date'],'2025-02-05')
            self.assertEqual(article['source_method'],'official_pdf_pages')
            with closing(sqlite3.connect(archive)) as db:
                self.assertEqual(db.execute('SELECT status,page_count,selected_pages FROM fetches').fetchone(),('ok',2,'1,2'))

    def test_failure_is_archived_without_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs, archive=Path(tmp)/'docs.db',Path(tmp)/'archive.db'
            with self.assertRaises(ValueError):archive_fetch(URL,docs,archive,'7203','x','1',lambda _:b'not-pdf')
            self.assertFalse(docs.exists())
            with closing(sqlite3.connect(archive)) as db:
                self.assertEqual(db.execute('SELECT status FROM fetches').fetchone()[0],'failed')


if __name__ == '__main__':unittest.main()
