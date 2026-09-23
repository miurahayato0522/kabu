"""Observed news/price outcome labels, kept separate from prediction inputs."""
from contextlib import closing
from datetime import timedelta
from pathlib import Path
import json
import math
import sqlite3
from system_data import provider,readonly,stamp,encode,digest,JST,daily_groups
from news_discovery import candidate_rows,settings,connect


def receipt_quote(path,code,when,asof):
    if not Path(path).is_file():return None
    best=None
    with closing(readonly(path)) as db:
        for ident,received,payload in db.execute("SELECT id,received_at,payload FROM events WHERE source IN ('push','snapshot') AND environment='production'"):
            rt=stamp(received)
            if not 0<=(when-rt).total_seconds()<=60 or rt>asof:continue
            q=json.loads(payload)
            if q.get('Symbol')!=code or q.get('Exchange')!=1 or not q.get('CurrentPriceTime'):continue
            pt=stamp(q['CurrentPriceTime']);price=q.get('CurrentPrice')
            if type(price) not in (int,float) or not math.isfinite(price) or price<=0:continue
            if pt<=rt and 0<=(when-pt).total_seconds()<=60 and (best is None or rt>stamp(best['received_at'])):
                best=dict(price=price,market_at=pt.isoformat(),received_at=received,event_id=ident,
                          label='取得直前60秒以内の実観測値。取得瞬間の価格を補間した値ではない')
    return best


def returns(series,base_day,base_price):
    import exchange_calendars as xcals
    start=stamp(base_day+'T00:00:00+09:00').date()
    cal=xcals.get_calendar('XTKS',start=str(start-timedelta(days=7)),end=str(start+timedelta(days=65)))
    dates=[str(d.date()) for d in cal.sessions if str(d.date())>base_day]
    by_day={b.day:b for b in series};output={}
    for horizon in (1,5,20):
        required=dates[:horizon];bars=[by_day.get(d) for d in required]
        valid=len(bars)==horizon and all(bars)
        item=dict(target_day=required[-1] if required else None,return_value=None,reason='価格未取得・期間未到来',refs=[])
        if valid:
            adjusted=base_price*math.prod(b.split_factor for b in bars)
            item.update(return_value=bars[-1].close/adjusted-1,reason=None,refs=[b.id for b in bars],
                        fetched_at=bars[-1].fetched_at)
        output[str(horizon)]=item
    return output


def measure(row,c,asof):
    source=row['article'];received=stamp(source['first_seen_at']);code=row['symbol'];s=settings(c)
    output=dict(candidate_id=row['id'],symbol=code,published_at=source.get('published_at'),received_at=source['first_seen_at'],
        analyzed_at=row['impact'].get('finished_at'),event_kind=(row['event'].get('result') or {}).get('kind'),
        impact=row['impact'].get('result'),computed_at=asof.isoformat(),observed_receipt_price=None,
        daily_anchor=None,observed_price_returns=None,daily_proxy_returns=None,price_path=[],benchmarks={},
        warning='結果ラベル専用。予測入力へ混入しない。配当・税金・売買コストはリターンに含まない。')
    if received>asof:return dict(output,error='future_news')
    try:
        quote=receipt_quote(c['paths']['ticks'],code,received,asof)
        output['observed_receipt_price']=quote
        series=[b for b in provider(c['paths']['prices']).bars([code]) if stamp(b.available_at)<=asof]
        if not series:raise ValueError('No available daily prices')
        daily_groups(series)
        import exchange_calendars as xcals
        cal=xcals.get_calendar('XTKS',start=series[0].day,end=series[-1].day)
        after=[b for b in series if cal.session_close(b.day).to_pydatetime()>=received]
        if quote:
            output['observed_price_returns']=returns(series,stamp(quote['market_at']).astimezone(JST).date().isoformat(),quote['price'])
        if not after:return dict(output,error='post_news_prices_unavailable')
        anchor=after[0]
        output['daily_anchor']=dict(day=anchor.day,price=anchor.close,fetched_at=anchor.fetched_at,id=anchor.id,
            label='取得後最初の営業日終値を別の基準にした研究用リターン。取得時点価格の代用ではない')
        output['daily_proxy_returns']=returns(series,anchor.day,anchor.close)
        output['price_path']=[b.record() for b in after[:21]]
        for name,definition in s['benchmarks'].items():
            try:
                path=Path(definition['db'])
                if not path.is_absolute():path=Path(__file__).resolve().parent/path
                benchmark=[b for b in provider(path).bars([definition['symbol']]) if stamp(b.available_at)<=asof]
                daily_groups(benchmark)
                base=next(b for b in benchmark if b.day==anchor.day)
                br=returns(benchmark,base.day,base.close)
                relative={h:output['daily_proxy_returns'][h]['return_value']-br[h]['return_value']
                    if output['daily_proxy_returns'][h]['return_value'] is not None and br[h]['return_value'] is not None else None for h in br}
                output['benchmarks'][name]=dict(baseline=base.record(),returns=br,relative_returns=relative)
            except (OSError,ValueError,KeyError,StopIteration,sqlite3.Error):output['benchmarks'][name]=dict(error='benchmark_unavailable')
        if not s['benchmarks']:output['benchmarks']={'status':'指数未取得。相対リターンは補完しません'}
    except (OSError,ValueError,KeyError,sqlite3.Error):output['error']='prices_unavailable_or_invalid'
    return output


def collect(c,asof):
    items=[measure(r,c,asof) for r in candidate_rows(c,asof)]
    with closing(connect(settings(c)['db'])) as db,db:
        for item in items:
            db.execute('INSERT OR IGNORE INTO discovery_outcomes VALUES (?,?,?,?)',
                       (digest(item),item['candidate_id'],asof.isoformat(),encode(item)))
    return dict(saved=len(items),status='観測結果を追記。未取得の価格はnull',items=items)
