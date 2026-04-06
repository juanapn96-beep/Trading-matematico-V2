"""Tests for Mejora 12 — MTF Confirmation and Mejora 15 — Shadow Mode."""
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# ── Ensure environment variables are set before importing config ─
import os
os.environ.setdefault("MT5_LOGIN",    "12345")
os.environ.setdefault("MT5_PASSWORD", "test_password")
os.environ.setdefault("MT5_SERVER",   "test-server")
os.environ.setdefault("TELEGRAM_TOKEN",   "0:test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "0")

# ── Stub MetaTrader5 if not installed ────────────────────────────
if "MetaTrader5" not in sys.modules:
    _mt5 = types.ModuleType("MetaTrader5")
    _mt5.TIMEFRAME_M1  = 1
    _mt5.TIMEFRAME_M5  = 5
    _mt5.TIMEFRAME_M15 = 15
    _mt5.TIMEFRAME_H1  = 16385
    _mt5.TIMEFRAME_H4  = 16388
    _mt5.TIMEFRAME_D1  = 16408
    _mt5.ORDER_TYPE_BUY  = 0
    _mt5.ORDER_TYPE_SELL = 1
    _mt5.TRADE_ACTION_DEAL  = 1
    _mt5.TRADE_ACTION_SLTP  = 6
    _mt5.ORDER_TIME_GTC     = 1
    _mt5.ORDER_FILLING_FOK    = 0
    _mt5.ORDER_FILLING_IOC    = 1
    _mt5.ORDER_FILLING_RETURN = 2
    _mt5.TRADE_RETCODE_DONE = 10009
    for _attr in ("initialize", "login", "account_info", "symbol_info",
                  "symbol_info_tick", "positions_get", "order_send",
                  "copy_rates_from_pos", "symbol_select",
                  "history_deals_get", "last_error"):
        setattr(_mt5, _attr, MagicMock(return_value=None))
    sys.modules["MetaTrader5"] = _mt5


# ════════════════════════════════════════════════════════════════
#  Helper: fake candle DataFrame
# ════════════════════════════════════════════════════════════════

def _make_candles(n: int = 100, trend: str = "up"):
    import pandas as pd
    import numpy as np

    rng = np.random.default_rng(42)
    base = 1.10000
    closes = [base]
    for _ in range(n - 1):
        step = rng.uniform(0.0001, 0.0005)
        closes.append(closes[-1] + (step if trend == "up" else -step))

    closes = np.array(closes)
    df = pd.DataFrame({
        "time":   pd.date_range("2024-01-01", periods=n, freq="1h"),
        "open":   closes * 0.9999,
        "high":   closes * 1.0005,
        "low":    closes * 0.9995,
        "close":  closes,
        "volume": rng.integers(100, 1000, n).astype(float),
    })
    return df


# ════════════════════════════════════════════════════════════════
#  MEJORA 12 — MTF CONFIRMATION TESTS
# ════════════════════════════════════════════════════════════════

class TestMTFConfirmationBullish(unittest.TestCase):
    """MTF check with H1 data that should yield a BULLISH trend."""

    def setUp(self):
        # Clear cache before each test
        from modules.mtf_confirmation import invalidate_cache
        invalidate_cache("EURUSD")

    @patch("modules.execution.get_candles")
    @patch("modules.indicators.compute_all")
    def test_buy_agrees_when_h1_alcista(self, mock_compute, mock_candles):
        """BUY should agree when H1 trend is ALCISTA."""
        mock_candles.return_value = _make_candles(100, "up")
        mock_compute.return_value = {
            "h1_trend":    "ALCISTA",
            "trend_votes": {"bull": 5, "bear": 1},
        }

        from modules.mtf_confirmation import check_mtf_confirmation
        result = check_mtf_confirmation("EURUSD", "BUY", {"min_hurst": 0.35})

        self.assertIsNotNone(result)
        self.assertEqual(result["htf_trend"], "ALCISTA")
        self.assertTrue(result["htf_agrees"])
        self.assertEqual(result["htf_bull_votes"], 5)
        self.assertEqual(result["htf_bear_votes"], 1)

    @patch("modules.execution.get_candles")
    @patch("modules.indicators.compute_all")
    def test_sell_disagrees_when_h1_alcista(self, mock_compute, mock_candles):
        """SELL should NOT agree when H1 is ALCISTA."""
        mock_candles.return_value = _make_candles(100, "up")
        mock_compute.return_value = {
            "h1_trend":    "ALCISTA",
            "trend_votes": {"bull": 5, "bear": 1},
        }

        from modules.mtf_confirmation import check_mtf_confirmation
        result = check_mtf_confirmation("EURUSD", "SELL", {"min_hurst": 0.35})

        self.assertIsNotNone(result)
        self.assertFalse(result["htf_agrees"])


class TestMTFConfirmationBearish(unittest.TestCase):
    """MTF check when H1 is BAJISTA."""

    def setUp(self):
        from modules.mtf_confirmation import invalidate_cache
        invalidate_cache("GBPUSD")

    @patch("modules.execution.get_candles")
    @patch("modules.indicators.compute_all")
    def test_sell_agrees_when_h1_bajista(self, mock_compute, mock_candles):
        """SELL should agree when H1 trend is BAJISTA."""
        mock_candles.return_value = _make_candles(100, "down")
        mock_compute.return_value = {
            "h1_trend":    "BAJISTA",
            "trend_votes": {"bull": 1, "bear": 5},
        }

        from modules.mtf_confirmation import check_mtf_confirmation
        result = check_mtf_confirmation("GBPUSD", "SELL", {"min_hurst": 0.35})

        self.assertIsNotNone(result)
        self.assertTrue(result["htf_agrees"])

    @patch("modules.execution.get_candles")
    @patch("modules.indicators.compute_all")
    def test_buy_disagrees_when_h1_bajista(self, mock_compute, mock_candles):
        """BUY should NOT agree when H1 is BAJISTA."""
        mock_candles.return_value = _make_candles(100, "down")
        mock_compute.return_value = {
            "h1_trend":    "BAJISTA",
            "trend_votes": {"bull": 1, "bear": 5},
        }

        from modules.mtf_confirmation import check_mtf_confirmation
        result = check_mtf_confirmation("GBPUSD", "BUY", {"min_hurst": 0.35})

        self.assertFalse(result["htf_agrees"])


class TestMTFConfirmationLateral(unittest.TestCase):
    """MTF check when H1 is LATERAL — neither BUY nor SELL agrees."""

    def setUp(self):
        from modules.mtf_confirmation import invalidate_cache
        invalidate_cache("XAUUSD")

    @patch("modules.execution.get_candles")
    @patch("modules.indicators.compute_all")
    def test_lateral_does_not_agree(self, mock_compute, mock_candles):
        """LATERAL H1 should not agree with BUY or SELL."""
        mock_candles.return_value = _make_candles(100, "up")
        mock_compute.return_value = {
            "h1_trend":    "LATERAL",
            "trend_votes": {"bull": 3, "bear": 3},
        }

        from modules.mtf_confirmation import check_mtf_confirmation
        r_buy  = check_mtf_confirmation("XAUUSD", "BUY",  {"min_hurst": 0.35})
        from modules.mtf_confirmation import invalidate_cache
        invalidate_cache("XAUUSD")
        r_sell = check_mtf_confirmation("XAUUSD", "SELL", {"min_hurst": 0.35})

        self.assertFalse(r_buy["htf_agrees"])
        self.assertFalse(r_sell["htf_agrees"])


class TestMTFConfirmationNoData(unittest.TestCase):
    """MTF should return None when candles are unavailable."""

    def setUp(self):
        from modules.mtf_confirmation import invalidate_cache
        invalidate_cache("EURUSD")

    @patch("modules.execution.get_candles", return_value=None)
    def test_returns_none_when_no_candles(self, _):
        from modules.mtf_confirmation import check_mtf_confirmation
        result = check_mtf_confirmation("EURUSD", "BUY", {})
        self.assertIsNone(result)


class TestMTFConfirmationCache(unittest.TestCase):
    """MTF cache prevents re-fetching within TTL."""

    def setUp(self):
        from modules.mtf_confirmation import invalidate_cache
        invalidate_cache("EURUSD")

    @patch("modules.execution.get_candles")
    @patch("modules.indicators.compute_all")
    def test_cache_reused_within_ttl(self, mock_compute, mock_candles):
        mock_candles.return_value = _make_candles(100, "up")
        mock_compute.return_value = {
            "h1_trend":    "ALCISTA",
            "trend_votes": {"bull": 5, "bear": 1},
        }

        from modules.mtf_confirmation import check_mtf_confirmation
        check_mtf_confirmation("EURUSD", "BUY", {})
        check_mtf_confirmation("EURUSD", "BUY", {})

        # compute_all should be called once (second call uses cache)
        self.assertEqual(mock_compute.call_count, 1)


# ════════════════════════════════════════════════════════════════
#  MEJORA 15 — SHADOW TRADE LIFECYCLE TESTS
# ════════════════════════════════════════════════════════════════

class TestShadowTradeLifecycle(unittest.TestCase):
    """Tests for shadow trade open/close using an in-memory SQLite DB."""

    def setUp(self):
        """Patch DB_PATH to use an in-memory SQLite database."""
        import pathlib
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = pathlib.Path(self._tmpdir) / "test_shadow.db"

        import modules.neural_brain as nb
        self._orig_db_path = nb.DB_PATH
        nb.DB_PATH = self._db_path
        nb.init_db()

        # Clear shadow tracker in-memory state
        import modules.shadow_tracker as st
        st._open_shadow.clear()

    def tearDown(self):
        import modules.neural_brain as nb
        nb.DB_PATH = self._orig_db_path
        import modules.shadow_tracker as st
        st._open_shadow.clear()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_open_shadow_trade_returns_id(self):
        """open_shadow_trade() should return a positive int id."""
        from modules.shadow_tracker import open_shadow_trade
        sid = open_shadow_trade(
            symbol="EURUSD", direction="BUY",
            entry_price=1.10000, sl=1.09900, tp=1.10200,
            volume=0.01, score=5.0, reason="test_open",
            ind={"h1_trend": "ALCISTA", "hurst": 0.55, "rsi": 45.0, "atr": 0.0010},
        )
        self.assertGreater(sid, 0)

    def test_shadow_trade_tracked_in_memory(self):
        """After open, shadow position should be in _open_shadow."""
        from modules.shadow_tracker import open_shadow_trade, get_open_shadow_count
        open_shadow_trade(
            symbol="EURUSD", direction="BUY",
            entry_price=1.10000, sl=1.09900, tp=1.10200,
            volume=0.01, score=5.0, reason="test_open",
        )
        self.assertEqual(get_open_shadow_count(), 1)

    @patch("modules.telegram_notifier.notify_shadow_trade_closed")
    @patch("modules.execution.price_distance_to_pips", return_value=10.0)
    def test_shadow_tp_hit_win(self, _mock_pips, mock_notify):
        """Price reaching TP should close shadow trade as WIN."""
        from modules.shadow_tracker import (
            open_shadow_trade, check_shadow_positions, get_open_shadow_count,
        )
        open_shadow_trade(
            symbol="EURUSD", direction="BUY",
            entry_price=1.10000, sl=1.09900, tp=1.10200,
            volume=0.01, score=5.0, reason="test_tp",
        )
        # Price hits TP
        check_shadow_positions({"EURUSD": {"bid": 1.10250, "ask": 1.10252}})

        self.assertEqual(get_open_shadow_count(), 0)
        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args[1]
        self.assertEqual(call_kwargs["result"], "WIN")

    @patch("modules.telegram_notifier.notify_shadow_trade_closed")
    @patch("modules.execution.price_distance_to_pips", return_value=10.0)
    def test_shadow_sl_hit_loss(self, _mock_pips, mock_notify):
        """Price reaching SL should close shadow trade as LOSS."""
        from modules.shadow_tracker import (
            open_shadow_trade, check_shadow_positions, get_open_shadow_count,
        )
        open_shadow_trade(
            symbol="EURUSD", direction="BUY",
            entry_price=1.10000, sl=1.09900, tp=1.10200,
            volume=0.01, score=5.0, reason="test_sl",
        )
        # Price hits SL
        check_shadow_positions({"EURUSD": {"bid": 1.09850, "ask": 1.09852}})

        self.assertEqual(get_open_shadow_count(), 0)
        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args[1]
        self.assertEqual(call_kwargs["result"], "LOSS")

    @patch("modules.telegram_notifier.notify_shadow_trade_closed")
    @patch("modules.execution.price_distance_to_pips", return_value=10.0)
    def test_shadow_sell_tp_hit_win(self, _mock_pips, mock_notify):
        """SELL trade reaching TP (price falls to tp) should be WIN."""
        from modules.shadow_tracker import (
            open_shadow_trade, check_shadow_positions, get_open_shadow_count,
        )
        open_shadow_trade(
            symbol="GBPUSD", direction="SELL",
            entry_price=1.27000, sl=1.27200, tp=1.26700,
            volume=0.01, score=5.0, reason="test_sell_tp",
        )
        # Price hits TP (drops below tp)
        check_shadow_positions({"GBPUSD": {"bid": 1.26650, "ask": 1.26655}})

        self.assertEqual(get_open_shadow_count(), 0)
        call_kwargs = mock_notify.call_args[1]
        self.assertEqual(call_kwargs["result"], "WIN")

    @patch("modules.telegram_notifier.notify_shadow_trade_closed")
    @patch("modules.execution.price_distance_to_pips", return_value=10.0)
    def test_shadow_sell_sl_hit_loss(self, _mock_pips, mock_notify):
        """SELL trade reaching SL (price rises to sl) should be LOSS."""
        from modules.shadow_tracker import (
            open_shadow_trade, check_shadow_positions, get_open_shadow_count,
        )
        open_shadow_trade(
            symbol="GBPUSD", direction="SELL",
            entry_price=1.27000, sl=1.27200, tp=1.26700,
            volume=0.01, score=5.0, reason="test_sell_sl",
        )
        # Price hits SL (rises above sl)
        check_shadow_positions({"GBPUSD": {"bid": 1.27250, "ask": 1.27255}})

        self.assertEqual(get_open_shadow_count(), 0)
        call_kwargs = mock_notify.call_args[1]
        self.assertEqual(call_kwargs["result"], "LOSS")

    def test_no_close_when_price_between_sl_tp(self):
        """Shadow trade should stay open if price is between SL and TP."""
        from modules.shadow_tracker import (
            open_shadow_trade, check_shadow_positions, get_open_shadow_count,
        )
        open_shadow_trade(
            symbol="EURUSD", direction="BUY",
            entry_price=1.10000, sl=1.09900, tp=1.10200,
            volume=0.01, score=5.0, reason="test_hold",
        )
        check_shadow_positions({"EURUSD": {"bid": 1.10050, "ask": 1.10052}})
        self.assertEqual(get_open_shadow_count(), 1)

    def test_get_shadow_stats_after_close(self):
        """get_shadow_stats should reflect closed trades."""
        from modules.neural_brain import save_shadow_trade, close_shadow_trade, get_shadow_stats
        sid = save_shadow_trade(
            symbol="EURUSD", direction="BUY", entry_price=1.10,
            sl=1.099, tp=1.103, volume=0.01, score=5.0, reason="t",
            h1_trend="ALCISTA", htf_trend="ALCISTA",
            hurst=0.55, rsi=45.0, atr=0.001,
        )
        close_shadow_trade(sid, exit_price=1.103, result="WIN", profit_pips=30.0, duration_min=5)
        stats = get_shadow_stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["losses"], 0)
        self.assertEqual(stats["win_rate"], 100.0)


# ════════════════════════════════════════════════════════════════
#  SHADOW + MTF DISABLED BY DEFAULT
# ════════════════════════════════════════════════════════════════

class TestDefaultsOff(unittest.TestCase):
    """Both features must be disabled by default."""

    def test_shadow_mode_disabled_by_default(self):
        import config as cfg
        self.assertFalse(getattr(cfg, "SHADOW_MODE_ENABLED", True),
                         "SHADOW_MODE_ENABLED should default to False")

    def test_mtf_enabled_by_default(self):
        """MTF is a quality filter — enabled by default."""
        import config as cfg
        self.assertTrue(getattr(cfg, "MTF_ENABLED", False),
                        "MTF_ENABLED should default to True")


if __name__ == "__main__":
    unittest.main()
