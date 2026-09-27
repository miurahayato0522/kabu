"""Read-only review queue. Empty candidate feeds never confirm absence."""
import argparse
from datetime import datetime,timezone
from pathlib import Path
import sqlite3
from system_data import encode,stamp


def review(c,now,observations=None,status=None):
    from system_status import sessions,account
    from corporate_actions import ConfirmedActions
    from action_candidates import report
    cal=sessions(now);acc=status['account'] if status else account(c,now)
    day=cal['next_session'];prior=acc.get('day') or cal['analysis_day']
    path=Path(observations) if observations else Path(c['paths']['actions']).with_suffix('.observations.sqlite3')
    seen=report(path) if path.is_file() else []
    from news_discovery import settings
    from company_graph import companies
    company_path=settings(c)['companies_db']
    registry={r['symbol']:r for r in companies(company_path,now)} if Path(company_path).is_file() else {}
    out=[]
    for code,name in c['symbols'].items():
        snapshots=[r for r in seen if r['symbol'][:4]==code and stamp(r['fetched_at'])<=now]
        latest=max(snapshots,key=lambda r:r['fetched_at']) if snapshots else None
        events=latest['events'] if latest else []
        confirmed=[];error=None
        try:confirmed=ConfirmedActions(c['paths']['actions']).between(code,prior,day,now)
        except (OSError,ValueError,sqlite3.Error) as exc:error=type(exc).__name__
        out.append(dict(symbol=code,company_name=name,target_session=day,from_session=prior,
            yahoo_candidates=events,yahoo_fetched_at=latest['fetched_at'] if latest else None,
            yahoo_coverage=([latest['start'],latest['end']] if latest else None),
            jpx_candidates=None,tdnet_candidates=None,official_ir_candidates=None,
            source_status=dict(yahoo='observed_unconfirmed' if latest else 'not_fetched',jpx='not_checked',tdnet='not_checked',official_ir='not_checked'),
            reference_links=['https://www.jpx.co.jp/listing/others/ex-rights/','https://www.release.tdnet.info/inbs/I_main_00.html'],
            official_business_reference_links=sorted({r['source'] for r in registry.get(code,{}).get('relations',[]) if r['state']=='確認済み'}),
            status='CONFIRMED' if confirmed and not error else 'REVIEW_REQUIRED',
            verified_by=None,verified_at=max((r['verified_at'] for r in confirmed),default=None),
            evidence=[r['evidence'] for r in confirmed],factor=[r['factor'] for r in confirmed],
            effective_day=None,correction_status='unknown',confirmations=confirmed,error=error,
            absence_confirmed=False,notice='観測0件は不存在の確認ではありません。確認対象期間・権利落日・効力日・訂正と配当を公式資料で照合してください。旧台帳には確認者/法的効力日の独立項目はありません。'))
    return out


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['status'])
    p.add_argument('--config',default='config/paper.json');p.add_argument('--observations-db',type=Path)
    p.add_argument('--json',action='store_true');p.add_argument('--output',type=Path)
    args=p.parse_args()
    from system_runtime import read_config
    rows=review(read_config(args.config),datetime.now(timezone.utc),args.observations_db)
    if args.output:
        with args.output.open('x',encoding='utf-8') as f:f.write(encode(rows))
    if args.json:print(encode(rows))
    else:
        for r in rows:
            print(r['symbol'],r['company_name'],'対象:',r['target_session'],r['status'])
            print('Yahoo観測:',len(r['yahoo_candidates']),'件 / coverage:',r['yahoo_coverage'],'/ JPX・TDnet・IR: 未確認')
            print(r['notice'])


if __name__=='__main__':main()
