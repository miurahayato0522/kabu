"""Offline shared-cash, long-only account; no broker order interface exists."""
from dataclasses import asdict
from datetime import datetime
import math
from daily_backtest import split_shares, simulate
from system_data import daily_groups, daily_cutoff, stamp, digest
from system_risk import RiskLimits
from system_strategy import decide


def legacy_baseline(bars, **kwargs):
    groups = daily_groups(bars, calendar=False)
    if len(groups) != 1:
        raise ValueError('Legacy baseline is a single-symbol independent account')
    return simulate([b.raw for b in next(iter(groups.values()))], **kwargs)


class Account:
    def __init__(self, initial, limits=None, fee=0, slippage_bps=5, spread_bps=0):
        self.limits = limits or RiskLimits()
        self.limits.validate()
        if not math.isfinite(initial) or initial <= 0:
            raise ValueError('Initial capital must be positive')
        if any(not math.isfinite(v) or v < 0 for v in (fee, slippage_bps, spread_bps)) or slippage_bps+spread_bps/2 >= 10000:
            raise ValueError('Invalid costs')
        self.initial = self.cash = float(initial)
        self.fee, self.slippage_bps, self.spread_bps = fee, slippage_bps, spread_bps
        self.positions, self.costs = {}, {}
        self.realized = self.fees = self.execution_cost = 0.0
        self.orders, self.fills = [], []
        self.day = None
        self.day_start = initial
        self.sent = set()
        self.order_count = 0

    def equity(self, marks):
        return self.cash + sum(q * marks[s] for s, q in self.positions.items() if q)

    def start_day(self, day, previous_equity):
        if day != self.day:
            self.day, self.day_start, self.sent, self.order_count = day, previous_equity, set(), 0

    def split(self, symbol, factor, day):
        if self.positions.get(symbol):
            self.positions[symbol] = split_shares(self.positions[symbol], factor, day)

    def execute(self, decision, reference, marks, at, quantity=100, capacity=None):
        p = decision['prediction']
        side, symbol = decision['action'], p['symbol']
        if side not in ('BUY', 'SELL'):
            return None
        wanted = self.positions.get(symbol, 0) if side == 'SELL' else quantity
        order = dict(id=digest([decision['id'], at]), decision_id=decision['id'], symbol=symbol,
                     side=side, at=at, requested=wanted, filled=0, status='REJECTED', reason=None)
        if not math.isfinite(reference) or reference <= 0 or stamp(at) <= stamp(p['at']):
            order['reason'] = 'invalid_price_or_not_after_decision'
        elif (symbol, side) in self.sent:
            order['reason'] = 'duplicate_order'
        elif self.order_count >= self.limits.orders_per_day:
            order['reason'] = 'daily_order_limit'
        else:
            self.sent.add((symbol, side))
            self.order_count += 1
            qty = wanted if capacity is None else min(wanted, max(0, int(capacity)//100*100))
            price = reference * (1 + (self.slippage_bps+self.spread_bps/2)/10000 * (1 if side == 'BUY' else -1))
            reason = None
            if qty <= 0 or qty % 100:
                reason = 'unfilled_no_lot_or_capacity'
            elif side == 'BUY':
                reason = self.limits.check(symbol, qty, price, self.cash, self.positions, marks, self.fee,
                                          self.equity(marks)-self.day_start, self.order_count-1)
            if reason:
                order['reason'] = reason
                if reason.startswith('unfilled'):
                    order['status'] = 'UNFILLED'
            else:
                if side == 'BUY':
                    self.cash -= qty * price + self.fee
                    self.positions[symbol] = self.positions.get(symbol, 0) + qty
                    self.costs[symbol] = self.costs.get(symbol, 0) + qty * price + self.fee
                    profit = 0
                else:
                    basis = self.costs[symbol] * qty/self.positions[symbol]
                    profit = qty * price - self.fee - basis
                    self.cash += qty * price - self.fee
                    self.positions[symbol] -= qty
                    self.costs[symbol] -= basis
                    self.realized += profit
                self.fees += self.fee
                self.execution_cost += abs(price-reference)*qty
                order.update(status='FILLED' if qty == wanted else 'PARTIAL_CANCELLED', filled=qty)
                self.fills.append(dict(order_id=order['id'], symbol=symbol, side=side, at=at,
                    quantity=qty, price=price, reference_price=reference, fee=self.fee, realized_pnl=profit))
        self.orders.append(order)
        return order


def run(bars, strategy, initial=10_000_000, quantity=100, limits=None, fee=0,
        slippage_bps=5, spread_bps=0, mode='research', start=None, end=None, calendar=True,
        capacities=None):
    if mode not in ('research', 'recorded') or quantity <= 0 or quantity % 100:
        raise ValueError('Invalid mode or quantity')
    groups = daily_groups(bars, calendar=calendar)
    days = [b.day for b in next(iter(groups.values()))]
    if any([b.day for b in series] != days for series in groups.values()):
        raise ValueError('All symbols must have identical sessions; missing data stops the account')
    account = Account(initial, limits, fee, slippage_bps, spread_bps)
    pending, decisions, equity, histories = {}, [], [], {s: [] for s in groups}
    peak, previous_equity = initial, initial
    for i, day in enumerate(days):
        current = {s: series[i] for s, series in groups.items()}
        if end and day > end:
            break
        for s, b in current.items():
            histories[s].append(b)
        if start and day < start:
            continue
        account.start_day(day, previous_equity)
        marks = {s: b.open for s, b in current.items()}
        for s, b in current.items():
            account.split(s, b.split_factor, day)
        at_open = f'{day}T09:00:00+09:00'
        for s, decision in sorted(pending.items(), key=lambda kv: (kv[1]['action'] != 'SELL', kv[0])):
            account.execute(decision, marks[s], marks, at_open, quantity,
                            (capacities or {}).get((s, day)))
        pending = {}
        for s in sorted(current):
            cutoff = daily_cutoff(day)
            history = histories[s]
            # Never pretend a later bulk download was already available in recorded mode.
            if mode == 'recorded' and any(stamp(b.available_at) > stamp(cutoff) for b in history):
                continue
            used_model = getattr(strategy, 'chart', strategy)
            if mode == 'recorded' and hasattr(used_model,'metadata') and stamp(used_model.metadata['created_at']) > stamp(cutoff):
                continue
            p = strategy.predict(history, cutoff)
            if p.symbol != s or stamp(p.at) != stamp(cutoff):
                raise ValueError('Prediction identity/time mismatch')
            d = decide(p, account.positions.get(s, 0))
            decisions.append(d)
            if d['action'] in ('BUY', 'SELL'):
                pending[s] = d
        close_marks = {s: b.close for s, b in current.items()}
        value = account.equity(close_marks)
        peak = max(peak, value)
        equity.append(dict(day=day, cash=account.cash, equity=value, positions=dict(account.positions),
            realized=account.realized, unrealized=value-account.cash-sum(account.costs.values()),
            drawdown_pct=(value/peak-1)*100))
        previous_equity = value
    if not equity:
        raise ValueError('No evaluation sessions')
    return dict(version='account-v1', strategy=strategy.name, mode=mode,
        availability_note='Historical reconstruction, not point-in-time vintage data' if mode == 'research' else 'Observed fetch availability required',
        parameters=dict(initial=initial, quantity=quantity, fee=fee, slippage_bps=slippage_bps,
                        spread_bps=spread_bps, risk=asdict(account.limits)),
        input_hash=digest([b.id for b in bars]), source=bars[0].source,
        final_equity=equity[-1]['equity'], net_pnl=equity[-1]['equity']-initial,
        realized=account.realized, unrealized=equity[-1]['unrealized'],
        max_drawdown_pct=-min(e['drawdown_pct'] for e in equity),
        trade_count=len(account.fills), fees=account.fees, execution_cost=account.execution_cost,
        decisions=decisions, orders=account.orders, fills=account.fills, equity=equity,
        pending=list(pending.values()),
        limitations=['No dividends/taxes; next-open fills are assumptions',
                     'Capacity is an explicit scenario, not inferred from future daily volume',
                     'Missing/halted daily bars stop the entire run; no fabricated prices',
                     'No closing liquidation; residual shares valued at last close'])
