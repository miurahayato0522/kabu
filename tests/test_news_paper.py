from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
import exchange_calendars as xcals
from news_paper import decision, observe, schedule, save, open_ledger, summarize


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,1,15,8,tzinfo=timezone.utc)
        cal=xcals.get_calendar('XTKS',start='2025-11-01',end='2026-01-15')
        days=[d.date().isoformat() for d in cal.sessions if str(d.date())<'2026-01-15'][-21:]
        self.records=[(dict(Date=d,O=100+i,C=101+i,Vo=1000+i,AdjFactor=1),self.now.isoformat()) for i,d in enumerate(days)]
        body='2027年3月期の営業利益予想を100億円から120億円に変更。'
        source=dict(symbols=['7203'],title='テスト',body=body,first_seen_at=self.now.isoformat(),observed_at=self.now.isoformat(),published_at=self.now.isoformat())
        result=dict(relations=[dict(symbol='7203',relation='直接')],numbers=[dict(label='営業利益',role=role,value=value,unit='億円',period='2027年3月期',quote=body,period_quote=body) for role,value in [('変更前','100'),('変更後','120')]])
        self.analysis=dict(article_id='a',request_hash='hash',version='body-v3',finished_at=self.now.isoformat(),source_json=json.dumps(source),result_json=json.dumps(result))

    def item(self):
        return decision(self.analysis,self.records,self.now,'yahoo_reconstructed_v1')

    def future(self,item):
        return [(dict(Date=d,O=100,C=110,Vo=1000,AdjFactor=1),self.now.isoformat()) for d in item['planned_sessions']]

    def test_recorded_only_and_news_filters(self):
        self.assertTrue(self.item()['combined_candidate'])
        late=[(r,(self.now+timedelta(days=1)).isoformat()) for r,_ in self.records]
        self.assertFalse(decision(self.analysis,late,self.now,'yahoo_reconstructed_v1')['chart_candidate'])
        source=json.loads(self.analysis['source_json']);source['published_at']='2024-01-01T00:00:00+00:00'
        self.analysis['source_json']=json.dumps(source)
        item=self.item()
        self.assertTrue(item['chart_candidate']);self.assertFalse(item['combined_candidate'])

    def test_calendar_waiting_missing_and_returns(self):
        holiday_schedule=schedule(datetime(2026,9,22,8,tzinfo=timezone.utc))
        self.assertEqual(holiday_schedule[0],'2026-09-24')
        self.assertEqual(len(holiday_schedule),20)
        item=self.item();rows=self.future(item)
        self.assertEqual(item['planned_sessions'][0],'2026-01-16')
        self.assertTrue(all(o['status']=='待機中' for o in observe(item,rows,self.now)))
        later=self.now+timedelta(days=60)
        evaluated=observe(item,rows,later)
        self.assertAlmostEqual(evaluated[0]['gross_return_pct'],10)
        self.assertLess(evaluated[0]['return_pct'],10)
        missing=observe(item,rows[1:],later)
        self.assertTrue(all(o['status']=='価格欠損' for o in missing))
        partial=observe(item,rows[:4]+rows[5:],later)
        self.assertEqual(partial[0]['status'],'評価済み');self.assertEqual(partial[1]['status'],'価格欠損')

    def test_split_adjustment_and_future_rows_not_read_early(self):
        item=self.item();rows=self.future(item)
        for r,_ in rows[1:]:r['C']=55
        rows[1][0].update(AdjFactor=0.5,ExRT='1')
        outcome=observe(item,rows,self.now+timedelta(days=60))
        self.assertAlmostEqual(outcome[1]['gross_return_pct'],10)
        future=[(r,(self.now+timedelta(days=100)).isoformat()) for r,_ in rows]
        self.assertTrue(all(o['status']=='価格欠損' for o in observe(item,future,self.now+timedelta(days=60))))

    def test_immutable_and_test_exclusion(self):
        item=self.item()
        with tempfile.TemporaryDirectory() as tmp,closing(open_ledger(Path(tmp)/'ledger.db')) as db:
            self.assertTrue(save(db,item))
            other=dict(item,decision_at='2099-01-01T00:00:00+00:00',combined_candidate=False)
            self.assertFalse(save(db,other))
            self.assertEqual(json.loads(db.execute('SELECT payload FROM decisions').fetchone()[0]),item)
        item['test_data']=True
        summary=summarize([dict(decision=item,outcomes=observe(item,self.future(item),self.now+timedelta(days=60)))])
        self.assertTrue(all(s['candidates']==0 and s['mean_return_pct'] is None for s in summary))


if __name__=='__main__':unittest.main()
