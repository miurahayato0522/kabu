"""Read-only, bounded human summaries and engineering cost estimates."""
from collections import Counter
from contextlib import closing
from pathlib import Path
import json
from system_data import stamp,encode,digest,readonly
from system_queue import eligibility,budget_status

def selected(rows,args):
    out=[]
    for r in rows:
        a=r['article'];impact=r['impact'];result=impact.get('result') or {};rel=r.get('relevance',{})
        if args.symbol and r['symbol']!=args.symbol:continue
        if args.name and args.name not in r['company']['name']:continue
        if args.industry and not any(x['topic']==args.industry for x in r['matched_relations']):continue
        if args.status and impact['status']!=args.status:continue
        if args.direction and result.get('short','不明')!=args.direction:continue
        if args.relation and rel.get('kind')!=args.relation:continue
        if args.watch=='existing' and not r['watched']:continue
        if args.watch=='new' and r['watched']:continue
        published=a.get('published_at')
        if args.since and (not published or stamp(published)<stamp(args.since)):continue
        if args.until and (not published or stamp(published)>stamp(args.until)):continue
        out.append(r)
    return sorted(out,key=lambda r:(-(r.get('rank',0)),r['article'].get('published_at') or '',r['symbol']))

def summary(rows,c,now,limit=10):
    states=Counter(r['impact']['status'] for r in rows)
    excluded=sum(bool(eligibility(r['article'],c,now)) or not r.get('relevance',{}).get('eligible',False) for r in rows)
    print(f"ニュース {len({r['article']['id'] for r in rows})}件 / 候補 {len(rows)}件 / 銘柄 {len({r['symbol'] for r in rows})}社")
    print(f"AI済 {states['ok']} / 待ち {states['pending']} / 除外・要確認 {excluded} / エラー {states['failed']} / 上位表示 {min(limit,len(rows))}")
    for r in rows[:limit]:
        a=r['article'];i=r['impact'];v=i.get('result') or {};rel=r.get('relevance',{})
        print(f"\n[{r['symbol']} {r['company']['name']}] {'既存監視' if r['watched'] else '新規候補'} | {i['status']} | {v.get('short','不明')}")
        print(a['title']);print('公表: '+str(a.get('published_at'))+' / '+rel.get('label',r['relation']))
        print('候補状態: '+r['state']+' / '+str(eligibility(a,c,now) or r['review_reason']))
    print('注文なし。産業一致は影響の確定ではありません。詳細: --json')

def plan(c,now):
    from news_discovery import settings,sources
    from company_graph import companies,CompanyIndex,discover,local_topics
    from discovery_ai import request,VERSION
    s=settings(c);index=CompanyIndex(companies(s['companies_db'],now));budget=budget_status(c,now)
    cache={}
    if Path(s['db']).is_file():
        with closing(readonly(s['db'])) as db:
            for ident,status,result in db.execute('SELECT id,status,result FROM discovery_ai'):
                cache[ident]=(status,json.loads(result) if result else None)
    articles=[];requests=[];unknown=False
    for a in sources(c,now):
        payload=request(a,c['llm'],s['periods']);key=digest([VERSION,a['id'],payload])
        cached=cache.get(key);event=cached[1] if cached and cached[0]=='ok' else None
        tags=[x['topic'] for x in event['industries']] if event else local_topics(a['title']+' '+a.get('body',''))
        found=discover(a,tags,index);eligible=[f for f in found[:s['max_candidates']] if f['relevance']['eligible']]
        reason=eligibility(a,c,now)
        if not reason:
            if not cached or cached[0]=='pending':requests.append(payload);unknown=True
            if event:
                for f in eligible:
                    p=request(a,c['llm'],s['periods'],event,f);k=digest([VERSION,a['id'],p])
                    if k not in cache or cache[k][0]=='pending':requests.append(p)
        articles.append(dict(id=a['id'],title=a['title'],url=a['url'],published_at=a.get('published_at'),
            excluded=reason,event_status=cached[0] if cached else 'not_run',candidates=len(found),detail_candidates=len(eligible)))
    requests=requests[:s['max_calls']]
    inp=sum(len(encode(p).encode('utf-8'))+4096 for p in requests);out=sum(p['max_output_tokens'] for p in requests)
    rates=budget.get('rates') if budget.get('rates_configured') else None
    cost=0.0 if not requests else ((inp*rates[0]+out*rates[1])/1e6 if rates else None)
    return dict(model=c['llm'],articles=articles,known_uncached_requests=len(requests),max_calls=s['max_calls'],
        input_token_allowance=inp,output_token_limit=out,known_request_estimated_usd=cost,budget=budget,
        company_stage_estimate_pending=unknown,
        notice='入力はUTF-8バイト数+4096の予算予約用概算。実トークン数ではない。産業解析前の企業別入力・総費用は未確定。キャッシュ、除外、予算で実送信数は変わる。')

def print_plan(p,limit=10):
    print(f"対象 {len(p['articles'])}記事 / モデル {p['model']} / 既知の未キャッシュ依頼 {p['known_uncached_requests']} / 呼出上限 {p['max_calls']}")
    print(f"入力概算枠 {p['input_token_allowance']} / 出力上限 {p['output_token_limit']} / 既知依頼の見積USD {p['known_request_estimated_usd']}")
    b=p['budget'];print(f"予算残額: 日次USD {b.get('daily_remaining_usd')} / 月次JPY {b.get('monthly_remaining_jpy')}")
    if not b.get('rates_configured'):print('使用モデルの料金設定なし。費用を確認してbudget_fileへ設定するまで有料送信不可。')
    for a in p['articles'][:limit]:print(f"{a['id']}\n{a['title']} | 候補{a['candidates']} / 詳細候補{a['detail_candidates']} / {a['excluded'] or a['event_status']}")
    print(p['notice'])
