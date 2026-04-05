"""Unit tests for risk management functions."""
import sys
import types
import unittest

from modules.risk_manager import calc_sl_tp, is_rr_valid, get_rr  # noqa: E402


class TestCalcSlTp(unittest.TestCase):
    def test_buy_sl_below_tp_above(self):
        sym_cfg = {"sl_atr_mult": 2.0, "tp_atr_mult_buy": 4.0, "tp_atr_mult": 3.0}
        sl, tp = calc_sl_tp("BUY", 1.1000, 0.0010, sym_cfg)
        self.assertLess(sl, 1.1000, "SL should be below entry for BUY")
        self.assertGreater(tp, 1.1000, "TP should be above entry for BUY")

    def test_sell_sl_above_tp_below(self):
        sym_cfg = {"sl_atr_mult": 2.0, "tp_atr_mult_sell": 3.0, "tp_atr_mult": 3.0}
        sl, tp = calc_sl_tp("SELL", 1.1000, 0.0010, sym_cfg)
        self.assertGreater(sl, 1.1000, "SL should be above entry for SELL")
        self.assertLess(tp, 1.1000, "TP should be below entry for SELL")

    def test_buy_exact_values(self):
        """SL = price - sl_mult*ATR; TP = price + tp_mult*ATR."""
        sym_cfg = {"sl_atr_mult": 2.0, "tp_atr_mult_buy": 4.0, "tp_atr_mult": 3.0}
        sl, tp = calc_sl_tp("BUY", 1.1000, 0.0010, sym_cfg)
        self.assertAlmostEqual(sl, 1.0980, places=4)
        self.assertAlmostEqual(tp, 1.1040, places=4)

    def test_sell_exact_values(self):
        """SL = price + sl_mult*ATR; TP = price - tp_mult*ATR."""
        sym_cfg = {"sl_atr_mult": 2.0, "tp_atr_mult_sell": 3.0, "tp_atr_mult": 3.0}
        sl, tp = calc_sl_tp("SELL", 1.1000, 0.0010, sym_cfg)
        self.assertAlmostEqual(sl, 1.1020, places=4)
        self.assertAlmostEqual(tp, 1.0970, places=4)

    def test_fallback_tp_mult(self):
        """When direction-specific mult is absent, falls back to tp_atr_mult."""
        sym_cfg = {"sl_atr_mult": 2.0, "tp_atr_mult": 3.5}
        sl, tp = calc_sl_tp("BUY", 1.2000, 0.0020, sym_cfg)
        self.assertAlmostEqual(tp - 1.2000, 3.5 * 0.0020, places=4)

    def test_asymmetric_tp(self):
        """Buy and sell can have different TP multipliers."""
        sym_cfg = {
            "sl_atr_mult":    2.0,
            "tp_atr_mult_buy":  5.0,
            "tp_atr_mult_sell": 2.5,
        }
        _sl_buy, tp_buy   = calc_sl_tp("BUY",  1.0, 0.01, sym_cfg)
        _sl_sell, tp_sell = calc_sl_tp("SELL", 1.0, 0.01, sym_cfg)
        self.assertAlmostEqual(tp_buy,  1.05, places=4)
        self.assertAlmostEqual(tp_sell, 0.975, places=4)


class TestIsRrValid(unittest.TestCase):
    def test_valid_rr(self):
        # RR = (1.1030 - 1.1000) / (1.1000 - 1.0990) = 0.003 / 0.001 = 3.0
        self.assertTrue(is_rr_valid(1.1000, 1.0990, 1.1030, min_rr=1.5))

    def test_invalid_rr(self):
        # RR = (1.1005 - 1.1000) / (1.1000 - 1.0990) = 0.0005 / 0.001 = 0.5
        self.assertFalse(is_rr_valid(1.1000, 1.0990, 1.1005, min_rr=1.5))

    def test_zero_risk_returns_false(self):
        # price == sl → risk = 0 → invalid
        self.assertFalse(is_rr_valid(1.1000, 1.1000, 1.1030, min_rr=1.5))

    def test_exact_minimum_rr(self):
        # risk = |1.0000 - 0.9800| = 0.02, reward = |1.0300 - 1.0000| = 0.03
        # RR = 0.03 / 0.02 = 1.5 exactly in IEEE 754
        self.assertTrue(is_rr_valid(1.0, 0.98, 1.03, min_rr=1.5))

    def test_sell_direction_valid(self):
        # SELL: price=1.1000, sl=1.1020 (+20 pips risk), tp=1.0940 (-60 pips reward)
        # RR = 60/20 = 3.0
        self.assertTrue(is_rr_valid(1.1000, 1.1020, 1.0940, min_rr=2.0))


class TestGetRr(unittest.TestCase):
    def test_standard_rr(self):
        rr = get_rr(1.1000, 1.0990, 1.1030)
        self.assertAlmostEqual(rr, 3.0, places=2)

    def test_zero_risk(self):
        rr = get_rr(1.1000, 1.1000, 1.1030)
        self.assertEqual(rr, 0.0)

    def test_rr_less_than_one(self):
        # reward 5 pips, risk 10 pips → RR = 0.5
        rr = get_rr(1.1000, 1.0990, 1.1005)
        self.assertAlmostEqual(rr, 0.5, places=2)

    def test_symmetric_rr(self):
        # Equal distance = RR of 1.0
        rr = get_rr(1.1000, 1.0990, 1.1010)
        self.assertAlmostEqual(rr, 1.0, places=2)


if __name__ == "__main__":
    unittest.main()
