from contextlib import closing
from datetime import timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from e2e_preflight import build
from news_ai import open_cache
from system_data import encode,stamp
from system_integration import BusinessImpactStrategy
from system_news import NewsStore
from test_system_backtest import bars
from test_system_integration import FixedChart
from system_runtime import read_config


class E2EPipelineTests(unittest.TestCase):
    def test_company_name_free_industry_article_reaches_verified_candidate_stage(self):
        from company_graph import CompanyIndex,discover
        company=dict(symbol='1001',name='採掘検証社',aliases=[],industry='rare_earth',business='採掘',relations=[dict(
            topic='rare_earth',role='採掘',business='採掘',state='確認済み',direct_business='あり',
            source='https://example.test/official',quote='採掘事業',verified_at='2025-01-01T00:00:00+09:00',revenue_ratio=None,profit_ratio=None,ratio_evidence='')])
        article=dict(title='大規模なレアアース資源を新規発見',body='',published_at='2025-01-06T09:00:00+09:00')
        candidates=discover(article,['rare_earth'],CompanyIndex([company]))
        self.assertEqual(candidates[0]['symbol'],'1001')
        self.assertFalse(candidates[0]['named_in_news'])
        self.assertTrue(candidates[0]['relevance']['eligible'])

    def test_quote_check_handles_closed_fresh_and_stale_without_writing(self):
        from quote_check import check
        c=read_config('config/paper.json');c['symbols']={'7203':'A','8306':'B'}
        closed=check(c,stamp('2025-01-05T10:00:00+09:00'),Mock())
        self.assertEqual(closed['status'],'WAITING_MARKET')
        now=stamp('2025-01-06T10:00:00+09:00');client=Mock()
        client.board.side_effect=lambda code:dict(Symbol=code,Exchange=1,CurrentPrice=100,CurrentPriceTime=now.isoformat())
        self.assertEqual(check(c,now,client)['status'],'PASS')
        client.board.side_effect=lambda code:dict(Symbol=code,Exchange=1,CurrentPrice=100,CurrentPriceTime=(now-timedelta(seconds=61)).isoformat())
        self.assertEqual(check(c,now,client)['status'],'BLOCKED')

    def test_saved_ai_result_reaches_business_impact_strategy(self):
        now=stamp('2025-01-06T10:00:00+09:00');article=dict(id='saved-direct',title='トヨタ 業績修正',
            symbols=['7203'],published_at=(now-timedelta(minutes=10)).isoformat(),first_seen_at=(now-timedelta(minutes=9)).isoformat(),
            observed_at=(now-timedelta(minutes=9)).isoformat(),body='営業利益予想を修正',body_status='registered')
        result=dict(summary='fixture',category='業績修正',related_symbols=['7203'],evidence=['営業利益予想を修正'],unknowns=[],facts=['fixture'],
            relations=[dict(symbol='7203',relation='直接',reason='企業自身')],numbers=[],
            impacts=[dict(symbol='7203',short_term='ポジティブ',long_term='不明',importance='高',reason='fixture',quote='営業利益予想を修正',expectation_comparison='不明')])
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'ai.sqlite3'
            with closing(open_cache(path)) as db,db:
                db.execute('INSERT INTO ai_analyses(request_hash,article_id,version,started_at,finished_at,status,request_json,source_json,result_json,usage_json,response_model) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    ('hash','saved-direct','body-v4-impact',(now-timedelta(minutes=8)).isoformat(),(now-timedelta(minutes=7)).isoformat(),'ok','{}',encode(article),encode(result),'{}','fixture'))
            news=NewsStore(path,[(now-timedelta(minutes=9)).isoformat(),now.isoformat()])
            prediction=BusinessImpactStrategy(FixedChart(),news).predict(bars([100]*70),now.isoformat())
            self.assertIn('event:',','.join(prediction.refs))
            self.assertIn('positive business news',' '.join(prediction.reasons))

    def test_preflight_is_read_only_and_reports_dry_run_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=read_config('config/paper.json');c['paths']={k:str(Path(tmp)/(k+'.sqlite3')) for k in c['paths']}
            c['budget_file']=str(Path(tmp)/'budget.json');Path(c['budget_file']).write_text(encode(dict(ledger='cost.sqlite3',daily_usd=1,monthly_jpy=500,jpy_per_usd=150,rates_per_million={})),encoding='utf-8')
            c['network_enabled']=False;c['dry_run']=True
            before={k:Path(v).exists() for k,v in c['paths'].items()}
            result=build(c,stamp('2025-01-06T20:00:00+09:00'))
            self.assertEqual(next(x for x in result['stages'] if x['name']=='news_ai')['status'],'BLOCKED')
            self.assertFalse(result['e2e_executable'])
            self.assertEqual(before,{k:Path(v).exists() for k,v in c['paths'].items()})
