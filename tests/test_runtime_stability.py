from contextlib import closing
from datetime import date,timedelta
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock,patch
import pandas as pd
from daily_updates import update,latest
from yahoo_history import convert,save
from jquants_history import HistoryError
from system_data import stamp
from system_runtime import Runtime,read_config,connect
from system_paper import step
from action_candidates import observe,store,report
from corporate_actions import ConfirmedActions,import_confirmations


def frame(close=100):
    return pd.DataFrame(dict(Open=[100],High=[100],Low=[100],Close=[close],Volume=[1000],
        **{'Stock Splits':[0],'Dividends':[0]}),index=pd.date_range('2026-09-24',periods=1,tz='Asia/Tokyo'))


class StabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.c=read_config('config/paper.json')
        self.c['paths']={k:str(self.root/(k+'.sqlite3')) for k in self.c['paths']}
        self.c['symbols']={'7203':'A','8306':'B'}
        self.c['network_enabled']=True;self.c['dry_run']=True;self.c['evening_report']=False
        self.now=stamp('2026-09-26T01:00:00+09:00')

    def test_partial_update_preserves_failed_symbol_and_redacts_remote(self):
        rows=convert(frame(),'72030',date(2024,6,27),date(2026,9,26))
        save(self.c['paths']['prices'],'72030',rows)
        get=Mock(side_effect=[RuntimeError('secret=do-not-print'),frame()])
        result=update(self.c,self.now-timedelta(days=1),fetch=get,pause=lambda n:None)
        self.assertEqual(result['status'],'partial')
        self.assertNotIn('do-not-print',json.dumps(result))
        self.assertEqual(latest(self.c['paths']['prices'],'72030'),'2026-09-24')
        self.assertEqual(latest(self.c['paths']['prices'],'83060'),'2026-09-24')
        self.assertEqual(result['symbols'][0]['stage'],'fetch')

    def test_stale_response_not_success(self):
        result=update(self.c,self.now,['7203'],fetch=lambda *a:frame(),pause=lambda n:None)
        self.assertEqual(result['status'],'failed')
        self.assertIn('2026-09-25',result['symbols'][0]['reason'])
        self.assertFalse(Path(self.c['paths']['prices']).exists())

    def test_invalid_direct_save_and_sql_failure_preserve_original(self):
        rows=convert(frame(),'72030',date(2024,6,27),date(2026,9,26))
        path=self.c['paths']['prices'];save(path,'72030',rows)
        with self.assertRaises(HistoryError):save(path,'72030',[dict(rows[0],C=float('nan'))])
        with self.assertRaises(sqlite3.IntegrityError):save(path,'72030',rows+rows)
        with closing(sqlite3.connect(path)) as db:
            values=db.execute('SELECT day,payload FROM daily_prices').fetchall()
            self.assertEqual(len(values),1);self.assertEqual(json.loads(values[0][1])['C'],100)

    def test_backup_has_consistent_copy_and_config(self):
        from operations_backup import backup
        c=self.c.copy();c['base_dir']=str(self.root)
        c['budget_file']=str(self.root/'missing_budget.json')
        cfg=self.root/'config.json';cfg.write_text(json.dumps(c),encoding='utf-8')
        with closing(connect(c['paths']['runtime'])) as db:
            db.execute("INSERT INTO runtime_meta VALUES ('test','value')");db.commit()
        folder=backup(cfg,self.root/'backup')
        with closing(sqlite3.connect(folder/'runtime.sqlite3')) as db:
            self.assertEqual(db.execute("SELECT value FROM runtime_meta WHERE key='test'").fetchone()[0],'value')
        self.assertEqual((folder/'paper.json').read_bytes(),cfg.read_bytes())

    def test_nan_zero_negative_volume_and_bad_split_stop(self):
        for column,value in [('Close',float('nan')),('Volume',0),('Volume',-1),('Stock Splits',float('nan')),('Low',101)]:
            f=frame();f.loc[f.index[0],column]=value
            with self.assertRaises(HistoryError):convert(f,'72030',date(2024,6,27),date(2026,9,26))

    def test_failure_does_not_remove_old_rows_or_mark_latest(self):
        rows=convert(frame(),'72030',date(2024,6,27),date(2026,9,26))
        save(self.c['paths']['prices'],'72030',rows)
        before=Path(self.c['paths']['prices']).read_bytes()
        result=update(self.c,self.now,['7203'],fetch=lambda *a:frame(float('nan')),pause=lambda n:None)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['symbols'][0]['last_good'],'2026-09-24')
        self.assertIn('fields=C',result['symbols'][0]['reason'])
        self.assertEqual(before,Path(self.c['paths']['prices']).read_bytes())
        with self.assertRaises(HistoryError):save(self.c['paths']['prices'],'72030',[dict(rows[0],Date='2026-09-25')])
        self.assertEqual(latest(self.c['paths']['prices'],'72030'),'2026-09-24')

    def test_scheduler_records_partial_and_next_attempt(self):
        result=dict(status='partial',symbols=[dict(symbol='72030',status='failed',stage='validate')])
        hooks={k:Mock() for k in ('news','documents','analysis','prices','paper')}
        with patch.object(Runtime,'daily_job',return_value=result):Runtime(self.c,hooks,clock=lambda:self.now).cycle()
        with closing(connect(self.c['paths']['runtime'])) as db:
            row=db.execute("SELECT status,next_at FROM jobs WHERE name='daily'").fetchone()
            self.assertEqual(row[0],'partial');self.assertGreater(stamp(row[1]),self.now)
            self.assertIn('validate',db.execute("SELECT detail FROM logs WHERE job='daily'").fetchone()[0])

    def test_real_price_adapter_authenticates_records_and_stale_fails(self):
        self.c['symbols']={'7203':'A'}
        r=Runtime(self.c,clock=lambda:self.now)
        client=Mock();client.board.return_value=dict(Symbol='7203',Exchange=1,CurrentPrice=100,CurrentPriceTime=self.now.isoformat())
        with patch('kabu_collector.KabuClient',return_value=client),patch.dict('os.environ',{'KABU_API_PASSWORD':'fake'}):
            r.price_job();client.authenticate.assert_called_once_with('fake')
            client.board.return_value['CurrentPriceTime']=(self.now-timedelta(minutes=2)).isoformat()
            with self.assertRaises(ValueError):r.price_job()
            self.assertIsNone(r.client)
        with closing(sqlite3.connect(self.c['paths']['ticks'])) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],1)

    def test_market_closed_before_account_access(self):
        path=self.root/'untouched.db'
        with self.assertRaisesRegex(ValueError,'Outside XTKS'):
            step(path,[],self.root/'missing.db',None,None,self.now)
        self.assertFalse(path.exists())

    def test_observations_no_events_never_confirm_absence(self):
        p=self.root/'obs.db';r=observe(frame(),'7203',date(2024,6,27),date(2026,9,26),self.now.isoformat())
        store(p,r);store(p,r)
        self.assertEqual(len(report(p)),1)
        self.assertFalse(report(p)[0]['absence_confirmed'])
        with self.assertRaises(sqlite3.Error):ConfirmedActions(p).between('7203','2026-09-24','2026-09-25',self.now)

    def test_split_observation_conflict_is_reported_not_promoted(self):
        f=frame();f.loc[f.index[0],'Stock Splits']=2
        p=self.root/'obs.db';a=self.root/'confirmed.db'
        r=observe(f,'7203',date(2024,6,27),date(2026,9,26),self.now.isoformat());store(p,r)
        import_confirmations(a,[dict(symbol='7203',day='2026-09-24',kind='none',factor=1,verified_at=self.now.isoformat(),evidence='synthetic')])
        event=report(p,a)[0]['events'][0]
        self.assertEqual(event['factor'],.5);self.assertIsNone(event['effective_day'])
        self.assertEqual(event['ledger_comparison'],'CONFLICT_MANUAL_REVIEW')
        self.assertEqual(ConfirmedActions(a).between('7203','2026-09-18','2026-09-24',self.now)[0]['kind'],'none')
        store(a.with_suffix('.observations.sqlite3'),r)
        with self.assertRaisesRegex(ValueError,'conflict'):
            ConfirmedActions(a).between('7203','2026-09-18','2026-09-24',self.now)
