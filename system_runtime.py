"""Single-process scheduled paper operations. Paid analysis is explicitly opt-in."""
from contextlib import closing, contextmanager
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import sqlite3
import time
from system_data import encode,digest,stamp,provider,JST
from system_budget import DailyBudget,BudgetError

ROOT=Path(__file__).resolve().parent


def read_config(path):
    path=Path(path).resolve()
    c=json.loads(path.read_text(encoding='utf-8-sig'))
    if c.get('mode')!='paper' or type(c.get('dry_run')) is not bool or type(c.get('network_enabled')) is not bool:
        raise ValueError('Only paper mode with explicit dry_run/network_enabled is supported')
    if not c['symbols'] or any(len(s)!=4 or not s.isalnum() or not s.isascii() for s in c['symbols']):
        raise ValueError('Invalid watch symbols')
    for name,seconds in c['intervals'].items():
        if name not in ('news','analysis','prices','daily','paper','documents') or type(seconds) is not int or seconds<30:
            raise ValueError('Job intervals must be integer seconds >=30')
    if set(c['intervals'])!={'news','analysis','prices','daily','paper','documents'}:
        raise ValueError('All job intervals required')
    if not 1<=c['analysis_limit']<=20:
        raise ValueError('Analysis limit must be 1..20')
    if c['chart']['strategy'] not in ('ma','lightgbm'):
        raise ValueError('Unsupported chart strategy')
    base=(path.parent/c.get('base_dir','..')).resolve()
    for k,value in c['paths'].items():
        c['paths'][k]=str((base/value).resolve())
    if len(set(c['paths'].values()))!=len(c['paths']):
        raise ValueError('Each database/stop path must be separate')
    for start,end in c['trading_hours']:
        if datetime.strptime(start,'%H:%M')>=datetime.strptime(end,'%H:%M'):
            raise ValueError('Trading windows must be ordered HH:MM intervals')
    if c['chart'].get('model'):
        c['chart']['model']=str((base/c['chart']['model']).resolve())
    c['budget_file']=str((base/c['budget_file']).resolve())
    return c


