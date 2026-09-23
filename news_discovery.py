"""Bounded industry discovery pipeline. Saved sources only; API opt-in; no orders."""
import argparse
from contextlib import closing
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sqlite3
from company_graph import companies,discover,local_topics,import_companies
from discovery_ai import VERSION,PERIODS,request,parse
from system_data import encode,digest,stamp,readonly
from system_budget import DailyBudget,BudgetError
from system_queue import eligibility,priority,budget_status


def settings(c):
    extra=dict(c.get('discovery',{}))
    root=Path(c['paths']['runtime']).parent
    result=dict(companies_db=str(root/'companies.sqlite3'),db=str(root/'discovery.sqlite3'),
                max_candidates=5,max_calls=3,periods=PERIODS,enabled=False,allow_indirect=False,
                horizon='short',min_turnover=100_000_000,max_reaction=.03,benchmarks={},timeframes={})
    unknown=set(extra)-set(result)
    if unknown:raise ValueError('Unknown discovery config keys')
    result.update(extra)
    for k in ('db','companies_db'):
        path=Path(result[k])
        if not path.is_absolute():path=Path(c.get('_base_dir',Path(__file__).resolve().parent))/path
        result[k]=str(path.resolve())
    if len({result['db'],result['companies_db']})!=2 or any(v in c['paths'].values() for v in (result['db'],result['companies_db'])):
        raise ValueError('Discovery databases must be separate from all existing stores')
    for k in ('max_candidates','max_calls'):
        if type(result[k]) is not int or not 1<=result[k]<=20:raise ValueError('Discovery limits must be 1..20')
    if type(result['enabled']) is not bool or type(result['allow_indirect']) is not bool:raise ValueError('Explicit booleans required')
    if set(result['periods'])!=set(PERIODS) or result['horizon'] not in PERIODS:raise ValueError('Invalid periods')
    previous=0
    for k in PERIODS:
        span=result['periods'][k]
        if not isinstance(span,list) or len(span)!=2 or type(span[0]) is not int or span[0]<=previous or (span[1] is not None and (type(span[1]) is not int or span[1]<span[0])):
            raise ValueError('Periods must be ordered nonoverlapping session ranges')
        if span[1] is None and k!='long':raise ValueError('Only long may be open-ended')
        previous=span[1] or span[0]
    import math
    if not all(type(result[k]) in (int,float) and math.isfinite(result[k]) and result[k]>=0 for k in ('min_turnover','max_reaction')):
        raise ValueError('Invalid discovery market checks')
    if not isinstance(result['benchmarks'],dict) or not isinstance(result['timeframes'],dict):
        raise ValueError('Benchmark/timeframe mappings required')
    if set(result['timeframes'])-set(PERIODS):raise ValueError('Unknown timeframe')
    for definition in result['benchmarks'].values():
        if not isinstance(definition,dict) or set(definition)!={'db','symbol'} or not all(isinstance(v,str) and v.strip() for v in definition.values()):
            raise ValueError('Benchmark db and symbol required')
    from system_risk import RiskLimits
    for frame in result['timeframes'].values():
        if not isinstance(frame,dict) or set(frame)-{'chart_model','news_model','risk'}:
            raise ValueError('Invalid timeframe settings')
        if frame.get('news_model') is not None:
            raise ValueError('Trained discovery news models are not implemented')
        if frame.get('chart_model') is not None and (not isinstance(frame['chart_model'],str) or not frame['chart_model'].strip()):
            raise ValueError('Chart model path required')
        if not isinstance(frame.get('risk',{}),dict):raise ValueError('Risk mapping required')
        limits=dict(c['risk'],**frame.get('risk',{}))
        if type(limits.get('stop_new',False)) is not bool:raise ValueError('Risk stop_new must be boolean')
        try:RiskLimits(**limits).validate()
        except TypeError as exc:raise ValueError('Invalid risk settings') from exc
    return result


def for_horizon(c):
    """Research copy only: never mutate the running paper account configuration."""
    from copy import deepcopy
    result=deepcopy(c);s=settings(c);frame=s['timeframes'].get(s['horizon'],{})
    if frame.get('chart_model'):
        path=Path(frame['chart_model'])
        if not path.is_absolute():path=Path(c.get('_base_dir',Path(__file__).resolve().parent))/path
        result['chart'].update(strategy='lightgbm',model=str(path.resolve()))
    result['risk'].update(frame.get('risk',{}))
    return result


def connect(path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path)
    db.executescript('''CREATE TABLE IF NOT EXISTS discovery_sources(id TEXT PRIMARY KEY,payload TEXT,recorded_at TEXT);
        CREATE TABLE IF NOT EXISTS discovery_ai(id TEXT PRIMARY KEY,source_id TEXT,stage TEXT,symbol TEXT,
            status TEXT,started_at TEXT,finished_at TEXT,request TEXT,input TEXT,result TEXT,usage TEXT,model TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS discovery_candidates(id TEXT PRIMARY KEY,source_id TEXT,symbol TEXT,
            recorded_at TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS discovery_outcomes(id TEXT PRIMARY KEY,candidate_id TEXT,recorded_at TEXT,payload TEXT);''')
    db.commit();return db


