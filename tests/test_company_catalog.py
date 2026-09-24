import csv
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from datetime import timedelta
import test_discovery as fixtures
from company_catalog import parse_rows,import_file,latest,merge
from company_graph import companies,CompanyIndex,discover,import_companies
from discovery_check import prepare,report
from news_discovery import process,candidate_rows,settings,sources
from system_data import encode
from system_runtime import read_config


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.h=fixtures.DiscoveryTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.c=self.h.c;self.now=self.h.now;self.root=self.h.root

    def row(self,code='1001',name='採掘テスト社',sector='鉱業',market='プライム（内国株式）',day='20241230'):
        return {'日付':day,'コード':code,'銘柄名':name,'市場・商品区分':market,'33業種区分':sector}

    def file(self,rows,name='master.csv'):
        path=self.root/name
        with path.open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
        return path

    def test_codes_filters_and_duplicate_future_rejected(self):
        rows=[self.row(),self.row('123A','新規社'),self.row('10015','種類株'),self.row('9999','ETF',market='ETF・ETN')]
        self.assertEqual([x['symbol'] for x in parse_rows(rows,self.now)],['1001','123A'])
        for bad in ([self.row(),self.row()],[self.row(day='20300101')],[{'コード':'1001'}]):
            with self.assertRaises(ValueError):parse_rows(bad,self.now)

    def test_import_preserves_verified_and_records_provenance_asof(self):
        path=self.file([self.row(),self.row('1007','新規銀行','銀行業')])
        import_file(self.h.s['companies_db'],path,self.now)
        old=companies(self.h.s['companies_db'],self.now-timedelta(seconds=1))
        self.assertEqual(len(old),6)
        rows=companies(self.h.s['companies_db'],self.now)
        self.assertEqual(len(rows),2)
        self.assertTrue(any(r['state']=='確認済み' for r in rows[0]['relations']))
        self.assertEqual(rows[1]['relations'][0]['state'],'推定')
        self.assertEqual(rows[1]['listing']['asof'],'2024-12-30')
        revision=rows[0]['revision']
        import_file(self.h.s['companies_db'],path,self.now+timedelta(hours=1))
        self.assertEqual(companies(self.h.s['companies_db'],self.now+timedelta(hours=1))[0]['revision'],revision)

    def test_older_snapshot_rejected_and_name_change_does_not_inherit_evidence(self):
        import_file(self.h.s['companies_db'],self.file([self.row(name='別会社')]),self.now)
        row=companies(self.h.s['companies_db'],self.now)[0]
        self.assertFalse(any(r['state']=='確認済み' for r in row['relations']))
        with self.assertRaises(ValueError):import_file(self.h.s['companies_db'],self.file([self.row(day='20241129')],'old.csv'),self.now)

    def test_index_matches_scan_and_preserves_unknown_relations(self):
        registry=companies(self.h.s['companies_db'],self.now)
        a={'title':'銀行テスト社とレアアース','body':''}
        self.assertEqual(discover(a,['rare_earth'],registry),discover(a,['rare_earth'],CompanyIndex(registry)))
        self.assertEqual(CompanyIndex(registry).select('無関係',[]),[])

    def test_sector_only_not_sent_to_company_llm(self):
        import_file(self.h.s['companies_db'],self.file([self.row('1007','未知銀行','銀行業')]),self.now)
        self.h.source('日銀の政策金利')
        result,caller=self.h.run_ai()
        self.assertEqual(caller.call_count,1)
        r=candidate_rows(self.c,self.now)[0]
        self.assertEqual(r['review_reason'],'business_evidence_required')
        self.assertEqual(r['impact']['status'],'pending')

    def test_selection_and_saved_cap_are_bounded(self):
        self.h.source();self.h.source('銀行テスト社の決算','b')
        chosen=sources(self.c,self.now)[0]['id']
        self.c['discovery'].update(source_ids=[chosen],max_saved_candidates=1)
        process(self.c,self.now)
        rows=candidate_rows(self.c,self.now)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['article']['id'],chosen)

    def test_new_listing_company_revisions_and_candidates_are_unique(self):
        path=self.file([self.row('1007','新規銀行Ａ','銀行業'),self.row('1008','新規銀行Ｂ','銀行業')])
        import_file(self.h.s['companies_db'],path,self.now)
        rows=companies(self.h.s['companies_db'],self.now)
        self.assertEqual(len({r['revision'] for r in rows}),2)
        self.h.source('日銀の政策金利')
        result=process(self.c,self.now)
        saved=candidate_rows(self.c,self.now)
        self.assertEqual(result['candidates'],2)
        self.assertEqual(len(saved),2)
        self.assertEqual({r['symbol'] for r in saved},{'1007','1008'})

    def test_prepare_and_report_never_call_api_and_pending_not_pass(self):
        self.h.source();self.h.source('銀行テスト社の決算','b')
        folder=prepare(self.c,self.root/'check',1,self.now)
        c=read_config(folder/'config.json')
        self.assertTrue(c['dry_run']);self.assertFalse(c['network_enabled'])
        with patch('news_ai.call_openai',side_effect=AssertionError('No paid API')):
            process(c,self.now);output,result=report(folder,self.now)
        self.assertEqual(len(result['cases']),1)
        self.assertIn('AI未検証',result['cases'][0]['state'])
        self.assertIn('少数実ニュースの検証',(output/'report.html').read_text(encoding='utf-8'))
        self.assertFalse(Path(self.c['paths']['ledger']).exists())

    def test_mock_completed_chain_appears_in_evening_and_manual_review_required(self):
        self.h.source()
        folder=prepare(self.c,self.root/'check',1,self.now)
        c=read_config(folder/'config.json');c.update(dry_run=False,network_enabled=True)
        c['discovery']['max_calls']=10
        # Persist mock-only config for inspection; never run live calls in this test.
        (folder/'config.json').write_text(encode(dict(c,base_dir=str(self.root))),encoding='utf-8')
        with patch.dict('os.environ',{'OPENAI_API_KEY':'fake-key'}):
            process(c,self.now,caller=self.h.response,clock=lambda:self.now)
        output,result=report(folder,self.now)
        self.assertEqual(result['cases'][0]['state'],'人手期待値未確認')
        self.assertEqual(len(result['cases'][0]['impacts']),3)
        evening=json.loads((output/'evening.json').read_text(encoding='utf-8'))
        self.assertEqual(len(evening['discovered_companies']),3)
        self.assertTrue(all(not r['execution_eligible'] for r in evening['discovered_companies']))

    def test_manifest_tamper_rejected(self):
        self.h.source();folder=prepare(self.c,self.root/'check',1,self.now)
        m=json.loads((folder/'manifest.json').read_text(encoding='utf-8'));m['cases'][0]['article']['title']='changed'
        (folder/'manifest.json').write_text(encode(m),encoding='utf-8')
        with self.assertRaises(ValueError):report(folder,self.now)
