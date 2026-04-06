"""
Unit tests for watch_closures() in modules/position_manager.py.

Covers:
- Successful reconciliation via primary path (position_id lookup)
- Successful reconciliation via fallback path (deal.order lookup)
- Unresolved tickets: warning suppression / no unbounded spam across cycles
- Cleanup of state.closure_retry_counts after successful reconciliation
"""
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import config as cfg  # noqa: F401 – ensures conftest defaults are applied


# ── Minimal deal stub ────────────────────────────────────────────────────────

def _make_deal(
    ticket=1,
    order=1,
    position_id=1001,
    entry=1,       # 1 = OUT (close), 0 = IN (open), 3 = INOUT (reverse)
    profit=5.0,
    symbol="EURUSD",
    price=1.1000,
    deal_type=0,   # 0=BUY, 1=SELL
    commission=0.0,
    swap=0.0,
    time=1000,
):
    d = types.SimpleNamespace(
        ticket=ticket,
        order=order,
        position_id=position_id,
        entry=entry,
        profit=profit,
        symbol=symbol,
        price=price,
        type=deal_type,
        commission=commission,
        swap=swap,
        time=time,
    )
    return d


def _make_pending(ticket, opened_at="2026-04-05T09:00:00", open_price=1.0990,
                  direction="BUY"):
    return {
        "ticket": ticket,
        "opened_at": opened_at,
        "open_price": open_price,
        "direction": direction,
        "slippage_pips": 0.0,
    }


# ── Helper: build a fresh isolated module under test ────────────────────────

def _import_fresh_pm():
    """
    Re-import position_manager with all heavy dependencies mocked so that
    individual test cases start from a clean module state.
    """
    import importlib
    import sys

    # Stub modules that aren't available in the test environment
    stubs = {
        "modules.trailing_manager": MagicMock(
            trail_stage={},
            manage_trailing_stop=MagicMock(return_value=None),
            cleanup_ticket=MagicMock(),
        ),
        "modules.execution": MagicMock(
            close_position_market=MagicMock(return_value=False),
            calc_pips_instrument=MagicMock(return_value=0.0),
        ),
        "modules.risk_manager": MagicMock(
            check_circuit_breaker=MagicMock(return_value=(False, "")),
        ),
        "modules.neural_brain": MagicMock(
            get_pending_trades=MagicMock(return_value=[]),
            update_trade_result=MagicMock(),
        ),
        "modules.telegram_notifier": MagicMock(
            notify_near_tp=MagicMock(),
            notify_trade_closed=MagicMock(),
            _send=MagicMock(),
        ),
        "modules.exec_quality_monitor": MagicMock(
            record_execution=MagicMock(),
        ),
    }

    for name, mock in stubs.items():
        sys.modules[name] = mock

    if "modules.position_manager" in sys.modules:
        del sys.modules["modules.position_manager"]

    import modules.position_manager as pm
    return pm, stubs


# ── Test cases ────────────────────────────────────────────────────────────────

class TestWatchClosuresNormalPath(unittest.TestCase):
    """Successful reconciliation via primary position_id lookup."""

    def setUp(self):
        self.pm, self.stubs = _import_fresh_pm()
        # Reset module-level sets
        self.pm.be_activated.clear()
        self.pm.tp_alerted.clear()
        self.pm._partial_tp_done.clear()

        from modules.bot_state import state as _state
        _state.closure_retry_counts.clear()
        _state.pending_closure_tickets.clear()
        _state.tickets_en_memoria.clear()
        _state.trade_mode_cache.clear()
        self._state = _state

    def test_successful_reconciliation_normal_path(self):
        """
        When a closing deal with entry==1 exists for a ticket,
        watch_closures() returns an empty unresolved set and calls
        update_trade_result once.
        """
        ticket = 1001
        closing_deal = _make_deal(
            ticket=2001, order=1001, position_id=ticket,
            entry=1, profit=10.0, symbol="EURUSD",
        )
        pending = [_make_pending(ticket)]

        self.stubs["modules.neural_brain"].get_pending_trades.return_value = pending

        import MetaTrader5 as _mt5
        _mt5.history_deals_get = MagicMock(return_value=[closing_deal])

        open_before = {ticket}
        open_now    = []

        unresolved = self.pm.watch_closures(open_before, open_now)

        self.assertEqual(unresolved, set(), "No unresolved tickets expected")
        self.stubs["modules.neural_brain"].update_trade_result.assert_called_once()
        self.assertNotIn(ticket, self._state.closure_retry_counts)

    def test_successful_reconciliation_clears_retry_count(self):
        """
        If a ticket had previous failed attempts, successful reconciliation
        removes it from closure_retry_counts.
        """
        ticket = 1002
        self._state.closure_retry_counts[ticket] = 3

        closing_deal = _make_deal(
            ticket=2002, order=1002, position_id=ticket,
            entry=1, profit=5.0, symbol="EURUSD",
        )
        pending = [_make_pending(ticket)]

        self.stubs["modules.neural_brain"].get_pending_trades.return_value = pending

        import MetaTrader5 as _mt5
        _mt5.history_deals_get = MagicMock(return_value=[closing_deal])

        unresolved = self.pm.watch_closures({ticket}, [])

        self.assertEqual(unresolved, set())
        self.assertNotIn(ticket, self._state.closure_retry_counts)