def sources(c,now):
    from news_triage import snapshot
    from news_documents import snapshot as documents
    rows=snapshot(c['paths']['news']) if Path(c['paths']['news']).is_file() else []
    by_url={r['url']:r for r in rows}
    if Path(c['paths']['documents']).is_file():
        for r in documents(c['paths']['documents']):by_url[r['url']]=r
    unique={}
    from news_ai import headline
    for a in sorted(by_url.values(),key=lambda a:a['first_seen_at']):
        if stamp(a['first_seen_at'])>now or (a.get('observed_at') and stamp(a['observed_at'])>now):continue
        if (now-stamp(a['first_seen_at'])).total_seconds()>7*86400:continue
        key=digest([headline(a),a.get('body'),a.get('published_at'),a.get('publisher')])
        unique.setdefault(key,dict(a,id=key,original_id=a['id']))
    return sorted(unique.values(),key=lambda a:(-priority(a),-(stamp(a['published_at']).timestamp() if a.get('published_at') else 0),a['id']))


def preview(c,now=None):
    now=now or datetime.now(timezone.utc);s=settings(c)
    registry=companies(s['companies_db'],now)
    articles=[]
    for a in sources(c,now):
        tags=local_topics(a['title']+' '+a.get('body',''))
        found=discover(a,tags,registry)
        articles.append(dict(id=a['id'],title=a['title'],published_at=a.get('published_at'),
            first_seen_at=a['first_seen_at'],excluded=eligibility(a,c,now),
            topics=tags,topic_basis='ローカル語彙による候補・AI解析待ち',
            candidates=[dict(symbol=f['symbol'],name=f['company']['name'],relation=f['relation'],state=f['relation_state']) for f in found[:s['max_candidates']]],
            total_candidates=len(found)))
    return dict(dry_run=c['dry_run'],max_calls=s['max_calls'],max_candidates=s['max_candidates'],
        company_count=len(registry),articles=articles,budget=budget_status(c,now),
        notice='読み取り専用。予定の概算上限はmax_calls。産業AI未実行のため企業別送信件数は未確定')


class Session:
    def __init__(self,c,db,now,caller=None,clock=None):
        self.c,self.db,self.now=c,db,now
        from news_ai import call_openai
        self.caller=caller or call_openai;self.calls=0;self.s=settings(c);self.budget=None;self.halted=False
        self.clock=clock or (lambda:datetime.now(timezone.utc))

    def analyze(self,a,candidate=None,event=None):
        payload=request(a,self.c['llm'],self.s['periods'],event,candidate)
        ident=digest([VERSION,a['id'],payload])
        stage='impact' if candidate else 'event'
        original=dict(article=a,candidate=candidate,event=event,periods=self.s['periods'])
        row=self.db.execute('SELECT status,result,finished_at FROM discovery_ai WHERE id=?',(ident,)).fetchone()
        if row and row[0]!='pending':return dict(id=ident,status=row[0],result=json.loads(row[1]) if row[1] else None,finished_at=row[2])
        self.db.execute('INSERT OR IGNORE INTO discovery_ai(id,source_id,stage,symbol,status,request,input) VALUES (?,?,?,?,?,?,?)',
            (ident,a['id'],stage,candidate['symbol'] if candidate else None,'pending',encode(payload),encode(original)));self.db.commit()
        response=None;reason=eligibility(a,self.c,self.now)
        if reason:
            self.db.execute("UPDATE discovery_ai SET status='excluded',error=? WHERE id=?",(reason,ident));self.db.commit()
            return dict(id=ident,status='excluded',result=None,finished_at=None)
        pending=dict(id=ident,status='pending',result=None,finished_at=None)
        if self.c['dry_run'] or not self.c['network_enabled'] or self.calls>=self.s['max_calls'] or self.halted:return pending
        key=os.environ.get('OPENAI_API_KEY')
        if not key:raise ValueError('OPENAI_API_KEY is not set')
        if self.budget is None:self.budget=DailyBudget(self.c['budget_file'],clock=lambda:self.now)
        try:self.budget.reserve(ident,payload)
        except BudgetError:
            self.halted=True
            self.db.execute("UPDATE discovery_ai SET error='budget_blocked' WHERE id=?",(ident,));self.db.commit()
            return pending
        self.db.execute("UPDATE discovery_ai SET status='started',started_at=?,error=NULL WHERE id=?",(self.now.isoformat(),ident));self.db.commit()
        self.calls+=1;result=None;status='failed';error=None
        try:
            response=self.caller(payload,key)
            result=parse(response,a,candidate);status='ok'
        except Exception as exc:
            error=type(exc).__name__  # No response/secret in logs; no automatic retry.
        response=response if isinstance(response,dict) else {}
        self.budget.settle(ident,payload,response.get('usage'))
        # Finish must be actual availability, never the earlier start of a long request.
        finished=max(self.now,self.clock()).isoformat()
        self.db.execute('UPDATE discovery_ai SET status=?,finished_at=?,result=?,usage=?,model=?,error=? WHERE id=?',
            (status,finished,encode(result) if result else None,encode(response.get('usage')),response.get('model'),error,ident));self.db.commit()
        if status=='failed':self.halted=True
        return dict(id=ident,status=status,result=result,finished_at=finished)


