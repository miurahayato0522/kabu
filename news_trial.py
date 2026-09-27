"""Explicit one-article, one-call analysis preview/trial; no automatic sends."""
import argparse
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
import os
import hashlib
from system_data import encode,readonly
from system_queue import budget_status


def prepare(c,article_id,cache,now):
    from news_discovery import sources,settings
    from news_quality import send_reason
    from company_graph import CompanyIndex,companies,discover
    from news_ai import make_request,BODY_VERSION,VERSION
    rows=[a for a in sources(c,now) if article_id in (a['id'],a.get('original_id'))]
    if len(rows)!=1:raise ValueError('Exact saved article ID required')
    a=rows[0];q=a['quality'];reason=send_reason(a,c,now)
    codes=q['direct_symbols'];names=dict(c['symbols'])
    s=settings(c);index=CompanyIndex(companies(s['companies_db'],now))
    names.update({code:row['name'] for code,row in index.rows.items() if code in codes})
    if q['category']=='industry' and not reason:
        found=discover(a,q['topics'],index)
        verified=[f for f in found if f['relevance']['eligible'] and f['relation_state']=='確認済み']
        codes=[f['symbol'] for f in verified]
        names.update({f['symbol']:f['company']['name'] for f in verified})
    if not codes:reason=reason or 'LOW_COMPANY_RELEVANCE'
    if q['category'] not in ('company_direct','industry'):reason=reason or 'CATEGORY_NOT_ALLOWED'
    article=dict(a,symbols=codes,analyze_impact=True)
    names={k:v for k,v in names.items() if k in codes}
    payload=make_request(article,names,c['llm'])
    version='body-v4-impact' if 'body' in article else 'headline-v3-impact'
    identity=dict(version=version,article_id=article['id'],payload=payload)
    if 'body' in article:identity['document_revision']=article.get('document_revision')
    fingerprint=hashlib.sha256(encode(identity).encode()).hexdigest()
    cached=None
    if Path(cache).is_file():
        with closing(readonly(cache)) as db:
            row=db.execute('SELECT status FROM ai_analyses WHERE request_hash=?',(fingerprint,)).fetchone()
            cached=row[0] if row else None
    if not cached:
        from news_quality import cache_states,cache_state
        states=cache_states(c)
        if states:
            cached=cache_state(dict(article,id=a.get('original_id',a['id'])),dict(c,symbols=names),states)
    budget=budget_status(c,now);size=len(encode(payload).encode('utf-8'))+4096
    rate=budget.get('rates') if budget.get('rates_configured') else None
    cost=(size*rate[0]+payload['max_output_tokens']*rate[1])/1e6 if rate else None
    blocked=[]
    if reason:blocked.append(reason)
    if c['dry_run']:blocked.append('DRY_RUN')
    if not c['network_enabled']:blocked.append('NETWORK_DISABLED')
    if not rate:blocked.append('RATES_NOT_CONFIGURED')
    if rate and (cost>budget['daily_remaining_usd'] or (budget.get('monthly_remaining_jpy') is not None and cost*budget['jpy_per_usd']>budget['monthly_remaining_jpy'])):blocked.append('BUDGET_LIMIT')
    return dict(article=article,names=names,request=payload,model=c['llm'],max_calls=1,cache=cached,
        input_size_allowance=size,output_token_limit=payload['max_output_tokens'],estimated_max_usd=cost,
        budget=budget,dry_run=c['dry_run'],network_enabled=c['network_enabled'],blocked=blocked,
        note='費用は既存予算予約式による概算上限。実トークン数/請求の保証ではありません。')


def execute(c,article_id,cache,now,caller=None):
    plan=prepare(c,article_id,cache,now)
    if plan['blocked']:return dict(status='blocked',reasons=plan['blocked'],api_calls=0)
    if plan['cache']:return dict(status='cached:'+plan['cache'],api_calls=0)
    from news_ai import analyze,open_cache,call_openai
    from system_budget import DailyBudget
    from system_runtime import process_lock
    calls=0
    def call(payload,key):
        nonlocal calls
        if calls>=1:raise ValueError('One-call limit')
        calls+=1
        return (caller or call_openai)(payload,key)
    key=os.environ.get('OPENAI_API_KEY')
    if not key and not plan['cache']:raise ValueError('OPENAI_API_KEY is not set')
    with process_lock(str(cache)+'.lock'),closing(open_cache(Path(cache))) as db:
        result=analyze(db,plan['article'],plan['names'],key,c['llm'],caller=call,budget=DailyBudget(c['budget_file'],clock=lambda:now))
    return dict(status=result,api_calls=calls)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['preview','run'])
    p.add_argument('--config',default='config/paper.json');p.add_argument('--article-id',required=True)
    p.add_argument('--cache',required=True,type=Path);p.add_argument('--max-calls',type=int,choices=[1],default=1)
    args=p.parse_args()
    from system_runtime import read_config
    c=read_config(args.config)
    from news_discovery import settings
    protected={Path(v).resolve() for v in c['paths'].values()} | {Path(c['budget_file']).resolve(),Path(settings(c)['db']),Path(settings(c)['companies_db'])}
    import json
    budget=Path(c['budget_file']);b=json.loads(budget.read_text(encoding='utf-8-sig'))
    protected.add((budget.parent/b['ledger']).resolve())
    if args.cache.resolve() in protected:p.error('Use a separate trial cache')
    now=datetime.now(timezone.utc)
    print(encode(prepare(c,args.article_id,args.cache,now) if args.command=='preview' else execute(c,args.article_id,args.cache,now)))


if __name__=='__main__':main()
