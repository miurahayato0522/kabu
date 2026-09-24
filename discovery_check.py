"""Prepare and inspect a bounded real-news evaluation; never sends API requests."""
import argparse
from datetime import datetime,timezone
from pathlib import Path
import json
from html import escape
from company_graph import companies,CompanyIndex,discover,local_topics
from news_discovery import settings,sources,candidate_rows,process
from system_data import encode,digest
from system_queue import eligibility


def prepare(c,folder,limit=3,now=None):
    if type(limit) is not int or not 1<=limit<=10:raise ValueError('limit must be 1..10')
    now=now or datetime.now(timezone.utc);s=settings(c)
    registry=companies(s['companies_db'],now);index=CompanyIndex(registry)
    available=sources(c,now)
    # Prefer usable, diverse topics; keep original publication/receipt times unchanged.
    available.sort(key=lambda a:(bool(eligibility(a,c,now)),-bool(local_topics(a['title']+' '+a.get('body','')))))
    chosen=[];used=set()
    for a in available:
        tags=tuple(local_topics(a['title']+' '+a.get('body','')))
        if tags in used:continue
        chosen.append(a);used.add(tags)
        if len(chosen)==limit:break
    for a in available:
        if len(chosen)==limit:break
        if a not in chosen:chosen.append(a)
    if not chosen:raise ValueError('No recent saved articles; collect news first')
    folder=Path(folder).resolve();folder.mkdir(parents=True,exist_ok=False)
    clone=json.loads(encode(c));clone['base_dir']=str(Path(__file__).resolve().parent)
    clone.pop('_base_dir',None)
    clone.update(dry_run=True,network_enabled=False,evening_report=False)
    clone['discovery']=dict(s,db=str(folder/'discovery.sqlite3'),enabled=False,source_ids=[a['id'] for a in chosen],max_calls=1)
    cases=[]
    for a in chosen:
        matches=discover(a,local_topics(a['title']+' '+a.get('body','')),index)
        cases.append(dict(article=a,eligibility=eligibility(a,c,now),local_candidates=[x['symbol'] for x in matches[:s['max_saved_candidates']]],
            total_matches=len(matches),expected_topics=None,expected_symbols=None,expected_impacts=None,
            reviewer=None,reviewed_at=None))
    manifest=dict(version='real-news-check-v1',created_at=now.isoformat(),cases=cases,company_revisions={x['symbol']:x['revision'] for x in registry})
    manifest['input_hash']=digest(manifest)
    (folder/'manifest.json').write_text(encode(manifest),encoding='utf-8')
    (folder/'config.json').write_text(encode(clone),encoding='utf-8')
    # Separate editable human expectations; never part of model input.
    (folder/'expected.json').write_text(encode({a['id']:dict(topics=None,symbols=None,impacts=None,reviewer='',notes='') for a in chosen}),encoding='utf-8')
    from discovery_ai import request
    (folder/'event_requests.json').write_text(encode([dict(source_id=a['id'],request=request(a,c['llm'],s['periods'])) for a in chosen]),encoding='utf-8')
    return folder


def report(folder,now=None):
    now=now or datetime.now(timezone.utc);folder=Path(folder).resolve()
    from system_runtime import read_config
    from system_evening import build,html
    c=read_config(folder/'config.json');manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    raw=dict(manifest);hashed=raw.pop('input_hash')
    if digest(raw)!=hashed:raise ValueError('Manifest changed; create a new evaluation')
    ids=[case['article']['id'] for case in manifest['cases']]
    if settings(c)['source_ids']!=ids or Path(settings(c)['db'])!=folder/'discovery.sqlite3':raise ValueError('Evaluation source IDs/database changed')
    expected=json.loads((folder/'expected.json').read_text(encoding='utf-8'))
    current={a['id'] for a in sources(c,now)}
    registry={x['symbol']:x['revision'] for x in companies(settings(c)['companies_db'],now)}
    registry_changed=registry!=manifest['company_revisions']
    rows=candidate_rows(c,now);cases=[]
    # Even when there are no company candidates, inspect the stage-1 cache.
    from contextlib import closing
    from system_data import readonly
    events={}
    if Path(settings(c)['db']).is_file():
        with closing(readonly(settings(c)['db'])) as db:
            for source,status,result,finished in db.execute("SELECT source_id,status,result,finished_at FROM discovery_ai WHERE stage='event' ORDER BY rowid"):
                from system_data import stamp
                if finished and stamp(finished)>now:continue
                events[source]=dict(status=status,result=json.loads(result) if result else None)
    for case in manifest['cases']:
        a=case['article'];found=[r for r in rows if r['article']['id']==a['id']]
        event=events.get(a['id'],dict(status='not_run',result=None))
        impacts={r['symbol']:r['impact']['result']['short'] for r in found if r['impact']['status']=='ok'}
        topics=[x['topic'] for x in (event['result'] or {}).get('industries',[])]
        symbols=[r['symbol'] for r in found];e=expected.get(a['id'],{})
        checked=bool(e.get('reviewer')) and all(e.get(k) is not None for k in ('topics','symbols','impacts'))
        completed=event['status']=='ok' and bool(found) and all(r['impact']['status']=='ok' for r in found[:settings(c)['max_candidates']])
        state='AI未検証（未解析・不足あり）'
        if completed:state='人手期待値未確認'
        if completed and checked:
            state='一致' if set(topics)==set(e['topics']) and set(symbols)==set(e['symbols']) and impacts==e['impacts'] else '不一致'
        if a['id'] not in current:state='入力記事が変更・期限外。再準備が必要'
        if registry_changed:state='企業DBが変更。再準備が必要'
        cases.append(dict(title=a['title'],source_id=a['id'],url=a['url'],published_at=a.get('published_at'),
            event_status=event['status'],topics=topics,symbols=symbols,impacts=impacts,state=state,
            candidates=found,expectation=e))
    evening=build(c,now)
    output=folder/('report_'+now.strftime('%Y%m%dT%H%M%S%f'));output.mkdir(exist_ok=False)
    result=dict(at=now.isoformat(),cases=cases,registry_changed=registry_changed,paid_calls=0,
        notice='保存済み結果の検証。未解析を成功と扱わない。好悪材料は上昇確率ではない。')
    (output/'validation.json').write_text(encode(result),encoding='utf-8')
    (output/'evening.json').write_text(encode(evening),encoding='utf-8')
    table=''.join('<tr><td>'+escape(x['title'])+'</td><td>'+escape(x['event_status'])+'</td><td>'+escape(x['state'])+'</td></tr>' for x in cases)
    page=html(evening).replace('</style>','</style><h1>少数実ニュースの検証</h1><p>'+escape(result['notice'])+'</p><table>'+table+'</table>',1)
    (output/'report.html').write_text(page,encoding='utf-8')
    return output,result


