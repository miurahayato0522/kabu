from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from news_collector import connect, parse_feed, save
from news_triage import classify, snapshot, build_report, write_report
from test_news import rss


class TriageTests(unittest.TestCase):
    def article(self, title, symbols=None):
        return {'id': 'id1', 'title': title, 'symbols': symbols or ['7203'],
                'date_quality': 'ok', 'url': 'https://example.com', 'publisher': 'source',
                'published_at': None, 'first_seen_at': '2026-09-20T00:00:00+00:00',
                'observed_at': '2026-09-20T00:00:00+00:00'}

    def test_evidence_and_multiple_categories(self):
        result = classify(self.article('トヨタ、決算で増益と自社株買いを発表'), {'7203': 'トヨタ自動車'})
        self.assertEqual(result['decision'], '確認候補')
        self.assertEqual(result['categories'], ['決算', '株主還元'])
        self.assertEqual(result['name_evidence']['7203'], ['トヨタ'])

    def test_ambiguous_and_irrelevant_preserved(self):
        self.assertEqual(classify(self.article('決算発表のお知らせ'), {})['decision'], '要確認')
        self.assertEqual(classify(self.article('トヨタの歴史'), {})['decision'], '保留')
        report = build_report([self.article('旅行の話題')], {})
        self.assertEqual(len(report['items']), 1)

    def test_latest_revision_and_analysis_does_not_mutate_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'news.db'
            with closing(connect(path)) as db, db:
                save(db, parse_feed(rss('トヨタ 上方修正'))[0], '7203', 'トヨタ', '2026-09-19T00:00:00+00:00')
                save(db, parse_feed(rss('訂正 トヨタ 下方修正'))[0], '7203', 'トヨタ', '2026-09-20T00:00:00+00:00')
            original = path.read_bytes()
            rows = snapshot(path)
            self.assertEqual(rows[0]['title'], '訂正 トヨタ 下方修正')
            self.assertEqual(rows[0]['first_seen_at'], '2026-09-19T00:00:00+00:00')
            report = build_report(rows, {'7203': 'トヨタ'})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(report['input_sha256'], build_report(rows, {'7203': 'トヨタ'})['input_sha256'])
            self.assertNotEqual(report['input_sha256'], build_report(rows, {'7203': 'Toyota'})['input_sha256'])

    def test_html_escapes_external_headlines_and_no_overwrite(self):
        report = build_report([self.article('<script>alert(1)</script> トヨタ 決算')], {})
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report'
            write_report(report, output)
            rendered = (output / 'report.html').read_text(encoding='utf-8')
            self.assertIn('&lt;script&gt;', rendered)
            self.assertNotIn('<script>alert(1)', rendered)
            with self.assertRaises(FileExistsError):
                write_report(report, output)


if __name__ == '__main__':
    unittest.main()
