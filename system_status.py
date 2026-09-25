"""Read-only operation snapshots, with explicit missing data and JST display."""
from contextlib import closing
from datetime import timedelta
from pathlib import Path
import json
import sqlite3
from system_data import readonly,stamp,JST
from system_queue import preview


def rows(path,sql,args=()):
    if not Path(path).is_file():return []
    with closing(readonly(path)) as db:
        db.row_factory=sqlite3.Row
        return [dict(r) for r in db.execute(sql,args)]


def sessions(now):
    import exchange_calendars as xcals
    day=now.astimezone(JST).date()
    cal=xcals.get_calendar('XTKS',start=str(day-timedelta(days=30)),end=str(day+timedelta(days=30)))
    days=[str(d.date()) for d in cal.sessions]
    today=str(day)
    return dict(today=today,is_session=today in days,
        analysis_day=max(d for d in days if d<=today),
        previous_day=max(d for d in days if d<today),next_session=min(d for d in days if d>today))


def account(c,now):
    found=rows(c['paths']['ledger'],"SELECT payload FROM system_state WHERE key='account'")
    if not found:return dict(status='未開始',at=None,cash=c['initial_cash'],equity=c['initial_cash'],positions={},realized=0,unrealized=0,fills=[])
    state=json.loads(found[0]['payload'])
    if stamp(state['at'])>now:return dict(status='未来の口座状態のため利用不可',at=state['at'],positions={})
    a=state['account'];equity=state['equity']
    return dict(status='最終保存評価（現在値への再評価ではありません）',at=state['at'],
        cash=a['cash'],equity=equity,positions=a['positions'],realized=a['realized'],
        unrealized=equity-a['cash']-sum(a['costs'].values()),fills=a['fills'],day=a.get('day'))


