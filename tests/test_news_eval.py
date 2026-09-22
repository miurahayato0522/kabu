import copy
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from news_ai import open_cache
from news_eval import DATASET, comparison, digest, load_cases, main, read_reviews, render, report
from news_documents import snapshot


class EvalTests(unittest.TestCase):
    def setUp(self):
        self.dataset = load_cases(DATASET)
        self.case = self.dataset['cases'][1]
        self.source = dict(body=self.case['body'], title=self.case['title'],url=self.case['url'],symbols=['7201'])
        self.result = dict(numbers=[dict(label='営業利益',role=role,value=value,unit='億円',period='2024年度通期',
                                        quote=self.case['body'],period_quote=self.case['body'])
                                   for role,value in [('変更前','6,000'),('変更後','5,000')]],
                           relations=[dict(symbol='7201',relation='直接',reason='日産の業績修正')])

    def test_match_and_detect_wrong_expected_period_and_value(self):
        self.assertTrue(comparison(self.case,self.result,self.source)['match'])
        for field,value in [('period','2025年度通期'),('before_yen','500000000000'),('revision_pct',None)]:
            case = copy.deepcopy(self.case)
            case['expected'][field] = value
            self.assertFalse(comparison(case,self.result,self.source)['match'])

    def test_observed_forecast_labels_compare_without_changing_expected(self):
        self.result['numbers'][0]['label'] = '営業利益見通し（前回）'
        self.result['numbers'][1]['label'] = '営業利益見通し（今回）'
        self.assertTrue(comparison(self.case,self.result,self.source)['match'])

    def test_pending_review_stale_source_and_latest_failed_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)/'ai.db'
            with closing(open_cache(cache)) as db, db:
                db.execute('INSERT INTO ai_analyses (request_hash,article_id,version,started_at,status,source_json,result_json,request_json,response_model) VALUES (?,?,?,?,?,?,?,?,?)',
                           ('one','id','body-v3','2026-09-22','ok',json.dumps(self.source),json.dumps(self.result),'{"model":"gpt-5-mini"}','gpt-5-mini-test'))
            dataset = dict(self.dataset,cases=[self.case])
            self.assertEqual(report(dataset,cache,{},'gpt-5-mini')['items'][0]['status'],'暫定一致（期待値未確認）')
            reviewed = {digest(self.case): {'reviewer':'test'}}
            self.assertEqual(report(dataset,cache,reviewed,'gpt-5-mini')['items'][0]['status'],'一致')
            self.assertEqual(report(dataset,cache,reviewed,'gpt-5-nano')['items'][0]['status'],'未分析')
            modified = copy.deepcopy(dataset)
            modified['cases'][0]['body'] += '訂正'
            self.assertEqual(report(modified,cache,reviewed,'gpt-5-mini')['items'][0]['status'],'未分析')
            modified = copy.deepcopy(dataset)
            modified['cases'][0]['expected']['scope'] += '要照合'
            self.assertIsNone(report(modified,cache,reviewed,'gpt-5-mini')['items'][0]['human_review'])
            with closing(open_cache(cache)) as db, db:
                db.execute("INSERT INTO ai_analyses SELECT 'two',article_id,version,'2026-09-23',finished_at,'failed',request_json,source_json,NULL,usage_json,response_id,response_model,'test error',diagnostic_json FROM ai_analyses WHERE request_hash='one'")
            self.assertEqual(report(dataset,cache,reviewed,'gpt-5-mini')['items'][0]['status'],'分析失敗・未完了')

    def test_prepare_separates_expected_and_report_escapes_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp)/'docs.db'
            self.assertEqual(main(['prepare','--documents-db',str(docs)]),0)
            for article in snapshot(docs):
                self.assertNotIn('expected',article)
                self.assertIsNone(article['published_at'])
            value = report(self.dataset,Path(tmp)/'missing.db',{},'gpt-5-mini')
            self.assertTrue(all(i['status']=='未分析' for i in value['items']))
            value['items'][0]['case']['title'] = '<script>alert(1)</script>'
            self.assertNotIn('<script>',render(value))
            reviews = Path(tmp)/'reviews.db'
            self.assertEqual(main(['review','--case',self.case['id'],'--reviewer','unit-test','--reviews-db',str(reviews)]),0)
            self.assertIn(digest(self.case),read_reviews(reviews))


if __name__ == '__main__':
    unittest.main()
