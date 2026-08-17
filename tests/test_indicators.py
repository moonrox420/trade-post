"""Unit tests for trade_post.market.indicators."""

import unittest

from trade_post.market import indicators as ind


class TestSMA(unittest.TestCase):
    def test_sma_basic(self):
        self.assertEqual(ind.sma([1, 2, 3, 4, 5], 3), 4.0)
        self.assertEqual(ind.sma([1, 2, 3, 4, 5], 5), 3.0)

    def test_sma_too_short(self):
        self.assertIsNone(ind.sma([1, 2], 5))

    def test_sma_zero_period(self):
        self.assertIsNone(ind.sma([1, 2, 3], 0))


class TestEMA(unittest.TestCase):
    def test_ema_monotonic(self):
        out = ind.ema([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15], 5)
        self.assertGreater(out, 8)  # trending up -> above middle
        self.assertLess(out, 15)

    def test_ema_short(self):
        self.assertIsNone(ind.ema([1, 2], 5))


class TestRSI(unittest.TestCase):
    def test_rsi_all_up(self):
        # Monotonically rising -> RSI should be 100.
        r = ind.rsi([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25])
        self.assertEqual(r, 100.0)

    def test_rsi_all_down(self):
        r = ind.rsi([25, 24, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10])
        self.assertEqual(r, 0.0)

    def test_rsi_flat(self):
        r = ind.rsi([20] * 20)
        self.assertEqual(r, 50.0)  # no movement -> neutral


class TestMACD(unittest.TestCase):
    def test_macd_returns_dict(self):
        v = [10 + i * 0.1 for i in range(50)]
        m = ind.macd(v)
        self.assertIsInstance(m, dict)
        self.assertIn("macd", m)
        self.assertIn("signal", m)
        self.assertIn("histogram", m)

    def test_macd_too_short(self):
        self.assertIsNone(ind.macd([1, 2, 3]))


class TestATR(unittest.TestCase):
    def test_atr_basic(self):
        highs = [11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25]
        lows = [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23]
        closes = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
        a = ind.atr(highs, lows, closes, 5)
        self.assertIsNotNone(a)
        self.assertGreater(a, 0)


class TestBollinger(unittest.TestCase):
    def test_bollinger_bands(self):
        v = [10] * 20 + [12, 11]  # flat then spike
        b = ind.bollinger(v, 20, 2.0)
        self.assertIsNotNone(b)
        self.assertGreater(b["upper"], b["middle"])
        self.assertLess(b["lower"], b["middle"])


class TestVolatility(unittest.TestCase):
    def test_realized_vol_positive(self):
        # Random walk produces positive volatility.
        v = [10, 11, 10, 12, 11, 13, 12, 14, 13, 12, 11, 10, 11, 12, 13, 14, 15, 14, 13, 12]
        vol = ind.realized_volatility([v[i] / v[i - 1] - 1 for i in range(1, len(v))])
        self.assertIsNotNone(vol)
        self.assertGreater(vol, 0)


class TestComputeAll(unittest.TestCase):
    def test_compute_all_with_ohlcv(self):
        # Generate a fake series.
        closes = [10 + 0.1 * i for i in range(60)]
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        vols = [100 + i for i in range(60)]
        out = ind.compute_all(closes, highs, lows, vols)
        self.assertIsNotNone(out["rsi"])
        self.assertIsNotNone(out["macd"])
        self.assertIsNotNone(out["ema_fast"])
        self.assertIsNotNone(out["ema_slow"])
        self.assertIsNotNone(out["sma"])
        self.assertIsNotNone(out["bollinger"])
        self.assertIsNotNone(out["atr"])
        self.assertIsNotNone(out["vwap"])
        self.assertIsNotNone(out["volatility"])


if __name__ == "__main__":
    unittest.main()
