"""Explicit point-in-time corporate-action confirmations; no inferred absence."""
import argparse
from contextlib import closing
from datetime import date,timedelta
from pathlib import Path
import json
import sqlite3
from system_data import stamp,encode,digest,readonly
from daily_backtest import positive
from jquants_history import symbol_code


def import_confirmations(path,items):
    normalized=[]
    for item in items:
        if set(item)!={'symbol','day','kind','factor','verified_at','evidence'}:
            raise ValueError('Corporate confirmation fields invalid')
        row=dict(item,symbol=symbol_code(item['symbol']))
        date.fromisoformat(row['day'])
        stamp(row['verified_at'])
        if not isinstance(row['evidence'],str) or not row['evidence'].strip():
            raise ValueError('Evidence is required, including confirmation of no action')
        if row['kind'] not in ('none','split','reverse','unsupported') or not positive(row['factor']):
            raise ValueError('Unsupported action confirmation')
        if (row['kind']=='none' and row['factor']!=1) or (row['kind']=='split' and row['factor']>=1) or (row['kind']=='reverse' and row['factor']<=1):
            raise ValueError('Action type and factor disagree')
        normalized.append(row)
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS confirmations(symbol TEXT,day TEXT,id TEXT,payload TEXT,PRIMARY KEY(symbol,day))')
        for row in normalized:
            old=db.execute('SELECT payload FROM confirmations WHERE symbol=? AND day=?',(row['symbol'],row['day'])).fetchone()
            if old and old[0]!=encode(row):
                raise ValueError('Conflicting confirmation; existing evidence is immutable')
            db.execute('INSERT OR IGNORE INTO confirmations VALUES (?,?,?,?)',
                       (row['symbol'],row['day'],digest(row),encode(row)))


class ConfirmedActions:
    """Adapter boundary for future licensed corporate-action feeds."""
    def __init__(self,path):
        self.path=path

    def between(self,symbol,previous_day,day,asof):
        import exchange_calendars as xcals
        start=date.fromisoformat(previous_day)+timedelta(days=1)
        cal=xcals.get_calendar('XTKS',start=str(start-timedelta(days=10)),end=day)
        days=[str(d.date()) for d in cal.sessions if str(start)<=str(d.date())<=day]
        result=[]
        with closing(readonly(self.path)) as db:
            for target in days:
                row=db.execute('SELECT id,payload FROM confirmations WHERE symbol=? AND day=?',(symbol_code(symbol),target)).fetchone()
                if not row:
                    raise ValueError(f'Corporate action unconfirmed: {symbol} {target}')
                value=json.loads(row[1])
                if row[0]!=digest(value) or value['symbol']!=symbol_code(symbol) or value['day']!=target:
                    raise ValueError('Corporate confirmation integrity mismatch')
                if stamp(value['verified_at'])>asof or value['kind']=='unsupported':
                    raise ValueError(f'Corporate action unavailable/unsupported: {symbol} {target}')
                result.append(dict(value,id=row[0]))
        return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--file',required=True,type=Path)
    p.add_argument('--db',required=True,type=Path)
    args=p.parse_args()
    import_confirmations(args.db,json.loads(args.file.read_text(encoding='utf-8-sig')))
    print('Corporate confirmations imported; no API calls')


if __name__=='__main__':
    main()
