"""Read-only kabu station quote validation; never records ticks or sends orders."""
from datetime import datetime,timezone
import os
from system_data import stamp,JST


def _market_open(c,now):
    from system_status import sessions
    return sessions(now)['is_session'] and any(start<=now.astimezone(JST).strftime('%H:%M')<=end for start,end in c['trading_hours'])


def check(c,now=None,client=None,secret=None):
    now=now or datetime.now(timezone.utc)
    if not _market_open(c,now):
        return dict(status='WAITING_MARKET',quotes=[],detail='XTKS market closed; no authentication or API call')
    if client is None:
        from kabu_collector import KabuClient
        secret=secret or os.environ.get('KABU_API_PASSWORD')
        if not secret:
            import getpass
            secret=getpass.getpass('kabuステーションのAPIパスワード（非表示）: ')
        if not secret:raise ValueError('KABU_API_PASSWORD is not set')
        client=KabuClient('production');client.authenticate(secret)
    rows=[];errors=[]
    for code in c['symbols']:
        try:
            q=client.board(code);at=stamp(q['CurrentPriceTime']);price=q['CurrentPrice']
            from daily_backtest import positive
            valid=(q.get('Symbol')==code and q.get('Exchange')==1 and positive(price) and at<=now and
                   (now-at).total_seconds()<=60)
            rows.append(dict(symbol=code,current_price=price,current_price_time=at.isoformat(),received_at=now.isoformat(),
                             exchange=q.get('Exchange'),status='PASS' if valid else 'INVALID_OR_STALE'))
            if not valid:errors.append(code)
        except (KeyError,TypeError,ValueError):
            rows.append(dict(symbol=code,status='INVALID_OR_STALE'));errors.append(code)
    return dict(status='PASS' if not errors else 'BLOCKED',quotes=rows,detail='all quotes validated' if not errors else 'invalid/stale: '+','.join(errors))