@contextmanager
def process_lock(path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    f=open(path,'a+b')
    try:
        if f.tell()==0:
            f.write(b'0');f.flush()
        f.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:
        f.close()  # OS releases the lock on normal exit and process death.


def connect(path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path)
    db.executescript('''CREATE TABLE IF NOT EXISTS jobs(name TEXT PRIMARY KEY,next_at TEXT,status TEXT,finished_at TEXT);
        CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY,at TEXT,job TEXT,status TEXT,detail TEXT);
        CREATE TABLE IF NOT EXISTS analysis_queue(id TEXT PRIMARY KEY,payload TEXT,status TEXT,updated_at TEXT);
        CREATE TABLE IF NOT EXISTS document_jobs(url TEXT PRIMARY KEY,status TEXT);
        CREATE TABLE IF NOT EXISTS runtime_meta(key TEXT PRIMARY KEY,value TEXT);''')
    db.commit()
    return db


def collect_topics(c):
    from news_collector import collect,connect as news_connect
    queries=dict(c['symbols'])
    for topic in c['topics']:
        queries['@'+topic['scope']+':'+topic['id']]=topic['query']
    with closing(news_connect(c['paths']['news'])) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS query_scopes(query_key TEXT PRIMARY KEY,scope TEXT,query TEXT)')
        for key,q in queries.items():
            scope=key[1:].split(':')[0] if key.startswith('@') else 'company'
            db.execute('INSERT OR REPLACE INTO query_scopes VALUES (?,?,?)',(key,scope,q))
    failures=collect(c['paths']['news'],queries)
    if failures:
        raise ValueError('One or more RSS queries failed; coverage unknown')


def candidates(c,now):
    from news_triage import snapshot,RULES
    from news_documents import snapshot as documents
    from news_ai import headline
    rows=snapshot(c['paths']['news']) if Path(c['paths']['news']).is_file() else []
    by_url={r['url']:r for r in rows}
    if Path(c['paths']['documents']).is_file():
        for r in documents(c['paths']['documents']):
            by_url[r['url']]=r
    groups={}
    for r in by_url.values():
        if not r.get('first_seen_at') or not 0<=(now-stamp(r['first_seen_at'])).total_seconds()<=7*86400:
            continue
        text=r['title']+' '+r.get('body','')
        important=any(word in text for words in RULES.values() for word in words) or any(word in text for word in c['market_keywords'])
        if not important:
            continue
        scopes=[s for s in r['symbols'] if s.startswith('@')]
        symbols=sorted(set(r['symbols']) & set(c['symbols']))
        if scopes:
            symbols=sorted(c['symbols'])  # AI must prove relation; macro does not imply direct impact.
        if not symbols:
            continue
        r=dict(r,symbols=symbols,scope_tags=scopes or ['company'],analyze_impact=True)
        key=digest([headline(r),r.get('body'),r.get('published_at'),r.get('publisher')])
        if key in groups:
            groups[key]['symbols']=sorted(set(groups[key]['symbols'])|set(symbols))
        else:
            groups[key]=r
    return [groups[k] for k in sorted(groups)]


class Runtime:
    def __init__(self,c,hooks=None,clock=None):
        self.c,self.hooks=c,hooks or {}
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.client=None

    def log(self,db,job,status,detail):
        db.execute('INSERT INTO logs(at,job,status,detail) VALUES (?,?,?,?)',
                   (self.clock().isoformat(),job,status,detail))
        db.commit()
        print(job,status,detail,flush=True)

    def trading_time(self,now):
        import exchange_calendars as xcals
        day=now.astimezone(JST).date()
        cal=xcals.get_calendar('XTKS',start=str(day-timedelta(days=10)),end=str(day+timedelta(days=10)))
        return str(day) in {str(d.date()) for d in cal.sessions} and any(a<=now.astimezone(JST).strftime('%H:%M')<=b for a,b in self.c['trading_hours'])

    def price_job(self):
        from kabu_collector import KabuClient,Recorder
        if self.client is None:
            secret=os.environ.get('KABU_API_PASSWORD')
            if not secret:
                raise ValueError('KABU_API_PASSWORD is not set')
            self.client=KabuClient('production')
            try:
                self.client.authenticate(secret)
            except Exception:
                self.client=None
                raise
        recorder=Recorder(Path(self.c['paths']['ticks']),'production')
        try:
            for symbol in self.c['symbols']:
                recorder.write('snapshot',self.client.board(symbol))
        finally:
            recorder.close()

    def daily_job(self):
        if not self.c.get('refresh_yahoo',False):
            return
        from yahoo_history import fetch_frame,convert,save
        from jquants_history import symbol_code
        day=self.clock().astimezone(JST).date()
        start=datetime.fromisoformat(self.c['history_start']).date()
        for symbol in self.c['symbols']:
            code=symbol_code(symbol)
            frame=fetch_frame(code,start,day)
            rows=convert(frame,code,start,day)
            save(Path(self.c['paths']['prices']),code,rows)

    def document_job(self,db):
        for item in self.c.get('official_documents',[]):
            if db.execute('SELECT 1 FROM document_jobs WHERE url=?',(item['url'],)).fetchone():
                continue
            db.execute('INSERT INTO document_jobs VALUES (?,?)',(item['url'],'started'));db.commit()
            try:
                if item['type']=='toyota_html':
                    from news_ir import collect
                    collect(item['url'],Path(self.c['paths']['documents']),Path(self.c['paths']['html_archive']))
                elif item['type']=='official_pdf':
                    from news_pdf import archive_fetch
                    archive_fetch(item['url'],Path(self.c['paths']['documents']),Path(self.c['paths']['pdf_archive']),item['symbol'],item['title'],item['pages'])
                else:
                    raise ValueError('Only explicitly configured supported official documents may be fetched')
                db.execute('UPDATE document_jobs SET status=? WHERE url=?',('ok',item['url']));db.commit()
            except Exception:
                db.execute('UPDATE document_jobs SET status=? WHERE url=?',('failed',item['url']));db.commit()
                raise

    def analysis_job(self,db):
        from news_ai import analyze,open_cache,call_openai
        selected=candidates(self.c,self.clock())
        current=[]
        for article in selected:
            ident=digest([article['title'],article.get('body'),article.get('published_at'),article['symbols'],self.c['llm'],'impact-v1'])
            current.append(ident)
            db.execute('INSERT OR IGNORE INTO analysis_queue VALUES (?,?,?,?)',
                       (ident,encode(article),'pending',self.clock().isoformat()))
        db.commit()
        queued=[(i,db.execute('SELECT payload,status FROM analysis_queue WHERE id=?',(i,)).fetchone()) for i in current]
        pending=[(i,row[0]) for i,row in queued if row[1]=='pending']
        if self.c['dry_run'] or not self.c['network_enabled']:
            self.log(db,'analysis','DRY_RUN',f'{len(pending)} pending; paid API not called')
            return
        key=os.environ.get('OPENAI_API_KEY')
        if not key:
            raise ValueError('OPENAI_API_KEY is not set')
        budget=DailyBudget(self.c['budget_file'],clock=self.clock)
        budget.rates(self.c['llm'])  # Configuration errors occur before claiming any article.
        with closing(open_cache(Path(self.c['paths']['ai']))) as cache:
            for ident,raw in pending[:self.c['analysis_limit']]:
                db.execute('UPDATE analysis_queue SET status=?,updated_at=? WHERE id=?',('started',self.clock().isoformat(),ident));db.commit()
                try:
                    status=analyze(cache,json.loads(raw),self.c['symbols'],key,self.c['llm'],
                                   caller=self.hooks.get('llm',call_openai),budget=budget)
                except BudgetError:
                    db.execute('UPDATE analysis_queue SET status=? WHERE id=?',('pending',ident));db.commit()
                    raise
                db.execute('UPDATE analysis_queue SET status=?,updated_at=? WHERE id=?',
                           ('ok' if status in ('ok','cached:ok') else 'failed',self.clock().isoformat(),ident));db.commit()
                if status not in ('ok','cached:ok'):
                    raise ValueError('News analysis failed; remaining sends stopped')

    def paper_job(self,db):
        from system_news import NewsStore
        from system_strategy import MovingAverage
        from system_integration import BusinessImpactStrategy
        from system_backtest import Account
        from system_risk import RiskLimits
        from system_paper import step
        from corporate_actions import ConfirmedActions
        now=self.clock()
        price_health=db.execute("SELECT status,finished_at FROM jobs WHERE name='prices'").fetchone()
        if not price_health or price_health[0]!='ok' or not price_health[1] or (now-stamp(price_health[1])).total_seconds()>self.c['intervals']['prices']*2:
            raise ValueError('Price collection failed/missing/stale; paper account paused')
        last=db.execute("SELECT status,finished_at FROM jobs WHERE name='news'").fetchone()
        pending=db.execute("SELECT count(*) FROM analysis_queue WHERE status!='ok' AND updated_at>=?",
                           ((now-timedelta(days=7)).isoformat(),)).fetchone()[0]
        healthy=bool(last and last[0]=='ok' and last[1] and (now-stamp(last[1])).total_seconds()<=self.c['intervals']['news']*2 and not pending)
        coverage=[last[1],now.isoformat()] if healthy else None
        news=NewsStore(self.c['paths']['ai'] if Path(self.c['paths']['ai']).is_file() else None,coverage)
        news.coverage_policy='runtime-last-successful-poll-v1'
        chart=MovingAverage()
        if self.c['chart']['strategy']=='lightgbm':
            from system_model import ReturnModel
            chart=ReturnModel(self.c['chart']['model'],self.c['chart'].get('threshold',0))
        strategy=BusinessImpactStrategy(chart,news,self.c['integration'])
        account=Account(self.c['initial_cash'],RiskLimits(**self.c['risk']),**self.c['costs'])
        return step(self.c['paths']['ledger'],provider(self.c['paths']['prices']).bars(list(self.c['symbols'])),
                    self.c['paths']['ticks'],strategy,account,now,
                    stop_new=Path(self.c['paths']['stop_new']).exists() or not healthy,
                    actions=ConfirmedActions(self.c['paths']['actions']),daily_once=True)

    def cycle(self):
        with closing(connect(self.c['paths']['runtime'])) as db:
            configuration=digest(self.c)
            old=db.execute("SELECT value FROM runtime_meta WHERE key='config_hash'").fetchone()
            if old and old[0]!=configuration:
                db.execute('UPDATE jobs SET next_at=?',(self.clock().isoformat(),))
            db.execute("INSERT OR REPLACE INTO runtime_meta VALUES ('config_hash',?)",(configuration,));db.commit()
            for name in ('news','documents','daily','prices','analysis','paper'):
                now=self.clock()
                row=db.execute('SELECT next_at FROM jobs WHERE name=?',(name,)).fetchone()
                if row and stamp(row[0])>now:
                    continue
                if name in ('prices','paper') and not self.trading_time(now):
                    continue
                interval=self.c['intervals'][name]
                db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?)',
                           (name,(now+timedelta(seconds=interval)).isoformat(),'started',None));db.commit()
                try:
                    if name in self.hooks:
                        self.hooks[name](self,db)
                    elif name in ('news','documents','daily','prices') and not self.c['network_enabled']:
                        self.log(db,name,'OFFLINE','network disabled')
                        if name=='news':
                            raise ValueError('News coverage unavailable in offline mode')
                    elif name=='news': collect_topics(self.c)
                    elif name=='documents': self.document_job(db)
                    elif name=='daily': self.daily_job()
                    elif name=='prices': self.price_job()
                    elif name=='analysis': self.analysis_job(db)
                    elif name=='paper': self.paper_job(db)
                    status,detail='ok','completed'
                except Exception as exc:
                    status,detail='failed',type(exc).__name__
                    # Do not log arbitrary exception bodies, which may contain credentials/URLs.
                    if isinstance(exc,BudgetError):
                        detail='budget_limit_or_unresolved_reservation'
                    elif name=='paper' and isinstance(exc,ValueError):
                        detail=str(exc)
                    db.execute('UPDATE jobs SET next_at=? WHERE name=?',
                               ((self.clock()+timedelta(seconds=max(interval,300))).isoformat(),name))
                    if name=='paper':
                        from system_paper import record_error
                        record_error(self.c['paths']['ledger'],detail)
                db.execute('UPDATE jobs SET status=?,finished_at=? WHERE name=?',(status,self.clock().isoformat(),name))
                self.log(db,name,status,detail)


def run_config(path,once=False):
    c=read_config(path)
    with process_lock(c['paths']['runtime']+'.lock'):
        runtime=Runtime(c)
        print('PAPER ONLY / DRY_RUN='+str(c['dry_run'])+' / Ctrl+C to stop',flush=True)
        try:
            while True:
                runtime.cycle()
                if once:return
                time.sleep(1)
        except KeyboardInterrupt:
            print('Stopped; state saved. Restart with the same config.')
