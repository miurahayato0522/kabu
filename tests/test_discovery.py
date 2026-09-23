from contextlib import closing
from datetime import timedelta
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock,patch
import exchange_calendars as xcals
from company_graph import import_companies,companies,local_topics,discover
from discovery_ai import parse,PERIODS
from discovery_integration import DiscoveryNews,NewsOnlyResearch,report_rows
from discovery_outcomes import measure,collect,receipt_quote
from news_discovery import settings,process,candidate_rows,preview,for_horizon
from system_runtime import read_config
from system_data import encode,stamp,provider
from system_strategy import Prediction,decide
from system_integration import BusinessImpactStrategy
from news_collector import connect as news_connect,save as news_save


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.now=stamp('2025-01-06T10:00:00+09:00')
        self.c=read_config(Path(__file__).resolve().parents[1]/'config/paper.json')
        self.c.update(symbols={'1001':'採掘テスト社'},topics=[],dry_run=True,network_enabled=False)
        self.c['paths']={k:str(self.root/(k+'.db')) for k in self.c['paths']}
        self.c['budget_file']=str(self.root/'budget.json')
        self.c['discovery']={'max_calls':10,'max_candidates':5,'min_turnover':1,'allow_indirect':True}
        Path(self.c['budget_file']).write_text(encode(dict(ledger='budget.db',daily_usd=1,monthly_jpy=500,jpy_per_usd=150,
            rates_per_million={self.c['llm']:[.1,.2]})),encoding='utf-8')
        self.s=settings(self.c)
        self.records=[self.company('1001','採掘テスト社','rare_earth','採掘'),
            self.company('1002','精製テスト社','rare_earth','精製'),self.company('1003','利用テスト社','rare_earth','原材料利用'),
            self.company('1004','銀行テスト社','banking','金融'),self.company('1005','不動産テスト社','real_estate','不動産'),
            self.company('1006','輸出テスト社','export_manufacturing','製造')]
        import_companies(self.s['companies_db'],self.records,self.now-timedelta(days=1))

    def company(self,code,name,topic,role):
        when=(self.now-timedelta(days=2)).isoformat()
        return dict(symbol=code,name=name,aliases=[],industry=topic,business=role,updated_at=when,
            relations=[dict(topic=topic,role=role,business=role,state='確認済み',direct_business='あり',
                source='https://example.com/fixture/'+code,quote=role+'を行う架空企業',verified_at=when,
                revenue_ratio=None,profit_ratio=None,ratio_evidence='')])

    def source(self,title='日本近海でレアアース資源を新規発見。商業採掘に向けた調査を計画。',ident='a'):
        when=(self.now-timedelta(minutes=30)).isoformat()
        a=dict(id=ident,title=title,url='https://example.com/news/'+ident,publisher='架空試験',
               published_at=when,published_raw='',date_quality='ok',guid='')
        with closing(news_connect(self.c['paths']['news'])) as db,db:
            news_save(db,[a],'@market:resources','レアアース',when)
        return a

    def response(self,payload,key):
        data=json.loads(payload['input'][0]['content']);text=data['text']
        if payload['text']['format']['name']=='industry_event':
            result=dict(summary=text,kind='産業動向',industries=[dict(topic=t,direction='不明',mechanism='要確認',delay='unknown',quote=text) for t in local_topics(text)],
                direct_names=[],facts=[text],plans=[],unknowns=['架空試験の入力'])
        else:
            company=data['company'];role=data['matched_relations'][0]['role']
            direction={'採掘':'ポジティブ','精製':'中立','原材料利用':'ネガティブ'}.get(role,'不明')
            result=dict(symbol=company['symbol'],business=role,relation='直接' if data['named_in_news'] else '間接',
                short=direction,medium=direction,long='不明',importance='高',reason='役割別の架空影響',conditions=['商業化・契約等の確認'],
                news_quote=text,company_quote=data['matched_relations'][0]['quote'],unknowns=['今回の資源への参画権は不明'])
        return dict(status='completed',model='fake-test',usage={'input_tokens':50,'output_tokens':50},
            output=[dict(type='message',content=[dict(type='output_text',text=encode(result))])])

    def run_ai(self):
        self.c['dry_run']=False;self.c['network_enabled']=True
        caller=Mock(side_effect=self.response)
        with patch.dict('os.environ',{'OPENAI_API_KEY':'fake-test-key'}):
            result=process(self.c,self.now,caller=caller,clock=lambda:self.now)
        return result,caller

    def prices(self,end='2025-02-28',split=False):
        cal=xcals.get_calendar('XTKS',start='2024-10-01',end=end)
        with closing(sqlite3.connect(self.c['paths']['prices'])) as db,db:
            db.execute('CREATE TABLE daily_prices(code TEXT,day TEXT,source TEXT,fetched_at TEXT,payload TEXT)')
            for day in [str(d.date()) for d in cal.sessions]:
                close=50 if split and day>='2025-01-08' else 100
                factor=.5 if split and day=='2025-01-08' else 1
                raw=dict(Code='10010',Date=day,O=close,H=close+1,L=close-1,C=close,Vo=10000,AdjFactor=factor,ExRT='1' if factor!=1 else '0')
                db.execute('INSERT INTO daily_prices VALUES (?,?,?,?,?)',('10010',day,'jquants_v2',day+'T16:00:00+09:00',encode(raw)))

    def test_A_no_name_discovery_is_pending_and_never_expands_watchlist(self):
        self.source();original=encode(self.c)
        caller=Mock(side_effect=AssertionError('No API'))
        result=process(self.c,self.now,caller=caller,clock=lambda:self.now)
        caller.assert_not_called();self.assertEqual(result['api_calls'],0)
        rows=candidate_rows(self.c,self.now)
        self.assertEqual({r['symbol'] for r in rows},{'1001','1002','1003'})
        self.assertTrue(all(not r['named_in_news'] and r['impact']['status']=='pending' for r in rows))
        self.assertEqual(encode(self.c),original)
        self.assertFalse(Path(self.c['paths']['ledger']).exists())
        self.assertTrue(all('不明' in r['project_participation'] for r in rows))

    def test_B_roles_have_individual_impacts_no_probability(self):
        self.source('レアアース輸出規制が発表された')
        _,call=self.run_ai();rows=candidate_rows(self.c,self.now)
        signs={r['symbol']:r['impact']['result']['short'] for r in rows if r['symbol'] in ('1001','1002','1003')}
        self.assertEqual(signs,{'1001':'ポジティブ','1002':'中立','1003':'ネガティブ'})
        self.assertNotIn('probability',encode(rows))
        self.assertEqual(sum(json.loads(x.args[0]['input'][0]['content']).get('company') is None for x in call.call_args_list),1)

    def test_C_named_contract_and_indirect_company_distinguished(self):
        self.source('採掘テスト社がレアアース資源開発事業を受注')
        self.run_ai();rows=candidate_rows(self.c,self.now)
        named=[r['symbol'] for r in rows if r['named_in_news']]
        self.assertEqual(named,['1001'])
        self.assertEqual(next(r for r in rows if r['symbol']=='1002')['impact']['result']['relation'],'間接')

    def test_D_policy_topics_find_banks_property_exporters(self):
        self.source('日銀が政策金利を変更')
        process(self.c,self.now)
        self.assertEqual({r['symbol'] for r in candidate_rows(self.c,self.now)},{'1004','1005','1006'})

    def test_E_unknown_news_does_not_invent_companies(self):
        self.source('詳細は明らかにされていない')
        self.run_ai()
        self.assertEqual(candidate_rows(self.c,self.now),[])

    def test_versioned_registry_asof_and_unverified_rejected(self):
        self.assertEqual(companies(self.s['companies_db'],self.now-timedelta(days=3)),[])
        changed=json.loads(encode(self.records[0]));changed['business']='更新';changed['updated_at']=self.now.isoformat()
        import_companies(self.s['companies_db'],[changed],self.now)
        old=companies(self.s['companies_db'],self.now-timedelta(hours=1))
        self.assertEqual(next(c for c in old if c['symbol']=='1001')['business'],'採掘')
        changed['relations'][0]['source']=''
        with self.assertRaises(ValueError):import_companies(self.s['companies_db'],[changed],self.now)

    def test_same_article_dedup_and_cache_company_revision_changes(self):
        self.source();self.source(ident='b')
        result,call=self.run_ai()
        self.assertEqual(call.call_count,4)
        with closing(sqlite3.connect(self.s['db'])) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM discovery_sources').fetchone()[0],1)
        _,again=self.run_ai();again.assert_not_called()
        changed=json.loads(encode(self.records[0]));changed['business']='採掘（情報更新）';changed['updated_at']=self.now.isoformat()
        import_companies(self.s['companies_db'],[changed],self.now)
        _,again=self.run_ai();self.assertEqual(again.call_count,1)

    def test_batch_bound_and_budget_block_persists_pending(self):
        self.source();self.c['discovery']['max_calls']=1
        result,call=self.run_ai();self.assertEqual(call.call_count,1)
        self.assertTrue(all(r['impact']['status']=='pending' for r in candidate_rows(self.c,self.now)))
        budget=json.loads(Path(self.c['budget_file']).read_text());budget['daily_usd']=0
        Path(self.c['budget_file']).write_text(encode(budget),encoding='utf-8')
        _,call=self.run_ai();call.assert_not_called()
        with closing(sqlite3.connect(self.s['db'])) as db:
            self.assertGreater(db.execute("SELECT count(*) FROM discovery_ai WHERE status='pending' AND error='budget_blocked'").fetchone()[0],0)

    def test_failed_response_not_retried_and_unknown_not_analyzed(self):
        self.source();self.c.update(dry_run=False,network_enabled=True)
        call=Mock(return_value={'status':'incomplete'})
        with patch.dict('os.environ',{'OPENAI_API_KEY':'fake'}):
            process(self.c,self.now,caller=call,clock=lambda:self.now)
            process(self.c,self.now,caller=call,clock=lambda:self.now)
        call.assert_called_once()
        self.assertTrue(all(r['impact']['status']=='pending' for r in candidate_rows(self.c,self.now)))

    def test_future_analysis_and_company_info_unavailable(self):
        self.source();self.run_ai()
        self.assertEqual(candidate_rows(self.c,self.now-timedelta(minutes=1)),[])
        row=candidate_rows(self.c,self.now)[0]
        news=DiscoveryNews([row],self.c)
        self.assertEqual(news.assessment('10010',(self.now-timedelta(minutes=1)).isoformat(),True)['status'],'UNKNOWN')
        row['impact']['finished_at']=(self.now+timedelta(hours=1)).isoformat()
        self.assertFalse(DiscoveryNews([row],self.c).assessment('10010',self.now.isoformat(),True)['positive'])

    def test_fabricated_evidence_extra_probability_and_direct_claim_rejected(self):
        self.source();self.run_ai();r=candidate_rows(self.c,self.now)[0]
        result=r['impact']['result']
        def envelope(v):return {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':encode(v)}]}]}
        with self.assertRaises(ValueError):parse(envelope(dict(result,probability=.9)),r['article'],r)
        with self.assertRaises(ValueError):parse(envelope(dict(result,company_quote='捏造')),r['article'],r)
        with self.assertRaises(ValueError):parse(envelope(dict(result,relation='直接')),r['article'],r)

    def test_six_integration_cases_keep_chart_return(self):
        self.source();self.run_ai();r=next(r for r in candidate_rows(self.c,self.now) if r['symbol']=='1001')
        class Chart:
            name='fixture';direction=1
            def predict(self,h,at):return Prediction('10010',at,5,.04,self.name,self.direction,[],[])
        chart=Chart();news=DiscoveryNews([r],self.c);strategy=BusinessImpactStrategy(chart,news,{'allow_indirect':True})
        when=self.now.isoformat()
        self.assertEqual(decide(strategy.predict([],when),0)['action'],'BUY')
        r['impact']['result']['short']='ネガティブ';r['impact']['result']['importance']='低'
        self.assertEqual(decide(strategy.predict([],when),0)['action'],'NO_TRADE')
        chart.direction=0;r['impact']['result']['short']='ポジティブ'
        self.assertEqual(decide(strategy.predict([],when),0)['action'],'NO_TRADE')
        chart.direction=1;r['relation_state']='不明'
        self.assertEqual(decide(strategy.predict([],when),0)['action'],'NO_TRADE')
        r['relation_state']='確認済み';r['impact']['result'].update(short='ネガティブ',importance='高')
        self.assertEqual(decide(strategy.predict([],when),100)['action'],'SELL')
        r['impact']['result']['short']='不明'
        p=strategy.predict([],when)
        self.assertEqual(decide(p,0)['action'],'NO_TRADE');self.assertEqual(p.value,.04)

    def test_receipt_price_not_fabricated_and_returns_split_adjusted(self):
        self.source();self.run_ai();self.prices(split=True)
        row=next(r for r in candidate_rows(self.c,self.now) if r['symbol']=='1001')
        outcome=measure(row,self.c,stamp('2025-02-28T18:00:00+09:00'))
        self.assertIsNone(outcome['observed_receipt_price'])
        self.assertIsNone(outcome['observed_price_returns'])
        self.assertEqual(outcome['daily_anchor']['day'],'2025-01-06')
        self.assertAlmostEqual(outcome['daily_proxy_returns']['5']['return_value'],0)
        self.assertAlmostEqual(outcome['daily_proxy_returns']['20']['return_value'],0)
        early=measure(row,self.c,stamp('2025-01-07T18:00:00+09:00'))
        self.assertIsNone(early['daily_proxy_returns']['5']['return_value'])

    def test_news_only_requires_observed_response_and_cost_liquidity(self):
        self.source();self.run_ai();self.prices(end='2025-01-10')
        rows=candidate_rows(self.c,self.now)
        strategy=NewsOnlyResearch(DiscoveryNews(rows,self.c),self.c)
        bars=provider(self.c['paths']['prices']).bars(['1001'])
        p=strategy.predict(bars,'2025-01-10T16:00:00+09:00')
        self.assertEqual(p.direction,0);self.assertIsNone(p.value)

    def test_evening_includes_new_unwatched_candidates_and_escapes(self):
        self.source('レアアース <script>alert(1)</script> 新規発見');process(self.c,self.now)
        from system_evening import build,html
        r=build(self.c,self.now)
        self.assertEqual(len(r['discovered_companies']),3)
        self.assertEqual(sum(not x['watched'] for x in r['discovered_companies']),2)
        self.assertNotIn('<script>',html(r));self.assertIn('&lt;script&gt;',html(r))
        self.assertTrue(all(not x['execution_eligible'] for x in r['discovered_companies']))

    def test_missing_prices_outcome_is_null_not_zero(self):
        self.source();self.run_ai()
        result=collect(self.c,self.now)
        self.assertGreater(result['saved'],0)
        self.assertTrue(all(x['daily_proxy_returns'] is None for x in result['items']))

    def test_preview_does_not_create_discovery_database_or_call_api(self):
        self.source()
        p=preview(self.c,self.now)
        self.assertEqual(p['company_count'],6)
        self.assertFalse(Path(self.s['db']).exists())

    def test_period_config_rejects_overlap(self):
        self.c['discovery']['periods']={'short':[1,5],'medium':[3,20],'long':[120,None]}
        with self.assertRaises(ValueError):settings(self.c)

    def test_timeframe_routing_is_research_copy_only(self):
        self.c['discovery'].update(horizon='medium',timeframes={'medium':{'chart_model':'models/medium','risk':{'per_symbol':12345}}})
        original=encode(self.c)
        routed=for_horizon(self.c)
        self.assertEqual(routed['risk']['per_symbol'],12345)
        self.assertEqual(routed['chart']['strategy'],'lightgbm')
        self.assertTrue(Path(routed['chart']['model']).is_absolute())
        self.assertEqual(encode(self.c),original)

    def test_timeframe_invalid_risk_and_untrained_news_model_rejected(self):
        for frame in ({'risk':{'per_symbol':-1}},{'risk':{'stop_new':'false'}},{'news_model':'invented-model'}):
            self.c['discovery']['timeframes']={'short':frame}
            with self.assertRaises(ValueError):settings(self.c)

    def test_runtime_connects_discovery_without_expanding_paper_symbols(self):
        from system_runtime import Runtime
        self.source();self.c['discovery']['enabled']=True
        original=dict(self.c['symbols'])
        hooks={k:(lambda *args:None) for k in ('news','documents','daily','prices','analysis','paper')}
        hooks['discovery_llm']=Mock(side_effect=AssertionError('Paid API forbidden'))
        Runtime(self.c,hooks=hooks,clock=lambda:self.now).cycle()
        self.assertEqual(len(candidate_rows(self.c,self.now)),3)
        self.assertEqual(self.c['symbols'],original)
        hooks['discovery_llm'].assert_not_called()
        self.assertFalse(Path(self.c['paths']['ledger']).exists())

    def test_empty_available_price_history_is_reported_missing(self):
        self.source();self.run_ai()
        row=candidate_rows(self.c,self.now)[0]
        with patch('discovery_outcomes.provider') as factory:
            factory.return_value.bars.return_value=[]
            result=measure(row,self.c,self.now)
        self.assertEqual(result['error'],'prices_unavailable_or_invalid')
        self.assertIsNone(result['daily_proxy_returns'])
