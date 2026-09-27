"""Yahoo observations for manual corroboration; never authorizes paper trading."""
import argparse
from contextlib import closing
from datetime import date,datetime,timezone
from pathlib import Path
import json
import math
import sqlite3
import time
from system_data import encode,digest,readonly
from jquants_history import symbol_code


def observe(frame,symbol,start,end,at):
    if frame.empty or frame.index.tz is None:raise ValueError('Empty or timezone-less action response')
    events=[]
    for stamp,row in frame.iterrows():
        day=str(stamp.tz_convert('Asia/Tokyo').date())
        for column,kind in [('Stock Splits','split'),('Dividends','dividend')]:
            number=float(row[column])
            if not math.isfinite(number) or number<0:raise ValueError('Invalid action value')
            if not number:continue
            events.append(dict(symbol=symbol_code(symbol),kind=('reverse' if number<1 else 'split') if kind=='split' else kind,
                observed_day=day,effective_day=None,ratio=number if kind=='split' else None,
                factor=1/number if kind=='split' else None,amount=number if kind=='dividend' else None,
                source='https://finance.yahoo.com/quote/'+symbol[:4]+'.T/history/',
                fetched_at=at,verified_at=None,status='unconfirmed',correction_status='unknown'))
    return dict(symbol=symbol_code(symbol),start=str(start),end=str(end),fetched_at=at,
        source='yahoo',status='unconfirmed',absence_confirmed=False,events=events)


def store(path,record):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        db.execute('INSERT OR IGNORE INTO observations VALUES (?,?)',(digest(record),encode(record)))


def report(path,confirmed_path=None):
    with closing(readonly(path)) as db:
        records=[json.loads(r[0]) for r in db.execute('SELECT payload FROM observations')]
    confirmations={}
    if confirmed_path and Path(confirmed_path).exists():
        with closing(readonly(confirmed_path)) as db:
            confirmations={(r[0],r[1]):json.loads(r[2]) for r in db.execute('SELECT symbol,day,payload FROM confirmations')}
    for record in records:
        for event in record['events']:
            found=confirmations.get((event['symbol'],event['observed_day']))
            event['ledger_comparison']='unconfirmed'
            if found:
                same=event['kind']==found['kind'] and event['factor']==found['factor']
                event['ledger_comparison']='same_values_still_verify_effective_day' if same else 'CONFLICT_MANUAL_REVIEW'
    return records


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['fetch','report'])
    p.add_argument('--config',default='config/paper.json')
    p.add_argument('--db',type=Path,required=True)
    p.add_argument('--symbols',nargs='+')
    p.add_argument('--from',dest='start',type=date.fromisoformat,default=date(2024,6,27))
    args=p.parse_args()
    from system_runtime import read_config,process_lock
    c=read_config(args.config)
    if args.db.resolve() in {Path(v).resolve() for v in c['paths'].values()}:p.error('Observation DB must be isolated from operating stores')
    if args.command=='fetch':
        from yahoo_history import fetch_frame
        import yfinance as yf
        yf.set_tz_cache_location(str(args.db.resolve().parent/'yfinance_cache'))
        with process_lock(str(args.db)+'.lock'):
            for i,s in enumerate(args.symbols or c['symbols']):
                if i:time.sleep(2)
                now=datetime.now(timezone.utc)
                try:
                    f=fetch_frame(symbol_code(s),args.start,now.date())
                    store(args.db,observe(f,s,args.start,now.date(),now.isoformat()))
                    print(s,'observed; unconfirmed')
                except Exception as exc:print(s,'failed',type(exc).__name__,'no confirmation created')
    else: print(encode(report(args.db,c['paths']['actions'])))


if __name__=='__main__':main()
