from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from jquants_history import HistoryError
from multi_backtest import read_symbols, fetch_many, run_many, write_report


def rows(code, count=45):
    result = []
    for i in range(count):
        price = 100 + abs((i % 20) - 10)
        result.append({"Date": (date(2025, 1, 1) + timedelta(days=i)).isoformat(), "Code": code,
                       "O": price, "C": price, "H": price+1, "L": price-1, "Vo": 100000, "AdjFactor": 1})
    return result


class MultiTests(unittest.TestCase):
    def test_ten_accounts_aggregate_is_sum_not_average(self):
        codes = [f"{1000+i}0" for i in range(10)]
        report = run_many(codes, "unused", loader=lambda _, c: rows(c))
        a = report["aggregate"]
        self.assertEqual(a["initial"], 10_000_000)
        self.assertEqual(len(report["results"]), 10)
        for i, value in enumerate(a["equity"]):
            self.assertAlmostEqual(value, sum(r["strategy"]["equity_curve"][i]["equity"] for r in report["results"]))
        self.assertAlmostEqual(a["pnl"], sum(r["strategy"]["total_pnl"] for r in report["results"]))

    def test_missing_data_never_becomes_fake_cash_account(self):
        report = run_many(["72030", "83060"], "unused", loader=lambda _, c: rows(c) if c == "72030" else [])
        self.assertIsNone(report["aggregate"])
        self.assertIn("83060", report["failures"])
        self.assertEqual(len(report["results"]), 1)

    def test_mismatched_days_disable_aggregate(self):
        def load(_, c):
            values = rows(c)
            if c == "83060":
                values.pop(4)
            return values
        report = run_many(["72030", "83060"], "unused", loader=load)
        self.assertIsNone(report["aggregate"])
        self.assertIn("日付", report["aggregate_reason"])

    def test_common_period_and_explicit_split_truncation(self):
        def load(_, c):
            values = rows(c, 60)
            values[10]["AdjFactor"] = .5
            values[10]["ExRT"] = "1"
            return values
        blocked = run_many(["72030", "83060"], "unused", loader=load)
        self.assertEqual(len(blocked["failures"]), 0)
        self.assertEqual(blocked["common_start"], "2025-01-01")
        allowed = run_many(["72030", "83060"], "unused", loader=load, after_actions=True)
        self.assertIsNotNone(allowed["aggregate"])
        self.assertEqual(allowed["common_start"], "2025-01-12")
        self.assertEqual(allowed["original_common_start"], "2025-01-01")
        self.assertEqual(allowed["results"][0]["strategy"]["days"], 49)

    def test_fetch_partial_failure_continues_without_saving_failed(self):
        fetch = Mock(side_effect=[HistoryError("取得不可"), rows("83060")])
        save = Mock(return_value=(45, "2025-01-01", "2025-02-14"))
        sleep = Mock()
        result = fetch_many(["72030", "83060"], "secret", "unused", fetch, save, sleep)
        self.assertEqual([r["status"] for r in result], ["failed", "ok"])
        save.assert_called_once()
        sleep.assert_called_once_with(15)
        self.assertNotIn("secret", json.dumps(result))

    def test_duplicate_codes_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "symbols.json"
            path.write_text('{"symbols":["7203","72030"]}')
            with self.assertRaises(HistoryError):
                read_symbols(path)

    def test_unaffordable_benchmark_does_not_remove_strategy(self):
        report = run_many(["72030"], "unused", initial=100, loader=lambda _, c: rows(c))
        self.assertIsNotNone(report["aggregate"])
        self.assertIsNone(report["aggregate"]["hold_equity"])
        self.assertIsNone(report["results"][0]["hold"])

    def test_writes_complete_report_with_ten_series(self):
        report = run_many([f"{1000+i}0" for i in range(10)], "unused", loader=lambda _, c: rows(c))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result"
            write_report(report, target)
            self.assertTrue((target / "summary.png").stat().st_size > 1000)
            self.assertEqual(len(list(target.glob("backtest_*.json"))), 10)
            saved = json.loads((target / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["aggregate"]["initial"], 10_000_000)
            with self.assertRaises(FileExistsError):
                write_report(report, target)


if __name__ == "__main__":
    unittest.main()
