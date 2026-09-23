"""Bounded news triage and read-only budget/queue previews. No API calls."""
from contextlib import closing
from pathlib import Path
import json
import math
from system_data import digest, encode, stamp, readonly


def eligibility(article, c, now):
    max_hours=c.get('analysis_max_age_hours',24)
    if not isinstance(max_hours,(int,float)) or not 0<max_hours<=168:
        raise ValueError('analysis_max_age_hours must be >0 and <=168')
    for field in ('published_at','first_seen_at'):
        if not article.get(field):return 'missing_'+field
        try:age=(now-stamp(article[field])).total_seconds()/3600
        except (ValueError,TypeError):return 'invalid_'+field
        if age<0:return 'future_'+field
        if age>max_hours:return 'old_'+field
    return None


def priority(article):
    text=article['title']+' '+article.get('body','')
    score=100*int(any(w in text for w in ('業績修正','上方修正','下方修正','倒産','上場廃止','決算')))
    score+=20*int(bool(article.get('body')))+10*int(article.get('scope_tags')==['company'])
    return score


def refresh_queue(db, articles, c, now):
    db.execute('CREATE TABLE IF NOT EXISTS queue_exclusions(id TEXT PRIMARY KEY,reason TEXT,at TEXT)')
    for article in articles:
        ident=digest([article['title'],article.get('body'),article.get('published_at'),article['symbols'],c['llm'],'impact-v1'])
        db.execute('INSERT OR IGNORE INTO analysis_queue VALUES (?,?,?,?)',
                   (ident,encode(article),'pending',now.isoformat()))
    pending=[]
    for ident,raw in db.execute("SELECT id,payload FROM analysis_queue WHERE status='pending'").fetchall():
        article=json.loads(raw)
        reason=eligibility(article,c,now)
        if reason:
            db.execute("UPDATE analysis_queue SET status='excluded',updated_at=? WHERE id=?",(now.isoformat(),ident))
            db.execute('INSERT OR REPLACE INTO queue_exclusions VALUES (?,?,?)',(ident,reason,now.isoformat()))
        else:pending.append((ident,raw))
    db.commit()
    return sorted(pending,key=lambda row:(-priority(json.loads(row[1])),-stamp(json.loads(row[1])['published_at']).timestamp(),row[0]))


def budget_status(c,now):
    try:
        path=Path(c['budget_file'])
        config=json.loads(path.read_text(encoding='utf-8-sig'))
        daily,monthly=0.,0.
        ledger=path.parent/config['ledger']
        day=now.astimezone(__import__('datetime').timezone.utc).date().isoformat()
        if ledger.is_file():
            with closing(readonly(ledger)) as db:
                daily=db.execute('SELECT coalesce(sum(coalesce(actual,reserved)),0) FROM api_costs WHERE day=?',(day,)).fetchone()[0]
                monthly=db.execute('SELECT coalesce(sum(coalesce(actual,reserved)),0) FROM api_costs WHERE substr(day,1,7)=?',(day[:7],)).fetchone()[0]
        rates=config.get('rates_per_million',{}).get(c['llm'])
        configured=bool(isinstance(rates,list) and len(rates)==2 and all(type(x) in (int,float) and math.isfinite(x) and x>0 for x in rates))
        return dict(day_utc=day,daily_used_usd=daily,daily_limit_usd=config['daily_usd'],
                    daily_remaining_usd=max(0,config['daily_usd']-daily),
                    monthly_used_jpy=monthly*config.get('jpy_per_usd',0),monthly_limit_jpy=config.get('monthly_jpy'),
                    monthly_remaining_jpy=max(0,config['monthly_jpy']-monthly*config.get('jpy_per_usd',0)) if 'monthly_jpy' in config else None,
                    jpy_per_usd=config.get('jpy_per_usd'),rates_configured=configured,rates=rates)
    except (OSError,ValueError,KeyError,__import__('sqlite3').Error):
        return dict(error='budget_unavailable',rates_configured=False)


def preview(c,now):
    counts,eligible,exclusions={},[],[]
    if Path(c['paths']['runtime']).is_file():
        with closing(readonly(c['paths']['runtime'])) as db:
            for ident,raw,status in db.execute('SELECT id,payload,status FROM analysis_queue'):
                counts[status]=counts.get(status,0)+1
                article=json.loads(raw)
                if status=='pending':
                    reason=eligibility(article,c,now)
                    if reason:exclusions.append(dict(id=ident,reason=reason))
                    else:eligible.append(dict(id=ident,title=article['title'],priority=priority(article),
                        published_at=article.get('published_at'),first_seen_at=article.get('first_seen_at'),article=article))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='queue_exclusions'").fetchone():
                exclusions += [dict(id=i,reason=r) for i,r in db.execute('SELECT id,reason FROM queue_exclusions')]
    eligible.sort(key=lambda a:(-a['priority'],-stamp(a['published_at']).timestamp(),a['id']))
    budget=budget_status(c,now)
    available=c['analysis_limit']
    # Match the existing conservative reservation formula; no budget mutation here.
    projected=[];cost=0.
    for item in eligible[:available]:
        if not budget.get('rates_configured'):break
        from news_ai import make_request
        request=make_request(item['article'],c['symbols'],c['llm'])
        rates=budget['rates']
        amount=((len(encode(request).encode('utf-8'))+4096)*rates[0]+request['max_output_tokens']*rates[1])/1e6
        if budget['daily_used_usd']+cost+amount>budget['daily_limit_usd']:break
        if budget.get('monthly_limit_jpy') is not None and budget['monthly_used_jpy']+(cost+amount)*budget['jpy_per_usd']>budget['monthly_limit_jpy']:break
        projected.append(item['id']);cost+=amount
    for item in eligible:item.pop('article')
    return dict(counts=counts,eligible_count=len(eligible),batch_limit=available,
        planned_send_count=0 if c['dry_run'] or not c['network_enabled'] else len(projected),
        budget_permitted_count=len(projected),estimated_batch_usd=cost,dry_run=c['dry_run'],
        candidates=eligible[:available],exclusions=exclusions,budget=budget)
