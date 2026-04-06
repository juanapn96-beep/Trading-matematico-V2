"""
Pytest configuration for ZAR Trading Bot tests.

Sets minimal environment variables required by config.py to avoid
sys.exit(1) when MT5_LOGIN and other credentials are not present.
These are stub values — no real MT5 connection is made during tests.
"""
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

# ── Must be set BEFORE any module that imports config.py ────────
_test_env = {
    "MT5_LOGIN":    "12345",
    "MT5_PASSWORD": "test_password",
    "MT5_SERVER":   "test-server",
    "TELEGRAM_TOKEN":   "0:test",
    "TELEGRAM_CHAT_ID": "0",
}
for key, value in _test_env.items():
    os.environ.setdefault(key, value)

# ── Mock MetaTrader5 globally (not installed in CI) ──────────────
if "MetaTrader5" not in sys.modules:
    _mt5_mock = types.ModuleType("MetaTrader5")
    _mt5_mock.TIMEFRAME_M1  = 1
    _mt5_mock.TIMEFRAME_M5  = 5
    _mt5_mock.TIMEFRAME_M15 = 15
    _mt5_mock.TIMEFRAME_H1  = 16385
    _mt5_mock.TIMEFRAME_H4  = 16388
    _mt5_mock.TIMEFRAME_D1  = 16408
    _mt5_mock.ORDER_TYPE_BUY  = 0
    _mt5_mock.ORDER_TYPE_SELL = 1
    _mt5_mock.TRADE_ACTION_DEAL  = 1
    _mt5_mock.TRADE_ACTION_SLTP  = 6
    _mt5_mock.ORDER_TIME_GTC     = 1
    _mt5_mock.ORDER_FILLING_FOK    = 0
    _mt5_mock.ORDER_FILLING_IOC    = 1
    _mt5_mock.ORDER_FILLING_RETURN = 2
    _mt5_mock.TRADE_RETCODE_DONE = 10009
    for _attr in ("initialize", "login", "account_info", "symbol_info", "symbol_info_tick",
                  "positions_get", "order_send", "copy_rates_from_pos",
                  "symbol_select", "history_deals_get", "last_error"):
        setattr(_mt5_mock, _attr, MagicMock(return_value=None))
    sys.modules["MetaTrader5"] = _mt5_mock


@pytest.fixture(autouse=True)
def scalping_stage_defaults(monkeypatch):
    """
    Ensure scalping stage config values are present for tests.
    Uses monkeypatch to avoid permanent side-effects on the config module.
    """
    import config as cfg
    _defaults = {
        "SCALPING_BE_PIPS_STAGE_1": 5.0,
        "SCALPING_BE_PIPS_STAGE_2": 8.0,
        "SCALPING_BE_PIPS_STAGE_3": 12.0,
        "SCALPING_BE_PIPS_STAGE_4": 18.0,
        "SCALPING_BE_MIN_PIPS": 5.0,
    }
    for attr, value in _defaults.items():
        if not hasattr(cfg, attr):
            monkeypatch.setattr(cfg, attr, value)
