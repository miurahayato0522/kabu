"""Causal OHLCV features. Only labels may reference a later session."""
import math
import numpy as np
from system_data import daily_groups, digest

VERSION = 'chart-features-v1'


def ema(values, span):
    out, value = [], float(values[0])
    alpha = 2/(span+1)
    for x in values:
        value += alpha*(float(x)-value)
        out.append(value)
    return np.array(out)


def chart_features(history):
    if len(history) < 61:
        return None
    # Adjust history only for splits already observed at this decision date.
    c, h, l, v = [], [], [], []
    for b in history:
        f = b.split_factor
        if f != 1:
            c, h, l, v = [x*f for x in c], [x*f for x in h], [x*f for x in l], [x/f for x in v]
        c.append(b.close)
        h.append(b.high)
        l.append(b.low)
        v.append(b.volume)
    c, h, l, v = [np.array(x, dtype=float) for x in (c, h, l, v)]
    result = {f'return_{n}': float(c[-1]/c[-n-1]-1) for n in (1, 5, 10, 20)}
    for n in (5, 20, 60):
        avg = float(c[-n:].mean())
        result.update({f'ma_{n}': avg, f'distance_{n}': float(c[-1]/avg-1),
                       f'slope_{n}': float(avg/c[-n-1:-1].mean()-1)})
    delta = np.diff(c[-15:])
    gain, loss = np.maximum(delta, 0).mean(), np.maximum(-delta, 0).mean()
    result['rsi_14'] = float(100*gain/(gain+loss)) if gain+loss else 50.0
    macd = ema(c, 12)-ema(c, 26)
    result['macd'] = float(macd[-1]/c[-1])
    result['macd_signal'] = float(ema(macd, 9)[-1]/c[-1])
    tr = np.maximum(h[1:]-l[1:], np.maximum(abs(h[1:]-c[:-1]), abs(l[1:]-c[:-1])))
    result['atr_14'] = float(tr[-14:].mean()/c[-1])
    width = h[-20:].max()-l[-20:].min()
    result['range_position_20'] = float((c[-1]-l[-20:].min())/width) if width else 0.5
    result['volume_ma20'] = float(v[-20:].mean())
    result['volume_ratio20'] = float(v[-1]/v[-21:-1].mean())
    result['volatility20'] = float(np.std(np.diff(c[-21:])/c[-21:-1], ddof=1))
    # Index feeds are not in the current stores. Missing != relative strength zero.
    result['index_relative_strength'] = None
    result['index_missing'] = 1.0
    if any(x is not None and not math.isfinite(x) for x in result.values()):
        raise ValueError('Non-finite chart feature')
    return result


def dataset(bars, horizon=5, calendar=True):
    if type(horizon) is not int or horizon < 1:
        raise ValueError('Horizon must be a positive session count')
    result = []
    for symbol, series in daily_groups(bars, calendar=calendar).items():
        for i, b in enumerate(series):
            f = chart_features(series[:i+1])
            if f is None:
                continue
            label, label_end = None, None
            if i+horizon < len(series):
                factor = math.prod(x.split_factor for x in series[i+1:i+horizon+1])
                label = series[i+horizon].close/factor/b.close-1
                label_end = series[i+horizon].day
            result.append(dict(symbol=symbol, day=b.day, bar_id=b.id,
                refs_hash=digest([x.id for x in series[:i+1]]), features=f,
                label=label, label_end=label_end, horizon=horizon, feature_version=VERSION))
    return sorted(result, key=lambda r: (r['day'], r['symbol']))


def chronological_split(rows):
    days = sorted({r['day'] for r in rows if r['label'] is not None})
    if len(days) < 60:
        raise ValueError('At least 60 labeled feature sessions required')
    validation_start, test_start = days[int(len(days)*.6)], days[int(len(days)*.8)]
    train = [r for r in rows if r['label_end'] and r['label_end'] < validation_start]
    val = [r for r in rows if validation_start <= r['day'] < test_start and r['label_end'] and r['label_end'] < test_start]
    test = [r for r in rows if r['day'] >= test_start and r['label'] is not None]
    if min(len(train), len(val), len(test)) < 10:
        raise ValueError('Insufficient rows after purging boundary-crossing labels')
    return train, val, test, dict(validation_start=validation_start, test_start=test_start)
