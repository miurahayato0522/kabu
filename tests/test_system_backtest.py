from dataclasses import replace
import unittest
from daily_backtest import simulate
from system_data import Bar, digest
from system_strategy import MovingAverage, Prediction, decide
from system_backtest import run, legacy_baseline, Account
from system_risk import RiskLimits
from test_history import records


def bars(prices, code='72030'):
    output = []
    for row in records(prices):
        row['Code'] = code
        at = row['Date']+'T16:00:00+09:00'
        output.append(Bar(digest(row), code, 'XTKS', '1d', row['Date'],
            row['O'], row['H'], row['L'], row['C'], row['Vo'], 'synthetic', None,
            at, at, row['AdjFactor'], 'unadjusted', (), row))
    return output


class BacktestTests(unittest.TestCase):
    def test_split_preserves_account_cost_and_value(self):
        original=bars([10]*20+[12,13,14,14,14,8,7,6]*3)
        adjusted=[]
        for i,b in enumerate(original):
            if i>=23:
                raw=dict(b.raw,AdjFactor=.5 if i==23 else 1,ExRT='1' if i==23 else '')
                for key in ('O','H','L','C'):
                    raw[key]*=.5
                b=replace(b,open=b.open*.5,high=b.high*.5,low=b.low*.5,close=b.close*.5,
                          split_factor=.5 if i==23 else 1,raw=raw)
            adjusted.append(b)
        # Restrict to the first holding/exit: subsequent purchases still use 100 shares.
        a=run(original[:30],MovingAverage(),calendar=False)
        b=run(adjusted[:30],MovingAverage(),calendar=False)
        self.assertAlmostEqual(a['net_pnl'],b['net_pnl'])
        self.assertEqual([e['equity'] for e in a['equity']],[e['equity'] for e in b['equity']])

    def test_legacy_and_shared_one_symbol_parity(self):
        values = [10]*20+[11,12,13,9,8,7,6,5,4,3]*4
        data = bars(values)
        expected = simulate([b.raw for b in data], fee=3, slippage_bps=10)
        self.assertEqual(legacy_baseline(data, fee=3, slippage_bps=10), expected)
        actual = run(data, MovingAverage(), initial=1_000_000, fee=3, slippage_bps=10, calendar=False)
        self.assertAlmostEqual(actual['net_pnl'], expected['total_pnl'])
        self.assertEqual(actual['trade_count'], len(expected['trades']))
        self.assertEqual([e['equity'] for e in actual['equity']], [e['equity'] for e in expected['equity_curve']])
        for fill in actual['fills']:
            d = next(d for d in actual['decisions'] if d['id'] == next(o for o in actual['orders'] if o['id'] == fill['order_id'])['decision_id'])
            self.assertLess(d['prediction']['at'][:10], fill['at'][:10])

    def test_cash_shared_and_recorded_cutoff(self):
        a, b = bars([10]*20+[11]*5), bars([10]*20+[11]*5, '83060')
        actual = run(a+b, MovingAverage(), initial=1500, calendar=False)
        self.assertEqual(actual['trade_count'], 1)
        self.assertIn('insufficient_cash', [o['reason'] for o in actual['orders']])
        late = [replace(b, available_at='2030-01-01T00:00:00+00:00') for b in a]
        self.assertEqual(run(late, MovingAverage(), mode='recorded', calendar=False)['trade_count'], 0)

    def test_partial_risk_duplicate_and_sell_cost(self):
        p = Prediction('72030', '2025-01-01T16:00:00+09:00', 5, .1, 'test', 1, [], [])
        d = decide(p, 0)
        account = Account(10000, RiskLimits(per_symbol=5000), fee=2, slippage_bps=0)
        account.start_day('2025-01-02', 10000)
        marks = {'72030': 10}
        order = account.execute(d, 10, marks, '2025-01-02T09:00:00+09:00', 200, 100)
        self.assertEqual(order['status'], 'PARTIAL_CANCELLED')
        self.assertEqual(account.positions['72030'], 100)
        self.assertEqual(account.execute(d, 10, marks, '2025-01-02T09:01:00+09:00')['reason'], 'duplicate_order')
        self.assertEqual(account.cash, 8998)
        stopped = Account(10000, RiskLimits(stop_new=True))
        stopped.start_day('2025-01-02',10000)
        self.assertEqual(stopped.execute(d,10,marks,'2025-01-02T09:00:00+09:00')['reason'],'manual_stop')
        limited = Account(10000,RiskLimits(daily_loss=10))
        limited.start_day('2025-01-02',10020)
        self.assertEqual(limited.execute(d,10,marks,'2025-01-02T09:00:00+09:00')['reason'],'daily_loss_limit')


if __name__ == '__main__':
    unittest.main()
