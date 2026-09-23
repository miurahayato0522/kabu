from contextlib import closing
from datetime import timedelta
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock,patch
from urllib.parse import urlsplit,parse_qs
import exchange_calendars as xcals
from system_data import stamp,encode
from system_runtime import read_config,connect,Runtime,collect_topics
from system_evening import build,save,auto_report
from system_status import snapshot,sessions,display
from system_queue import refresh_queue,preview
from system_strategy import Prediction
from news_collector import feed_url,collect,connect as news_connect,save as save_news


class EveningTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.c=read_config(Path(__file__).resolve().parents[1]/'config/paper.json')
        self.c.update(symbols={'7203':'トヨタ'},topics=[],network_enabled=False,dry_run=True)
        self.c['paths']={k:str(self.root/(k+'.sqlite3')) for k in self.c['paths']}
        self.c['budget_file']=str(self.root/'budget.json')
        Path(self.c['budget_file']).write_text(encode(dict(ledger='cost.sqlite3',daily_usd=1,
            monthly_jpy=500,jpy_per_usd=150,rates_per_million={self.c['llm']:[.1,.4]})),encoding='utf-8')
        self.now=stamp('2025-01-31T18:00:00+09:00')

    def prices(self,end='2025-01-31',fetched=None):
        cal=xcals.get_calendar('XTKS',start='2024-10-01',end=end)
        with closing(sqlite3.connect(self.c['paths']['prices'])) as db,db:
            db.execute('CREATE TABLE daily_prices(code TEXT,day TEXT,source TEXT,fetched_at TEXT,payload TEXT)')
            for d in cal.sessions:
                day=str(d.date());close=100 if day<end else 110
                raw=dict(Code='72030',Date=day,O=close,H=close+1,L=close-1,C=close,Vo=1000,AdjFactor=1)
                db.execute('INSERT INTO daily_prices VALUES (?,?,?,?,?)',('72030',day,'jquants_v2',
                    fetched or '2025-01-31T17:00:00+09:00',encode(raw)))

    def healthy(self):
        with closing(connect(self.c['paths']['runtime'])) as db,db:
            db.execute("INSERT INTO jobs VALUES ('news',?,'ok',?)",(self.now.isoformat(),self.now.isoformat()))
            db.execute("INSERT INTO runtime_meta VALUES ('heartbeat',?)",(self.now.isoformat(),))

    def article(self,i=0,age=0):
        when=(self.now-timedelta(hours=age)).isoformat()
        return dict(id=str(i),url='https://example.com/'+str(i),title='トヨタ 上方修正 '+str(i),
                    symbols=['7203'],published_at=when,first_seen_at=when,observed_at=when,
                    analyze_impact=True,scope_tags=['company'])

    def test_evening_weekend_immutable_and_no_source_mutation(self):
        self.prices();self.healthy()
        before={p:p.read_bytes() for p in self.root.glob('*.sqlite3')}
        target,r=save(self.c,self.now,self.root/'report')
        self.assertEqual(r['next_session'],'2025-02-03')
        self.assertEqual(r['results'][0]['decision']['action'],'BUY')
        self.assertEqual(r['results'][0]['ma5'],102)
        self.assertEqual(r['results'][0]['news']['status'],'NONE')
        self.assertTrue(any('企業行動未確認' in x for x in r['status']['stop_new_reasons']))
        for path,content in before.items():self.assertEqual(path.read_bytes(),content)
        fixed=(target/'report.json').read_bytes()
        build(self.c,self.now+timedelta(hours=1))
        self.assertEqual((target/'report.json').read_bytes(),fixed)
        with self.assertRaises(FileExistsError):save(self.c,self.now,target)

    def test_holiday_and_weekend_use_latest_completed_day(self):
        self.prices('2025-01-10',fetched='2025-01-10T17:00:00+09:00')
        for day in ('2025-01-11','2025-01-12','2025-01-13'):
            r=build(self.c,stamp(day+'T18:00:00+09:00'))
            self.assertEqual(r['analysis_day'],'2025-01-10')
            self.assertEqual(r['next_session'],'2025-01-14')
            self.assertIsNone(r['results'][0]['decision']['prediction']['error'])

    def test_missing_unconfirmed_and_future_prices_never_buy(self):
        self.prices('2025-01-30');self.healthy()
        self.assertEqual(build(self.c,self.now)['results'][0]['decision']['action'],'ERROR')
        before=stamp('2025-01-31T15:45:00+09:00')
        self.assertEqual(build(self.c,before)['results'][0]['decision']['action'],'ERROR')
        with closing(sqlite3.connect(self.c['paths']['prices'])) as db,db:
            db.execute("UPDATE daily_prices SET fetched_at='2025-02-03T17:00:00+09:00'")
        r=build(self.c,self.now)
        self.assertEqual(r['inputs']['bars'],[])
        self.assertEqual(r['results'][0]['decision']['action'],'ERROR')

    def test_news_missing_veto_and_pending_escaped(self):
        self.prices()
        self.assertEqual(build(self.c,self.now)['results'][0]['decision']['action'],'NO_TRADE')
        self.healthy()
        a=self.article();a['title']='<script>alert(1)</script> トヨタ 上方修正'
        a.update(published_raw='',date_quality='ok',guid='',publisher='test')
        with closing(news_connect(self.c['paths']['news'])) as db,db:
            save_news(db,[a],'7203','トヨタ',self.now.isoformat())
        target,r=save(self.c,self.now,self.root/'xss')
        self.assertEqual(r['results'][0]['news']['status'],'UNKNOWN')
        self.assertEqual(r['results'][0]['decision']['action'],'NO_TRADE')
        markup=(target/'report.html').read_text(encoding='utf-8')
        self.assertNotIn('<script>',markup)
        self.assertIn('&lt;script&gt;',markup)
        self.assertIn('解析待ち',markup)

    def test_lightgbm_five_session_horizon_separate_from_sentiment(self):
        self.prices();self.healthy();self.c['chart']={'strategy':'lightgbm','model':'fake','threshold':0}
        class Model:
            name='chart:fake-version'
            metadata=dict(horizon=5,created_at='2025-01-01T00:00:00+00:00',kind='chart')
            def predict(self,h,at):return Prediction(h[-1].symbol,at,5,.031,self.name,1,[],[])
        with patch('system_model.ReturnModel',return_value=Model()):r=build(self.c,self.now)
        row=r['results'][0]
        self.assertEqual(row['chart_return'],.031)
        self.assertEqual(row['forecast_horizon_sessions'],5)
        self.assertIn('翌日リターンではありません',row['forecast_label'])
        self.assertEqual(row['news']['events'],[])

    def test_queue_old_publication_and_batch_bound_with_reasons(self):
        articles=[dict(self.article(i),published_at=(self.now-timedelta(days=3)).isoformat()) for i in range(80)]
        articles += [self.article(i) for i in range(80,100)]
        with closing(connect(self.c['paths']['runtime'])) as db:
            pending=refresh_queue(db,articles,self.c,self.now)
            self.assertEqual(len(pending),20)
            refresh_queue(db,articles,self.c,self.now)
            self.assertEqual(db.execute('SELECT count(*) FROM analysis_queue').fetchone()[0],100)
            self.assertEqual(db.execute('SELECT count(*) FROM queue_exclusions').fetchone()[0],80)
            v=preview(self.c,self.now)
            self.assertEqual(v['planned_send_count'],0)
            self.assertEqual(v['budget_permitted_count'],3)
            self.c['dry_run']=False;self.c['network_enabled']=True
            with patch.dict('os.environ',{'OPENAI_API_KEY':'fake-test'}),patch('system_runtime.candidates',return_value=articles),patch('news_ai.analyze',return_value='ok') as call:
                Runtime(self.c,clock=lambda:self.now).analysis_job(db)
            self.assertEqual(call.call_count,3)

    def test_market_search_not_exact_phrase_and_split_identifiers(self):
        self.assertEqual(parse_qs(urlsplit(feed_url('日銀 OR 政策金利',exact=False)).query)['q'][0],
                         '(日銀 OR 政策金利) when:7d')
        self.c['topics']=[dict(id='usa',scope='market',queries=['米国 CPI','FRB 政策金利'])]
        with patch('news_collector.collect',return_value=0) as call:collect_topics(self.c)
        queries=call.call_args.args[1]
        self.assertEqual(queries['@market:usa:0'],'米国 CPI')
        self.assertEqual(queries['@market:usa:1'],'FRB 政策金利')

    def test_empty_feed_differs_from_malformed_and_failed(self):
        path=self.root/'feeds.sqlite3'
        empty=b'<rss><channel></channel></rss>'
        self.assertEqual(collect(path,{'@market:test':'FRB'},fetcher=lambda u:empty),0)
        self.assertEqual(collect(path,{'@market:test':'FRB'},fetcher=lambda u:b'<html/>'),1)
        bad=b'<rss><channel><item><title>x</title></item></channel></rss>'
        self.assertEqual(collect(path,{'@market:test':'FRB'},fetcher=lambda u:bad),1)
        with closing(sqlite3.connect(path)) as db:
            rows=db.execute('SELECT status,received FROM fetch_runs ORDER BY id').fetchall()
        self.assertEqual(rows[0],('ok',0));self.assertEqual(rows[1][0],'failed');self.assertEqual(rows[2][0],'failed')

    def test_status_is_read_only_market_closed_not_price_error(self):
        self.healthy()
        with closing(connect(self.c['paths']['runtime'])) as db,db:
            db.execute("INSERT INTO jobs VALUES ('prices',?,'failed',?)",(self.now.isoformat(),self.now.isoformat()))
        before=Path(self.c['paths']['runtime']).read_bytes()
        s=snapshot(self.c,self.now)
        self.assertEqual(s['market_state'],'市場時間外')
        self.assertFalse(any('prices' in e for e in s['errors']))
        self.assertIn('DRY_RUN',display(s))
        self.assertIn('+09:00',display(s))
        self.assertEqual(Path(self.c['paths']['runtime']).read_bytes(),before)

    def test_auto_report_once_and_missing_close_waits(self):
        self.prices();self.healthy()
        with closing(connect(self.c['paths']['runtime'])) as db:
            with patch('system_evening.save',return_value=(self.root/'report',{})) as call:
                auto_report(self.c,db,self.now)
                auto_report(self.c,db,self.now+timedelta(minutes=6))
                self.assertEqual(call.call_count,1)

    def test_daily_refresh_includes_today_only_after_cutoff(self):
        self.c['network_enabled']=True
        runtime=Runtime(self.c,clock=lambda:self.now)
        with patch('yahoo_history.fetch_frame',return_value='frame'),patch('yahoo_history.convert',return_value=[]) as convert,patch('yahoo_history.save'):
            runtime.daily_job()
            self.assertEqual(str(convert.call_args.args[3]),'2025-02-01')
            runtime.clock=lambda:stamp('2025-01-31T15:59:00+09:00')
            runtime.daily_job()
            self.assertEqual(str(convert.call_args.args[3]),'2025-01-31')

    def test_daily_evening_failure_does_not_busy_retry(self):
        self.c['network_enabled']=True
        hooks={k:Mock() for k in ('news','documents','daily','prices','analysis','paper')}
        hooks['daily'].side_effect=ValueError('test failure')
        runtime=Runtime(self.c,hooks,clock=lambda:self.now)
        runtime.cycle();runtime.cycle()
        self.assertEqual(hooks['daily'].call_count,1)

    def test_future_analysis_not_used_or_archived_as_known(self):
        from news_ai import open_cache
        self.prices();self.healthy();a=self.article()
        result=dict(relations=[dict(symbol='7203',relation='直接')],
            impacts=[dict(symbol='7203',short_term='ポジティブ',long_term='不明',importance='高',reason='fixture',quote='上方修正')])
        with closing(open_cache(Path(self.c['paths']['ai']))) as db,db:
            db.execute('''INSERT INTO ai_analyses(request_hash,article_id,version,started_at,finished_at,status,source_json,result_json,response_model)
                VALUES (?,?,?,?,?,?,?,?,?)''',('r','0','headline-v3-impact',self.now.isoformat(),
                (self.now+timedelta(minutes=1)).isoformat(),'ok',encode(a),encode(result),'fake'))
        r=build(self.c,self.now)
        self.assertEqual(r['results'][0]['decision']['action'],'NO_TRADE')
        self.assertEqual(r['inputs']['news'],[])
        with closing(sqlite3.connect(self.c['paths']['ai'])) as db,db:
            db.execute('UPDATE ai_analyses SET finished_at=?',(self.now.isoformat(),))
        r=build(self.c,self.now)
        self.assertEqual(r['results'][0]['decision']['action'],'BUY')
        self.assertEqual(len(r['inputs']['news']),1)

    def test_no_database_report_is_error_without_creating_sources(self):
        target,r=save(self.c,self.now,self.root/'missing')
        self.assertEqual(r['results'][0]['decision']['action'],'ERROR')
        self.assertTrue((target/'report.html').is_file())
        for path in self.c['paths'].values():self.assertFalse(Path(path).exists())

    def test_missing_news_publication_is_unknown_not_no_news(self):
        self.prices();self.healthy();a=self.article()
        a.update(published_at=None,published_raw='',date_quality='missing',guid='',publisher='test')
        with closing(news_connect(self.c['paths']['news'])) as db,db:
            save_news(db,[a],'7203','トヨタ',self.now.isoformat())
        r=build(self.c,self.now)
        self.assertEqual(r['results'][0]['news']['status'],'UNKNOWN')
        self.assertEqual(r['results'][0]['decision']['action'],'NO_TRADE')
