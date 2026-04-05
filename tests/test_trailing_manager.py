"""Unit tests for trailing manager: pip estimation and stage logic."""
import sys
import types
import unittest
from unittest.mock import MagicMock

import config as cfg  # noqa: F401 (imported to ensure defaults are available via conftest)

from modules.backtester import _estimate_pip_size  # noqa: E402


class TestEstimatePipSize(unittest.TestCase):
    """Tests for the backtester's _estimate_pip_size() helper."""

    def test_gold_pip_size(self):
        self.assertAlmostEqual(_estimate_pip_size("XAUUSDm"), 0.1)

    def test_bitcoin_pip_size(self):
        self.assertAlmostEqual(_estimate_pip_size("BTCUSDm"), 0.1)

    def test_us500_pip_size(self):
        self.assertAlmostEqual(_estimate_pip_size("US500m"), 1.0)

    def test_eurusd_pip_size(self):
        self.assertAlmostEqual(_estimate_pip_size("EURUSD"), 0.0001)

    def test_gbpusd_pip_size(self):
        self.assertAlmostEqual(_estimate_pip_size("GBPUSD"), 0.0001)

    def test_silver_pip_size(self):
        self.assertAlmostEqual(_estimate_pip_size("XAGUSDm"), 0.01)


class TestBacktesterScalpTrail(unittest.TestCase):
    """
    Tests for _apply_trailing() in scalp mode (SimTrade.scalp_mode=True).
    Verifies that the pips-based stage logic matches the real bot behaviour.
    """

    def _make_trade(self, direction="BUY", entry=1.0, sl=None, tp=None, scalp=True):
        from modules.backtester import SimTrade
        if sl is None:
            sl = entry - 0.005 if direction == "BUY" else entry + 0.005
        if tp is None:
            tp = entry + 0.010 if direction == "BUY" else entry - 0.010
        t = SimTrade(
            symbol="EURUSD",
            direction=direction,
            entry_time=None,
            entry_price=entry,
            sl=sl,
            tp=tp,
            scalp_mode=scalp,
        )
        return t

    def _backtester(self):
        from modules.backtester import Backtester
        from unittest.mock import MagicMock
        bt = Backtester.__new__(Backtester)
        bt.results = {}
        return bt

    def test_no_move_below_min_pips(self):
        """SL should NOT move when gained pips < SCALPING_BE_MIN_PIPS."""
        bt = self._backtester()
        trade = self._make_trade()
        original_sl = trade.sl
        # 1 pip gain on EURUSD = 0.0001 price; min is 2 pips
        bt._apply_trailing(trade, trade.entry_price + 0.0001)
        self.assertAlmostEqual(trade.sl, original_sl, places=5)

    def test_move_to_stage_1_at_5_pips(self):
        """SL should move to lock 15% when gained pips >= STAGE_1 (5 pips)."""
        bt = self._backtester()
        trade = self._make_trade(entry=1.0, sl=0.990, tp=1.050)
        # Use 6 pips (safely above STAGE_1=5.0) to avoid floating-point boundary issues
        gained = 6 * 0.0001
        bt._apply_trailing(trade, trade.entry_price + gained)
        # SL should have moved above entry (breakeven territory)
        self.assertGreater(trade.sl, trade.entry_price)

    def test_stage_progression_higher_pips(self):
        """Higher pips should result in higher SL lock (stage 4 > stage 1)."""
        bt = self._backtester()
        trade1 = self._make_trade(entry=1.0, sl=0.990, tp=1.050)
        trade2 = self._make_trade(entry=1.0, sl=0.990, tp=1.050)

        # 5 pips
        bt._apply_trailing(trade1, 1.0 + 5 * 0.0001)
        # 18 pips (should hit stage 4 = lock 70%)
        bt._apply_trailing(trade2, 1.0 + 18 * 0.0001)

        # Stage 4 should lock more profit than stage 1
        self.assertGreater(trade2.sl, trade1.sl)

    def test_no_sl_regression(self):
        """SL should never move backwards (ratchet protection)."""
        bt = self._backtester()
        trade = self._make_trade(entry=1.0, sl=0.990, tp=1.050)

        # Move SL forward
        bt._apply_trailing(trade, 1.0 + 10 * 0.0001)
        sl_after_first = trade.sl

        # Price drops back (still above entry) — SL should NOT move back
        bt._apply_trailing(trade, 1.0 + 3 * 0.0001)
        self.assertGreaterEqual(trade.sl, sl_after_first)


class TestBacktesterNormalTrail(unittest.TestCase):
    """Tests for _apply_trailing() in normal (non-scalp) mode."""

    def _make_trade(self, direction="BUY", scalp=False):
        from modules.backtester import SimTrade
        entry = 1.0
        sl    = 0.990 if direction == "BUY" else 1.010
        tp    = 1.020 if direction == "BUY" else 0.980
        return SimTrade(
            symbol="EURUSD",
            direction=direction,
            entry_time=None,
            entry_price=entry,
            sl=sl,
            tp=tp,
            scalp_mode=scalp,
        )

    def _backtester(self):
        from modules.backtester import Backtester
        bt = Backtester.__new__(Backtester)
        bt.results = {}
        return bt

    def test_no_move_below_30_pct(self):
        """SL should NOT move when TP progress < 30%."""
        bt = self._backtester()
        trade = self._make_trade()
        original_sl = trade.sl
        # 25% progress: price = 1.0 + 0.25 * (1.020 - 1.0) = 1.005
        bt._apply_trailing(trade, 1.005)
        self.assertAlmostEqual(trade.sl, original_sl, places=5)

    def test_move_at_30_pct_progress(self):
        """SL should move to lock 15% when TP progress >= 30%."""
        bt = self._backtester()
        trade = self._make_trade()
        # 35% progress: price = 1.0 + 0.35 * 0.020 = 1.007
        bt._apply_trailing(trade, 1.007)
        # SL should have moved from 0.990
        self.assertGreater(trade.sl, 0.990)


if __name__ == "__main__":
    unittest.main()
