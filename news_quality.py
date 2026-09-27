"""Deterministic candidate quality metadata, not financial impact judgments."""
import re
import unicodedata
from urllib.parse import urlsplit
from system_data import digest,stamp

ALIASES={'7203':['トヨタ'],'8306':['三菱UFJ'],'6758':['ソニー'],'6501':['日立'],
         '4502':['武田薬品'],'2914':['JT','日本たばこ'],'9432':['NTT'],'9984':['ソフトバンクグループ']}
EVENTS=('決算','修正','提携','受注','訴訟','規制','建設','発表','発売','開始','承認','買収','撤退','停止','中止','再開','増益','減益','工場')

def normalized(text):return re.sub(r'\s+','',unicodedata.normalize('NFKC',text))

def classify(a,c,now):
    title=a['title'].split(' - ')[0];text=title+' '+a.get('body','');url=a.get('url','')
    named=[]
    for code,name in c['symbols'].items():
        names=[name]+ALIASES.get(code,[])
        mention_text=text.replace('日立市','') if code=='6501' else text
        if code=='9432' and any(w in text for w in ('NTTデータ','ＮＴＴデータ','NTTドコモ')) and not any(w in text for w in ('9432','NTTグループ','ＮＴＴグループ')):
            continue  # Subsidiary mentions alone do not prove parent-company impact.
        if any(n and (re.search(r'(?<![A-Za-z])'+re.escape(n)+r'(?![A-Za-z])',mention_text) if n.isascii() else n in mention_text) for n in names):named.append(code)
    # Discovery can recognise a registered, non-watched company without sending the
    # whole registry to an LLM.  Use title-only, reasonably specific names: article
    # bodies often contain broad lists and short aliases are too ambiguous here.
    registry=c.get('_quality_company_index')
    if registry:
        for row in registry.select(title,[]):
            names=[row['name']]+row.get('aliases',[])
            if any(len(normalized(name)) >= 3 and name in title for name in names):
                named.append(row['symbol'])
    named=sorted(set(named))
    from company_graph import local_topics
    topics=local_topics(text)
    macro=any(w in text for w in ('為替','CPI','雇用統計','政策金利','金融政策','FRB','日銀','ユーロ','原油','地政学'))
    reason=None
    # Event language overrides a title keyword; explicit board URLs remain references.
    path=urlsplit(url).path.lower()
    if '/bbs/' in path or '/messageboard' in path:reason='MESSAGE_BOARD'
    elif not any(w in title for w in EVENTS):
        for words,code in [(('基準価額','基準価格'),'FUND_INFORMATION_PAGE'),(('掲示板',),'MESSAGE_BOARD'),
            (('株価情報','株価・株式','板情報','株価チャート'),'PRICE_INFORMATION_PAGE'),
            (('企業情報','会社概要','企業基本情報','検索結果','ランキング'),'STATIC_REFERENCE_PAGE')]:
            if any(w in title for w in words):reason=code;break
        if not reason and any(w in title for w in ('購入する方法','価格比較','最安','クーポン','抽選会','お買い物券')):
            reason='PROMOTIONAL_OR_REFERENCE_PAGE'
    category='noise_or_reference' if reason else ('company_direct' if named else ('macro' if macro else ('industry' if topics else 'noise_or_reference')))
    from system_queue import eligibility
    age=eligibility(a,c,now)
    reasons=[reason] if reason else []
    if age:reason='STALE_NEWS' if age.startswith('old_') else 'UNKNOWN_PUBLICATION_TIME'
    if not reason and category=='noise_or_reference':reason='LOW_COMPANY_RELEVANCE'
    # Macro retained as shared context; no unsupported per-company API fanout.
    if not reason and category=='macro':reason='LOW_COMPANY_RELEVANCE'
    if reason and reason not in reasons:reasons.append(reason)
    score={'company_direct':300,'industry':200,'macro':100,'noise_or_reference':0}[category]
    host=urlsplit(url).hostname or ''
    return dict(version='quality-v1',category=category,direct_symbols=named,topics=topics,reason=reason,
        eligible=reason is None,reasons=reasons,priority=score,priority_reason=category+': freshness then stable ID',
        source_type=a.get('kind','rss_headline'),text_available=bool(a.get('body')),source_host=host,
        credibility=('official_domain_content_unverified' if host in ('www.jpx.co.jp','global.toyota','www.hitachi.com','www.sony.com','group.ntt','group.softbank') else 'unverified_source'),freshness_reason=age)

