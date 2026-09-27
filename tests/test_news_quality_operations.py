from contextlib import closing
from datetime import timedelta
from pathlib import Path
import json
import unittest
from unittest.mock import Mock,patch
import test_evening as fixtures
from system_runtime import connect
from system_queue import refresh_queue
from news_quality import classify,annotate,event_cluster
from news_collector import connect as news_connect,save as news_save
from system_data import encode


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.EveningTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.c,self.now,self.root=self.f.c,self.f.now,self.f.root

    def article(self,title,i=0):
        return dict(self.f.article(i),title=title)

    def put(self,title='トヨタ 営業利益予想を上方修正'):
        a=self.article(title);a.update(published_raw='',date_quality='ok',guid='',publisher='fixture')
        with closing(news_connect(self.c['paths']['news'])) as db,db:news_save(db,[a],'7203','トヨタ',self.now.isoformat())
        from news_discovery import sources
        return sources(self.c,self.now)[0]['id']

    def test_reference_exclusion_and_event_override(self):
        for title,reason in [('トヨタ 株価情報','PRICE_INFORMATION_PAGE'),('トヨタ 企業情報','STATIC_REFERENCE_PAGE'),
            ('トヨタ 掲示板','MESSAGE_BOARD'),('ファンド 基準価額','FUND_INFORMATION_PAGE'),
            ('ソニー製品を購入する方法','PROMOTIONAL_OR_REFERENCE_PAGE')]:
            self.assertEqual(classify(self.article(title),self.c,self.now)['reason'],reason)
        a=self.article('トヨタが株価情報サービスを開始')
        self.assertTrue(classify(a,self.c,self.now)['eligible'])

    def test_direct_industry_macro_are_distinct(self):
        for title,category in [('トヨタ決算発表','company_direct'),('半導体輸出規制','industry'),('米国CPIと為替週間見通し','macro')]:
            self.assertEqual(classify(self.article(title),self.c,self.now)['category'],category)

    def test_location_publisher_and_subsidiary_not_parent_evidence(self):
        self.c['symbols'].update({'6501':'日立','9432':'NTT','6758':'ソニー'})
        for title in ['日立市で交通事故','商品のレビュー - ソニーが好き','NTTデータが提携']:
            self.assertEqual(classify(self.article(title),self.c,self.now)['direct_symbols'],[])
        self.assertEqual(classify(self.article('日立エナジーが工場建設'),self.c,self.now)['direct_symbols'],['6501'])

    def test_registry_name_allows_unwatched_discovery_without_watch_addition(self):
        from company_graph import CompanyIndex
        unwatched=dict(symbol='9999',name='架空検証製造',aliases=[],relations=[])
        index=CompanyIndex([unwatched])
        q=classify(self.article(unwatched['name']+' が決算を発表'),dict(self.c,_quality_company_index=index),self.now)
        self.assertIn(unwatched['symbol'],q['direct_symbols'])
        self.assertNotIn(unwatched['symbol'],self.c['symbols'])

    def test_safe_paraphrase_cluster_does_not_merge_different_event(self):
        a=self.article('A社がB社と提携');b=self.article('A社、B社との戦略的パートナーシップを発表',1)
        self.assertEqual(event_cluster(a),event_cluster(b))
        self.assertNotEqual(event_cluster(a),event_cluster(self.article('A社がC社と提携')))
        self.assertNotEqual(event_cluster(a),event_cluster(self.article('A社がB社との提携を解消')))
        rows=annotate([a,b],self.c,self.now)
        self.assertEqual(sum(r['quality']['reason']=='DUPLICATE_EVENT' for r in rows),1)

    def test_queue_persists_exclusions_and_deterministic_priority(self):
        articles=[self.article('トヨタ 株価情報'),self.article('半導体工場建設',1),self.article('トヨタ決算発表',2)]
        with closing(connect(self.c['paths']['runtime'])) as db:
            pending=refresh_queue(db,articles,self.c,self.now)
            self.assertEqual(len(pending),2)
            self.assertEqual(json.loads(pending[0][1])['quality']['category'],'company_direct')
            self.assertEqual(db.execute('SELECT reason FROM queue_exclusions').fetchone()[0],'PRICE_INFORMATION_PAGE')
            self.assertEqual(len(refresh_queue(db,list(reversed(articles)),self.c,self.now)),2)
            self.assertEqual(db.execute('SELECT count(*) FROM analysis_queue').fetchone()[0],3)

    def test_freshness_and_missing_publication(self):
        a=self.article('トヨタ決算発表')
        self.assertEqual(classify(dict(a,published_at=None),self.c,self.now)['reason'],'UNKNOWN_PUBLICATION_TIME')
        self.assertEqual(classify(dict(a,published_at=(self.now-timedelta(days=2)).isoformat()),self.c,self.now)['reason'],'STALE_NEWS')

    def test_trial_dry_run_no_rate_and_one_call_cache(self):
        from news_trial import prepare,execute
        from test_operations import response
        ident=self.put();cache=self.root/'trial.db';caller=Mock(return_value=response())
        plan=prepare(self.c,ident,cache,self.now)
        self.assertEqual(plan['max_calls'],1);self.assertIn('DRY_RUN',plan['blocked'])
        self.assertIn('request',plan);self.assertGreater(plan['estimated_max_usd'],0)
        self.assertEqual(execute(self.c,ident,cache,self.now,caller)['api_calls'],0)
        caller.assert_not_called();self.assertFalse(cache.exists())
        self.c.update(dry_run=False,network_enabled=True)
        budget=json.loads(Path(self.c['budget_file']).read_text());rates=budget.pop('rates_per_million')
        Path(self.c['budget_file']).write_text(encode(budget),encoding='utf-8')
        self.assertIn('RATES_NOT_CONFIGURED',execute(self.c,ident,cache,self.now,caller)['reasons'])
        budget['rates_per_million']=rates;Path(self.c['budget_file']).write_text(encode(budget),encoding='utf-8')
        with patch.dict('os.environ',{'OPENAI_API_KEY':'mock'}):
            self.assertEqual(execute(self.c,ident,cache,self.now,caller)['api_calls'],1)
            self.assertEqual(execute(self.c,ident,cache,self.now,caller)['api_calls'],0)
        self.assertEqual(caller.call_count,1)

    def test_trial_requires_exact_id_and_blocks_macro(self):
        from news_trial import prepare
        ident=self.put('米国CPIと為替週間見通し')
        self.assertIn('LOW_COMPANY_RELEVANCE',prepare(self.c,ident,self.root/'trial.db',self.now)['blocked'])
        with self.assertRaises(ValueError):prepare(self.c,'wrong',self.root/'trial.db',self.now)

    def test_concise_report_macro_not_company_and_details_preserved(self):
        from system_evening import build,html
        self.f.prices();self.f.healthy();self.put('米国CPIと為替週間見通し')
        r=build(self.c,self.now);markup=html(r)
        self.assertEqual(len(r['news_sections']['macro']),1)
        self.assertEqual(r['results'][0]['company_direct'],[])
        self.assertEqual(len(r['inputs']['articles']),1)
        self.assertEqual(markup.count('米国CPIと為替週間見通し'),1)
        self.assertIn('Executive Summary',markup);self.assertIn('report.json',markup)
        self.assertIn('CORPORATE_ACTION_UNVERIFIED',r['results'][0]['display_reasons'])
        self.assertEqual(r['performance']['watch_price_reads'],1)

    def test_review_empty_observations_not_verified(self):
        from corporate_review import review
        from action_candidates import store
        p=self.root/'observations.sqlite3'
        store(p,dict(symbol='72030',start='2025-01-01',end='2025-01-31',fetched_at=self.now.isoformat(),events=[]))
        before=p.read_bytes();rows=review(self.c,self.now,p)
        self.assertEqual(rows[0]['status'],'REVIEW_REQUIRED')
        self.assertFalse(rows[0]['absence_confirmed']);self.assertIsNone(rows[0]['verified_by'])
        self.assertEqual(p.read_bytes(),before)