def mock_report(folder,now=None):
    """In-memory plumbing exercise on real text. Never writes an AI result cache."""
    now=now or datetime.now(timezone.utc);folder=Path(folder).resolve()
    from system_runtime import read_config
    from discovery_ai import parse
    from discovery_integration import report_rows
    from system_evening import build,html
    c=read_config(folder/'config.json');s=settings(c)
    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    raw=dict(manifest);expected_hash=raw.pop('input_hash')
    if digest(raw)!=expected_hash:raise ValueError('Manifest changed')
    index=CompanyIndex(companies(s['companies_db'],now));rows=[]
    def envelope(result):return dict(status='completed',output=[dict(type='message',content=[dict(type='output_text',text=encode(result))])])
    for case in manifest['cases']:
        a=case['article'];text=(a.get('body') or a['title'])[:1000];tags=local_topics(a['title']+' '+a.get('body',''))
        event=dict(summary='MOCK: 配線検証のみ',kind='モック',industries=[dict(topic=t,direction='不明',mechanism='モック・影響未評価',delay='unknown',quote=text) for t in tags],direct_names=[],facts=[],plans=[],unknowns=['実LLM未実行'])
        event=parse(envelope(event),a)
        for f in discover(a,tags,index)[:s['max_candidates']]:
            quotes=[r['quote'] for r in f['matched_relations'] if r['state']=='確認済み']
            impact=dict(symbol=f['symbol'],business='モック。事業評価なし',relation='不明',short='不明',medium='不明',long='不明',importance='不明',reason='実LLM未実行',conditions=[],news_quote=text,company_quote=quotes[0] if quotes else '',unknowns=['実LLM未実行'])
            impact=parse(envelope(impact),a,f)
            ident=digest(['MOCK',a['id'],f['symbol']])
            stage=lambda value:dict(id='MOCK:'+ident,status='ok',result=value,finished_at=now.isoformat(),model='MOCK-no-api')
            rows.append(dict(f,id=ident,article=a,event=stage(event),impact=stage(impact),periods=s['periods'],watched=f['symbol'] in c['symbols'],state='MOCK_ONLY',review_reason='MOCK_NO_REAL_ANALYSIS',discovered_at=now.isoformat()))
    evening=build(c,now);evening['discovered_companies']=report_rows(c,now,rows)
    evening['inputs']['discovery']=evening['discovered_companies'];evening['input_hash']=digest(evening['inputs'])
    evening['limitations'].insert(0,'MOCK: 実ニュースの原文を使用した配線検証。AI精度・好悪材料を検証した結果ではありません。')
    target=folder/('mock_'+now.strftime('%Y%m%dT%H%M%S%f'));target.mkdir(exist_ok=False)
    (target/'report.json').write_text(encode(evening),encoding='utf-8')
    (target/'report.html').write_text(html(evening).replace('</style>','</style><h1>MOCK・有料API未実行・判断精度未検証</h1>',1),encoding='utf-8')
    return target


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['prepare','dry-run','report','mock'])
    p.add_argument('--folder',type=Path,required=True);p.add_argument('--limit',type=int,default=3)
    p.add_argument('--config',type=Path,default=Path(__file__).resolve().parent/'config/paper.json')
    a=p.parse_args(argv)
    from system_runtime import read_config
    if a.command=='prepare':print(prepare(read_config(a.config),a.folder,a.limit))
    elif a.command=='mock':print(mock_report(a.folder)/'report.html')
    elif a.command=='dry-run':
        c=read_config(a.folder/'config.json');c.update(dry_run=True,network_enabled=False)
        print(encode(process(c)))
    else:
        folder,result=report(a.folder)
        for row in result['cases']:print(row['title'],row['state'])
        print(folder/'report.html')


if __name__=='__main__':main()
