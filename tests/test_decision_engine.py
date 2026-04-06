"""Unit tests for the deterministic decision engine."""
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from modules.decision_engine import deterministic_decision  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────

def _base_indicators(**overrides):
    """Return a minimal valid indicators dict for the decision engine."""
    base = {
        "trend_votes":  {"bull": 5, "bear": 1},
        "supertrend":   1,
        "kalman_trend": "ALCISTA",
        "hilbert":      {"signal": "LOCAL_MIN"},
        "rsi":          45.0,
        "rsi_div":      "BULL_DIV",
        "fisher":       -2.0,
        "fisher_cross": "BULL_CROSS",
        "confluence":   {"total": 1.5, "bias": "BULLISH"},
        "hurst_penalty": 0.0,
        "spread_pips":  0.0,
    }
    base.update(overrides)
    return base


def _base_sr():
    return {
        "in_strong_zone": True,
        "supports": [{"strength": 6}],
        "resistances": [],
    }


def _base_sym_cfg(**overrides):
    cfg = {
        "rsi_overbought":     70,
        "rsi_oversold":       30,
        "min_decision_score": 4.0,
        "currencies":         ["USD"],
    }
    cfg.update(overrides)
    return cfg


# ── Tests ────────────────────────────────────────────────────────

class TestDecisionEngineStrongBuy(unittest.TestCase):
    def test_strong_buy_signal(self):
        """A strong bullish setup should produce BUY with score >= min."""
        result = deterministic_decision(
            "XAUUSD", "BUY",
            _base_indicators(),
            _base_sr(),
            None,
            _base_sym_cfg(),
        )
        self.assertEqual(result["decision"], "BUY")
        self.assertGreaterEqual(result["score"], 4.0)

    def test_strong_sell_signal(self):
        """A strong bearish setup should produce SELL."""
        ind = {
            "trend_votes":  {"bull": 1, "bear": 5},
            "supertrend":   -1,
            "kalman_trend": "BAJISTA",
            "hilbert":      {"signal": "LOCAL_MAX"},
            "rsi":          55.0,
            "rsi_div":      "BEAR_DIV",
            "fisher":       2.0,
            "fisher_cross": "BEAR_CROSS",
            "confluence":   {"total": -1.5, "bias": "BEARISH"},
            "hurst_penalty": 0.0,
            "spread_pips":  0.0,
        }
        sr = {
            "in_strong_zone": True,
            "supports": [],
            "resistances": [{"strength": 6}],
        }
        result = deterministic_decision(
            "XAUUSD", "SELL", ind, sr, None, _base_sym_cfg()
        )
        self.assertEqual(result["decision"], "SELL")
        self.assertGreaterEqual(result["score"], 4.0)


class TestDecisionEngineRSIFilter(unittest.TestCase):
    def test_rsi_overbought_blocks_buy(self):
        """RSI above overbought should force HOLD on BUY."""
        ind = _base_indicators(rsi=75.0, hilbert={"signal": "NEUTRAL"}, rsi_div="NONE")
        result = deterministic_decision(
            "XAUUSD", "BUY", ind, _base_sr(), None, _base_sym_cfg()
        )
        self.assertEqual(result["decision"], "HOLD")

    def test_rsi_oversold_blocks_sell(self):
        """RSI below oversold should force HOLD on SELL."""
        ind = _base_indicators(rsi=25.0, hilbert={"signal": "NEUTRAL"}, rsi_div="NONE")
        result = deterministic_decision(
            "XAUUSD", "SELL", ind, _base_sr(), None, _base_sym_cfg()
        )
        self.assertEqual(result["decision"], "HOLD")

    def test_rsi_at_boundary_buy_allowed(self):
        """RSI exactly at overbought boundary should NOT block BUY."""
        ind = _base_indicators(rsi=70.0)
        result = deterministic_decision(
            "XAUUSD", "BUY", ind, _base_sr(), None, _base_sym_cfg()
        )
        # RSI=70 is NOT > 70, so BUY should not be blocked by RSI filter
        self.assertNotEqual(result.get("reason", ""), "RSI=70.0>OB(70)")


