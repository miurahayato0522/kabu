import hashlib
import json
import unittest
from plot_backtest import comparison, drawdown


class PlotTests(unittest.TestCase):
    def test_hold_uses_same_cash_quantity_and_entry_cost(self):
        rows = [{"O": 100, "C": 110, "AdjFactor": 1}, {"O": 110, "C": 90, "AdjFactor": 1, "Date": "2025-01-02"}]
        report = {"input_sha256": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
                  "parameters": {"initial": 20000, "quantity": 100, "fee_per_order": 10, "slippage_bps": 100},
                  "trades": []}
        hold, pairs = comparison(rows, report)
        self.assertEqual(hold, [20890, 18890])
        self.assertEqual(pairs, [])
        rows[0]["C"] = 999
        with self.assertRaises(ValueError):
            comparison(rows, report)

    def test_drawdown_includes_starting_capital(self):
        self.assertAlmostEqual(drawdown([90, 110, 99], 100)[0], -10)
        self.assertAlmostEqual(drawdown([90, 110, 99], 100)[-1], -10)


if __name__ == "__main__":
    unittest.main()
