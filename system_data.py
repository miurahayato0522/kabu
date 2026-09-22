"""Read-only adapters for existing stores. No network or database migration."""
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Protocol

from daily_backtest import positive, split_factor
from jquants_history import symbol_code

JST = timezone(timedelta(hours=9))
DATA_VERSION = 'bars-v1'


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def stamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('Timezone required')
    return result


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)


def daily_cutoff(day):
    # A conservative research decision time, NOT a claimed exchange timestamp.
    return f'{day}T16:00:00+09:00'


@dataclass(frozen=True)
class Bar:
    id: str
    symbol: str
    exchange: str
    timeframe: str
    day: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str
    market_at: str | None
    fetched_at: str
    available_at: str
    split_factor: float
    price_basis: str
    quality: tuple
    raw: dict

    def record(self):
        return asdict(self)


class PriceProvider(Protocol):
    def bars(self, symbols: list[str]) -> list[Bar]: ...


class DailyProvider:
    """Yahoo/J-Quants adapters share their existing daily_prices storage contract."""
    def __init__(self, path, source=None):
        self.path, self.source = path, source

    def bars(self, symbols):
        requested = {symbol_code(s) for s in symbols}
        with closing(readonly(self.path)) as db:
            sources = [r[0] for r in db.execute('SELECT DISTINCT source FROM daily_prices')]
            if len(sources) != 1 or sources[0] not in ('jquants_v2', 'yahoo_reconstructed_v1'):
                raise ValueError('Empty, mixed or unsupported daily source')
            source = sources[0]
            if self.source and self.source != source:
                raise ValueError('Provider/source mismatch')
            output = []
            for code, day, fetched, payload in db.execute(
                    'SELECT code,day,fetched_at,payload FROM daily_prices ORDER BY code,day'):
                if code not in requested:
                    continue
                raw = json.loads(payload)
                if raw['Code'] != code or raw['Date'] != day:
                    raise ValueError('Payload/key mismatch')
                valid = all(positive(raw.get(k)) for k in ('O', 'H', 'L', 'C', 'Vo'))
                if valid:
                    valid = raw['L'] <= min(raw['O'], raw['C']) <= max(raw['O'], raw['C']) <= raw['H']
                available = max(stamp(fetched), stamp(daily_cutoff(day))).isoformat()
                output.append(Bar(digest([DATA_VERSION, source, code, day, fetched, raw]),
                    code, 'XTKS', '1d', day, *[raw.get(k) for k in ('O', 'H', 'L', 'C', 'Vo')],
                    source, None, fetched, available, split_factor(raw),
                    'reconstructed_unadjusted' if source.startswith('yahoo') else 'unadjusted',
                    ('market_time_date_only',) + (() if valid else ('invalid_ohlcv',)), raw))
        if requested != {b.symbol for b in output}:
            raise ValueError('One or more requested symbols have no data')
        return output


class SnapshotProvider:
    def __init__(self, path):
        self.path = path

    def bars(self, symbols):
        requested = {symbol_code(s) for s in symbols}
        with closing(readonly(self.path)) as db:
            output = []
            for ident,payload in db.execute('SELECT id,payload FROM normalized_bars'):
                value=json.loads(payload)
                if value['symbol'] not in requested:
                    continue
                if value['id'] != ident:
                    raise ValueError('Snapshot identity mismatch')
                value['quality']=tuple(value['quality'])
                output.append(Bar(**value))
        if requested != {b.symbol for b in output}:
            raise ValueError('Snapshot missing requested symbols')
        return sorted(output,key=lambda b:(b.symbol,b.day,b.id))


def provider(path):
    with closing(readonly(path)) as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'normalized_bars' in tables and 'daily_prices' not in tables:
        return SnapshotProvider(path)
    return DailyProvider(path)


class YahooProvider(DailyProvider):
    def __init__(self, path):
        super().__init__(path, 'yahoo_reconstructed_v1')


class JQuantsProvider(DailyProvider):
    def __init__(self, path):
        super().__init__(path, 'jquants_v2')


class KabuObservedProvider:
    def __init__(self, path, environment='production'):
        self.path, self.environment = path, environment

    def bars(self, symbols):
        from kabu_analysis import analyze, read_events
        events = list(read_events(self.path, self.environment))
        report = analyze(events)
        # Reconstructed observations can be revised by late arrivals. Availability
        # is conservatively the whole snapshot's final receipt time.
        received = max((stamp(e['received_at']) for e in events), default=None)
        output = []
        wanted = {symbol_code(s) for s in symbols}
        for b in report['bars']:
            code = symbol_code(b['symbol'])
            if code not in wanted:
                continue
            end = stamp(b['minute']) + timedelta(minutes=1)
            output.append(Bar(digest([DATA_VERSION, self.environment, b, received.isoformat()]),
                code, str(b['exchange']), 'observed_1m', b['minute'][:10],
                b['open'], b['high'], b['low'], b['close'], b['observed_volume'],
                'kabu_push:' + self.environment, b['minute'], received.isoformat(),
                max(received, end).isoformat(), 1.0, 'unadjusted',
                tuple(b['flags']) + ('possibly_incomplete', 'split_unknown'), b))
        return output


def daily_groups(bars, calendar=True):
    if not bars or {b.timeframe for b in bars} != {'1d'} or {b.exchange for b in bars} != {'XTKS'} or len({b.source for b in bars}) != 1 or len({b.price_basis for b in bars}) != 1:
        raise ValueError('Require one daily source; observed bars cannot be used as official daily bars')
    groups = {}
    for b in bars:
        if 'invalid_ohlcv' in b.quality or not all(positive(v) for v in (b.open,b.high,b.low,b.close,b.volume,b.split_factor)) or not b.low <= min(b.open,b.close) <= max(b.open,b.close) <= b.high:
            raise ValueError(f'Invalid OHLCV: {b.symbol} {b.day}')
        groups.setdefault(b.symbol, []).append(b)
    for code, series in groups.items():
        days = [b.day for b in series]
        if days != sorted(set(days)):
            raise ValueError('Duplicate or unordered daily bars')
        if calendar:
            import exchange_calendars as xcals
            start = datetime.fromisoformat(days[0]) - timedelta(days=10)
            end = datetime.fromisoformat(days[-1]) + timedelta(days=10)
            cal = xcals.get_calendar('XTKS', start=start, end=end)
            expected = [d.date().isoformat() for d in cal.sessions if days[0] <= str(d.date()) <= days[-1]]
            if days != expected:
                raise ValueError(f'Missing/non-session daily bars: {code}; no forward fill')
    return groups


def save_snapshot(path, bars):
    """Append-only normalized snapshot, distinct from the input database."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables & {'daily_prices', 'events', 'ai_analyses', 'documents'}:
            raise ValueError('Refusing to add normalized data to an existing source database')
        db.execute('CREATE TABLE IF NOT EXISTS normalized_bars (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        db.executemany('INSERT OR IGNORE INTO normalized_bars VALUES (?,?)',
                       [(b.id, encode(b.record())) for b in bars])
