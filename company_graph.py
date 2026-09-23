"""Versioned, evidence-backed company/business registry; never an order universe."""
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
import json
import math
import sqlite3
from urllib.parse import urlsplit
from system_data import readonly,stamp,encode,digest

TOPICS={
    'rare_earth':['レアアース','希土類'], 'semiconductor':['半導体','AI','人工知能'],
    'banking':['銀行','日銀','FRB','政策金利','金融政策'],
    'real_estate':['不動産','政策金利','金融政策'],
    'export_manufacturing':['輸出','為替','円安','円高','政策金利'],
    'energy':['エネルギー','原油','発電'], 'healthcare':['医薬品','医療','感染症'],
    'trade':['貿易','関税','輸出規制'], 'policy':['補助金','規制緩和','政府施策']}
ROLES=['採掘','探査・開発','精製','加工','輸入','販売','原材料利用','装置供給','金融','不動産','製造','その他','不明']


def connect(path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path)
    db.executescript('''CREATE TABLE IF NOT EXISTS company_revisions(
        id TEXT PRIMARY KEY,symbol TEXT,recorded_at TEXT,payload TEXT);
        CREATE INDEX IF NOT EXISTS company_asof ON company_revisions(symbol,recorded_at);''')
    db.commit();return db


def validate(company,now):
    if set(company)!={'symbol','name','aliases','industry','business','updated_at','relations'}:
        raise ValueError('Company fields mismatch')
    code=company['symbol']
    if not isinstance(code,str) or len(code)!=4 or not code.isascii() or not code.isalnum():raise ValueError('Invalid symbol')
    for k in ('name','industry','business'):
        if not isinstance(company[k],str) or not company[k].strip():raise ValueError('Company text required')
    if stamp(company['updated_at'])>now:raise ValueError('Future company update')
    if not isinstance(company['aliases'],list) or not all(isinstance(x,str) and len(x)>=2 for x in company['aliases']):raise ValueError('Invalid aliases')
    if not isinstance(company['relations'],list):raise ValueError('Relations must be a list')
    for r in company['relations']:
        if set(r)!={'topic','role','business','state','direct_business','source','quote','verified_at','revenue_ratio','profit_ratio','ratio_evidence'}:
            raise ValueError('Relation fields mismatch')
        if r['topic'] not in TOPICS or r['role'] not in ROLES or r['state'] not in ('確認済み','推定','不明') or r['direct_business'] not in ('あり','なし','不明'):
            raise ValueError('Unknown relation enum')
        if not all(isinstance(r[k],str) for k in ('business','source','quote','ratio_evidence')):raise ValueError('Invalid evidence')
        if r['state']=='確認済み':
            if not r['quote'].strip() or not r['business'].strip() or urlsplit(r['source']).scheme not in ('http','https') or not urlsplit(r['source']).hostname:
                raise ValueError('Confirmed relation needs source and quotation')
            if not r['verified_at'] or stamp(r['verified_at'])>now:raise ValueError('Unverified/future relation')
        elif r['verified_at'] and stamp(r['verified_at'])>now:raise ValueError('Future verification')
        for k in ('revenue_ratio','profit_ratio'):
            v=r[k]
            if v is not None and (type(v) not in (float,int) or not math.isfinite(v) or not 0<=v<=1 or not r['ratio_evidence']):
                raise ValueError('Ratios require finite fraction and evidence; otherwise null')
    return company


def import_companies(path,records,now=None):
    now=now or datetime.now(timezone.utc)
    if not isinstance(records,list) or len({r['symbol'] for r in records})!=len(records):raise ValueError('Duplicate company records')
    for c in records:validate(c,now)
    with closing(connect(path)) as db,db:
        for c in records:
            # Every revision is retained; repeated identical imports do not rewrite known-at.
            db.execute('INSERT OR IGNORE INTO company_revisions VALUES (?,?,?,?)',(digest(c),c['symbol'],now.isoformat(),encode(c)))
    return len(records)


def companies(path,asof):
    if not Path(path).is_file():return []
    latest={}
    with closing(readonly(path)) as db:
        for ident,code,recorded,raw in db.execute('SELECT * FROM company_revisions ORDER BY recorded_at,rowid'):
            if stamp(recorded)>asof:continue
            c=json.loads(raw)
            if digest(c)!=ident:raise ValueError('Company registry integrity mismatch')
            validate(c,asof)
            latest[code]=dict(c,revision=ident,recorded_at=recorded)
    return list(latest.values())


def local_topics(text):
    return [tag for tag,words in TOPICS.items() if any(word in text for word in words)]


def discover(article,topic_ids,registry):
    text=article['title']+'\n'+article.get('body','')
    found=[]
    for c in registry:
        names=[c['name']]+c['aliases']
        named=[name for name in names if name in text]
        relations=[r for r in c['relations'] if r['topic'] in topic_ids]
        if not named and not relations:continue
        verified=[r for r in relations if r['state']=='確認済み']
        found.append(dict(symbol=c['symbol'],company=c,matched_relations=relations,named_in_news=bool(named),
            names_in_news=named,relation='記事に企業名あり' if named else '事業情報から探索（間接候補）',
            relation_state='確認済み' if verified else '不明',
            project_participation='不明（事業関連だけでは当該案件への参画を意味しない）',
            rank=100*bool(named)+10*len(verified)+len(relations)))
    return sorted(found,key=lambda r:(-r['rank'],r['symbol']))
