from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock,patch
import exchange_calendars as xcals
from corporate_actions import ConfirmedActions,import_confirmations
from system_data import encode,stamp
from system_budget import DailyBudget,BudgetError
from system_runtime import Runtime,read_config,connect,process_lock
from system_paper import report
from news_ai import analyze,open_cache,make_request,parse_response,AnalysisValidationError
from news_collector import connect as news_connect,save
from system_integration import BusinessImpactStrategy
from system_strategy import Prediction,decide
from system_backtest import Account
from test_system_backtest import bars
from system_news import NewsStore


def response():
    title='トヨタ 営業利益予想を上方修正'
    result=dict(summary=title,category='業績修正',related_symbols=['7203'],evidence=['営業利益予想を上方修正'],
        facts=[title],unknowns=['本文未取得'],relations=[dict(symbol='7203',relation='直接',reason='トヨタの予想変更')],
        impacts=[dict(symbol='7203',short_term='ポジティブ',long_term='不明',importance='高',
                      reason='利益見通しの改善',quote='営業利益予想を上方修正',expectation_comparison='不明')])
    return dict(status='completed',model='fake',usage={'input_tokens':100,'output_tokens':200},
                output=[{'type':'message','content':[{'type':'output_text','text':encode(result)}]}])


class OperationTests(unittest.TestCase):
    def test_business_news_future_and_pending_are_not_used(self):
        when=stamp('2025-01-06T10:00:00+09:00')
        news=NewsStore(coverage=[when.isoformat(),when.isoformat()])
        result=json.loads(response()['output'][0]['content'][0]['text'])
        item=dict(news_id='a',event_id='e',source={'symbols':['7203']},first_seen_at=when.isoformat(),
                  started_at=when.isoformat(),available_at=(when+timedelta(minutes=1)).isoformat(),
                  published_at=when.isoformat(),status='ok',quality=[],result=result,analysis_id='hash')
        news.items=[item]
        self.assertFalse(news.assessment('72030',when.isoformat())['positive'])
        self.assertEqual(news.assessment('72030',when.isoformat())['status'],'UNKNOWN')
        item['available_at']=when.isoformat()
        self.assertTrue(news.assessment('72030',when.isoformat())['positive'])
        item['status']='failed'
        self.assertFalse(news.assessment('72030',when.isoformat())['positive'])
    def test_business_cases_keep_forecast_and_unknown_blocks(self):
        class Chart:
            name='fixed'
            direction=1
            def predict(self,h,at):
                return Prediction('72030',at,5,.03,'fixed',self.direction,[],[])
        class News:
            item=dict(status='AVAILABLE',positive=True,negative=False,severe_negative=False,events=[])
            def assessment(self,*args):return self.item
        chart,news=Chart(),News();strategy=BusinessImpactStrategy(chart,news)
        h=bars([100]*61)
        self.assertEqual(decide(strategy.predict(h),0)['action'],'BUY')
        news.item=dict(news.item,positive=False,negative=True)
        self.assertEqual(decide(strategy.predict(h),0)['action'],'NO_TRADE')
        news.item=dict(news.item,negative=False,status='NONE')
        self.assertEqual(decide(strategy.predict(h),0)['action'],'BUY')
        news.item=dict(news.item,status='UNKNOWN')
        self.assertEqual(decide(strategy.predict(h),0)['action'],'NO_TRADE')
        chart.direction=0;news.item=dict(news.item,status='AVAILABLE',positive=True)
        self.assertEqual(decide(strategy.predict(h),0)['action'],'NO_TRADE')
        news.item=dict(news.item,positive=False,negative=True,severe_negative=True)
        p=strategy.predict(h)
        self.assertEqual(p.value,.03)
        self.assertEqual(decide(p,100)['action'],'SELL')

    def test_confirmations_reverse_conflict_future_and_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'actions.db';now=stamp('2025-01-07T09:00:00+09:00')
            row=dict(symbol='7203',day='2025-01-07',kind='reverse',factor=2,verified_at=now.isoformat(),evidence='test fixture')
            import_confirmations(path,[row]);feed=ConfirmedActions(path)
            with self.assertRaises(ValueError):feed.between('7203','2025-01-06','2025-01-07',now-timedelta(seconds=1))
            a=Account(100000);a.positions={'72030':200};a.costs={'72030':20000}
            action=feed.between('7203','2025-01-06','2025-01-07',now)[0]
            a.split('72030',action['factor'],action['day'])
            self.assertEqual(a.positions['72030'],100);self.assertEqual(a.costs['72030'],20000)
            with self.assertRaises(ValueError):import_confirmations(path,[dict(row,factor=3)])
            import_confirmations(path,[dict(row,day='2025-01-08',kind='unsupported',factor=1)])
            with self.assertRaises(ValueError):feed.between('7203','2025-01-07','2025-01-08',now+timedelta(days=1))

    def test_impact_schema_cache_and_quote_validation(self):
        article=dict(id='a',title='トヨタ 営業利益予想を上方修正',symbols=['7203'],analyze_impact=True)
        names={'7203':'トヨタ'}
        self.assertIn('impacts',make_request(article,names)['text']['format']['schema']['required'])
        with tempfile.TemporaryDirectory() as tmp,closing(open_cache(Path(tmp)/'ai.db')) as db:
            caller=Mock(return_value=response())
            self.assertEqual(analyze(db,article,names,'fake',caller=caller),'ok')
            self.assertEqual(analyze(db,article,names,'fake',caller=caller),'cached:ok')
            caller.assert_called_once()
            row=db.execute('SELECT version,result_json FROM ai_analyses').fetchone()
            self.assertEqual(row[0],'headline-v3-impact')
            self.assertEqual(json.loads(row[1])['impacts'][0]['short_term'],'ポジティブ')
            bad=response();result=json.loads(bad['output'][0]['content'][0]['text'])
            result['impacts'][0]['quote']='本文にない文'
            bad['output'][0]['content'][0]['text']=encode(result)
            with self.assertRaises(AnalysisValidationError):parse_response(bad,article,names)

    def test_monthly_budget_survives_restart_and_rolls_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json'
            path.write_text(encode(dict(ledger='cost.db',daily_usd=1,monthly_jpy=1,jpy_per_usd=150,rates_per_million={'fake':[1,1]})),encoding='utf-8')
            now=[stamp('2025-01-01T00:00:00+00:00')]
            budget=DailyBudget(path,clock=lambda:now[0]);payload={'model':'fake','max_output_tokens':100}
            budget.reserve('a',payload)
            now[0]+=timedelta(days=1)
            with self.assertRaises(BudgetError):DailyBudget(path,clock=lambda:now[0]).reserve('b',payload)
            now[0]=stamp('2025-02-01T00:00:00+00:00')
            DailyBudget(path,clock=lambda:now[0]).reserve('b',payload)

    def test_runtime_pipeline_restart_split_and_missing_confirmation(self):
        """Entire loop uses synthetic RSS/ticks/prices and a fake LLM; no network."""
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            c=read_config(Path(__file__).resolve().parents[1]/'config/paper.json')
            c['symbols']={'7203':'トヨタ'};c['topics']=[];c['network_enabled']=True;c['dry_run']=False
            c['paths']={k:str(root/(k+'.db')) for k in c['paths']}
            c['budget_file']=str(root/'budget.json')
            Path(c['budget_file']).write_text(encode(dict(ledger='cost.db',daily_usd=1,monthly_jpy=500,jpy_per_usd=150,
                rates_per_million={c['llm']:[1,1]})),encoding='utf-8')
            days=[str(d.date()) for d in xcals.get_calendar('XTKS',start='2025-01-01',end='2025-07-01').sessions]
            today=days[75];clock=[stamp(today+'T10:00:00+09:00')]
            publication=clock[0].isoformat()
            def news(runtime,db):
                with closing(news_connect(c['paths']['news'])) as news_db,news_db:
                    for ident in ('a','b'):
                        article=dict(id=ident,title='トヨタ 営業利益予想を上方修正',url='https://example.com/'+ident,
                            publisher='fixture',published_at=publication,published_raw='fixture',date_quality='ok',guid=ident)
                        save(news_db,[article],'7203','トヨタ',clock[0].isoformat())
            def daily(runtime,db):
                with closing(sqlite3.connect(c['paths']['prices'])) as prices,prices:
                    prices.execute('CREATE TABLE IF NOT EXISTS daily_prices(code TEXT,day TEXT,source TEXT,fetched_at TEXT,payload TEXT,PRIMARY KEY(code,day))')
                    for i,day in enumerate(days):
                        if day>=str(clock[0].date()):break
                        price=101 if i>=74 else 100
                        row=dict(Code='72030',Date=day,O=price,H=price,L=price,C=price,Vo=10000,AdjFactor=1,ExRT='')
                        prices.execute('INSERT OR IGNORE INTO daily_prices VALUES (?,?,?,?,?)',('72030',day,'jquants_v2',day+'T16:00:00+09:00',encode(row)))
            def prices(runtime,db):
                with closing(sqlite3.connect(c['paths']['ticks'])) as ticks,ticks:
                    ticks.execute('CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,received_at TEXT,environment TEXT,source TEXT,payload TEXT)')
                    price=50.5 if str(clock[0].date())>=days[76] else 101
                    ticks.execute('INSERT INTO events(received_at,environment,source,payload) VALUES (?,?,?,?)',
                        (clock[0].isoformat(),'production','snapshot',encode(dict(Symbol='7203',Exchange=1,CurrentPrice=price,CurrentPriceTime=clock[0].isoformat()))))
            def confirm(day,kind='none',factor=1):
                import_confirmations(c['paths']['actions'],[dict(symbol='7203',day=day,kind=kind,factor=factor,
                    verified_at=clock[0].isoformat(),evidence='synthetic fixture confirmation')])
            confirm(today)
            caller=Mock(return_value=response())
            hooks=dict(news=news,daily=daily,prices=prices,documents=lambda r,d:None,llm=caller)
            with patch.dict('os.environ',{'OPENAI_API_KEY':'fake-test-key'}),patch('news_ai.now',side_effect=lambda:clock[0].isoformat()):
                Runtime(c,hooks,lambda:clock[0]).cycle()
                self.assertEqual(caller.call_count,1) # same content at two URLs
                clock[0]+=timedelta(seconds=60)
                Runtime(c,hooks,lambda:clock[0]).cycle() # restart; not due for another RSS/LLM job
                state=report(c['paths']['ledger'])[-1]
                self.assertEqual(state['positions']['72030'],100)
                self.assertEqual(len(state['fills']),1)
                before=state['equity']
                clock[0]=stamp(days[76]+'T10:00:00+09:00')
                confirm(days[76],'split',.5)
                Runtime(c,hooks,lambda:clock[0]).cycle()
                state=report(c['paths']['ledger'])[-1]
                self.assertEqual(state['positions']['72030'],200)
                self.assertAlmostEqual(state['equity'],before)
                self.assertAlmostEqual(state['holdings']['72030']['average_cost'],101*1.0005/2)
                clock[0]=stamp(days[77]+'T10:00:00+09:00')
                Runtime(c,hooks,lambda:clock[0]).cycle()
                self.assertEqual(report(c['paths']['ledger'])[-1]['kind'],'ERROR')
                with closing(sqlite3.connect(c['paths']['ledger'])) as db:
                    self.assertEqual(json.loads(db.execute("SELECT payload FROM system_state WHERE key='account'").fetchone()[0])['account']['positions']['72030'],200)
                self.assertEqual(caller.call_count,1)
            with closing(connect(c['paths']['runtime'])) as db:
                self.assertEqual(db.execute("SELECT status FROM jobs WHERE name='paper'").fetchone()[0],'failed')

    def test_dry_run_never_invokes_llm_or_network_and_lock_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=read_config(Path(__file__).resolve().parents[1]/'config/paper.json')
            c['paths']={k:str(Path(tmp)/(k+'.db')) for k in c['paths']}
            caller=Mock(side_effect=AssertionError('network must not run'))
            now=lambda:stamp('2025-01-06T20:00:00+09:00')
            article=dict(id='fixture',title='トヨタ 営業利益予想を上方修正',symbols=['7203'],analyze_impact=True)
            with patch('news_collector.fetch',caller),patch('news_ai.call_openai',caller),patch('system_runtime.candidates',return_value=[article]):
                Runtime(c,clock=now).cycle()
            caller.assert_not_called()
            with closing(connect(c['paths']['runtime'])) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM analysis_queue WHERE status='pending'").fetchone()[0],1)
            with process_lock(Path(tmp)/'lock'):
                with self.assertRaises(OSError):
                    with process_lock(Path(tmp)/'lock'):pass
            with process_lock(Path(tmp)/'lock'):pass

    def test_failed_analysis_not_retried_and_loop_continues(self):
        from news_ai import APIError
        with tempfile.TemporaryDirectory() as tmp:
            c=read_config(Path(__file__).resolve().parents[1]/'config/paper.json')
            c['paths']={k:str(Path(tmp)/(k+'.db')) for k in c['paths']}
            c['network_enabled']=True;c['dry_run']=False;c['symbols']={'7203':'トヨタ'}
            c['budget_file']=str(Path(tmp)/'budget.json')
            Path(c['budget_file']).write_text(encode(dict(ledger='cost.db',daily_usd=1,monthly_jpy=500,jpy_per_usd=150,rates_per_million={c['llm']:[1,1]})),encoding='utf-8')
            now=[stamp('2025-01-06T20:00:00+09:00')]
            article=dict(id='fixture',title='トヨタ 営業利益予想を上方修正',symbols=['7203'],analyze_impact=True)
            caller=Mock(side_effect=APIError('HTTP 429'))
            collected=Mock()
            hooks=dict(news=lambda r,d:collected(),daily=lambda r,d:None,documents=lambda r,d:None,llm=caller)
            with patch('system_runtime.candidates',return_value=[article]),patch.dict('os.environ',{'OPENAI_API_KEY':'fake-test-key'}):
                Runtime(c,hooks,lambda:now[0]).cycle()
                now[0]+=timedelta(minutes=16)
                Runtime(c,hooks,lambda:now[0]).cycle()
            self.assertEqual(caller.call_count,1);self.assertEqual(collected.call_count,2)
            with closing(connect(c['paths']['runtime'])) as db:
                self.assertEqual(db.execute('SELECT status FROM analysis_queue').fetchone()[0],'failed')


if __name__=='__main__':unittest.main()