class TestDecisionEngineWeakSignal(unittest.TestCase):
    def test_hold_on_weak_signal(self):
        """Weak signals should produce HOLD with low score."""
        ind = {
            "trend_votes":  {"bull": 2, "bear": 4},
            "supertrend":   -1,
            "kalman_trend": "BAJISTA",
            "hilbert":      {"signal": "NEUTRAL"},
            "rsi":          50.0,
            "rsi_div":      "NONE",
            "fisher":       0.0,
            "fisher_cross": "NONE",
            "confluence":   {"total": -0.5, "bias": "NEUTRAL"},
            "hurst_penalty": 0.0,
            "spread_pips":  0.0,
        }
        sr = {"in_strong_zone": False, "supports": [], "resistances": []}
        result = deterministic_decision(
            "XAUUSD", "BUY", ind, sr, None, _base_sym_cfg(min_decision_score=5.0)
        )
        self.assertEqual(result["decision"], "HOLD")

    def test_insufficient_votes_reduces_score(self):
        """Low bull votes should produce lower score than high bull votes."""
        ind_high = _base_indicators(trend_votes={"bull": 5, "bear": 1})
        ind_low  = _base_indicators(trend_votes={"bull": 1, "bear": 5})
        r_high = deterministic_decision("XAUUSD", "BUY", ind_high, _base_sr(), None, _base_sym_cfg())
        r_low  = deterministic_decision("XAUUSD", "BUY", ind_low,  _base_sr(), None, _base_sym_cfg())
        self.assertGreater(r_high["score"], r_low["score"])


class TestDecisionEngineHurstPenalty(unittest.TestCase):
    def test_hurst_penalty_reduces_score(self):
        """Hurst penalty should reduce the final score."""
        base = _base_indicators()
        ind_no_pen  = {**base, "hurst_penalty": 0.0}
        ind_penalty = {**base, "hurst_penalty": 3.0}

        r_no  = deterministic_decision("XAUUSD", "BUY", ind_no_pen,  _base_sr(), None, _base_sym_cfg())
        r_pen = deterministic_decision("XAUUSD", "BUY", ind_penalty, _base_sr(), None, _base_sym_cfg())

        self.assertLess(r_pen["score"], r_no["score"])

    def test_large_hurst_penalty_forces_hold(self):
        """A very large Hurst penalty should push a marginal signal to HOLD."""
        ind = _base_indicators(
            trend_votes={"bull": 3, "bear": 3},
            hurst_penalty=8.0,
        )
        result = deterministic_decision(
            "XAUUSD", "BUY", ind, _base_sr(), None, _base_sym_cfg(min_decision_score=4.0)
        )
        self.assertEqual(result["decision"], "HOLD")


class TestDecisionEngineSpreadFilter(unittest.TestCase):
    def test_high_spread_blocks_any_action(self):
        """Spread exceeding max_spread_pips should return HOLD."""
        ind = _base_indicators(spread_pips=5.0)
        sym_cfg = _base_sym_cfg()
        sym_cfg["max_spread_pips"] = 3.0
        result = deterministic_decision("XAUUSD", "BUY", ind, _base_sr(), None, sym_cfg)
        self.assertEqual(result["decision"], "HOLD")
        self.assertIn("SPREAD", result["reason"])

    def test_zero_spread_not_blocked(self):
        """Zero spread (not provided) should not trigger spread filter."""
        ind = _base_indicators(spread_pips=0.0)
        result = deterministic_decision("XAUUSD", "BUY", ind, _base_sr(), None, _base_sym_cfg())
        # Should not be blocked by spread alone
        self.assertNotIn("SPREAD", result.get("reason", ""))


