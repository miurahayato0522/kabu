"""Independent, fail-closed daily refresh; diagnostics contain no remote bodies."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import time
from system_data import readonly, JST, encode
from jquants_history import symbol_code, HistoryError


def latest(path, code):
    if not Path(path).exists(): return None
    with closing(readonly(path)) as db:
        return db.execute('SELECT max(day) FROM daily_prices WHERE code=?', (code,)).fetchone()[0]


def update(c, now, symbols=None, fetch=None, pause=time.sleep):
    from yahoo_history import fetch_frame, convert, save
    fetch = fetch or fetch_frame
    local = now.astimezone(JST)
    end = local.date() + timedelta(days=1) if local.hour >= 16 else local.date()
    start = datetime.fromisoformat(c['history_start']).date()
    results = []
    for i, symbol in enumerate(symbols or c['symbols']):
        if i: pause(2)
        code = symbol_code(symbol)
        r = dict(symbol=code, source='yahoo', start=str(start), end_exclusive=str(end),
                 at=now.isoformat(), status='failed', stage='read', last_good=None,
                 retry_at=(now+timedelta(seconds=max(300,c['intervals']['daily']))).isoformat())
        try:
            r['last_good'] = latest(c['paths']['prices'], code)
            r['stage'] = 'fetch'
            frame = fetch(code, start, local.date())
            if hasattr(frame,'columns'):
                r['received_count']=len(frame)
                r['received_last_day']=str(frame.index[-1].date()) if len(frame) else None
            r['stage'] = 'validate'
            data = convert(frame, code, start, end)
            import exchange_calendars as xcals
            cal=xcals.get_calendar('XTKS',start=str(end-timedelta(days=30)),end=str(end))
            expected=max(str(d.date()) for d in cal.sessions if str(d.date())<str(end))
            if not data or data[-1]['Date']!=expected:
                raise HistoryError('Latest completed session missing; expected='+expected)
            r['stage'] = 'save'
            save(Path(c['paths']['prices']), code, data)
            r.update(status='ok', count=len(data), last_good=data[-1]['Date'], retry_at=None)
        except Exception as exc:
            r['error_type'] = type(exc).__name__
            # Only our local validation exception messages are safe to publish.
            r['reason'] = str(exc) if isinstance(exc, HistoryError) and r['stage'] in ('validate','save') else 'provider_or_storage_error; remote response suppressed'
        results.append(r)
    good = sum(r['status']=='ok' for r in results)
    return dict(status='ok' if good==len(results) else ('partial' if good else 'failed'), symbols=results)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',default='config/paper.json')
    p.add_argument('--db',type=Path,required=True,help='Isolated output DB (runtime DB is prohibited)')
    p.add_argument('--report',type=Path,required=True)
    p.add_argument('--retry-report',type=Path)
    args=p.parse_args()
    from system_runtime import read_config,process_lock
    c=read_config(args.config)
    protected={Path(v).resolve() for v in c['paths'].values()}
    if args.db.resolve() in protected or args.report.resolve() in protected or args.report.resolve()==args.db.resolve():
        p.error('Output must be separate from all operating paths')
    symbols=None
    if args.retry_report:
        previous=json.loads(args.retry_report.read_text(encoding='utf-8'))
        symbols=[r['symbol'][:4] for r in previous['symbols'] if r['status']!='ok']
        if not symbols: print('No failed symbols');return
        if not set(symbols)<=set(c['symbols']):p.error('Retry symbols outside watch list')
    c['paths']['prices']=str(args.db.resolve())
    import yfinance as yf
    yf.set_tz_cache_location(str(args.db.resolve().parent/'yfinance_cache'))
    with process_lock(str(args.db)+'.lock'):
        result=update(c,datetime.now(timezone.utc),symbols)
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(encode(result),encoding='utf-8')
    print(encode(result))


if __name__=='__main__': main()