def snapshot(c,now):
    calendar=sessions(now)
    local=now.astimezone(JST)
    market=calendar['is_session'] and any(a<=local.strftime('%H:%M')<=b for a,b in c['trading_hours'])
    jobs={r['name']:r for r in rows(c['paths']['runtime'],'SELECT * FROM jobs')}
    for job in jobs.values():
        value=job.get('finished_at')
        job['finished_at_jst']=stamp(value).astimezone(JST).isoformat() if value else None
        job['next_at_jst']=stamp(job['next_at']).astimezone(JST).isoformat() if job.get('next_at') else None
        for label,condition in [('last_success',"status='ok'"),('last_failure',"status IN ('failed','partial')")]:
            found=rows(c['paths']['runtime'],f'SELECT at,detail FROM logs WHERE job=? AND {condition} ORDER BY id DESC LIMIT 1',(job['name'],))
            job[label]=dict(at_jst=stamp(found[0]['at']).astimezone(JST).isoformat(),detail=found[0]['detail']) if found else None
    meta=rows(c['paths']['runtime'],"SELECT value FROM runtime_meta WHERE key='heartbeat'")
    heartbeat=meta[0]['value'] if meta else None
    active=bool(heartbeat and 0<=(now-stamp(heartbeat)).total_seconds()<120)
    logs=rows(c['paths']['runtime'],'SELECT at,job,status,detail FROM logs ORDER BY id DESC LIMIT 20')
    for log in logs:log['at_jst']=stamp(log['at']).astimezone(JST).isoformat()
    news=jobs.get('news',{})
    healthy=bool(news.get('status')=='ok' and news.get('finished_at') and 0<=(now-stamp(news['finished_at'])).total_seconds()<=c['intervals']['news']*2)
    queue=preview(c,now)
    errors=[f"{name}: {j['status']}" for name,j in jobs.items() if j['status'] in ('failed','partial') and (market or name not in ('prices','paper'))]
    warnings=[];stop=[]
    if c['dry_run']:warnings.append('ニュースAI: DRY_RUN（有料解析なし）')
    if not market:warnings.append('現在値・仮想売買: 市場時間外のため待機')
    if not market:
        warnings += [f"前回の取引時間内エラー {name}: {jobs[name]['status']}" for name in ('prices','paper') if jobs.get(name,{}).get('status')=='failed']
    if not c['network_enabled']:warnings.append('ネットワーク無効')
    if not healthy:stop.append('ニュース収集未確認・失敗・鮮度不足')
    if queue['counts'].get('pending',0) or queue['counts'].get('failed',0) or queue['counts'].get('started',0):stop.append('ニュース解析待ち・失敗・未完了あり')
    if any(not x['reason'].startswith('old_') for x in queue['exclusions']):stop.append('日時不明・不正等で除外されたニュースあり（好悪未確認）')
    if Path(c['paths']['stop_new']).exists():stop.append('新規買い停止ファイルあり')
    price=jobs.get('prices',{})
    if market and not (price.get('status')=='ok' and price.get('finished_at') and 0<=(now-stamp(price['finished_at'])).total_seconds()<=c['intervals']['prices']*2):
        stop.append('現在値の取得なし・失敗・鮮度不足')
    acc=account(c,now)
    action_errors=[]
    from corporate_actions import ConfirmedActions
    # Display the required operating session even when the market is closed.
    check_day=calendar['today'] if calendar['is_session'] else calendar['next_session']
    for symbol in c['symbols']:
        try:ConfirmedActions(c['paths']['actions']).between(symbol,acc.get('day') or calendar['previous_day'],check_day,now)
        except (OSError,ValueError,sqlite3.Error):action_errors.append(symbol)
    if action_errors:stop.append('企業行動未確認のため仮想売買停止中: '+','.join(action_errors))
    success=rows(c['paths']['runtime'],"SELECT max(at) AS at FROM logs WHERE status='ok'")
    prices=rows(c['paths']['prices'],'SELECT max(fetched_at) AS at,max(day) AS day FROM daily_prices')
    by_symbol=rows(c['paths']['prices'],'SELECT code,max(day) AS day,count(*) AS count FROM daily_prices GROUP BY code')
    expected=calendar['analysis_day'] if local.hour>=16 or not calendar['is_session'] else calendar['previous_day']
    dates={r['code'][:4]:r['day'] for r in by_symbol}
    stale=[s for s in c['symbols'] if dates.get(s,'')<expected]
    if stale:stop.append('確定日足不足（必要日 '+expected+'）: '+','.join(stale))
    ticks=rows(c['paths']['ticks'],"SELECT max(received_at) AS at FROM events WHERE source IN ('snapshot','push') AND environment='production'")
    rss=rows(c['paths']['news'],'''SELECT symbol,finished_at,status,received,error FROM fetch_runs
        WHERE id IN (SELECT max(id) FROM fetch_runs GROUP BY symbol) ORDER BY symbol''')
    return dict(at=now.isoformat(),at_jst=local.isoformat(),calendar=calendar,
        bot_state=('稼働中・一部ジョブ失敗' if errors else '直近のハートビートあり（稼働の目安）') if active else '停止中または応答なし・起動未確認',heartbeat=heartbeat,
        market_state='取引時間内' if market else '市場時間外',
        last_success=success[0]['at'] if success else None,jobs=jobs,logs=logs,
        daily_prices=prices[0] if prices else dict(at=None,day=None),daily_by_symbol=by_symbol,news_coverage=healthy,
        last_tick_received_at=ticks[0]['at'] if ticks else None,rss_queries=rss,
        queue=queue,account=acc,errors=errors,warnings=warnings,stop_new_reasons=stop)


def display(s):
    lines=[f"確認: {s['at_jst']}",f"Bot: {s['bot_state']} / {s['market_state']}",
           f"次の東証営業日: {s['calendar']['next_session']}",f"最終正常実行: {s['last_success']}",
           f"日足: {s['daily_prices']}",f"現在値の最終受信: {s['last_tick_received_at']}"]
    for name in ('news','daily','prices','analysis','paper','documents'):
        job=s['jobs'].get(name,{})
        lines.append(f"{name}: {job.get('status','未実行')} / {job.get('finished_at_jst','日時なし')}")
        lines.append(f"  最終成功: {job.get('last_success')} / 最終失敗: {job.get('last_failure')} / 次回: {job.get('next_at_jst')}")
    lines.append('銘柄別日足: '+str(s.get('daily_by_symbol',[])))
    q=s['queue'];a=s['account']
    lines += [f"AIキュー: {q['counts']} / 次回送信予定 {q['planned_send_count']}件 / DRY_RUN={q['dry_run']}",
              f"予算: {q['budget']}",f"仮想口座: 評価日時 {a.get('at')} / 資産 {a.get('equity')} / 現金 {a.get('cash')}",
              f"保有: {a['positions']} / 実現損益 {a.get('realized')} / 評価損益 {a.get('unrealized')}"]
    lines += ['注意: '+x for x in s['warnings']+s['errors']+s['stop_new_reasons']]
    lines += [f"RSS {r['symbol']}: {r['status']} / 受信 {r['received']}件 / {r['finished_at']}" for r in s['rss_queries']]
    lines += [f"ログ {x['at_jst']} {x['job']} {x['status']} {x['detail']}" for x in s['logs']]
    return '\n'.join(lines)
