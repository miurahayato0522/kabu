"""Optional shared UTC-day cost ledger. Rates are explicit user configuration."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import json
import math
import sqlite3
from system_data import encode


class BudgetError(ValueError):
    pass


class DailyBudget:
    def __init__(self, config_path):
        config_path = Path(config_path)
        self.config = json.loads(config_path.read_text(encoding='utf-8'))
        self.path = config_path.parent / self.config['ledger']
        if self.path.resolve() == config_path.resolve():
            raise ValueError('Budget ledger must be separate')
        if not math.isfinite(self.config['daily_usd']) or self.config['daily_usd'] < 0:
            raise ValueError('Invalid daily budget')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS api_costs (id TEXT PRIMARY KEY, day TEXT, reserved REAL, actual REAL, payload TEXT)')

    def rates(self, model):
        rates = self.config['rates_per_million'][model]
        if len(rates)!=2 or any(not math.isfinite(x) or x<=0 for x in rates):
            raise ValueError('Explicit positive input/output prices required')
        return rates

    def reserve(self, request_id, payload):
        rates = self.rates(payload['model'])
        # Conservative engineering allowance, not a provider billing guarantee.
        input_bound = len(encode(payload).encode('utf-8'))+4096
        output_bound = payload['max_output_tokens']
        amount = (input_bound*rates[0]+output_bound*rates[1])/1e6
        day = datetime.now(timezone.utc).date().isoformat()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM api_costs WHERE id=?',(request_id,)).fetchone():
                db.rollback()
                raise BudgetError('予算台帳に送信予約があります。状態未確定の依頼は自動再送しません')
            used = db.execute('SELECT coalesce(sum(coalesce(actual,reserved)),0) FROM api_costs WHERE day=?',(day,)).fetchone()[0]
            if used+amount > self.config['daily_usd']:
                db.rollback()
                raise BudgetError('日次API見積り予算を超えるため送信しません')
            db.execute('INSERT INTO api_costs VALUES (?,?,?,?,?)',
                       (request_id,day,amount,None,encode(dict(model=payload['model'],rates=rates,input_bound=input_bound,output_bound=output_bound))))
            db.commit()

    def settle(self, request_id, payload, usage):
        if not isinstance(usage,dict) or not all(type(usage.get(k)) is int and usage[k]>=0 for k in ('input_tokens','output_tokens')):
            return  # Unknown usage keeps the full reservation charged against budget.
        rates = self.rates(payload['model'])
        amount = (usage['input_tokens']*rates[0]+usage['output_tokens']*rates[1])/1e6
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE api_costs SET actual=?,payload=? WHERE id=?',
                       (amount,encode(dict(model=payload['model'],rates=rates,usage=usage,estimated_usd=amount)),request_id))
