"""Independent cash/position constraints, never controlled by model outputs."""
from dataclasses import dataclass, asdict
import math


@dataclass(frozen=True)
class RiskLimits:
    per_symbol: float = 1_000_000
    total: float = 5_000_000
    holdings: int = 10
    daily_loss: float = 100_000
    orders_per_day: int = 20
    stop_new: bool = False

    def validate(self):
        for key, value in asdict(self).items():
            if key != 'stop_new' and (not math.isfinite(value) or value <= 0):
                raise ValueError('Risk limits must be finite and positive')
        if type(self.holdings) is not int or type(self.orders_per_day) is not int:
            raise ValueError('Counts must be integers')

    def check(self, symbol, quantity, price, cash, positions, marks, fee,
              daily_pnl, orders_today, duplicate=False):
        if quantity <= 0 or quantity % 100:
            return 'invalid_lot'
        if duplicate:
            return 'duplicate_order'
        if self.stop_new:
            return 'manual_stop'
        if daily_pnl <= -self.daily_loss:
            return 'daily_loss_limit'
        if orders_today >= self.orders_per_day:
            return 'daily_order_limit'
        cost = price * quantity
        current = positions.get(symbol, 0) * marks[symbol]
        invested = sum(q * marks[s] for s, q in positions.items())
        if cost + fee > cash:
            return 'insufficient_cash'
        if current + cost > self.per_symbol:
            return 'symbol_limit'
        if invested + cost > self.total:
            return 'total_limit'
        if not positions.get(symbol) and sum(q > 0 for q in positions.values()) >= self.holdings:
            return 'holdings_limit'
        return None
