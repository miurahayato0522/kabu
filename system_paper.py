"""Forward simulation using saved kabu ticks. No networking, orders or credentials."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from system_data import readonly, stamp, encode, JST, daily_groups, digest
from system_backtest import Account
from system_strategy import decide


def live_marks(path, symbols, now, max_age=60):
    """A received quote is not executable retroactively; only fresh ticks qualify."""
    wanted = {s[:4]: s for s in symbols}
    latest, last_connection = {}, None
    with closing(readonly(path)) as db:
        # This initial implementation reads the snapshot; no persistent cursor.
        for ident, received, source, payload in db.execute(
                "SELECT id,received_at,source,payload FROM events WHERE environment='production' ORDER BY id"):
            rt = stamp(received)
            if rt > now:
                continue
            if source in ('connected','disconnected','stopped'):
                last_connection = (rt, source)
            if source not in ('push','snapshot'):
                continue
            row = json.loads(payload)
            if row.get('Symbol') not in wanted:
                continue
            pt = stamp(row['CurrentPriceTime'])
            price = row['CurrentPrice']
            from daily_backtest import positive
            if not positive(price) or row.get('Exchange') != 1 or pt > now:
                raise ValueError('Invalid live quote')
            latest[wanted[row['Symbol']]] = dict(price=price, at=pt.isoformat(), received_at=received, event_id=ident)
    if set(latest) != set(symbols):
        raise ValueError('Missing live symbols')
    for q in latest.values():
        if any(not 0 <= (now-stamp(q[k])).total_seconds() <= max_age for k in ('at','received_at')):
            raise ValueError('Stale live market/receipt time; account not advanced')
        if last_connection and last_connection[1] != 'connected' and last_connection[0] >= stamp(q['received_at']):
            raise ValueError('Collector disconnected; account not advanced')
    return latest


def ledger(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE IF NOT EXISTS system_state (key TEXT PRIMARY KEY, payload TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS system_events (id TEXT PRIMARY KEY, kind TEXT NOT NULL, at TEXT NOT NULL, payload TEXT NOT NULL)')
    db.commit()
    return db


def record_error(path, error):
    now = datetime.now(timezone.utc).isoformat()
    with closing(ledger(path)) as db, db:
        db.execute('INSERT INTO system_events VALUES (?,?,?,?)',
                   (digest([now,'error',error]),'ERROR',now,encode({'error':error})))


def step(path, bars, ticks_path, strategy, account, now=None, stop_new=False, actions=None, daily_once=False):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Paper time requires timezone')
    today = now.astimezone(JST).date().isoformat()
    used_model = getattr(strategy,'chart',strategy)
    if hasattr(used_model,'metadata') and stamp(used_model.metadata['created_at']) > now:
        raise ValueError('Model creation time is in the future')
    groups = daily_groups(bars)
    quotes = live_marks(ticks_path, groups, now)
    histories = {}
    import exchange_calendars as xcals
    cal = xcals.get_calendar('XTKS', start=str(now.date()-timedelta(days=15)), end=today)
    prior = [str(d.date()) for d in cal.sessions if str(d.date()) < today][-1]
    if today not in [str(d.date()) for d in cal.sessions]:
        raise ValueError('No XTKS session today')
    for symbol, series in groups.items():
        history = [b for b in series if b.day < today and stamp(b.available_at) <= now]
        if not history or history[-1].day != prior:
            raise ValueError('Latest completed daily session unavailable')
        histories[symbol] = history
    config = dict(strategy=strategy.name, source=bars[0].source, symbols=sorted(groups),
                  initial=account.initial, limits=__import__('dataclasses').asdict(account.limits),
                  fee=account.fee, slippage_bps=account.slippage_bps, spread_bps=account.spread_bps)
    news = getattr(strategy,'news',None)
    config['news_coverage'] = getattr(news,'coverage_policy',getattr(news,'coverage',None))
    if daily_once:
        config['daily_once'] = True
    with closing(ledger(path)) as db:
        db.execute('BEGIN IMMEDIATE')
        stored = db.execute("SELECT payload FROM system_state WHERE key='account'").fetchone()
        pending, previous_equity, previous_day = [], account.initial, None
        decision_keys, consumed_events, opened = set(), set(), {}
        if stored:
            state = json.loads(stored[0])
            if state['config'] != config:
                raise ValueError('Paper account configuration/model changed; use a new ledger')
            for key,value in state['account'].items():
                setattr(account,key,value)
            account.sent = {tuple(v) for v in state['sent']}
            pending = state['pending']
            decision_keys=set(state.get('decision_keys',[]))
            consumed_events=set(state.get('consumed_events',[]))
            opened=state.get('opened',{})
            previous_equity, previous_day = state['equity'], account.day
            if stamp(state['at']) >= now:
                raise ValueError('Paper time must advance')
        applied=[]
        if actions is not None:
            for symbol in sorted(groups):
                confirmations=actions.between(symbol,previous_day or prior,today,now)
                for confirmation in confirmations:
                    account.split(symbol,confirmation['factor'],confirmation['day'])
                    applied.append(confirmation)
        elif previous_day and previous_day != today and any(account.positions.values()):
            raise ValueError('Overnight paper holdings require confirmed corporate-action data')
        marks = {s:q['price'] for s,q in quotes.items()}
        account.start_day(today, previous_equity)
        old_orders, old_fills = len(account.orders), len(account.fills)
        keep = []
        if stop_new:
            from dataclasses import replace
            account.limits = replace(account.limits, stop_new=True)
        for d in sorted(pending, key=lambda d:(d['action']!='SELL',d['prediction']['symbol'])):
            symbol = d['prediction']['symbol']
            if stamp(quotes[symbol]['at']) <= stamp(d['prediction']['at']):
                keep.append(d)
                continue
            account.execute(d, marks[symbol], marks, quotes[symbol]['at'])
        decisions = []
        for s in sorted(groups):
            if any(d['prediction']['symbol']==s for d in keep):
                continue
            p = strategy.predict(histories[s], now.isoformat())
            d = decide(p, account.positions.get(s,0))
            event_refs=sorted(r for r in p.refs if r.startswith('event:'))
            key=digest([s,histories[s][-1].day,strategy.name,event_refs,p.direction,p.error])
            if daily_once and key in decision_keys:
                continue
            decision_keys.add(key)
            if daily_once and event_refs and d['action'] in ('BUY','SELL'):
                event_keys={s+':'+d['action']+':'+ref for ref in event_refs}
                if event_keys & consumed_events:
                    d['action']='HOLD' if account.positions.get(s,0) else 'NO_TRADE'
                    d['prediction']['reasons'].append('Same news event already used; duplicate candidate suppressed')
                else:
                    consumed_events.update(event_keys)
            decisions.append(d)
            if d['action'] in ('BUY','SELL'):
                keep.append(d)
        value = account.equity(marks)
        for f in account.fills[old_fills:]:
            if f['side']=='BUY':
                opened.setdefault(f['symbol'],f['at'])
            elif not account.positions.get(f['symbol']):
                opened.pop(f['symbol'],None)
        holdings={s:dict(quantity=q,cost=account.costs[s],average_cost=account.costs[s]/q,
                        price=marks[s],unrealized=q*marks[s]-account.costs[s],
                        opened_at=opened.get(s),valued_at=now.isoformat()) for s,q in account.positions.items() if q}
        record = dict(at=now.isoformat(), mode='forward_saved_ticks_simulation', decisions=decisions,
            orders=account.orders[old_orders:], fills=account.fills[old_fills:], pending=keep,
            cash=account.cash, positions=account.positions, equity=value,
            realized=account.realized, unrealized=value-account.cash-sum(account.costs.values()),
            quote_refs=quotes, news_and_model_available_at=now.isoformat(),
            holdings=holdings,corporate_actions=applied,
            limitations=['No spread/orderbook liquidity simulation beyond configured costs',
                         'Corporate actions require explicit evidence; missing/unsupported actions stop the account'])
        attrs = {k:v for k,v in vars(account).items() if k not in ('limits','sent')}
        state = dict(config=config, account=attrs, sent=sorted(account.sent), pending=keep,
                     equity=value, at=now.isoformat(),decision_keys=sorted(decision_keys),
                     consumed_events=sorted(consumed_events),opened=opened)
        db.execute('INSERT INTO system_events VALUES (?,?,?,?)',
                   (digest(record),'STEP',now.isoformat(),encode(record)))
        db.execute("INSERT OR REPLACE INTO system_state VALUES ('account',?)",(encode(state),))
        db.commit()
    return record


def report(path):
    with closing(readonly(path)) as db:
        return [dict(json.loads(payload), kind=kind, at=at) for kind,at,payload in
                db.execute('SELECT kind,at,payload FROM system_events ORDER BY at,id')]
