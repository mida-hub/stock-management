import unittest
import duckdb
import pandas as pd

from stock_management.screener import _classify_lot_type, _get_drop_reason


class TestScreenerHelpers(unittest.TestCase):
    def test_classify_lot_type(self):
        self.assertEqual(
            _classify_lot_type(pd.Series({"market": "US", "raw_close": 500.0}), 5000.0, 25000.0),
            "米国(1株~)",
        )
        self.assertEqual(
            _classify_lot_type(pd.Series({"market": "JP", "raw_close": 5000.0}), 5000.0, 25000.0),
            "単元(100株)",
        )
        self.assertEqual(
            _classify_lot_type(pd.Series({"market": "JP", "raw_close": 12000.0}), 5000.0, 25000.0),
            "S株(未満株)",
        )
        self.assertEqual(
            _classify_lot_type(pd.Series({"market": "JP", "raw_close": 25001.0}), 5000.0, 25000.0),
            "除外(値嵩)",
        )

    def test_get_drop_reason(self):
        cfg = {"sma_25_dev_min": -6.0, "sma_25_dev_max": 5.0, "rsi_14_min": 30.0, "rsi_14_max": 65.0}
        row = pd.Series(
            {
                "close": 1000.0,
                "sma_200": 1100.0,
                "sma_75_slope_pct": -0.5,
                "sma_25_dev_pct": -8.0,
                "rsi_14": 25.0,
                "lot_type": "単元(100株)",
            }
        )
        reason = _get_drop_reason(row, cfg)
        self.assertIn("トレンド外", reason)
        self.assertIn("25MA乖離(-8.0%)", reason)
        self.assertIn("RSI(25.0)", reason)

    def test_volume_profile_in_memory(self):
        con = duckdb.connect(":memory:")
        con.execute(
            "CREATE TABLE daily_prices (code VARCHAR, date DATE, close DOUBLE, volume DOUBLE)"
        )
        rows = [
            ("7936", "2026-11-10", 4000.0, 1000.0),
            ("7936", "2026-11-11", 4500.0, 3000.0),
            ("7936", "2026-11-12", 5000.0, 2000.0),
        ]
        con.executemany("INSERT INTO daily_prices VALUES (?, ?, ?, ?)", rows)
        vp_df = con.execute(
            "SELECT close, volume FROM daily_prices WHERE code = '7936'"
        ).df()
        self.assertEqual(len(vp_df), 3)
        self.assertEqual(vp_df["volume"].sum(), 6000.0)


if __name__ == "__main__":
    unittest.main()
