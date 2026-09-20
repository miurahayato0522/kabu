from datetime import date, timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import urllib.error

from daily_backtest import simulate
from jquants_history import fetch_daily, save_daily, load_daily, HistoryError


def records(prices):
    return [{"Date": (date(2025, 1, 1) + timedelta(days=i)).isoformat(), "Code": "72030",
             "O": p, "H": p + 1, "L": p - 1, "C": p, "Vo": 10000, "AdjFactor": 1}
            for i, p in enumerate(prices)]


class HistoryTests(unittest.TestCase):
    def test_pagination_and_no_key_in_url(self):
        opener = Mock()
        opener.open.side_effect = [io.BytesIO(json.dumps(p).encode()) for p in
            [{"data": [{"Date": "2025-01-01"}], "pagination_key": "next"}, {"data": [{"Date": "2025-01-02"}]}]]
        sleep = Mock()
        result = fetch_daily("secret-key", "72030", opener, sleep)
        self.assertEqual(len(result), 2)
        self.assertIn("pagination_key=next", opener.open.call_args.args[0].full_url)
        self.assertNotIn("secret-key", opener.open.call_args.args[0].full_url)
        sleep.assert_called_once_with(15)

    def test_api_error_does_not_expose_response_or_key(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError("url", 403, "secret-key", {}, io.BytesIO(b'secret-key'))
        with self.assertRaises(HistoryError) as context:
            fetch_daily("secret-key", "72030", opener)
        self.assertNotIn("secret-key", str(context.exception))
        self.assertIn("403", str(context.exception))

    def test_repeated_cursor_rejected(self):
        opener = Mock()
        opener.open.side_effect = [io.BytesIO(b'{"data":[],"pagination_key":"same"}') for _ in range(2)]
        with self.assertRaises(HistoryError):
            fetch_daily("key", "72030", opener, Mock())

    def test_store_idempotent_and_invalid_batch_preserves_existing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"
            rows = records([100, 101, 102])
            save_daily(path, "72030", rows)
            save_daily(path, "72030", rows)
            self.assertEqual(load_daily(path, "72030"), rows)
            for bad in [[], [dict(rows[0], Code="83060")]]:
                with self.assertRaises(HistoryError):
                    save_daily(path, "72030", bad)
            self.assertEqual(load_daily(path, "72030"), rows)

    def test_cross_executes_next_open_with_costs(self):
        # 2/3MA: index2で下、index3で上、index4始値で買う。
        rows = records([10, 9, 8, 12, 14, 7, 6])
        result = simulate(rows, initial=10000, short=2, long=3, fee=10, slippage_bps=100)
        buy, sell = result["trades"]
        self.assertEqual(buy["signal_date"], rows[3]["Date"])
        self.assertEqual(buy["date"], rows[4]["Date"])
        self.assertAlmostEqual(buy["price"], 14 * 1.01)
        self.assertEqual(sell["date"], rows[6]["Date"])
        expected = 6 * .99 * 100 - 10 - (14 * 1.01 * 100 + 10)
        self.assertAlmostEqual(result["total_pnl"], expected)
        self.assertEqual(result["ending_shares"], 0)

    def test_last_signal_not_filled_and_future_does_not_change_past(self):
        prefix = records([10, 9, 8, 12])
        short = simulate(prefix, short=2, long=3)
        self.assertEqual(short["trades"], [])
        self.assertIsNotNone(short["unexecuted_last_signal"])
        full = simulate(records([10, 9, 8, 12, 100]), short=2, long=3)
        self.assertEqual(full["equity_curve"][:4], short["equity_curve"])

    def test_no_negative_cash_and_mark_to_market(self):
        rows = records([10, 9, 8, 12, 14])
        small = simulate(rows, initial=100, short=2, long=3)
        self.assertEqual(small["trades"], [])
        self.assertEqual(len(small["skipped_orders"]), 1)
        held = simulate(rows, initial=10000, short=2, long=3, fee=10, slippage_bps=0)
        self.assertEqual(held["ending_shares"], 100)
        self.assertAlmostEqual(held["total_pnl"], -10)
        self.assertAlmostEqual(held["total_pnl"], held["realized_pnl"] + held["unrealized_pnl"])

    def test_split_missing_and_invalid_inputs_stop(self):
        for field, value in [("AdjFactor", .5), ("C", None), ("Vo", 0), ("H", 1)]:
            rows = records([10, 9, 8, 12, 14])
            rows[2][field] = value
            with self.assertRaises(HistoryError):
                simulate(rows, short=2, long=3)


if __name__ == "__main__":
    unittest.main()