def process(c,now=None,caller=None,clock=None):
    now=now or datetime.now(timezone.utc);s=settings(c)
    registry=companies(s['companies_db'],now)
    from system_runtime import process_lock
    with process_lock(s['db']+'.lock'),closing(connect(s['db'])) as db:
        session=Session(c,db,now,caller,clock);n=0
        for a in sources(c,now):
            db.execute('INSERT OR IGNORE INTO discovery_sources VALUES (?,?,?)',(a['id'],encode(a),now.isoformat()));db.commit()
            event=session.analyze(a)
            tags=[i['topic'] for i in event['result']['industries']] if event['status']=='ok' else local_topics(a['title']+' '+a.get('body',''))
            found=discover(a,tags,registry)
            for index,f in enumerate(found):
                impact=session.analyze(a,f,event['result']) if event['status']=='ok' and index<s['max_candidates'] and not eligibility(a,c,now) else dict(id=None,status='pending',result=None,finished_at=None)
                row=dict(f,article=a,event=event,impact=impact,periods=s['periods'],
                    topic_basis='LLM' if event['status']=='ok' else 'local_keywords_unconfirmed',
                    watched=f['symbol'] in c['symbols'],state='ANALYSIS_ONLY',
                    review_reason='candidate_limit' if index>=s['max_candidates'] else (eligibility(a,c,now) or impact['status']),
                    promotion='手動確認が必要: 価格履歴・流動性・関連性・企業行動・資金/リスク上限。監視対象へ自動追加しません')
                ident=digest([a['id'],f['company']['revision'],event['id'],impact['id'],row['review_reason']])
                recorded=max([now]+[stamp(x['finished_at']) for x in (event,impact) if x.get('finished_at')]).isoformat()
                db.execute('INSERT OR IGNORE INTO discovery_candidates VALUES (?,?,?,?,?)',(ident,a['id'],f['symbol'],recorded,encode(row)));n+=1
            db.commit()
        return dict(candidates=n,api_calls=session.calls,dry_run=c['dry_run'],db=s['db'])


def candidate_rows(c,asof):
    path=settings(c)['db'];latest={}
    if not Path(path).is_file():return []
    with closing(readonly(path)) as db:
        for ident,source,code,recorded,raw in db.execute('SELECT * FROM discovery_candidates ORDER BY recorded_at,rowid'):
            if stamp(recorded)>asof:continue
            r=json.loads(raw)
            if stamp(r['article']['first_seen_at'])>asof or stamp(r['company']['recorded_at'])>asof:continue
            for field in ('event','impact'):
                analysis=r[field]
                if analysis['id']:
                    a=db.execute('SELECT status,result,finished_at,model FROM discovery_ai WHERE id=?',(analysis['id'],)).fetchone()
                    if a and a[2] and stamp(a[2])<=asof:
                        r[field]=dict(id=analysis['id'],status=a[0],result=json.loads(a[1]) if a[1] else None,finished_at=a[2],model=a[3])
                    else:r[field]=dict(id=analysis['id'],status='pending',result=None,finished_at=None)
            latest[(source,code)]=dict(r,id=ident,discovered_at=recorded)
    return list(latest.values())


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['companies-import','companies-list','preview','run','list','outcomes','backtest'])
    p.add_argument('--config',type=Path,default=Path(__file__).resolve().parent/'config/paper.json')
    p.add_argument('--file',type=Path)
    p.add_argument('--output',type=Path)
    p.add_argument('--dry-run',action='store_true',help='Force paid API calls off, regardless of config')
    args=p.parse_args(argv)
    from system_runtime import read_config
    c=read_config(args.config)
    if args.dry_run:c['dry_run']=True
    now=datetime.now(timezone.utc);s=settings(c)
    if args.command=='companies-import':
        if not args.file:p.error('--file is required')
        value=import_companies(s['companies_db'],json.loads(args.file.read_text(encoding='utf-8-sig')),now)
        print(f'企業情報 {value}件を登録。外部API・注文なし。')
    elif args.command=='companies-list':print(encode(companies(s['companies_db'],now)))
    elif args.command=='preview':print(encode(preview(c,now)))
    elif args.command=='run':print(encode(process(c,now)))
    elif args.command=='list':print(encode(candidate_rows(c,now)))
    elif args.command=='outcomes':
        from discovery_outcomes import collect
        print(encode(collect(c,now)))
    elif args.command=='backtest':
        from discovery_integration import backtest
        result=backtest(c,now)
        if not args.output:p.error('--output must be a new JSON file')
        with args.output.open('x',encoding='utf-8') as f:f.write(encode(result))
        print('研究用バックテストを保存。仮想口座は更新しません。')
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,ValueError,KeyError,sqlite3.Error) as exc:
        print('停止: '+type(exc).__name__+'。設定・入力・DBを確認してください。API応答とキーは表示しません。')
        raise SystemExit(1)