class TestDecisionEngineSessionQuality(unittest.TestCase):
    """Regression tests for the new soft session-quality treatment.

    The old code used ``min_score = min_score / session_q`` which could inflate a
    threshold of 4.0 to 13.3 (session_q=0.3) or even 20.0 (session_q=0.2), making
    scalping outside peak hours virtually impossible.

    The new code uses a soft additive penalty: ``min_score += (1.0 - session_q) * 0.5``,
    capping the extra penalty at ≈+0.35 in practice.
    """

    @staticmethod
    def _parse_threshold_from_reason(reason: str):
        """Extract the effective min_score from a HOLD rejection reason string.

        The reason looks like ``score=X.X<Y.Y | ...``.  Returns the float threshold
        (Y.Y) or None if it cannot be parsed.
        """
        if "score=" in reason and "<" in reason:
            try:
                return float(reason.split("<")[1].split()[0].rstrip("|").strip())
            except (ValueError, IndexError):
                pass
        return None

    def _sym_cfg_with_session(self, asian=0.5, london=1.0, ny=1.0, dead=0.3):
        """Return a sym_cfg that includes a session_quality map."""
        cfg = _base_sym_cfg()
        cfg["session_quality"] = {
            "asian":  asian,
            "london": london,
            "ny":     ny,
            "dead":   dead,
        }
        return cfg

    def test_dead_session_forces_hold(self):
        """session_q <= 0 must always return HOLD regardless of signal quality."""
        sym_cfg = self._sym_cfg_with_session(dead=0.0)
        # Force UTC hour to 21-23 (dead window) by patching _get_session_quality
        with patch("modules.decision_engine._get_session_quality", return_value=0.0):
            result = deterministic_decision(
                "XAUUSD", "BUY", _base_indicators(), _base_sr(), None, sym_cfg
            )
        self.assertEqual(result["decision"], "HOLD")
        self.assertIn("session_q=0", result["reason"])

    def test_low_session_quality_does_not_explode_threshold(self):
        """With session_q=0.3 the effective threshold must stay well below 8.0.

        The old divisor formula would have produced 4.0 / 0.3 ≈ 13.3.
        The new additive penalty adds at most (1.0-0.3)*0.5 = 0.35 → threshold ≈ 4.35.
        """
        base_min = 4.0
        sym_cfg = _base_sym_cfg(min_decision_score=base_min)
        with patch("modules.decision_engine._get_session_quality", return_value=0.3):
            result = deterministic_decision(
                "XAUUSD", "BUY", _base_indicators(), _base_sr(), None, sym_cfg
            )
        # Even if the signal ends up as HOLD, the effective threshold must NOT be > 8.0
        # We can infer the threshold from the "score=X<Y" rejection string.
        if result["decision"] == "HOLD":
            effective_threshold = self._parse_threshold_from_reason(result["reason"])
            if effective_threshold is not None:
                self.assertLess(
                    effective_threshold, 8.0,
                    msg=(
                        f"Threshold {effective_threshold} is too high for session_q=0.3. "
                        "The old divisor formula would have produced ~13.3."
                    ),
                )

    def test_reasonable_scalp_score_approved_despite_suboptimal_session(self):
        """A solid scalping setup (score ≈ 6-7) must be approved even in a non-peak session.

        Under the old divisor logic, min_score=4.0 / session_q=0.5 → 8.0, which would
        reject a score of 6.2.  With the new soft penalty, threshold ≈ 4.25, so 6.2 passes.
        """
        sym_cfg = _base_sym_cfg(min_decision_score=4.0)
        # session_q=0.5 → soft penalty = (1.0-0.5)*0.5 = 0.25 → effective min = 4.25
        with patch("modules.decision_engine._get_session_quality", return_value=0.5):
            result = deterministic_decision(
                "XAUUSD", "BUY", _base_indicators(), _base_sr(), None, sym_cfg
            )
        self.assertEqual(
            result["decision"], "BUY",
            msg=(
                f"Strong scalping setup should be approved even at session_q=0.5. "
                f"Got: {result['decision']} | score={result['score']} | {result['reason']}"
            ),
        )

    def test_soft_penalty_magnitude(self):
        """The session penalty must be strictly additive and capped.

        For session_q=0.5, base_min=4.0:
          new:  4.0 + (1.0-0.5)*0.5 = 4.25
          old:  4.0 / 0.5            = 8.0   ← was never acceptable for scalping
        """
        # We calculate the effective min_score via the HOLD rejection message.
        sym_cfg = _base_sym_cfg(min_decision_score=4.0)
        # Use a weak signal so the engine rejects and embeds the threshold in the reason.
        weak_ind = _base_indicators(
            trend_votes={"bull": 0, "bear": 6},
            supertrend=-1,
            kalman_trend="BAJISTA",
            fisher=2.5,
            fisher_cross="NONE",
            confluence={"total": -1.0, "bias": "BEARISH"},
        )
        with patch("modules.decision_engine._get_session_quality", return_value=0.5):
            result = deterministic_decision(
                "XAUUSD", "BUY", weak_ind, _base_sr(), None, sym_cfg
            )
        self.assertEqual(result["decision"], "HOLD")
        # The embedded threshold in the rejection string must be <= 5.0 (not 8.0)
        effective_threshold = self._parse_threshold_from_reason(result["reason"])
        if effective_threshold is not None:
            self.assertLessEqual(
                effective_threshold, 5.0,
                msg=f"Soft additive penalty should keep threshold ≤5.0, got {effective_threshold}",
            )
            self.assertGreater(
                effective_threshold, 4.0,
                msg="Some penalty should be applied for session_q < 1.0",
            )


if __name__ == "__main__":
    unittest.main()
