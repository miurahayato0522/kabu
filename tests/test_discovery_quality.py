import io
import json
from contextlib import redirect_stdout,closing
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
import unittest
import test_discovery as fixtures
from company_graph import discover,companies,local_topics
from discovery_relevance import classify,event_key
from discovery_display import selected,summary,plan
from news_discovery import process,candidate_rows,main,sources
from discovery_integration import DiscoveryNews
from news_collector import connect,save

class QualityTests(unittest.TestCase):
    def setUp(self):
        self.h=fixtures.DiscoveryTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.c=self.h.c;self.now=self.h.now

    def test_generic_ai_does_not_imply_semiconductor_equipment(self):
        self.assertNotIn('semiconductor',local_topics('生成AIの活用で事務を効率化'))
        c=self.h.company('8035','装置会社','semiconductor','装置供給')
        f=discover({'title':'AIサービスを開発','body':''},['semiconductor'],[c])[0]
        self.assertEqual(f['relevance']['kind'],'industry');self.assertFalse(f['relevance']['eligible'])
        self.assertNotIn('ai',local_topics('DAIKINの決算'))

    def test_named_company_can_use_verified_business_without_industry_keyword(self):
        c=self.h.company('1001','会社テスト','banking','金融')
        f=discover({'title':'会社テストの決算'},[],[c])[0]
        self.assertEqual(f['relevance']['kind'],'company')
        self.assertTrue(f['relevance']['eligible'])

    def test_name_product_supply_chain_and_ambiguity(self):
        c=self.h.company('1001','NTT','telecom','その他');c['relations'][0]['quote']='通信ネットワーク'
        f=discover({'title':'NTTデータの新事業'},['telecom'],[c])[0]
        self.assertEqual(f['relevance']['kind'],'unknown');self.assertFalse(f['relevance']['eligible'])
        c['name']='通信会社'
        f=discover({'title':'通信ネットワークの整備'},['telecom'],[c])[0]
        self.assertEqual(f['relevance']['kind'],'product')
        f=discover({'title':'通信会社の決算'},['telecom'],[c])[0]
        self.assertEqual(f['relevance']['kind'],'company')
        c=self.h.company('1001','銀行社','banking','金融')
        f=discover({'title':'政策金利を変更'},['banking'],[c])[0]
        self.assertEqual(f['relevance']['kind'],'supply_chain')

    def test_exact_event_dedup_across_publishers(self):
        a=self.h.source('日銀の政策金利変更 - 媒体A');a['publisher']='媒体A'
        b=dict(a,id='b',publisher='媒体B',title='日銀の政策金利変更 - 媒体B',url='https://example.com/b')
        with closing(connect(self.c['paths']['news'])) as db,db:
            save(db,[a,b],'@market:test','日銀',(self.now-timedelta(minutes=30)).isoformat())
        self.assertEqual(event_key(a),event_key(b))
        self.assertEqual(len(sources(self.c,self.now)),1)

    def test_old_publication_not_new_material(self):
        self.h.source();self.h.run_ai();rows=candidate_rows(self.c,self.now)
        assessment=DiscoveryNews(rows,self.c).assessment('10010',(self.now+timedelta(days=2)).isoformat(),True)
        self.assertFalse(assessment['positive'])

    def test_plan_is_read_only_and_cache_reduces_estimate(self):
        self.h.source()
        with patch('news_ai.call_openai',side_effect=AssertionError('paid')):
            p=plan(self.c,self.now)
        self.assertEqual(p['known_uncached_requests'],1)
        self.assertGreater(p['known_request_estimated_usd'],0)
        self.h.run_ai();p=plan(self.c,self.now)
        self.assertEqual(p['known_uncached_requests'],0)

    def test_summary_bound_and_filters(self):
        self.h.source();process(self.c,self.now);rows=candidate_rows(self.c,self.now)
        args=SimpleNamespace(symbol='1001',name=None,industry='rare_earth',status='pending',direction='不明',relation='supply_chain',watch='existing',since=None,until=None)
        self.assertEqual(len(selected(rows,args)),1)
        out=io.StringIO()
        with redirect_stdout(out):summary(rows*100,self.c,self.now,2)
        self.assertEqual(out.getvalue().count('公表:'),2)
        self.assertIn('待ち',out.getvalue())

    def test_json_cli_and_url_preview_do_not_call_api(self):
        article=self.h.source()
        with patch('system_runtime.read_config',return_value=self.c),patch('news_discovery.datetime') as dt,patch('news_ai.call_openai',side_effect=AssertionError('paid')):
            dt.now.return_value=self.now
            out=io.StringIO()
            with redirect_stdout(out):main(['api-preview','--url',article['url'],'--json'])
            self.assertEqual(len(json.loads(out.getvalue())['articles']),1)

    def test_confirmed_industry_only_is_not_sent(self):
        self.h.source('レアアースについて雑談')
        result,caller=self.h.run_ai()
        self.assertEqual(caller.call_count,1)
        self.assertTrue(all(r['review_reason']=='relevance_unconfirmed' for r in candidate_rows(self.c,self.now)))

    def test_mock_real_text_report_is_explicit_and_does_not_write_ai_cache(self):
        from discovery_check import prepare,mock_report
        self.h.source();folder=prepare(self.c,self.h.root/'mock-check',1,self.now)
        with patch('news_ai.call_openai',side_effect=AssertionError('paid')):
            output=mock_report(folder,self.now)
        report=json.loads((output/'report.json').read_text(encoding='utf-8'))
        self.assertTrue(report['discovered_companies'])
        self.assertTrue(all(x['impact']['model']=='MOCK-no-api' for x in report['discovered_companies']))
        self.assertFalse((folder/'discovery.sqlite3').exists())
        self.assertIn('判断精度未検証',(output/'report.html').read_text(encoding='utf-8'))
