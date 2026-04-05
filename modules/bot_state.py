"""
Centralized mutable state for the ZAR trading bot.

Encapsulates all global variables that were previously scattered across main.py.
Uses threading.RLock for thread-safe access from the web dashboard thread.
"""
import threading


class BotState:
    """Thread-safe container for all mutable bot state."""

    def __init__(self):
        self.lock = threading.RLock()

        # ── Trade tracking ──
        self.tickets_en_memoria: set = set()
        self.pending_closure_tickets: set = set()
        self.last_trade_time: dict = {}
        self.trade_mode_cache: dict = {}

        # ── Daily stats ──
        self.daily_start_balance: float = 0.0
        self.daily_pnl: float = 0.0
        self.trades_today: int = 0
        self.wins_today: int = 0
        self.losses_today: int = 0
        self.be_today: int = 0
        self.daily_trades_log: list = []
        self.last_summary_date: str = ""
        self.last_eod_date: str = ""

        # ── Cycle state ──
        self.cycle_count: int = 0
        self.last_action: str = ""

        # ── Per-cycle caches (cleared each cycle) ──
        self.ind_cache: dict = {}
        self.sr_cache: dict = {}
        self.symbol_status_cache: dict = {}
        self.symbol_detail_cache: dict = {}

        # ── News ──
        self.news_cache: dict = {}
        self.news_last_update: dict = {}
        self.shared_news_cache = None
        self.shared_news_last_update: float = 0.0

        # ── Notification cooldowns ──
        self.news_pause_notified: dict = {}
        self.memory_block_notified: dict = {}
        self.equity_guard_notified: dict = {}
        self.daily_loss_guard_notified: float = 0.0

        # ── Tilt Guard ──
        self.consecutive_losses: int = 0
        self.tilt_active_until: float = 0.0  # timestamp UTC when tilt expires
        self.tilt_notified: bool = False

        # ── Decision engine ──
        self.decision_call_cache: dict = {}
        self.last_decision_analysis: dict = {}
        self.decision_usage_stats: dict = {
            "api_calls_total": 0,
            "api_success_total": 0,
            "cache_hits_total": 0,
            "skipped_by_gate_total": 0,
            "skipped_by_budget_total": 0,
            "cooldown_skips_total": 0,
            "symbol_cooldown_skips_total": 0,
            "quota_hits_total": 0,
            "fallback_holds_total": 0,
            "errors_total": 0,
            "key_rotations_total": 0,
        }
        self.decision_hourly_usage: dict = {}
        self.decision_daily_usage: dict = {}

        # ── Symbol state for deterministic engine ──
        self._symbol_state: dict = {}
        self._h1_cache: dict = {}

        # ── Web dashboard ──
        self.web_status_snapshot: dict = {}

    def clear_cycle_caches(self):
        """Called at the start of each cycle to reset per-cycle data."""
        self.ind_cache.clear()
        self.sr_cache.clear()
        self.symbol_status_cache.clear()
        self.decision_call_cache.clear()

    def cleanup_ticket(self, ticket: int):
        """Remove all state associated with a closed trade ticket.

        Note: per-ticket trailing-stop state (trail_stage, profit_candle_count,
        profit_candle_last_seen) lives in modules/trailing_manager.py as module-level
        dicts.  Call ``trailing_manager.cleanup_ticket(ticket)`` as well when
        closing a trade — position_manager.watch_closures() does this automatically.
        """
        self.trade_mode_cache.pop(ticket, None)
        self.tickets_en_memoria.discard(ticket)
        self.pending_closure_tickets.discard(ticket)

    def reset_daily_stats(self):
        """Called at EOD to reset daily counters."""
        self.daily_pnl = 0.0
        self.trades_today = 0
        self.wins_today = 0
        self.losses_today = 0
        self.be_today = 0
        self.daily_trades_log.clear()
        self.consecutive_losses = 0
        self.tilt_active_until = 0.0
        self.tilt_notified = False

    def set_symbol_status(self, symbol: str, status: str):
        self.symbol_status_cache[symbol] = status

    def get_symbol_state(self, symbol: str) -> dict:
        if symbol not in self._symbol_state:
            self._symbol_state[symbol] = {}
        return self._symbol_state[symbol]


# Module-level singleton — imported by main.py and all sub-modules
state = BotState()