class TestWatchClosuresFallbackPath(unittest.TestCase):
    """Successful reconciliation via deal.order fallback."""

    def setUp(self):
        self.pm, self.stubs = _import_fresh_pm()
        self.pm.be_activated.clear()
        self.pm.tp_alerted.clear()
        self.pm._partial_tp_done.clear()

        from modules.bot_state import state as _state
        _state.closure_retry_counts.clear()
        _state.pending_closure_tickets.clear()
        _state.tickets_en_memoria.clear()
        _state.trade_mode_cache.clear()
        self._state = _state

    def test_fallback_by_order_field(self):
        """
        When position_id does NOT match the ticket but deal.order does,
        the fallback path reconciles the closure and returns no unresolved tickets.
        """
        ticket = 1003
        # Simulate a deal whose position_id differs from ticket but order == ticket
        closing_deal = _make_deal(
            ticket=2003,
            order=ticket,        # order == ticket → should be found by fallback
            position_id=9999,    # different position_id → primary path misses it
            entry=1,
            profit=8.0,
            symbol="GBPUSD",
        )
        pending = [_make_pending(ticket, direction="BUY")]

        self.stubs["modules.neural_brain"].get_pending_trades.return_value = pending

        import MetaTrader5 as _mt5
        _mt5.history_deals_get = MagicMock(return_value=[closing_deal])

        unresolved = self.pm.watch_closures({ticket}, [])

        self.assertEqual(unresolved, set(), "Fallback should resolve the ticket")
        self.stubs["modules.neural_brain"].update_trade_result.assert_called_once()

    def test_entry_3_reverse_deal_matched(self):
        """
        A deal with entry==3 (reverse/inout) is accepted as a closing deal.
        """
        ticket = 1004
        closing_deal = _make_deal(
            ticket=2004, order=ticket,
            position_id=9998,
            entry=3,  # INOUT / reverse
            profit=-2.0, symbol="XAUUSD",
        )
        pending = [_make_pending(ticket, direction="BUY")]
        self.stubs["modules.neural_brain"].get_pending_trades.return_value = pending

        import MetaTrader5 as _mt5
        _mt5.history_deals_get = MagicMock(return_value=[closing_deal])

        unresolved = self.pm.watch_closures({ticket}, [])

        self.assertEqual(unresolved, set())
        self.stubs["modules.neural_brain"].update_trade_result.assert_called_once()


