from copy import deepcopy
import unittest

from daily_backtest import simulate, chart_adjustments
from jquants_history import HistoryError
from plot_backtest import comparison
from test_history import records


class SplitTests(unittest.TestCase):
    def run_rows(self, rows):
        return simulate(rows, initial=10000, short=2, long=3, fee=10, slippage_bps=100)

    def split_rows(self, rows, index, factor):
        result = deepcopy(rows)
        for row in result[index:]:
            for field in ('O', 'H', 'L', 'C'):
                row[field] *= factor
        result[index].update(AdjFactor=factor, ExRT='1' if factor < 1 else '2')
        return result

    def test_held_split_and_pending_sell_preserve_pnl_costs_and_equity(self):
        original = records([10, 9, 8, 12, 14, 7, 6])
        expected = self.run_rows(original)
        for index in (5, 6):
            rows = self.split_rows(original, index, .2)
            result = self.run_rows(rows)
            self.assertEqual(result['trades'][-1]['quantity'], 500)
            for a, b in zip(result['equity_curve'], expected['equity_curve']):
                self.assertAlmostEqual(a['equity'], b['equity'])
            self.assertAlmostEqual(result['realized_pnl'], expected['realized_pnl'])
            self.assertAlmostEqual(result['max_drawdown_pct'], expected['max_drawdown_pct'])
            self.assertAlmostEqual(comparison(rows, result)[0][-1], comparison(original, expected)[0][-1])

    def test_split_on_buy_day_buys_only_configured_quantity(self):
        rows = self.split_rows(records([10, 9, 8, 12, 14]), 4, .5)
        result = self.run_rows(rows)
        self.assertEqual(result['ending_shares'], 100)
        self.assertAlmostEqual(result['trades'][0]['price'], 7 * 1.01)

    def test_first_day_factor_does_not_multiply_benchmark_purchase(self):
        rows = records([10, 9, 8, 12, 14])
        rows[0].update(AdjFactor=.2, ExRT='1')
        result = self.run_rows(rows)
        self.assertAlmostEqual(comparison(rows, result)[0][0], 9980)

    def test_no_spurious_cross_and_no_future_leak(self):
        rows = records([10] * 8)
        changed = self.split_rows(rows, 5, .2)
        result = self.run_rows(changed)
        self.assertEqual(result['trades'], [])
        self.assertEqual(result['equity_curve'][:5], self.run_rows(rows[:5])['equity_curve'])
        self.assertEqual([r['C'] * chart_adjustments(changed)[r['Date']] for r in changed], [2] * 8)

    def test_multiple_splits_reverse_split_and_open_position(self):
        rows = records([10, 9, 8, 12, 14, 14, 14, 14])
        changed = self.split_rows(self.split_rows(rows, 5, .2), 6, 5)
        actual, expected = self.run_rows(changed), self.run_rows(rows)
        self.assertEqual(actual['ending_shares'], 100)
        self.assertAlmostEqual(actual['unrealized_pnl'], expected['unrealized_pnl'])
        self.assertAlmostEqual(actual['total_pnl'], actual['realized_pnl'] + actual['unrealized_pnl'])
        self.assertEqual(comparison(changed, actual)[0], comparison(rows, expected)[0])

    def test_unknown_rights_invalid_factors_and_odd_lots_stop(self):
        for factor, kind in [(None, '1'), (0, '1'), (-1, '1'), (float('nan'), '1'), (.5, '3'), (.5, ''), (2, '2')]:
            rows = records([10, 9, 8, 12, 14, 14])
            rows[5].update(AdjFactor=factor, ExRT=kind)
            with self.assertRaises(HistoryError):
                self.run_rows(rows)


if __name__ == '__main__':
    unittest.main()
