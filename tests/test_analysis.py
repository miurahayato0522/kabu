import tempfile
from pathlib import Path
import unittest

from kabu_analysis import analyze, demo, read_events


def event(at, price, volume, source="push"):
    return {"source": source, "received_at": at, "payload": {"Symbol": "7203", "Exchange": 1,
        "CurrentPriceTime": at, "CurrentPrice": price, "TradingVolume": volume, "TradingVolumeTime": at}}


class AnalysisTests(unittest.TestCase):
    def test_ohlc_and_volume(self):
        rows = [event(f"2026-09-14T09:00:{s:02}+09:00", p, v) for s, p, v in
                [(0, 100, 1000), (10, 105, 1100), (20, 98, 1300), (30, 102, 1400)]]
        result = analyze(rows + [rows[-1]])
        bar = result["bars"][0]
        self.assertEqual([bar[k] for k in ("open", "high", "low", "close", "observed_volume")], [100, 105, 98, 102, 400])
        self.assertEqual(bar["observations"], 4)

    def test_cross_minute_day_and_disconnect(self):
        rows = [event("2026-09-14T09:00:55+09:00", 100, 1000),
                event("2026-09-14T09:01:05+09:00", 101, 1200),
                event("2026-09-15T09:00:05+09:00", 102, 100),
                event("2026-09-15T09:00:06+09:00", 102, 100, "disconnected"),
                event("2026-09-15T09:00:15+09:00", 103, 500)]
        bars = analyze(rows)["bars"]
        self.assertTrue(all(b["observed_volume"] == 0 for b in bars))
        self.assertIn("volume_crosses_minute", bars[1]["flags"])
        self.assertIn("no_volume_baseline", bars[2]["flags"])

    def test_null_snapshot_and_out_of_order(self):
        rows = [event("2026-09-14T09:00:20+09:00", 100, 1000),
                event("2026-09-14T09:00:10+09:00", 999, 900),
                event("2026-09-14T09:01:00+09:00", None, None),
                event("2026-09-13T15:30:00+09:00", 999, 900, "snapshot")]
        result = analyze(rows)
        self.assertEqual(result["bar_count"], 1)
        self.assertEqual(result["bars"][0]["close"], 100)
        self.assertEqual(result["quality_counts"]["invalid_price"], 1)
        self.assertEqual(result["quality_counts"]["out_of_order"], 1)

    def test_demo_is_separate_reproducible_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demo.sqlite3"
            demo(path)
            result = analyze(read_events(path, "demo"))
            self.assertEqual(result["event_counts"], {"push": 240})
            self.assertEqual(result["bar_count"], 20)
            self.assertEqual(list(read_events(path, "production")), [])
            with self.assertRaises(FileExistsError):
                demo(path)
            self.assertEqual(analyze(read_events(path, "demo")), result)


if __name__ == "__main__":
    unittest.main()
