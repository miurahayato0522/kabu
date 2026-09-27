from contextlib import closing
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest
import exchange_calendars as xcals
from system_data import encode,stamp
from system_backtest import Account
from system_paper import step,report,live_marks
from system_risk import RiskLimits
from corporate_actions import ConfirmedActions,import_confirmations
from test_system_backtest import bars
from test_system_integration import FixedChart


class PaperAccountTests(unittest.TestCase):
    def test_unconfirmed_unheld_symbol_is_blocked_without_stopping_confirmed_symbol(self):
        cal=xcals.get_calendar('XTKS',start='2025-01-01',end='2025-07-01')
        days=[str(d.date()) for d in cal.sessions];today=days[75]
        one=[replace(b,day=d,fetched_at=d+'T16:00:00+09:00',available_at=d+'T16:00:00+09:00')
             for b,d in zip(bars([100]*75),days[:75])]
        history=one+[replace(b,symbol='83060') for b in one]
        now=stamp(today+'T10:00:00+09:00')
        with tempfile.TemporaryDirectory() as tmp:
            ticks,ledger,actions=Path(tmp)/'ticks.db',Path(tmp)/'ledger.db',Path(tmp)/'actions.db'
            import_confirmations(actions,[dict(symbol='7203',day=today,kind='none',factor=1,
                verified_at=now.isoformat(),evidence='synthetic official review')])
            with closing(sqlite3.connect(ticks)) as db,db:
                db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,received_at TEXT,environment TEXT,source TEXT,payload TEXT)')
                def insert(at):
                    for symbol in ('7203','8306'):
                        db.execute('INSERT INTO events(received_at,environment,source,payload) VALUES (?,?,?,?)',
                            (at,'production','push',encode(dict(Symbol=symbol,Exchange=1,CurrentPrice=100,CurrentPriceTime=at))))
                    db.commit()
                insert(now.isoformat())
                first=step(ledger,history,ticks,FixedChart(),Account(100000),now,actions=ConfirmedActions(actions))
                insert((now+timedelta(seconds=2)).isoformat())
                result=step(ledger,history,ticks,FixedChart(),Account(100000),now+timedelta(seconds=3),actions=ConfirmedActions(actions))
            self.assertEqual(result['positions']['72030'],100)
            self.assertNotIn('83060',result['positions'])
            self.assertIn('83060',first['corporate_action_blocks'])
            blocked=next(d for d in first['decisions'] if d['prediction']['symbol']=='83060')
            self.assertEqual(blocked['action'],'NO_TRADE')

    def test_later_tick_only_persistence_and_stale_stop(self):
        cal=xcals.get_calendar('XTKS',start='2025-01-01',end='2025-07-01')
        days=[str(d.date()) for d in cal.sessions]
        today=days[75]
        history=[replace(b,day=d,fetched_at=d+'T16:00:00+09:00',available_at=d+'T16:00:00+09:00')
                 for b,d in zip(bars([100]*75),days[:75])]
        now=stamp(today+'T10:00:00+09:00')
        with tempfile.TemporaryDirectory() as tmp:
            ticks,ledger=Path(tmp)/'ticks.db',Path(tmp)/'ledger.db'
            with closing(sqlite3.connect(ticks)) as db,db:
                db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,received_at TEXT,environment TEXT,source TEXT,payload TEXT)')
                def insert(at):
                    db.execute('INSERT INTO events(received_at,environment,source,payload) VALUES (?,?,?,?)',
                        (at,'production','push',encode(dict(Symbol='7203',Exchange=1,CurrentPrice=100,CurrentPriceTime=at))))
                    db.commit()
                insert(now.isoformat())
                a=step(ledger,history,ticks,FixedChart(),Account(100000),now)
                self.assertEqual(len(a['fills']),0)
                b=step(ledger,history,ticks,FixedChart(),Account(100000),now+timedelta(seconds=1))
                self.assertEqual(len(b['fills']),0)
                insert((now+timedelta(seconds=2)).isoformat())
                c=step(ledger,history,ticks,FixedChart(),Account(100000),now+timedelta(seconds=3))
                self.assertEqual(c['positions']['72030'],100)
                self.assertEqual(len(c['fills']),1)
                self.assertEqual(len(report(ledger)),3)
                with self.assertRaises(ValueError):
                    step(ledger,history,ticks,FixedChart(),Account(100000),now+timedelta(minutes=5))
                self.assertEqual(len(report(ledger)),3)


if __name__=='__main__':
    unittest.main()