class TestWatchClosuresUnresolvedHandling(unittest.TestCase):
    """Unresolved tickets: bounded retries, warning suppression."""

    def setUp(self):
        self.pm, self.stubs = _import_fresh_pm()
        self.pm.be_activated.clear()
        self.pm.tp_alerted.clear()
        self.pm._partial_tp_done.clear()

        from modules.bot_state import state as _state
        _state.closure_retry_counts.clear()
        _state.pending_closure_tickets.clear()
        _state.tickets_en_memoria.clear()
        _state.trade_mode_cache.clear()
        self._state = _state

    def _run_cycle(self, ticket, history):
        """Simulate one watch_closures cycle for a ticket with no open position."""
        import MetaTrader5 as _mt5
        _mt5.history_deals_get = MagicMock(return_value=history)
        return self.pm.watch_closures({ticket}, [])

    def test_first_attempt_warns_and_returns_unresolved(self):
        """On the first failed attempt, a warning is logged and ticket is unresolved."""
        ticket = 2001
        pending = [_make_pending(ticket)]
        self.stubs["modules.neural_brain"].get_pending_trades.return_value = pending

        with patch.object(self.pm.log, "warning") as mock_warn:
            result = self._run_cycle(ticket, [])

        self.assertIn(ticket, result)
        self.assertEqual(self._state.closure_retry_counts.get(ticket), 1)
        mock_warn.assert_called_once()

    def test_second_attempt_silent_still_unresolved(self):
        """On the second attempt (WARN_EVERY_N=3), no warning is logged."""
        ticket = 2002
        pending = [_make_pending(ticket)]
        self.stubs["modules.neural_brain"].get_pending_trades.return_value = pending
        # Pre-seed: first attempt already happened
        self._state.closure_retry_counts[ticket] = 1

        with patch("config.CLOSURE_WARN_EVERY_N", 3), \
             patch.object(self.pm.log, "warning") as mock_warn:
            result = self._run_cycle(ticket, [])

        self.assertIn(ticket, result)
        self.assertEqual(self._state.closure_retry_counts.get(ticket), 2)
        mock_warn.assert_not_called()

    def test_warning_emitted_on_every_nth_retry(self):
        """A warning is logged when attempt count is a multiple of WARN_EVERY_N."""
        ticket = 2003
        pending = [_make_pending(ticket)]
        self.stubs["modules.neural_brain"].get_pending_trades.return_value = pending
        # Pre-seed: 2 previous attempts
        self._state.closure_retry_counts[ticket] = 2

        with patch("config.CLOSURE_WARN_EVERY_N", 3), \
             patch.object(self.pm.log, "warning") as mock_warn:
            result = self._run_cycle(ticket, [])

        # Attempt 3 is a multiple of 3 → should warn
        self.assertIn(ticket, result)
        mock_warn.assert_called_once()

    def test_gives_up_after_max_retries(self):
        """After CLOSURE_MAX_RETRIES attempts, ticket is discarded (not re-added)."""
        ticket = 2004
        max_r = 5
        pending = [_make_pending(ticket)]
        self.stubs["modules.neural_brain"].get_pending_trades.return_value = pending
        # Pre-seed: one below max
        self._state.closure_retry_counts[ticket] = max_r - 1

        with patch("config.CLOSURE_MAX_RETRIES", max_r), \
             patch.object(self.pm.log, "error") as mock_err, \
             patch.object(self.pm.log, "warning"):
            result = self._run_cycle(ticket, [])

        # Ticket should NOT be in unresolved (given up)
        self.assertNotIn(ticket, result)
        # retry count should be cleaned up
        self.assertNotIn(ticket, self._state.closure_retry_counts)
        mock_err.assert_called_once()

    def test_no_warning_spam_across_many_cycles(self):
        """Across N cycles without resolution, warning count is bounded."""
        ticket = 2005
        pending = [_make_pending(ticket)]
        self.stubs["modules.neural_brain"].get_pending_trades.return_value = pending

        max_r     = 10
        warn_n    = 3
        total_cycles = max_r  # run until give-up

        warning_count = 0

        def run_one():
            import MetaTrader5 as _mt5
            _mt5.history_deals_get = MagicMock(return_value=[])
            return self.pm.watch_closures({ticket}, [])

        with patch("config.CLOSURE_MAX_RETRIES", max_r), \
             patch("config.CLOSURE_WARN_EVERY_N", warn_n):
            for _ in range(total_cycles):
                with patch.object(self.pm.log, "warning") as mw, \
                     patch.object(self.pm.log, "error"):
                    run_one()
                    warning_count += mw.call_count

        # With max_retries=10 and warn_every_n=3, warnings occur at attempts
        # 1, 3, 6, 9 → 4 warnings (attempt 10 triggers error, not warning)
        self.assertEqual(warning_count, 4,
                         "Expected warnings only at attempts 1, 3, 6, 9")


class TestWatchClosuresNoHistory(unittest.TestCase):
    """When MT5 returns None for history, all tickets are returned unresolved."""

    def setUp(self):
        self.pm, self.stubs = _import_fresh_pm()

        from modules.bot_state import state as _state
        _state.closure_retry_counts.clear()
        _state.pending_closure_tickets.clear()
        _state.tickets_en_memoria.clear()
        _state.trade_mode_cache.clear()
        self._state = _state

    def test_none_history_returns_all_closed(self):
        ticket = 3001
        pending = [_make_pending(ticket)]
        self.stubs["modules.neural_brain"].get_pending_trades.return_value = pending

        import MetaTrader5 as _mt5
        _mt5.history_deals_get = MagicMock(return_value=None)

        result = self.pm.watch_closures({ticket}, [])
        self.assertIn(ticket, result)
