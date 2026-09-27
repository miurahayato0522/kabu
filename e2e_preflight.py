"""Read-only readiness check for one paper end-to-end cycle."""
from datetime import datetime,timezone
from pathlib import Path
import sqlite3


def _stage(name,status,detail=''):
    return dict(name=name,status=status,detail=detail)


def build(c,now=None):
    """Inspect persisted state only.  No API call, order, or database write."""
    from system_status import snapshot
    from system_data import JST
    from news_discovery import sources
    from corporate_actions import ConfirmedActions
    from system_paper import live_marks
    now=now or datetime.now(timezone.utc)
    s=snapshot(c,now);cal=s['calendar'];account=s['account'];stages=[];blockers=[]
    expected=cal['analysis_day'] if now.astimezone(JST).hour>=16 or not cal['is_session'] else cal['previous_day']
    by_code={r['code'][:4]:r for r in s['daily_by_symbol']}
    daily_bad=[code for code in c['symbols'] if by_code.get(code,{}).get('day','')<expected]
    stages.append(_stage('daily','PASS' if not daily_bad else 'BLOCKED',
                         'latest '+str(s['daily_prices'].get('day')) if not daily_bad else 'missing '+','.join(daily_bad)))
    stages.append(_stage('news_collection','PASS' if s['news_coverage'] else 'BLOCKED',
                         'fresh successful RSS poll' if s['news_coverage'] else 'collection missing, failed, or stale'))
    try:
        articles=sources(c,now)
        fresh=[a for a in articles if a['quality']['eligible'] and a['quality']['category'] in ('company_direct','industry')]
        stages.append(_stage('news_quality','PASS','saved '+str(len(articles))+' / eligible '+str(len(fresh))))
    except (OSError,ValueError,KeyError,sqlite3.Error) as exc:
        fresh=[];stages.append(_stage('news_quality','BLOCKED',type(exc).__name__))
    q=s['queue'];budget=q['budget'];key_set=bool(__import__('os').environ.get('OPENAI_API_KEY'))
    if c['dry_run']:ai_status,ai_detail='BLOCKED','DRY_RUN'
    elif not c['network_enabled']:ai_status,ai_detail='BLOCKED','network disabled'
    elif not budget.get('rates_configured'):ai_status,ai_detail='BLOCKED','rates not configured'
    elif not key_set:ai_status,ai_detail='BLOCKED','OPENAI_API_KEY not set'
    elif not fresh:ai_status,ai_detail='WAITING','no fresh eligible article'
    else:ai_status,ai_detail='READY','fresh eligible articles '+str(len(fresh))
    stages.append(_stage('news_ai',ai_status,ai_detail))
    stages.append(_stage('chart','PASS' if not daily_bad else 'BLOCKED','existing chart strategy: '+c['chart']['strategy']))
    if s['market_state']!='取引時間内':quote_status,quote_detail='WAITING_MARKET','XTKS market closed'
    else:
        try:
            live_marks(c['paths']['ticks'],[code+'0' for code in c['symbols']],now)
            quote_status,quote_detail='PASS','all watch symbols: price > 0, exchange=1, market and receipt <=60 seconds'
        except (OSError,ValueError,KeyError,sqlite3.Error) as exc:
            quote_status,quote_detail='BLOCKED',str(exc)
    stages.append(_stage('current_price',quote_status,quote_detail))
    action_day=cal['today'] if cal['is_session'] else cal['next_session']
    action_from=account.get('day') or cal['previous_day'];actions={};held_blocks=[]
    for code in c['symbols']:
        try:
            ConfirmedActions(c['paths']['actions']).between(code,action_from,action_day,now)
            actions[code]='CONFIRMED'
        except (OSError,ValueError,sqlite3.Error) as exc:
            actions[code]='REVIEW_REQUIRED: '+type(exc).__name__
            if account.get('positions',{}).get(code+'0',0):held_blocks.append(code)
    action_status='PASS' if all(v=='CONFIRMED' for v in actions.values()) else ('BLOCKED' if held_blocks else 'PARTIAL')
    action_detail='all confirmed' if action_status=='PASS' else ('held symbols '+','.join(held_blocks) if held_blocks else 'unheld symbols blocked individually')
    stages.append(_stage('corporate_action',action_status,action_detail))
    prerequisites=not daily_bad and s['news_coverage'] and quote_status=='PASS' and not held_blocks
    if not prerequisites:
        integration='NOT_REACHED';risk='NOT_REACHED';paper='NOT_REACHED';ledger='NOT_REACHED'
    else:
        integration='READY';risk='READY';paper='READY';ledger='READY'
    stages += [_stage('integration',integration,'saved AI results flow through BusinessImpactStrategy'),
               _stage('risk',risk,'existing limits are unchanged'),
               _stage('paper_decision',paper,'NO_TRADE is a successful decision-engine result'),
               _stage('paper_ledger',ledger,'writes only when the user runs the paper cycle'),
               _stage('report','PASS','evening-report uses saved data')]
    for x in stages:
        if x['status'] in ('BLOCKED','WAITING_MARKET','NOT_REACHED'):
            blockers.append(x['name']+': '+x['detail'])
    executable=not blockers and ai_status=='READY'
    return dict(at=now.isoformat(),bot_state=s['bot_state'],daily_by_symbol=s['daily_by_symbol'],
        news_queue=q['counts'],fresh_trial_articles=[dict(id=a['id'],title=a['title'],category=a['quality']['category']) for a in fresh[:10]],
        dry_run=c['dry_run'],network_enabled=c['network_enabled'],api_rates_configured=budget.get('rates_configured'),
        api_budget=budget,openai_api_key_set=key_set,current_quote=dict(status=quote_status,detail=quote_detail,last_received_at=s['last_tick_received_at']),
        corporate_actions=actions,account=account,stages=stages,e2e_executable=executable,blockers=blockers,
        notice='Read-only: no API request, order, or SQLite write was performed.')


def display(result):
    lines=['E2E preflight: '+('PASS' if result['e2e_executable'] else 'BLOCKED'),
           'Bot: '+result['bot_state'],
           'dry_run='+str(result['dry_run'])+' / network_enabled='+str(result['network_enabled'])+
           ' / API rates='+str(result['api_rates_configured'])+' / OPENAI_API_KEY set='+str(result['openai_api_key_set']),
           'AI queue: '+str(result['news_queue'])+' / fresh trial candidates: '+str(len(result['fresh_trial_articles'])),
           'Quote: '+result['current_quote']['status']+' / '+result['current_quote']['detail']]
    lines += [x['name'].ljust(18)+' '+x['status']+' '+x['detail'] for x in result['stages']]
    if result['blockers']:lines += ['Blockers:']+['- '+x for x in result['blockers']]
    return '\n'.join(lines)