def event_cluster(a):
    title=a['title'].split(' - ')[0]
    day=a.get('published_at','') or ''
    try:day=stamp(day).astimezone(__import__('datetime').timezone(__import__('datetime').timedelta(hours=9))).date().isoformat()
    except (ValueError,TypeError):return digest(['undated',a.get('id'),title])
    if a.get('body'):return digest([day,normalized(a['body'])])
    # Only unambiguous, complete two-party partnership headlines are paraphrase merged.
    pattern=r'^(.+?)(?:が|、)(.+?)(?:と提携|との戦略的パートナーシップを発表)[。！]?$'
    m=re.fullmatch(pattern,title)
    if m and not any(w in title for w in ('解消','中止','検討','噂','予定','再','拡大')):
        return digest(['partnership',day,sorted(normalized(x) for x in m.groups())])
    return digest([day,normalized(a.get('body') or title)])

def annotate(articles,c,now):
    result=[];seen={}
    for a in sorted(articles,key=lambda x:(x.get('first_seen_at') or '',x.get('id',''),x['title'])):
        q=classify(a,c,now);key=event_cluster(a)
        ident=a.get('id') or digest([a['title'],a.get('url')])
        if key in seen:
            q.update(reason='DUPLICATE_EVENT',eligible=False,duplicate_of=seen[key],reasons=q['reasons']+['DUPLICATE_EVENT'])
        else:seen[key]=ident
        result.append(dict(a,quality=dict(q,event_cluster=key)))
    return sorted(result,key=lambda a:(-a['quality']['priority'],-stamp(a['published_at']).timestamp() if a.get('published_at') and a['quality']['freshness_reason'] not in ('invalid_published_at',) else 0,a.get('id','')))

def send_reason(a,c,now):
    # Re-evaluate freshness even for persisted quality metadata.
    q=classify(a,c,now)
    return a.get('quality',{}).get('reason') or q['reason']


def cache_states(c):
    from contextlib import closing
    from pathlib import Path
    from system_data import readonly
    if not Path(c['paths']['ai']).is_file():return {}
    with closing(readonly(c['paths']['ai'])) as db:
        return dict(db.execute('SELECT request_hash,status FROM ai_analyses'))


def cache_state(article,c,states):
    from news_ai import make_request,BODY_VERSION,VERSION
    from system_data import encode
    import hashlib
    version=('body-v4-impact' if 'body' in article else 'headline-v3-impact') if article.get('analyze_impact') else (BODY_VERSION if 'body' in article else VERSION)
    identity=dict(version=version,article_id=article['id'],payload=make_request(article,c['symbols'],c['llm']))
    if 'body' in article:identity['document_revision']=article.get('document_revision')
    return states.get(hashlib.sha256(encode(identity).encode()).hexdigest())


if __name__=='__main__':
    import argparse
    from collections import Counter
    from datetime import datetime,timezone
    from system_runtime import read_config
    from news_discovery import sources
    from system_data import encode
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['status'])
    p.add_argument('--config',default='config/paper.json');p.add_argument('--json',action='store_true')
    p.add_argument('--limit',type=int,default=10)
    args=p.parse_args()
    if not 1<=args.limit<=100:p.error('limit must be 1..100')
    rows=sources(read_config(args.config),datetime.now(timezone.utc))
    good=[a for a in rows if a['quality']['eligible']]
    result=dict(categories=dict(Counter(a['quality']['category'] for a in rows)),
        exclusions=dict(Counter(a['quality']['reason'] for a in rows if a['quality']['reason'])),
        eligible=len(good),articles=[dict(id=a['id'],title=a['title'],quality=a['quality']) for a in good[:args.limit]])
    print(encode(result))
