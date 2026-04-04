"""
╔══════════════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — main.py  (v6.5 — TRAILING STOP + WR FIX)     ║
║                                                                          ║
║   FIXES v6.5 (sobre v6.4):                                             ║
║   ✅ FIX 11 — Trailing stop progresivo (no BE inmediato)               ║
║       • Etapa 1 (0–1.5×ATR):   SL intacto — dejar respirar             ║
║       • Etapa 2 (1.5–2.5×ATR): SL→breakeven + buffer 0.1×ATR          ║
║       • Etapa 3 (2.5–4×ATR):   SL trail a 1.5×ATR del precio actual   ║
║       • Etapa 4 (>4×ATR):      SL trail a 1.0×ATR del precio actual   ║
║       • Etapa 5 (>80% hacia TP): SL trail a 0.5×ATR (máxima captura)  ║
║   ✅ FIX 12 — WR = wins/(wins+losses) — BE excluido del cálculo        ║
║   ✅ FIX 13 — BE no cuenta como trade en WR ni en EOD                  ║
║   ✅ FIX 14 — Filtro anti-contra-tendencia: bloquear cuando 4+         ║
║               indicadores primarios contradicen la dirección           ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import sys
import time
import json
import math
import threading
import re
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd
import requests

try:
    import MetaTrader5 as mt5
except ImportError:
    print("❌  pip install MetaTrader5"); sys.exit(1)

try:
    from groq import Groq
    _GROQ_AVAILABLE = True
except ImportError:
    _GROQ_AVAILABLE = False

import config as cfg
from modules.indicators        import compute_all
from modules.sr_zones          import build_sr_context, sr_for_prompt
from modules.decision_engine   import deterministic_decision
from modules.news_engine       import (
    build_news_context, build_shared_news_context, derive_symbol_news_context,
    format_news_for_prompt,
)
from modules.economic_calendar import calendar as eco_calendar
from modules.neural_brain import (
    init_db, save_trade, update_trade_result,
    build_features, check_memory,
    get_learning_report, get_pending_trades, get_memory_stats,
    derive_setup_id, derive_session_from_ind, evaluate_scorecard,
    evaluate_policy, get_adaptive_trail_params,
)
from modules.risk_manager import (
    get_lot_size, get_lot_size_kelly, calc_sl_tp, is_rr_valid,
    is_session_valid, is_daily_loss_ok, get_rr,
    is_market_tradeable, check_circuit_breaker,
)
from modules.telegram_notifier import (
    notify_bot_started, notify_trade_opened, notify_breakeven,
    notify_near_tp, notify_trade_closed, notify_news_pause,
    notify_memory_block, notify_daily_summary, notify_error,
    notify_eod_analysis,
    _send as telegram_send,
)
from modules import dashboard
from modules.web_dashboard import start_web_dashboard
from modules.portfolio_risk import get_effective_portfolio_risk
from modules.sentiment_data import get_sentiment_for_symbol

try:
    from modules.data_providers import get_twelve_data, get_polygon
    _DATA_PROVIDERS_AVAILABLE = True
except ImportError:
    _DATA_PROVIDERS_AVAILABLE = False

# ── Logging ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("zar_v6.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Groq ────────────────────────────────────────────────────────
def _build_groq_client(api_key: str):
    if not _GROQ_AVAILABLE:
        return None
    max_retries = int(getattr(cfg, "GROQ_CLIENT_MAX_RETRIES", 0) or 0)
    try:
        return Groq(api_key=api_key, max_retries=max_retries)
    except TypeError:
        return Groq(api_key=api_key)


_groq_keys = getattr(cfg, "GROQ_API_KEYS", []) or []
groq_clients = [
    _build_groq_client(api_key)
    for api_key in _groq_keys
    if api_key
]

# ── TF Map MT5 ───────────────────────────────────────────────────
TF_MAP = {
    "M1":  mt5.TIMEFRAME_M1,  "M5":  mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15, "H1":  mt5.TIMEFRAME_H1,
    "H4":  mt5.TIMEFRAME_H4,  "D1":  mt5.TIMEFRAME_D1,
}
TF_CANDLES = {
    "M1": 300, "M5": 250, "M15": 200,
    "H1": 150, "H4": 100, "D1":  60,
}

# Desviación máxima de precio (pips) permitida en el cierre de emergencia.
# Se usa un valor mayor que el cierre normal (20) para garantizar ejecución
# en condiciones de alta volatilidad (Cisne Negro).
_EMERGENCY_CLOSE_DEVIATION = 50

# ════════════════════════════════════════════════════════════════
#  ESTADO GLOBAL
# ════════════════════════════════════════════════════════════════

tickets_en_memoria:  set   = set()
news_cache:          dict  = {}
news_last_update:    dict  = {}
daily_start_balance: float = 0.0
daily_pnl:           float = 0.0
trades_today:        int   = 0
wins_today:          int   = 0
losses_today:        int   = 0
be_today:            int   = 0
last_summary_date:   str   = ""
last_eod_date:       str   = ""
cycle_count:         int   = 0
last_action:         str   = ""
ind_cache:           dict  = {}
sr_cache:            dict  = {}
symbol_status_cache: dict  = {}
symbol_detail_cache: dict  = {}  # FASE 12: {symbol: {enhanced_regime: str, regime_confidence: "HIGH"|"MEDIUM"|"LOW", z_score: float|None}}

news_pause_notified:   dict = {}
memory_block_notified: dict = {}  # {symbol: {"ts": float, "fingerprint": str}}
equity_guard_notified: dict = {}
daily_loss_guard_notified: float = 0.0
NOTIF_COOLDOWN_SEC = 1800

# Cooldown por símbolo
last_trade_time: dict = {}
SYMBOL_COOLDOWN_SEC = getattr(cfg, "SYMBOL_COOLDOWN_SEC", 300)

# Registro diario de trades para EOD
daily_trades_log: list = []
trade_mode_cache: dict = {}
_profit_candle_count: dict = {}
_profit_candle_last_seen: dict = {}
pending_closure_tickets: set = set()
last_groq_analysis: dict = {}
web_status_snapshot: dict = {}
state_lock = threading.RLock()
groq_cooldown_until: float = 0.0
groq_call_cache: dict = {}
groq_key_cooldowns: dict = {}
groq_symbol_cooldowns: dict = {}
groq_key_cursor: int = 0
groq_usage_stats: dict = {
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
groq_hourly_usage: dict = {}
groq_daily_usage: dict = {}
groq_hourly_usage_by_key: dict = {}
groq_daily_usage_by_key: dict = {}
shared_news_cache = None
shared_news_last_update: float = 0.0

# ── Per-symbol state for deterministic decision engine ───────────
_symbol_state: dict = {}  # {symbol: {mem_direction, last_indicators, last_sr_context, last_news_context, hurst_penalty}}

# ── H1 indicator cache (H1 candle changes at most once per hour) ─
_h1_cache: dict = {}      # {symbol: {"indicators": dict, "timestamp": float, "h1_candle_time": object}}
H1_CACHE_TTL_SEC = 300    # 5 minutes


def _set_symbol_status(symbol: str, status: str):
    global symbol_status_cache
    symbol_status_cache[symbol] = status


def _set_last_groq_analysis(symbol: str, analysis: Optional[dict]):
    global last_groq_analysis
    if not analysis:
        return
    with state_lock:
        last_groq_analysis = {
            "symbol": symbol,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "decision": analysis.get("decision", "HOLD"),
            "confidence": analysis.get("confidence", 0),
            "reason": analysis.get("reason", ""),
            "key_signals": analysis.get("key_signals", []),
            "main_risk": analysis.get("main_risk", ""),
        }


def _bump_groq_metric(metric: str, key_idx: Optional[int] = None):
    now = datetime.now(timezone.utc)
    hour_key = now.strftime("%Y-%m-%d %H")
    day_key = now.strftime("%Y-%m-%d")
    with state_lock:
        groq_usage_stats[metric] = int(groq_usage_stats.get(metric, 0)) + 1
        if metric == "api_calls_total":
            groq_hourly_usage[hour_key] = int(groq_hourly_usage.get(hour_key, 0)) + 1
            groq_daily_usage[day_key] = int(groq_daily_usage.get(day_key, 0)) + 1
            if key_idx is not None:
                hourly_by_key = groq_hourly_usage_by_key.setdefault(key_idx, {})
                daily_by_key = groq_daily_usage_by_key.setdefault(key_idx, {})
                hourly_by_key[hour_key] = int(hourly_by_key.get(hour_key, 0)) + 1
                daily_by_key[day_key] = int(daily_by_key.get(day_key, 0)) + 1

            while len(groq_hourly_usage) > 72:
                oldest = sorted(groq_hourly_usage.keys())[0]
                groq_hourly_usage.pop(oldest, None)
            while len(groq_daily_usage) > 14:
                oldest = sorted(groq_daily_usage.keys())[0]
                groq_daily_usage.pop(oldest, None)
            if key_idx is not None:
                while len(groq_hourly_usage_by_key.get(key_idx, {})) > 72:
                    oldest = sorted(groq_hourly_usage_by_key[key_idx].keys())[0]
                    groq_hourly_usage_by_key[key_idx].pop(oldest, None)
                while len(groq_daily_usage_by_key.get(key_idx, {})) > 14:
                    oldest = sorted(groq_daily_usage_by_key[key_idx].keys())[0]
                    groq_daily_usage_by_key[key_idx].pop(oldest, None)


def _get_groq_metrics_snapshot() -> dict:
    now = datetime.now(timezone.utc)
    hour_key = now.strftime("%Y-%m-%d %H")
    day_key = now.strftime("%Y-%m-%d")
    with state_lock:
        return {
            **groq_usage_stats,
            "api_calls_current_hour": int(groq_hourly_usage.get(hour_key, 0)),
            "api_calls_current_day": int(groq_daily_usage.get(day_key, 0)),
            "configured_keys": len(groq_clients),
            "per_key_budget": _format_groq_key_budget_status(),
            "cooldown_until": datetime.fromtimestamp(groq_cooldown_until, tz=timezone.utc).isoformat() if groq_cooldown_until > time.time() else "",
        }


def _get_groq_metrics_snapshot_unlocked() -> dict:
    now = datetime.now(timezone.utc)
    hour_key = now.strftime("%Y-%m-%d %H")
    day_key = now.strftime("%Y-%m-%d")
    return {
        **groq_usage_stats,
        "api_calls_current_hour": int(groq_hourly_usage.get(hour_key, 0)),
        "api_calls_current_day": int(groq_daily_usage.get(day_key, 0)),
        "configured_keys": len(groq_clients),
        "per_key_budget": _format_groq_key_budget_status(),
        "cooldown_until": datetime.fromtimestamp(groq_cooldown_until, tz=timezone.utc).isoformat() if groq_cooldown_until > time.time() else "",
    }


def _get_groq_key_usage_snapshot(key_idx: int) -> tuple:
    now = datetime.now(timezone.utc)
    hour_key = now.strftime("%Y-%m-%d %H")
    day_key = now.strftime("%Y-%m-%d")
    with state_lock:
        hour_calls = int(groq_hourly_usage_by_key.get(key_idx, {}).get(hour_key, 0))
        day_calls = int(groq_daily_usage_by_key.get(key_idx, {}).get(day_key, 0))
    return hour_calls, day_calls


def _is_groq_key_budget_available(key_idx: int) -> tuple:
    max_calls_hour = int(getattr(cfg, "GROQ_MAX_CALLS_PER_HOUR", 0) or 0)
    max_calls_day = int(getattr(cfg, "GROQ_MAX_CALLS_PER_DAY", 0) or 0)
    hour_calls, day_calls = _get_groq_key_usage_snapshot(key_idx)
    hour_ok = max_calls_hour <= 0 or hour_calls < max_calls_hour
    day_ok = max_calls_day <= 0 or day_calls < max_calls_day
    return (hour_ok and day_ok), hour_calls, day_calls


def _format_groq_key_budget_status() -> str:
    max_calls_hour = int(getattr(cfg, "GROQ_MAX_CALLS_PER_HOUR", 0) or 0)
    max_calls_day = int(getattr(cfg, "GROQ_MAX_CALLS_PER_DAY", 0) or 0)
    parts = []
    for key_idx in range(len(groq_clients)):
        hour_calls, day_calls = _get_groq_key_usage_snapshot(key_idx)
        hour_limit = max_calls_hour if max_calls_hour > 0 else "-"
        day_limit = max_calls_day if max_calls_day > 0 else "-"
        parts.append(f"k{key_idx+1}:h {hour_calls}/{hour_limit}, d {day_calls}/{day_limit}")
    return " | ".join(parts) if parts else "sin keys"


def _build_groq_fallback_decision(reason: str) -> dict:
    _bump_groq_metric("fallback_holds_total")
    fallback = {
        "decision": "HOLD",
        "confidence": 0,
        "reason": reason,
        "key_signals": ["fallback_system", "groq_unavailable"],
        "main_risk": "Groq no disponible para validar noticias y sentimiento",
    }
    _set_last_groq_analysis("SYSTEM", fallback)
    return fallback


def _serialize_position_for_web(pos: dict) -> dict:
    direction = "LONG" if pos.get("type") == 0 else "SHORT"
    return {
        "ticket": int(pos.get("ticket", 0)),
        "symbol": pos.get("symbol", ""),
        "direction": direction,
        "volume": float(pos.get("volume", 0.0) or 0.0),
        "profit": float(pos.get("profit", 0.0) or 0.0),
        "price_open": float(pos.get("price_open", 0.0) or 0.0),
        "price_current": float(pos.get("price_current", 0.0) or 0.0),
        "sl": float(pos.get("sl", 0.0) or 0.0),
        "tp": float(pos.get("tp", 0.0) or 0.0),
    }


def _update_web_status_snapshot(balance: float, equity: float, open_positions: list):
    global web_status_snapshot
    news_ctx = next(iter(news_cache.values()), None)
    news_payload = None
    if news_ctx is not None:
        news_payload = {
            "avg_sentiment": float(getattr(news_ctx, "avg_sentiment", 0.0) or 0.0),
            "high_impact_count": int(getattr(news_ctx, "high_impact_count", 0) or 0),
            "should_pause": bool(getattr(news_ctx, "should_pause", False)),
        }

    # Performance tracking — métricas globales de memoria neural
    mem_stats = get_memory_stats()
    perf_payload = {}
    if mem_stats:
        total = int(mem_stats.get("total", 0))
        wins = int(mem_stats.get("wins", 0))
        losses = int(mem_stats.get("losses", 0))
        be_count = total - wins - losses
        wr = float(mem_stats.get("win_rate", 0.0))
        avg_win = float(mem_stats.get("avg_win", 0.0))
        avg_loss = float(mem_stats.get("avg_loss", 0.0))
        # Profit factor = ganancias_totales / pérdidas_totales
        gross_wins  = avg_win  * wins   if wins   > 0 else 0.0
        gross_losses = abs(avg_loss) * losses if losses > 0 else 0.0
        pf = gross_wins / gross_losses if gross_losses > 0 else 0.0
        best = float(mem_stats.get("best_trade", 0.0))
        worst = float(mem_stats.get("worst_trade", 0.0))
        perf_payload = {
            "win_rate": wr,
            "profit_factor": round(pf, 2),
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "breakeven": be_count,
            "rolling_wr_20": round(wr, 1),
            "max_drawdown_pct": round(float(mem_stats.get("max_drawdown_pct", 0.0)), 2),
            "best_trade": round(best, 2),
            "worst_trade": round(worst, 2),
        }

    # Portfolio risk snapshot
    portfolio_payload = {}
    bot_positions = [p for p in open_positions if getattr(p, "magic", 0) == getattr(cfg, "MAGIC_NUMBER", 0)]
    if bot_positions:
        positions_detail = []
        for p in bot_positions:
            positions_detail.append({
                "symbol": getattr(p, "symbol", "?"),
                "direction": "LONG" if getattr(p, "type", 0) == 0 else "SHORT",
            })
        # Calcular riesgo efectivo reutilizando el módulo portfolio_risk
        from modules.portfolio_risk import _get_correlation
        corr_count = 0
        for i, p1 in enumerate(bot_positions):
            for p2 in bot_positions[i + 1:]:
                rho = _get_correlation(getattr(p1, "symbol", ""), getattr(p2, "symbol", ""))
                if abs(rho) >= 0.60:
                    corr_count += 1
        # Riesgo efectivo del portafolio existente (sin trade propuesto adicional)
        risk_per = getattr(cfg, "RISK_PER_TRADE", 0.01)
        n = len(bot_positions)
        risk_sq = risk_per ** 2
        variance = n * risk_sq
        for i, p1 in enumerate(bot_positions):
            for j in range(i + 1, n):
                p2 = bot_positions[j]
                rho = _get_correlation(getattr(p1, "symbol", ""), getattr(p2, "symbol", ""))
                same_dir = (getattr(p1, "type", 0) == getattr(p2, "type", 0))
                sign = 1.0 if same_dir else -1.0
                variance += 2 * rho * sign * risk_sq
        eff_risk = math.sqrt(max(0.0, variance)) * 100
        portfolio_payload = {
            "open_positions": n,
            "effective_risk_pct": round(eff_risk, 1),
            "max_risk_pct": getattr(cfg, "MAX_PORTFOLIO_RISK_PCT", 5.0),
            "correlated_pairs": corr_count,
            "positions_detail": positions_detail,
        }

    with state_lock:
        web_status_snapshot = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "cycle": cycle_count,
            "balance": float(balance or 0.0),
            "equity": float(equity or 0.0),
            "daily_pnl": float(daily_pnl or 0.0),
            "last_action": last_action,
            "active_trades": [_serialize_position_for_web(pos) for pos in open_positions],
            "symbol_status": dict(symbol_status_cache),
            "symbol_details": dict(symbol_detail_cache),
            "memory": mem_stats,
            "news": news_payload,
            "shared_news_fetched_at": getattr(shared_news_cache, "fetched_at", "") if shared_news_cache is not None else "",
            "groq_metrics": _get_groq_metrics_snapshot_unlocked(),
            "last_groq_analysis": dict(last_groq_analysis),
            "performance": perf_payload,
            "portfolio_risk": portfolio_payload,
        }


def _get_web_status_snapshot() -> dict:
    with state_lock:
        return dict(web_status_snapshot)


def _get_last_candle_stamp(df: pd.DataFrame) -> str:
    if df is None or len(df) == 0:
        return ""
    ts = pd.Timestamp(df["time"].iloc[-1]).to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


def _extract_retry_delay_seconds(err_str: str) -> float:
    match = re.search(r"retry in\s+([0-9.]+)s", err_str, re.IGNORECASE)
    if not match:
        return 60.0
    try:
        return max(1.0, float(match.group(1)))
    except Exception:
        return 60.0


def _build_groq_cache_key(symbol: str, direction_hint: str, candle_stamp: str, ind: dict) -> str:
    conf_total = round(float(ind.get("confluence", {}).get("total", 0.0) or 0.0), 2)
    h1_trend = ind.get("h1_trend", "LATERAL")
    hilbert_signal = ind.get("hilbert", {}).get("signal", "NEUTRAL")
    return "|".join([
        symbol,
        direction_hint,
        candle_stamp,
        h1_trend,
        hilbert_signal,
        f"{conf_total:.2f}",
    ])


def _get_groq_symbol_cooldown_remaining(symbol: str, now_ts: Optional[float] = None) -> int:
    now_ts = time.time() if now_ts is None else now_ts
    until_ts = float(groq_symbol_cooldowns.get(symbol, 0.0) or 0.0)
    if until_ts <= now_ts:
        groq_symbol_cooldowns.pop(symbol, None)
        return 0
    return int(max(1, math.ceil(until_ts - now_ts)))


def _set_groq_symbol_cooldown(symbol: str, seconds: Optional[float] = None):
    cooldown_sec = int(
        seconds
        if seconds is not None
        else (getattr(cfg, "GROQ_SYMBOL_COOLDOWN_SEC", 0) or 0)
    )
    if cooldown_sec <= 0:
        return
    groq_symbol_cooldowns[symbol] = time.time() + cooldown_sec


def _select_groq_client(now_ts: float, excluded: Optional[set] = None):
    global groq_key_cursor
    excluded = excluded or set()
    if not groq_clients:
        return None, None

    total = len(groq_clients)
    for offset in range(total):
        idx = (groq_key_cursor + offset) % total
        if idx in excluded:
            continue
        budget_ok, _, _ = _is_groq_key_budget_available(idx)
        if not budget_ok:
            continue
        cooldown_until = float(groq_key_cooldowns.get(idx, 0.0) or 0.0)
        if cooldown_until > now_ts:
            continue
        groq_key_cursor = (idx + 1) % total
        return idx, groq_clients[idx]
    return None, None


def _compute_trade_plan(symbol: str, action: str, ind: dict, sym_cfg: dict) -> Optional[dict]:
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    price = tick.ask if action == "BUY" else tick.bid

    # FASE-A FIX: en modo scalping usar el mismo cálculo ATR-entry que _execute_decision
    # para que el gate check y la ejecución sean coherentes.
    if bool(getattr(cfg, "SCALPING_ONLY", False)):
        atr_entry = float(ind.get("atr_entry", ind.get("atr", 0)) or 0)
        if atr_entry > 0:
            scalp_sl_mult = float(getattr(cfg, "SCALPING_SL_ATR_MULT", 3.0))
            scalp_tp_mult = float(getattr(cfg, "SCALPING_TP_ATR_MULT", 6.0))
            if action == "BUY":
                sl = round(price - scalp_sl_mult * atr_entry, 5)
                tp = round(price + scalp_tp_mult * atr_entry, 5)
            else:
                sl = round(price + scalp_sl_mult * atr_entry, 5)
                tp = round(price - scalp_tp_mult * atr_entry, 5)
            rr = get_rr(price, sl, tp)
            return {"action": action, "price": price, "sl": sl, "tp": tp, "rr": rr}

    sl, tp = calc_sl_tp(action, price, ind["atr"], sym_cfg)
    rr = get_rr(price, sl, tp)
    return {
        "action": action,
        "price": price,
        "sl": sl,
        "tp": tp,
        "rr": rr,
    }


def _get_entry_min_rr(sym_cfg: dict) -> float:
    return float(sym_cfg.get("min_rr", getattr(cfg, "ENTRY_MIN_RR", 1.20)) or getattr(cfg, "ENTRY_MIN_RR", 1.20))


def _should_allow_low_hurst_scalp(ind: dict, sr_ctx, sym_cfg: dict) -> tuple:
    hurst_val = float(ind.get("hurst", 0.5) or 0.5)
    min_hurst = float(sym_cfg.get("min_hurst", 0.40) or 0.40)
    if hurst_val >= min_hurst:
        return True, ""

    if not bool(getattr(cfg, "SCALPING_ALLOW_LOW_HURST", True)):
        return False, f"Hurst {hurst_val:.3f} < {min_hurst:.3f}"

    hard_floor = float(getattr(cfg, "SCALPING_HURST_HARD_FLOOR", 0.18) or 0.18)
    soft_margin = float(getattr(cfg, "SCALPING_HURST_SOFT_MARGIN", 0.20) or 0.20)
    soft_floor = max(hard_floor, min_hurst - soft_margin)
    if hurst_val < soft_floor:
        return False, f"Hurst {hurst_val:.3f} < piso scalp {soft_floor:.3f}"

    enhanced_regime = str(ind.get("enhanced_regime", "UNKNOWN") or "UNKNOWN")
    zscore_signal = str(ind.get("zscore_returns", {}).get("signal", "NEUTRAL") or "NEUTRAL")
    in_strong_zone = bool(getattr(sr_ctx, "in_strong_zone", False))

    reasons = [f"zona gris {hurst_val:.2f}/{min_hurst:.2f}"]
    if enhanced_regime != "UNKNOWN":
        reasons.append(enhanced_regime)
    if in_strong_zone:
        reasons.append("SR fuerte")
    if zscore_signal != "NEUTRAL":
        reasons.append(f"z={zscore_signal}")
    return True, " | ".join(reasons)


def _evaluate_strategy_specific_gate(action: str, ind: dict, sr_ctx, sym_cfg: dict) -> tuple:
    strategy_type = str(sym_cfg.get("strategy_type", "HYBRID") or "HYBRID")
    rsi = float(ind.get("rsi", 50.0) or 50.0)
    fisher = float(ind.get("fisher", 0.0) or 0.0)
    macd_dir = ind.get("macd_dir", "NEUTRAL")
    ha_trend = ind.get("ha_trend", "NEUTRAL")
    kalman_trend = ind.get("kalman_trend", "NEUTRAL")
    supertrend = int(ind.get("supertrend", 0) or 0)
    hilbert_signal = ind.get("hilbert", {}).get("signal", "NEUTRAL")
    conf_total = float(ind.get("confluence", {}).get("total", 0.0) or 0.0)
    in_strong_zone = bool(getattr(sr_ctx, "in_strong_zone", False))

    bullish_primaries = sum([
        macd_dir == "ALCISTA",
        ha_trend == "ALCISTA",
        kalman_trend == "ALCISTA",
        supertrend == 1,
    ])
    bearish_primaries = sum([
        macd_dir == "BAJISTA",
        ha_trend == "BAJISTA",
        kalman_trend == "BAJISTA",
        supertrend == -1,
    ])

    if strategy_type in {"VOLATILITY_CYCLE", "CYCLE_REVERSION", "GOLD_BETA_REVERSION", "CRYPTO_WAVE"}:
        # FASE-B sniper: estas estrategias se basan en reversiones de ciclo.
        # El ideal de entrada es Hilbert en extremo (LOCAL_MIN para BUY, LOCAL_MAX para SELL)
        # o Fisher extremo con S/R fuerte. Sin confirmación de ciclo ni zona, la entrada
        # no es sniper — se requiere al menos una condición de confirmación de ciclo.
        if action == "BUY":
            if hilbert_signal == "LOCAL_MAX":
                return False, f"{strategy_type}: Hilbert en techo"
            if rsi >= float(sym_cfg.get("rsi_overbought", 70)) and not in_strong_zone:
                return False, f"{strategy_type}: RSI demasiado alto sin S/R fuerte"
            if fisher > 2.5 and not in_strong_zone:
                return False, f"{strategy_type}: Fisher extremo contrario"
            # FASE-B: gate sniper de ciclo — al menos una condición de ciclo/zona activa
            cycle_buy_ok = (
                hilbert_signal == "LOCAL_MIN"
                or (fisher < -2.0)
                or in_strong_zone
            )
            if not cycle_buy_ok:
                return False, f"{strategy_type}: sin confirmación de ciclo para BUY sniper (Hilbert={hilbert_signal}, Fisher={fisher:.1f})"
        else:
            if hilbert_signal == "LOCAL_MIN":
                return False, f"{strategy_type}: Hilbert en suelo"
            if rsi <= float(sym_cfg.get("rsi_oversold", 30)) and not in_strong_zone:
                return False, f"{strategy_type}: RSI demasiado bajo sin S/R fuerte"
            if fisher < -2.5 and not in_strong_zone:
                return False, f"{strategy_type}: Fisher extremo contrario"
            # FASE-B: gate sniper de ciclo — al menos una condición de ciclo/zona activa
            cycle_sell_ok = (
                hilbert_signal == "LOCAL_MAX"
                or (fisher > 2.0)
                or in_strong_zone
            )
            if not cycle_sell_ok:
                return False, f"{strategy_type}: sin confirmación de ciclo para SELL sniper (Hilbert={hilbert_signal}, Fisher={fisher:.1f})"
        return True, ""

    if strategy_type in {"MOMENTUM_TREND", "MOMENTUM_SURGE", "TECH_MOMENTUM", "FRANKFURT_BREAKOUT", "RANGE_BREAKOUT_OIL"}:
        primaries = bullish_primaries if action == "BUY" else bearish_primaries
        if primaries < 3:
            return False, f"{strategy_type}: primarios alineados insuficientes ({primaries}/4)"
        if abs(conf_total) < float(getattr(cfg, "CONFLUENCE_MIN_SCORE", 0.5)) * 1.2:
            return False, f"{strategy_type}: confluencia insuficiente ({conf_total:+.2f})"
        return True, ""

    if strategy_type in {"TREND_KALMAN", "RISK_CARRY"}:
        if action == "BUY" and not (kalman_trend == "ALCISTA" and supertrend == 1):
            return False, f"{strategy_type}: Kalman/SuperTrend no confirman BUY"
        if action == "SELL" and not (kalman_trend == "BAJISTA" and supertrend == -1):
            return False, f"{strategy_type}: Kalman/SuperTrend no confirman SELL"
        return True, ""

    if strategy_type == "DRAGON_EXPLOSION":
        primaries = bullish_primaries if action == "BUY" else bearish_primaries
        if primaries < 4:
            return False, f"{strategy_type}: requiere 4/4 primarios alineados"
        if abs(conf_total) < 0.8:
            return False, f"{strategy_type}: confluencia no sniper ({conf_total:+.2f})"
        return True, ""

    return True, ""


def _is_groq_candidate_ready(symbol: str, action: str, ind: dict, sr_ctx, sym_cfg: dict) -> tuple:
    plan = _compute_trade_plan(symbol, action, ind, sym_cfg)
    if plan is None:
        return False, "⚠️ Tick no disponible", None

    # FASE-A: _compute_trade_plan ya usa el cálculo ATR-entry correcto en modo
    # SCALPING_ONLY, por lo que no se necesita aplicar SCALPING_TP_MULT aquí.
    # El gate y la ejecución son ahora coherentes (mismo SL/TP).

    min_rr = _get_entry_min_rr(sym_cfg)
    if not is_rr_valid(plan["price"], plan["sl"], plan["tp"], min_rr=min_rr):
        return False, f"⚖️ R:R inválido ({plan['rr']:.2f})", plan

    passes_filter, filter_reason = _passes_direction_filter(action, ind)
    if not passes_filter:
        return False, f"🚫 {filter_reason}", plan

    conf = ind.get("confluence", {})
    conf_total = float(conf.get("total", 0.0) or 0.0)
    conf_min = float(getattr(cfg, "CONFLUENCE_MIN_SCORE", 0.3))
    sniper_aligned = bool(conf.get("sniper_aligned", False))
    in_strong_zone = bool(getattr(sr_ctx, "in_strong_zone", False))
    trend = ind.get("h1_trend", "LATERAL")
    votes = ind.get("trend_votes", {"bull": 0, "bear": 0})
    vote_edge = (votes.get("bull", 0) - votes.get("bear", 0)) if action == "BUY" else (votes.get("bear", 0) - votes.get("bull", 0))
    hilbert_signal = ind.get("hilbert", {}).get("signal", "NEUTRAL")

    if action == "BUY":
        conf_ok = conf_total >= conf_min
        cycle_ok = hilbert_signal != "LOCAL_MAX"
        trend_ok = ("ALCISTA" in trend) or vote_edge >= 2
    else:
        conf_ok = conf_total <= -conf_min
        cycle_ok = hilbert_signal != "LOCAL_MIN"
        trend_ok = ("BAJISTA" in trend) or vote_edge >= 2

    # ── HARD GATE: Sin confluencia suficiente NO se consulta Groq ──
    if not conf_ok:
        return False, (
            f"🔒 Confluencia insuficiente {action}: "
            f"conf={conf_total:+.2f} (min={'+'if action=='BUY' else '-'}{conf_min:.2f})"
        ), plan

    support_ok = sniper_aligned or in_strong_zone or abs(conf_total) >= (conf_min * 1.8)
    quality_score = sum([cycle_ok, trend_ok, support_ok])
    min_quality = max(2, int(getattr(cfg, "GROQ_MIN_ENTRY_QUALITY", 3) or 3))

    if quality_score < min_quality:
        return False, (
            f"🧪 Setup débil {action}: q={quality_score}/{min_quality} "
            f"conf={conf_total:+.2f} votes={vote_edge:+d}"
        ), plan

    strategy_ok, strategy_reason = _evaluate_strategy_specific_gate(action, ind, sr_ctx, sym_cfg)
    if not strategy_ok:
        return False, f"🧭 {strategy_reason}", plan

    strong_entry_required = bool(getattr(cfg, "GROQ_ENTRY_STRONG_ONLY", True))
    premium_conf_mult = float(getattr(cfg, "GROQ_ENTRY_CONF_MULT", 1.8) or 1.8)
    premium_conf_ok = abs(conf_total) >= (conf_min * premium_conf_mult)
    hilbert_extreme_ok = (
        (action == "BUY" and hilbert_signal == "LOCAL_MIN")
        or (action == "SELL" and hilbert_signal == "LOCAL_MAX")
    )
    if strong_entry_required and not (
        sniper_aligned or in_strong_zone or premium_conf_ok or hilbert_extreme_ok
    ):
        return False, (
            f"⏱️ Entrada aún no es inminente {action}: "
            f"sin zona fuerte ni confluencia premium ({conf_total:+.2f})"
        ), plan

    return True, "", plan

# ════════════════════════════════════════════════════════════════
#  MT5 — CONEXIÓN Y DATOS
# ════════════════════════════════════════════════════════════════

# Número de reintentos y espera (segundos) cuando copy_rates_from_pos devuelve None.
# MT5 descarga el historial de forma asíncrona tras symbol_select, por lo que el
# primer intento puede fallar aunque el símbolo exista.
_GET_CANDLES_RETRIES = 3
_GET_CANDLES_RETRY_WAIT = 0.5

# Espera (segundos) después de pre-seleccionar símbolos en conectar_mt5().
# Da tiempo a MT5 para iniciar la descarga asíncrona de historial.
_SYMBOL_WARMUP_WAIT_SEC = 2


def conectar_mt5() -> bool:
    if not mt5.initialize():
        log.error("[MT5] initialize() falló"); return False
    ok = mt5.login(cfg.MT5_LOGIN, password=cfg.MT5_PASSWORD, server=cfg.MT5_SERVER)
    if not ok:
        log.error(f"[MT5] login falló: {mt5.last_error()}"); return False
    info = mt5.account_info()
    log.info(f"[MT5] Conectado — Balance: ${info.balance:,.2f} | Server: {cfg.MT5_SERVER}")
    # Pre-seleccionar todos los símbolos configurados para disparar la descarga
    # de historial en MT5 (asíncrona). Esto evita "Datos insuficientes" en el
    # primer ciclo cuando el símbolo no está en el Market Watch.
    log.info("[MT5] Pre-seleccionando símbolos para cargar historial...")
    for sym in cfg.SYMBOLS:
        if not mt5.symbol_select(sym, True):
            log.warning(f"[MT5] symbol_select falló al iniciar para {sym} — verifica el nombre en el broker")
    # Pequeña espera para que MT5 inicie la descarga de historial de todos los símbolos
    time.sleep(_SYMBOL_WARMUP_WAIT_SEC)
    log.info("[MT5] Pre-selección de símbolos completada.")
    return True


def get_candles(symbol: str, tf: str, n: int = 200) -> Optional[pd.DataFrame]:
    if not mt5.symbol_select(symbol, True):
        # symbol_select puede fallar por razones transitorias (mercado cerrado para ese símbolo,
        # etc.); copy_rates_from_pos puede devolver datos igualmente si el historial está en
        # caché local, por eso se continúa y se deja que el retry decida.
        log.debug(f"[MT5] symbol_select falló para {symbol}")
    rates = None
    for attempt in range(_GET_CANDLES_RETRIES):
        rates = mt5.copy_rates_from_pos(symbol, TF_MAP[tf], 0, n)
        if rates is not None and len(rates) > 0:
            break
        if attempt < _GET_CANDLES_RETRIES - 1:
            time.sleep(_GET_CANDLES_RETRY_WAIT)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df.rename(columns={"tick_volume": "volume"})


def get_account_info():
    info = mt5.account_info()
    if info is None:
        return None, None
    return float(info.balance), float(info.equity)


def get_open_positions():
    positions = mt5.positions_get()
    if positions is None:
        return []
    return [p._asdict() for p in positions if p.magic == cfg.MAGIC_NUMBER]


def get_open_positions_count_realtime() -> int:
    positions = mt5.positions_get()
    if positions is None:
        return 0
    return sum(1 for p in positions if p.magic == cfg.MAGIC_NUMBER)


# ════════════════════════════════════════════════════════════════
#  GROQ — CON RETRY EXPONENCIAL
# ════════════════════════════════════════════════════════════════

SYSTEM_PROMPT_BASE = """
Eres ZAR — un algoritmo de trading institucional de precisión matemática.
Tu única función es analizar el contexto de mercado y decidir: BUY, SELL o HOLD.
Responde SIEMPRE con JSON exacto, nada más.

REGLA 1 — TENDENCIA (7 niveles — lee con cuidado):
  ALCISTA_FUERTE   → BUY fuertemente favorecido. Solo SELL si hay señal extrema contraria.
  ALCISTA          → BUY es la dirección correcta. Confirmar con S/R o momentum.
  LATERAL_ALCISTA  → Señales mayoritariamente alcistas. BUY desde soporte fuerte o con 3+ confirmaciones.
  LATERAL          → Sin dirección clara. BUY/SELL SOLO si estás EN soporte/resistencia fuerte (strength>5).
                     Si no hay S/R fuerte cercano → HOLD.
  LATERAL_BAJISTA  → Señales mayoritariamente bajistas. SELL desde resistencia fuerte o con 3+ confirmaciones.
  BAJISTA          → SELL es la dirección correcta. Confirmar con S/R o momentum.
  BAJISTA_FUERTE   → SELL fuertemente favorecido. Solo BUY si hay señal extrema contraria.

  trend_votes muestra cuántos de 6 indicadores votan alcista (bull) vs bajista (bear).

REGLA 2 — ZONAS S/R FUERTES:
  En soporte (strength>5): BUY si hay confirmación de momentum
  En resistencia (strength>5): SELL si hay confirmación de momentum
  in_strong_zone=True: zona de alta probabilidad — aumenta confianza

REGLA 3 — CONFLUENCIA MÍNIMA (al menos 4 de 6 — AUMENTADO a 4):
  ✓ DEMA cross o posición (fast>slow = alcista)
  ✓ MACD histograma en dirección correcta
  ✓ RSI no en zona opuesta extrema
  ✓ Heiken Ashi en dirección correcta
  ✓ SuperTrend en dirección correcta
  ✓ Kalman trend en dirección correcta

  IMPORTANTE: Si SuperTrend y Kalman CONTRADICEN la dirección → HOLD obligatorio.
  Ambos deben confirmar la dirección del trade.

REGLA 4 — RSI EXTREMOS:
  RSI > overbought → NO BUY. RSI < oversold → NO SELL.
  Excepción: RSI extremo en S/R fuerte puede confirmar divergencia.

REGLA 5 — NOTICIAS Y CALENDARIO:
  Si should_pause=True → HOLD siempre. Sin excepción.

REGLA 6 — MEMORIA NEURAL:
  Si should_block=True → HOLD.
  confidence_adj > 0 → patrón ganador similar.
  confidence_adj < 0 → patrón perdedor similar.

REGLA 7 — R:R MÍNIMO:
  R:R < 1.5 → HOLD. R:R 1.5-2.0 = aceptable. R:R > 2.5 = excelente.

REGLA 8 — HILBERT TRANSFORM:
  LOCAL_MAX (sine > 0.85) → NO BUY. Techo del ciclo.
  LOCAL_MIN (sine < -0.85) → NO SELL. Suelo del ciclo.
  BUY_CYCLE → Favorece BUY.
  SELL_CYCLE → Favorece SELL.

REGLA 9 — FISHER TRANSFORM:
  Fisher > +2.0 → sobrecomprado → precaución BUY.
  Fisher < -2.0 → sobrevendido → precaución SELL.

REGLA 10 — HURST EXPONENT:
  Hurst > 0.6 → tendencia persistente → seguir agresivamente.
  Hurst 0.45-0.6 → mixto → requerir S/R adicional.
  Hurst < 0.45 → reversión → usar S/R como criterio principal.

REGLA 11 — CICLO ADAPTATIVO:
  cycle_phase = TECHO → favorece SELL.
  cycle_phase = SUELO → favorece BUY.
  cycle_phase = TRANSICION → esperar confirmación.

REGLA 12 — REGRESIÓN LINEAL:
  lr_r2 > 0.7 → tendencia estadísticamente robusta.
  lr_r2 < 0.4 → usar S/R, no la tendencia lineal.

REGLA 13 — ANTI-CONTRA-TENDENCIA (NUEVO — CRÍTICO):
  Si la dirección propuesta CONTRADICE a SuperTrend + Kalman simultáneamente → HOLD.
  Si la dirección propuesta CONTRADICE a 4 o más indicadores primarios → HOLD.
  El bot tuvo el 97% de trades en breakeven por entrar contra la tendencia.
  Solo operar cuando la dirección está ALINEADA con la mayoría de indicadores.

REGLA 14 — PILAR 3: MICROESTRUCTURA (NUEVO — FASE 1):
  Volume Profile (POC/VAH/VAL basado en tick-volume de Exness):
    Precio > POC → sesgo alcista. Precio < POC → sesgo bajista.
    Precio > VAH → breakout bullish (fuerte). Precio < VAL → breakdown bearish (fuerte).
    Precio dentro del Value Area (VAL–VAH) → zona de equilibrio, menor edge.
  Session VWAP (VWAP anclado por sesión UTC):
    Precio sobre S-VWAP → sesgo alcista. Precio bajo S-VWAP → sesgo bajista.
    Desviación S-VWAP > 0.5 % → posible reversión a VWAP antes del TP.
  Fair Value Gaps (FVG / Imbalances):
    FVG Bullish activo (fvg_bull): precio entrando en el gap desde arriba → SOPORTE. Favorece BUY.
    FVG Bearish activo (fvg_bear): precio llegando al gap desde abajo → RESISTENCIA. Favorece SELL.
    FVG mitigado: ya NO es soporte/resistencia válido.

REGLA 15 — CONFLUENCIA DE 3 PILARES (SNIPER — NUEVO — FASE 1):
  El bot evalúa 3 pilares independientes para cada señal:
    P1 (Estadístico): votos de tendencia de indicadores técnicos.
    P2 (Matemático):  Hilbert + Hurst + Kalman + Fisher + Ciclo Adaptativo.
    P3 (Microestructura): POC + Session VWAP + FVG (calculado arriba).
  confluence.total en [-3, +3]:
    >= +1.0 → BULLISH fuerte. <= -1.0 → BEARISH fuerte. Entre → neutral/débil.
  confluence.sniper_aligned = True → los 3 pilares apuntan en la MISMA dirección.
    → Alta probabilidad de éxito. Aumentar confianza en 1 punto.
  confluence.sniper_aligned = False → pilares en desacuerdo.
    → Si conf.total < 0.5, reducir confianza. Si conf.total < 0.0 en la dirección → HOLD.
  NUNCA ejecutar BUY si confluence.total < -0.5 (todos los pilares en contra).
  NUNCA ejecutar SELL si confluence.total > 0.5 (todos los pilares en contra).

FORMATO (JSON exacto, sin markdown, sin texto extra):
{
  "decision": "BUY" | "SELL" | "HOLD",
  "confidence": 1-10,
  "reason": "explicación concisa en español (máx 150 palabras)",
  "key_signals": ["señal1", "señal2", "señal3"],
  "main_risk": "principal riesgo de este trade"
}
"""


def get_system_prompt(sym_cfg: dict) -> str:
    extra         = sym_cfg.get("strategy_extra_rules", "")
    strategy_type = sym_cfg.get("strategy_type", "HYBRID")
    if not extra:
        return SYSTEM_PROMPT_BASE
    return SYSTEM_PROMPT_BASE + (
        f"\n\n{'='*60}\n"
        f"ESTRATEGIA ESPECÍFICA — {strategy_type}:\n"
        f"{'='*60}\n"
        f"{extra}\n"
        f"{'='*60}\n"
        f"Aplica estas reglas ADICIONALES junto con las reglas base.\n"
    )


def ask_groq(symbol: str, context: str, sym_cfg: dict, cache_key: str = "") -> Optional[dict]:
    """
    Decision gateway: uses deterministic engine by default (no Groq key needed).
    If GROQ_API_KEY is configured and groq is available, delegates to the Groq LLM.
    Maintains backward compatibility with callers expecting {"action", "confidence", ...}.
    """
    # ── Deterministic path (default, no Groq key required) ───────
    if not groq_clients:
        state = _symbol_state.get(symbol, {})
        direction = state.get("mem_direction", "")
        indicators = state.get("last_indicators", {})
        sr_context = state.get("last_sr_context", {})
        news_ctx = state.get("last_news_context", None)

        if not direction:
            log.debug(f"[decision] {symbol}: sin dirección pre-calculada → HOLD")
            return {"decision": "HOLD", "confidence": 1, "reason": "Sin dirección"}

        result = deterministic_decision(
            symbol=symbol,
            direction=direction,
            indicators=indicators,
            sr_context=sr_context,
            news_context=news_ctx,
            sym_cfg=sym_cfg,
        )
        return {
            "decision": result["decision"],
            "confidence": result["confidence"],
            "reason": result.get("reason", ""),
            "score": result.get("score", 0.0),
        }

    # ── Groq LLM path (only when GROQ_API_KEY is set) ────────────
    return _ask_groq_llm(symbol, context, sym_cfg, cache_key)


def _ask_groq_llm(symbol: str, context: str, sym_cfg: dict, cache_key: str = "") -> Optional[dict]:
    """Original Groq LLM call — only invoked when GROQ_API_KEY is configured."""
    system_prompt = get_system_prompt(sym_cfg)
    global groq_cooldown_until, groq_call_cache

    now_ts = time.time()
    if cache_key and cache_key in groq_call_cache:
        _bump_groq_metric("cache_hits_total")
        return groq_call_cache[cache_key]

    symbol_wait_left = _get_groq_symbol_cooldown_remaining(symbol, now_ts)
    if symbol_wait_left > 0:
        log.info(f"[groq] Cooldown por simbolo activo - omitiendo consulta {symbol} ({symbol_wait_left}s restantes)")
        _bump_groq_metric("symbol_cooldown_skips_total")
        fallback = _build_groq_fallback_decision(
            f"Cooldown Groq por simbolo ({symbol_wait_left}s restantes)"
        )
        if cache_key:
            groq_call_cache[cache_key] = fallback
        return fallback

    if now_ts < groq_cooldown_until:
        wait_left = int(max(1, groq_cooldown_until - now_ts))
        log.warning(f"[groq] Cooldown global activo — omitiendo consulta {symbol} ({wait_left}s restantes)")
        _bump_groq_metric("cooldown_skips_total")
        fallback = _build_groq_fallback_decision(f"Cooldown Groq activo ({wait_left}s restantes)")
        _set_groq_symbol_cooldown(symbol, seconds=wait_left)
        if cache_key:
            groq_call_cache[cache_key] = fallback
        return fallback

    budget_ready_keys = []
    for key_idx in range(len(groq_clients)):
        budget_ok, _, _ = _is_groq_key_budget_available(key_idx)
        if budget_ok:
            budget_ready_keys.append(key_idx)

    if groq_clients and not budget_ready_keys:
        limit_reason = f"Presupuesto Groq agotado en todas las keys ({_format_groq_key_budget_status()})"
        log.warning(f"[groq] {limit_reason} - omitiendo consulta {symbol}")
        _bump_groq_metric("skipped_by_budget_total")
        fallback = _build_groq_fallback_decision(limit_reason)
        _set_groq_symbol_cooldown(symbol)
        if cache_key:
            groq_call_cache[cache_key] = fallback
        return fallback

    max_attempts = max(1, len(groq_clients))
    tried_keys = set()
    while len(tried_keys) < max(1, len(groq_clients)):
        client_idx, client = _select_groq_client(time.time(), excluded=tried_keys)
        if client is None:
            break
        tried_keys.add(client_idx)
        attempt = len(tried_keys)
        try:
            _bump_groq_metric("api_calls_total", key_idx=client_idx)
            response = client.chat.completions.create(
                model=cfg.GROQ_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": context},
                ],
                temperature=0.2,   # bajo para decisiones deterministas de trading
                max_tokens=512,    # suficiente para el JSON de respuesta (~150 palabras)
                response_format={"type": "json_object"},  # fuerza JSON válido; fallback de parseo por compatibilidad
            )
            raw = response.choices[0].message.content.strip()
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].strip()
            payload = json.loads(raw)
            _set_last_groq_analysis(symbol, payload)
            _bump_groq_metric("api_success_total")
            _set_groq_symbol_cooldown(symbol)
            if cache_key:
                groq_call_cache[cache_key] = payload
            return payload

        except json.JSONDecodeError as e:
            log.error(f"[groq] JSON inválido (intento {attempt}/{max_attempts}): {e}")
            _bump_groq_metric("errors_total")
            _set_groq_symbol_cooldown(symbol)
            fallback = _build_groq_fallback_decision("Groq devolvió JSON inválido")
            if cache_key:
                groq_call_cache[cache_key] = fallback
            return fallback

        except Exception as e:
            err_str = str(e)
            status_code = getattr(e, "status_code", None)
            if status_code == 429 or "429" in err_str or "rate_limit" in err_str.lower():
                retry_after = _extract_retry_delay_seconds(err_str)
                groq_key_cooldowns[client_idx] = time.time() + retry_after + 1.0
                if len(groq_clients) > 1 and len(tried_keys) < len(groq_clients):
                    _bump_groq_metric("key_rotations_total")
                    log.warning(
                        f"[groq] Key #{client_idx+1} en cooldown {retry_after:.0f}s - probando siguiente key"
                    )
                    continue
                groq_cooldown_until = time.time() + retry_after + 1.0
                _set_groq_symbol_cooldown(
                    symbol,
                    seconds=max(
                        int(math.ceil(retry_after)) + 1,
                        int(getattr(cfg, "GROQ_SYMBOL_COOLDOWN_SEC", 0) or 0),
                    ),
                )
                log.error(f"[groq] Cuota agotada - cooldown global {retry_after:.0f}s")
                _bump_groq_metric("quota_hits_total")
                fallback = _build_groq_fallback_decision(
                    f"Cuota Groq agotada, reintentar tras {retry_after:.0f}s"
                )
                if cache_key:
                    groq_call_cache[cache_key] = fallback
                return fallback
            if "503" in err_str or "UNAVAILABLE" in err_str:
                if len(groq_clients) > 1 and len(tried_keys) < len(groq_clients):
                    _bump_groq_metric("key_rotations_total")
                    log.warning(f"[groq] 503 UNAVAILABLE en key #{client_idx+1} - probando siguiente key")
                    continue
            elif "404" in err_str or "NOT_FOUND" in err_str:
                log.error(f"[groq] Modelo no disponible: {e}")
                _bump_groq_metric("errors_total")
                fallback = _build_groq_fallback_decision("Modelo Groq no disponible")
                _set_groq_symbol_cooldown(symbol)
                if cache_key:
                    groq_call_cache[cache_key] = fallback
                return fallback
            log.error(f"[groq] Error con key #{client_idx+1}: {e}")
            if len(groq_clients) > 1 and len(tried_keys) < len(groq_clients):
                _bump_groq_metric("key_rotations_total")
                continue
            if attempt == max_attempts:
                _bump_groq_metric("errors_total")
                fallback = _build_groq_fallback_decision("Error transitorio de Groq")
                _set_groq_symbol_cooldown(symbol)
                if cache_key:
                    groq_call_cache[cache_key] = fallback
                return fallback
    fallback = _build_groq_fallback_decision("Groq sin respuesta")
    _set_groq_symbol_cooldown(symbol)
    if cache_key:
        groq_call_cache[cache_key] = fallback
    return fallback


def build_context(
    symbol: str, ind: dict, sr_ctx, news_ctx,
    mem_check, sym_cfg: dict,
    cal_events: list = None,
    scorecard_check = None,
    policy_check = None,
    trade_plan = None,
) -> str:
    price   = ind.get("price", 0)
    hilbert = ind.get("hilbert", {})
    fourier = ind.get("fourier", {})
    hurst   = ind.get("hurst", 0.5)
    votes   = ind.get("trend_votes", {"bull": 0, "bear": 0})

    lines = [
        f"=== ANÁLISIS {symbol} — {sym_cfg.get('name', symbol)} ===",
        f"Estrategia activa: {sym_cfg.get('strategy_type', '?')}",
        f"Hora UTC actual:   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}",
        f"Precio actual:     {price}",
        f"Tendencia H1:      {ind.get('h1_trend', '?')}  "
        f"(votos: bull={votes.get('bull',0)}/6  bear={votes.get('bear',0)}/6)",
        "",
        "── ALGORITMOS MATEMÁTICOS AVANZADOS ──",
        f"HILBERT TRANSFORM:",
        f"  Señal:          {hilbert.get('signal', '?')}",
        f"  Descripción:    {hilbert.get('description', '?')}",
        f"  Seno (sine):    {hilbert.get('sine', 0):.4f}",
        f"  Lead sine (+45°): {hilbert.get('lead_sine', 0):.4f}",
        f"  Fase:           {hilbert.get('phase', 0):.1f}°",
        f"  Período ciclo:  {hilbert.get('period', 0):.1f} velas",
        f"  Fuerza ciclo:   {hilbert.get('strength', 0):.3f}",
        f"HURST: {hurst:.3f} → {ind.get('hurst_regime','?')} | min={sym_cfg.get('min_hurst',0.5):.2f}",
    ]

    # ── FASE 12: ADF + Z-score — régimen mejorado ──
    adf = ind.get("adf_test", {})
    zsc = ind.get("zscore_returns", {})
    enhanced_regime = ind.get("enhanced_regime", "UNKNOWN")
    regime_conf = ind.get("regime_confidence", "LOW")

    lines.append(f"Régimen mejorado: {enhanced_regime} (confianza: {regime_conf})")
    if adf.get("p_value", 1.0) < 1.0:
        lines.append(
            f"ADF: stat={adf.get('adf_statistic', 0):.3f} p={adf.get('p_value', 1):.3f} "
            f"{'✓ estacionario' if adf.get('is_stationary') else '✗ no estacionario'}"
        )
    if zsc.get("signal", "NEUTRAL") != "NEUTRAL":
        lines.append(
            f"Z-Score: {zsc.get('z_score', 0):.2f} → {zsc.get('signal')} "
            f"(fuerza: {zsc.get('strength', 0):.0%})"
        )

    lines += [
        f"KALMAN: precio={ind.get('kalman_price',0):.4f} | tend={ind.get('kalman_trend','?')} | slope={ind.get('kalman_slope',0):.5f}",
        f"FISHER: {ind.get('fisher',0):.3f} | señal={ind.get('fisher_signal',0):.3f} | cruce={ind.get('fisher_cross','?')}",
        f"FOURIER: período={fourier.get('dominant_period','?')}v | fuerza={fourier.get('strength',0):.3f}",
        f"CICLO: osc={ind.get('cycle_osc',0):.3f} | fase={ind.get('cycle_phase','?')}",
        f"LR: slope={ind.get('lr_slope',0):.6f} | R²={ind.get('lr_r2',0):.3f} | tend={ind.get('lr_trend','?')}",
        "",
        "── INDICADORES TÉCNICOS ──",
        f"RSI: {ind.get('rsi',0):.1f} (OS={sym_cfg.get('rsi_oversold',30)} / OB={sym_cfg.get('rsi_overbought',70)}) | Div: {ind.get('rsi_div','NONE')}",
        f"MACD hist: {ind.get('macd_hist',0):.4f} | Dir: {ind.get('macd_dir','?')}",
        f"Stoch K={ind.get('stoch_k',0):.1f} D={ind.get('stoch_d',0):.1f}",
        f"CCI: {ind.get('cci',0):.1f} | Williams %R: {ind.get('williams',0):.1f}",
        f"ATR: {ind.get('atr',0):.4f} ({ind.get('atr_pct',0):.2f}%)",
        f"BB: {ind.get('bb_pos','?')} | Squeeze: {ind.get('bb_squeeze',False)} | KC: {ind.get('kc_squeeze',False)}",
        f"DEMA cross: {ind.get('dema_cross','?')} | SuperTrend: {'ALCISTA' if ind.get('supertrend',0)==1 else 'BAJISTA'}",
        f"Heiken Ashi: {ind.get('ha_trend','?')} ({ind.get('ha_streak',0)} velas)",
        f"VWAP: {ind.get('vwap',0):.4f} | Precio {'sobre' if price > ind.get('vwap',price) else 'bajo'} VWAP",
        f"OBV: {ind.get('obv_trend','?')} | CMF: {ind.get('cmf',0):.4f} | MFI: {ind.get('mfi',0):.1f}",
        f"SAR: {'alcista' if ind.get('sar_trend',0)==1 else 'bajista'} @ {ind.get('sar_value',0):.4f}",
        f"Ichimoku: {'sobre nube' if ind.get('ichi_above_cloud',False) else 'bajo nube'}",
        f"Vela: {ind.get('candle_pattern','NONE')} | Momentum: {ind.get('momentum',0):.4f}",
        "",
        "── SOPORTE / RESISTENCIA ──",
        sr_for_prompt(sr_ctx),
        "",
        "── CALENDARIO ECONÓMICO ──",
    ]

    if cal_events:
        for ev in cal_events[:5]:
            lines.append(
                f"  {'🚨' if ev.minutes_until() <= 1 else '📅'} "
                f"[{ev.currency}] {ev.title} {ev.time_label()} | {ev.impact}"
                + (f" | Forecast: {ev.forecast}" if ev.forecast else "")
            )
    else:
        lines.append("  ✅ Sin eventos de alto impacto próximos (2h)")

    lines += [
        "",
        "── MEMORIA NEURAL ──",
        f"Bloquear: {mem_check.should_block} | Ajuste: {mem_check.confidence_adj:+.1f}",
        f"Pérdidas similares: {mem_check.similar_losses} | Wins: {mem_check.similar_wins}",
        f"Detalle: {mem_check.warning_msg}",
        "",
        "── SCORECARD JERÁRQUICO ──",
    ]
    if scorecard_check is not None:
        lines += [
            f"Setup: {scorecard_check.setup_id}",
            f"Sesión: {scorecard_check.session} | Régimen: {scorecard_check.regime}",
            f"Nivel: {scorecard_check.level} | Sample: {scorecard_check.sample_size}",
            f"WR: {scorecard_check.win_rate:.1f}% | Mín WR: {scorecard_check.min_win_rate:.1f}%",
            f"Bloquear setup: {scorecard_check.should_block} | {scorecard_check.reason}",
            "",
        ]
    else:
        lines += [
            "Sin scorecard disponible",
            "",
        ]

    lines += [
        "── POLICY ENGINE ──",
    ]
    if policy_check is not None:
        lines += [
            f"Dirección candidata: {policy_check.direction}",
            f"Policy score: {policy_check.policy_score:.3f} | Block: {policy_check.should_block}",
            f"WR: {policy_check.win_rate:.1f}% | PF: {policy_check.profit_factor:.2f} | "
            f"AvgR: {policy_check.avg_reward:+.3f} | n={policy_check.sample_size}",
            f"Detalle: {policy_check.reason}",
            "",
        ]
    else:
        lines += [
            "Sin policy check disponible",
            "",
        ]

    lines += [
        "── PLAN DE TRADE PREVIO ──",
    ]
    if trade_plan is not None:
        lines += [
            f"Dirección candidata: {trade_plan['action']}",
            f"Entry estimado: {trade_plan['price']:.5f} | SL: {trade_plan['sl']:.5f} | TP: {trade_plan['tp']:.5f}",
            f"R:R estimado: {trade_plan['rr']:.2f}",
            "",
        ]
    else:
        lines += [
            "Plan no disponible",
            "",
        ]

    lines += [
        "── NOTICIAS RSS ──",
        format_news_for_prompt(news_ctx),
        "",
    ]

    # ── SENTIMIENTO DE MERCADO (fuentes externas) ────────────────
    sentiment = get_sentiment_for_symbol(symbol)
    if sentiment:
        sent_lines = ["── SENTIMIENTO DE MERCADO ──"]
        if "crypto_fng" in sentiment:
            sent_lines.append(
                f"Crypto Fear & Greed: {sentiment['crypto_fng']:.0f} "
                f"({sentiment.get('crypto_fng_label', '?')})"
            )
        if "vix" in sentiment:
            sent_lines.append(
                f"VIX: {sentiment['vix']:.2f} ({sentiment.get('vix_label', '?')})"
            )
        if "cot_net_position" in sentiment:
            cot_date = sentiment.get("cot_report_date", "")
            date_str = f" ({cot_date})" if cot_date else ""
            sent_lines.append(
                f"COT Non-Commercial net: {sentiment['cot_net_position']:+,} "
                f"→ {sentiment.get('cot_bias', '?')}{date_str}"
            )
        sent_lines.append(f"Sesgo sentimiento: {sentiment.get('sentiment_bias', 'NEUTRAL')}")
        sent_lines.append("")
        lines += sent_lines

    # ── PILAR 3: MICROESTRUCTURA ─────────────────────────────────
    micro = ind.get("microstructure", {})
    conf  = ind.get("confluence", {})

    # ── DATOS EXTERNOS (FASE 11: Twelve Data / Polygon) ──────────
    ext_data_source = ind.get("ext_data_source", "tick_volume")
    if _DATA_PROVIDERS_AVAILABLE:
        try:
            td_enabled   = getattr(cfg, "TWELVE_DATA_ENABLED",  False)
            poly_enabled = getattr(cfg, "POLYGON_ENABLED",      False)
            ext_lines = []

            if poly_enabled:
                try:
                    poly = get_polygon()
                    if symbol in poly.SYMBOL_MAP:
                        snap = poly.get_snapshot(symbol)
                        if snap:
                            ext_lines.append(
                                f"Polygon {symbol}: Vol={snap['day_volume']:,.0f} | "
                                f"VWAP={snap['day_vwap']:.2f} | Cambio={snap['change_pct']:+.2f}%"
                            )
                except Exception:
                    pass

            if td_enabled:
                try:
                    td = get_twelve_data()
                    if symbol in td.SYMBOL_MAP:
                        quote = td.get_quote(symbol)
                        if quote:
                            ext_lines.append(
                                f"Twelve Data {symbol}: Vol={quote['volume']:,.0f} | "
                                f"Precio={quote['close']:.2f} | Cambio={quote['percent_change']:+.2f}%"
                            )
                except Exception:
                    pass

            if ext_lines:
                lines += ["── DATOS EXTERNOS (FASE 11) ──"] + ext_lines + [
                    f"Fuente volumen activa: {ext_data_source}",
                    "",
                ]
        except Exception:
            pass

    fvg_bull = micro.get("fvg_bull")
    fvg_bear = micro.get("fvg_bear")
    sess_vwap_dev = micro.get("session_vwap_dev", 0.0)
    above_svwap   = micro.get("above_session_vwap", True)

    lines += [
        "── PILAR 3: MICROESTRUCTURA ──",
        f"Volume Profile:  POC={micro.get('poc',0):.4f} | VAH={micro.get('vah',0):.4f} | VAL={micro.get('val',0):.4f}",
        f"  Precio {'SOBRE' if micro.get('above_poc') else 'BAJO'} POC | "
        f"{'DENTRO' if micro.get('in_value_area') else ('SOBRE VAH' if price > micro.get('vah', price) else 'BAJO VAL')} del Value Area",
        f"Session VWAP ({micro.get('session','?')}): {micro.get('session_vwap',0):.4f} | "
        f"Precio {'sobre' if above_svwap else 'bajo'} S-VWAP ({sess_vwap_dev:+.2f}%)",
        f"FVG Bullish: " + (
            f"activo en {fvg_bull['low']:.4f}–{fvg_bull['high']:.4f} (hace {fvg_bull['age']} velas)"
            if fvg_bull else "ninguno activo cercano"
        ),
        f"FVG Bearish: " + (
            f"activo en {fvg_bear['low']:.4f}–{fvg_bear['high']:.4f} (hace {fvg_bear['age']} velas)"
            if fvg_bear else "ninguno activo cercano"
        ),
        f"Micro Score: {micro.get('micro_score', 0):+.1f} | Sesgo: {micro.get('micro_bias','NEUTRAL')}",
        f"  Detalle: {micro.get('description','')}",
        "",
        "── CONFLUENCIA 3 PILARES ──",
        f"P1 (Estadístico):  {conf.get('p1_score', 0):+.2f}",
        f"P2 (Matemático):   {conf.get('p2_score', 0):+.2f}",
        f"P3 (Microestr.):   {conf.get('p3_score', 0):+.2f}",
        f"TOTAL (ponderado): {conf.get('total', 0):+.2f} → {conf.get('bias','NEUTRAL')}",
        f"Sniper aligned:    {'✅ SÍ — todos los pilares alineados' if conf.get('sniper_aligned') else '⚠️  NO — pilares en desacuerdo'}",
        "",
        "── PARÁMETROS ──",
        f"SL={sym_cfg.get('sl_atr_mult','?')}×ATR | TP={sym_cfg.get('tp_atr_mult','?')}×ATR",
        f"Min Hurst: {sym_cfg.get('min_hurst',0.5):.2f} | Min confianza: {sym_cfg.get('min_confidence',6)}",
        f"Estrategia: {sym_cfg.get('strategy_type','HYBRID')}",
    ]
    return "\n".join(lines)


def build_lateral_context(
    symbol: str,
    ind: dict,
    sr_ctx,
    news_ctx,
    sym_cfg: dict,
    cal_events: list,
    candidate_payloads: dict,
) -> str:
    base_mem = next(iter(candidate_payloads.values()))["mem_check"]
    lines = [
        "── CANDIDATOS LATERALES ──",
        "Compara BUY y SELL como candidatos completos. Si ninguno domina claramente, responde HOLD.",
    ]

    for action in ("BUY", "SELL"):
        payload = candidate_payloads.get(action)
        if payload is None:
            continue
        scorecard = payload["scorecard"]
        policy = payload["policy"]
        mem_check = payload["mem_check"]
        plan = payload["trade_plan"]
        lines += [
            f"[{action}] listo_para_groq={payload['candidate_ok']}",
            f"  gate: {payload['candidate_reason'] or 'OK'}",
            f"  memory: block={mem_check.should_block} adj={mem_check.confidence_adj:+.1f} detail={mem_check.warning_msg}",
            f"  scorecard: block={scorecard.should_block} wr={scorecard.win_rate:.1f}% n={scorecard.sample_size} reason={scorecard.reason}",
            f"  policy: block={policy.should_block} score={policy.policy_score:.3f} n={policy.sample_size} reason={policy.reason}",
        ]
        if plan is not None:
            lines.append(
                f"  plan: entry={plan['price']:.5f} sl={plan['sl']:.5f} tp={plan['tp']:.5f} rr={plan['rr']:.2f}"
            )
        lines.append("")

    return build_context(
        symbol=symbol,
        ind=ind,
        sr_ctx=sr_ctx,
        news_ctx=news_ctx,
        mem_check=base_mem,
        sym_cfg=sym_cfg,
        cal_events=cal_events,
        scorecard_check=None,
        policy_check=None,
        trade_plan=None,
    ) + "\n\n" + "\n".join(lines)


# ════════════════════════════════════════════════════════════════
#  ÓRDENES MT5
# ════════════════════════════════════════════════════════════════

def open_order(symbol: str, direction: str, sl: float, tp: float, volume: float) -> Optional[int]:
    action  = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    tick    = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    price   = tick.ask if direction == "BUY" else tick.bid
    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       volume,
        "type":         action,
        "price":        price,
        "sl":           sl,
        "tp":           tp,
        "deviation":    20,
        "magic":        cfg.MAGIC_NUMBER,
        "comment":      "ZAR v6",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"[orden] ✅ #{result.order} {direction} {symbol} vol={volume}")
        return result.order
    log.error(f"[orden] ❌ {result.comment} (retcode={result.retcode})")
    return None


def move_sl(ticket: int, new_sl: float) -> bool:
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        return False
    r = mt5.order_send({
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "sl":       new_sl,
        "tp":       pos[0].tp,
    })
    return r.retcode == mt5.TRADE_RETCODE_DONE


def close_position_market(pos: dict, comment: str = "CB-emergency") -> bool:
    """
    Cierra una posición abierta con una orden de mercado inmediata.
    Usado por el Circuit Breaker de emergencia (Cisne Negro).
    """
    ticket    = pos.get("ticket")
    symbol    = pos.get("symbol")
    volume    = float(pos.get("volume", 0.01))
    pos_type  = pos.get("type", 0)   # 0=BUY, 1=SELL

    if not ticket or not symbol or volume <= 0:
        log.error(f"[close_market] Datos de posición inválidos: {pos}")
        return False

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log.error(f"[close_market] No hay tick para {symbol}")
        return False

    # Para cerrar un BUY enviamos SELL al precio bid; para SELL enviamos BUY al ask
    close_type  = mt5.ORDER_TYPE_SELL if pos_type == 0 else mt5.ORDER_TYPE_BUY
    close_price = tick.bid            if pos_type == 0 else tick.ask

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       volume,
        "type":         close_type,
        "price":        close_price,
        "position":     ticket,
        "deviation":    _EMERGENCY_CLOSE_DEVIATION,
        "magic":        cfg.MAGIC_NUMBER,
        "comment":      comment[:31],
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        log.warning(
            f"[close_market] ✅ Posición #{ticket} {symbol} cerrada ({comment})"
        )
        return True
    log.error(
        f"[close_market] ❌ Error cerrando #{ticket} {symbol}: "
        f"{result.comment} (retcode={result.retcode})"
    )
    return False


# ════════════════════════════════════════════════════════════════
#  FIX 6: CÁLCULO DE PIPS POR INSTRUMENTO
# ════════════════════════════════════════════════════════════════

def _calc_pips_instrument(deal, symbol: str) -> float:
    profit = float(deal.profit)
    volume = float(deal.volume)
    if volume <= 0:
        return 0.0
    sym_info = mt5.symbol_info(symbol)
    if sym_info is not None:
        tick_val = float(getattr(sym_info, 'trade_tick_value', 0) or 0)
        tick_sz  = float(getattr(sym_info, 'trade_tick_size',  0) or 0)
        if tick_val > 0 and tick_sz > 0:
            pip_size = tick_sz * 10
            pip_val  = tick_val * 10 * volume
            if pip_val > 0:
                return round(profit / pip_val, 1)
    s = symbol.upper()
    if "XAU" in s or "GOLD" in s or "BTC" in s or "ETH" in s:
        pip_value = volume * 100
    elif "XAG" in s or "OIL" in s or "WTI" in s:
        pip_value = volume * 50
    elif "500" in s or "NAS" in s or "GER" in s or "DAX" in s:
        pip_value = volume * 10
    else:
        pip_value = volume * 10
    return round(profit / pip_value, 1) if pip_value != 0 else 0.0


def _price_distance_to_pips(symbol: str, price_distance: float) -> float:
    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        return 0.0
    tick_sz = float(getattr(sym_info, "trade_tick_size", 0) or 0)
    point = float(getattr(sym_info, "point", 0) or 0)
    pip_size = tick_sz * 10 if tick_sz > 0 else point * 10
    if pip_size <= 0:
        return 0.0
    return max(0.0, float(price_distance) / pip_size)


# ════════════════════════════════════════════════════════════════
#  FIX 11 — TRAILING STOP PROGRESIVO (reemplaza _check_breakeven)
# ════════════════════════════════════════════════════════════════

# Estado del trailing por ticket: nivel de protección alcanzado
_trail_stage: dict = {}


def _manage_trailing_stop(pos: dict, sym_cfg: dict):
    """Trailing proporcional por progreso de TP con ratchet y anti-SL-hunting."""
    global last_action, symbol_status_cache

    ticket    = pos["ticket"]
    symbol    = pos["symbol"]
    direction = "BUY" if pos["type"] == 0 else "SELL"
    open_p    = pos["price_open"]
    cur_p     = pos["price_current"]
    sl        = float(pos.get("sl", 0.0) or 0.0)
    tp        = float(pos.get("tp", 0.0) or 0.0)

    # Obtener ATR del cache de indicadores
    # FASE-C: preferir atr_entry (M1) en scalp mode, fallback a atr (M15)
    ind     = ind_cache.get(symbol, {})
    atr_val = float(ind.get("atr_entry", ind.get("atr", 0)) or 0)

    if atr_val <= 0:
        atr_val = float(ind.get("atr", 0) or 0)
    if atr_val <= 0:
        return  # Sin ATR disponible, no actuar

    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        return
    point = sym_info.point or 0.00001
    candle_stamp = str(ind.get("entry_candle_time", "") or "")

    favorable_move = (cur_p - open_p) if direction == "BUY" else (open_p - cur_p)
    if favorable_move <= 0:
        _profit_candle_count[ticket] = 0
        _profit_candle_last_seen.pop(ticket, None)
        return
    profit_price = abs(cur_p - open_p)

    # Obtener trade_mode primero para saber si es scalp ANTES de aplicar los gates
    trade_mode = trade_mode_cache.get(ticket)
    if not trade_mode:
        trade_mode = get_adaptive_trail_params(sym_cfg, direction)
        trade_mode_cache[ticket] = trade_mode

    scalp_mode = bool(trade_mode.get("scalp_mode", False))
    be_atr_mult = float(trade_mode.get("be_atr_mult", sym_cfg.get("be_atr_mult", 2.0)))
    be_buffer_mult = float(trade_mode.get("be_buffer_mult", 0.5))

    if scalp_mode:
        # SCALP MODE: activar BE basado en pips ganados, ignorar conteo de velas H1 y
        # umbral be_atr_mult×ATR(H1) — ambos son inapropiados para trades de M1
        # (el conteo de velas H1 tarda 2+ horas; el ATR H1 es 60-100 pips en EURUSD)
        gained_pips = _price_distance_to_pips(symbol, profit_price)
        min_be_pips = float(getattr(cfg, "SCALPING_BE_MIN_PIPS", 2.0))
        if gained_pips < min_be_pips:
            return
    else:
        # MODO NORMAL: esperar 2+ velas H1 en ganancia y superar umbral ATR
        if candle_stamp and _profit_candle_last_seen.get(ticket) != candle_stamp:
            _profit_candle_count[ticket] = _profit_candle_count.get(ticket, 0) + 1
            _profit_candle_last_seen[ticket] = candle_stamp
        if _profit_candle_count.get(ticket, 0) < 2:
            return
        be_threshold_price = atr_val * be_atr_mult
        if profit_price < be_threshold_price:
            return
    if tp != 0:
        tp_total = abs(tp - open_p)
        if direction == "BUY":
            tp_remaining = max(tp - cur_p, 0.0)
        else:
            tp_remaining = max(cur_p - tp, 0.0)
        tp_progress = 1.0 - (tp_remaining / tp_total) if tp_total > 0 else 0.0
    else:
        tp_progress = 0.0
    tp_progress = max(0.0, min(1.0, tp_progress))

    if scalp_mode:
        # gained_pips ya está calculado en el gate anterior
        # FASE-C: Stage 5 añadido para protección final cerca del TP
        stage_definitions = [
            (float(getattr(cfg, "SCALPING_BE_PIPS_STAGE_5", 25.0)), 0.85, 6, "Scalp lock 85%"),
            (float(getattr(cfg, "SCALPING_BE_PIPS_STAGE_4")),       0.70, 5, "Scalp lock 70%"),
            (float(getattr(cfg, "SCALPING_BE_PIPS_STAGE_3")),       0.50, 4, "Scalp lock 50%"),
            (float(getattr(cfg, "SCALPING_BE_PIPS_STAGE_2")),       0.30, 3, "Scalp lock 30%"),
            (float(getattr(cfg, "SCALPING_BE_PIPS_STAGE_1")),       0.15, 2, "Scalp lock 15%"),
        ]
        lock_pct = 0.0
        new_stage = 1
        stage_label = "Scalp BE buffer"
        for min_pips, pct, stage_num, label in stage_definitions:
            if gained_pips >= min_pips:
                lock_pct = pct
                new_stage = stage_num
                stage_label = label
                break
    else:
        stage_definitions = [
            (0.85, 0.70, 5, "Lock 70%"),
            (0.70, 0.50, 4, "Lock 50%"),
            (0.50, 0.35, 3, "Lock 35%"),
            (0.30, 0.15, 2, "Lock 15%"),
        ]

        lock_pct = 0.0
        new_stage = 1
        stage_label = "BE buffer"
        for min_progress, pct, stage_num, label in stage_definitions:
            if tp_progress >= min_progress:
                lock_pct = pct
                new_stage = stage_num
                stage_label = label
                break

    if lock_pct > 0:
        locked_profit = profit_price * lock_pct
        new_sl = open_p + locked_profit if direction == "BUY" else open_p - locked_profit
    else:
        buffer_price = max(point, atr_val * be_buffer_mult)
        new_sl = open_p + buffer_price if direction == "BUY" else open_p - buffer_price

    digits = int(getattr(sym_info, "digits", 5) or 5)
    new_sl = round(new_sl, digits)

    if direction == "BUY":
        if sl > 0 and new_sl <= sl:
            return
    else:
        if sl > 0 and new_sl >= sl:
            return

    sl_reference = sl if sl > 0 else open_p
    diff_pts = abs(new_sl - sl_reference) / point
    if diff_pts < 0.5:
        return

    # ── Mover SL ──
    if move_sl(ticket, new_sl):
        prev_stage = _trail_stage.get(ticket, 0)
        _trail_stage[ticket] = new_stage

        profit_pts = profit_price / point
        locked_pts = (new_sl - open_p) / point if direction == "BUY" else (open_p - new_sl) / point

        if prev_stage < 1:
            notify_breakeven(symbol, ticket, new_sl, profit_pts)
            if scalp_mode:
                log.info(
                    f"[Trail] 🛡 #{ticket} {symbol} {direction} "
                    f"— {stage_label} | pips={gained_pips:.1f} "
                    f"| TP%={tp_progress:.0%} | SL→{new_sl:.5f}"
                )
            else:
                log.info(
                    f"[Trail] 🛡 #{ticket} {symbol} {direction} "
                    f"— {stage_label} | profit_cycles={_profit_candle_count.get(ticket, 0)} "
                    f"| TP%={tp_progress:.0%} | SL→{new_sl:.5f}"
                )
        else:
            if scalp_mode:
                log.info(
                    f"[Trail] 📈 #{ticket} {symbol} {direction} "
                    f"— {stage_label} | pips={gained_pips:.1f} | "
                    f"lock={locked_pts:.0f}pts | SL→{new_sl:.5f} | TP%={tp_progress:.0%}"
                )
            else:
                log.info(
                    f"[Trail] 📈 #{ticket} {symbol} {direction} "
                    f"— {stage_label} | cycles={_profit_candle_count.get(ticket, 0)} | "
                    f"lock={locked_pts:.0f}pts | SL→{new_sl:.5f} | TP%={tp_progress:.0%}"
                )

        last_action = f"Trail {stage_label} #{ticket} {symbol} lock={locked_pts:.0f}pts"


# ════════════════════════════════════════════════════════════════
#  FIX 14 — FILTRO ANTI-CONTRA-TENDENCIA
# ════════════════════════════════════════════════════════════════

def _passes_direction_filter(action: str, ind: dict) -> tuple:
    """
    FIX 14: Bloquea trades cuando los indicadores primarios contradicen la dirección.

    Análisis del DB muestra el patrón de pérdida más frecuente:
    - XAUUSDm BUY con supertrend=-1 y kalman=BAJISTA → reversion
    - GBPUSDm SELL con kalman=ALCISTA y ha=ALCISTA → contratendencia

    Regla: Si Kalman + SuperTrend AMBOS contradicen la dirección → HOLD forzado.

    FIX v7.0 (SCALPING): En modo scalping puro (M1), el filtro de 3/4 contradicciones
    H1 era demasiado restrictivo — bloqueaba todos los SELL durante sesiones alcistas en H1,
    aunque el M1 mostrara reversión válida. Se elimina el conteo de 3/4 en scalping:
    solo aplica el bloqueo duro (Kalman+SuperTrend AMBOS contra la dirección).
    """
    kalman_trend = ind.get("kalman_trend", "NEUTRAL")
    supertrend   = ind.get("supertrend", 0)
    ha_trend     = ind.get("ha_trend", "NEUTRAL")
    macd_dir     = ind.get("macd_dir", "NEUTRAL")

    if action == "BUY":
        kalman_ok     = kalman_trend == "ALCISTA"
        supertrend_ok = supertrend == 1
        ha_ok         = ha_trend == "ALCISTA"
        macd_ok       = macd_dir == "ALCISTA"
    else:  # SELL
        kalman_ok     = kalman_trend == "BAJISTA"
        supertrend_ok = supertrend == -1
        ha_ok         = ha_trend == "BAJISTA"
        macd_ok       = macd_dir == "BAJISTA"

    # BLOQUEO DURO: Si Kalman Y SuperTrend ambos contradicen → bloquear
    # Este bloqueo aplica siempre, incluso en scalping.
    if not kalman_ok and not supertrend_ok:
        return False, f"Kalman+SuperTrend contradicen {action} → HOLD forzado"

    # FIX v7.0: En modo scalping, el bloqueo duro (arriba) es suficiente.
    # No aplicar el conteo de 3/4 indicadores H1 para scalping M1:
    # en tendencias H1 alcistas, HA y MACD H1 contradicen SELL aunque M1 sea válido,
    # causando que ~80% de los SELL queden bloqueados injustamente.
    if bool(getattr(cfg, "SCALPING_ONLY", False)):
        return True, ""

    # Contar indicadores primarios que contradicen
    contradictions = sum([
        not kalman_ok,
        not supertrend_ok,
        not ha_ok,
        not macd_ok,
    ])

    if contradictions >= 3:
        return False, f"{contradictions}/4 indicadores primarios contradicen {action} → HOLD"

    return True, ""


# ════════════════════════════════════════════════════════════════
#  GESTIÓN DE POSICIONES
# ════════════════════════════════════════════════════════════════

# be_activated: set de tickets que ya pasaron al menos a etapa 1 de trail
be_activated: set = set()
tp_alerted:   set = set()


def manage_positions(positions: list, ind_by_sym: dict):
    global last_action
    # Obtener balance actual para el circuit breaker
    acc_info = mt5.account_info()
    balance  = float(acc_info.balance) if acc_info else 0.0

    for pos in positions:
        ticket = pos["ticket"]
        symbol = pos["symbol"]
        cur_p  = pos["price_current"]
        tp     = pos["tp"]
        profit = pos["profit"]
        sym_cfg_data = cfg.SYMBOLS.get(symbol, {})

        # ── Circuit Breaker individual (Cisne Negro) ──────────────
        cb_triggered, cb_reason = check_circuit_breaker(pos, balance)
        if cb_triggered:
            log.warning(f"[circuit_breaker] {cb_reason} — cerrando emergencia")
            closed = close_position_market(pos, comment="CB-emergency")
            if closed:
                last_action = f"🚨 CB {symbol} #{ticket}"
                telegram_send(f"🚨 <b>CIRCUIT BREAKER</b>\n{cb_reason}")
            continue   # no gestionar trailing en esta posición

        # FIX 11: trailing stop progresivo (reemplaza _check_breakeven)
        _manage_trailing_stop(pos, sym_cfg_data)

        # Marcar tickets que están en trail
        if _trail_stage.get(ticket, 0) >= 1:
            be_activated.add(ticket)

        # Alerta TP cercano (15% restante del camino)
        if ticket not in tp_alerted and tp != 0:
            open_p  = pos["price_open"]
            dist_tp = abs(tp - cur_p)
            total   = abs(tp - open_p)
            if total > 0 and dist_tp / total <= 0.15:
                notify_near_tp(symbol, ticket, cur_p, tp, profit)
                tp_alerted.add(ticket)
                last_action = f"TP cercano #{ticket} {symbol} ${profit:.2f}"


def watch_closures(open_tickets_before: set, open_positions_now: list):
    """
    Detecta posiciones cerradas y registra el resultado.

    FIX 12/13: Win Rate = wins/(wins+losses). BE NO cuenta en WR.
    El WR solo mide trades con resultado claro. BE se muestra separado.
    """
    global daily_pnl, trades_today, wins_today, losses_today, be_today, last_action

    current_tickets = {p["ticket"] for p in open_positions_now}
    closed          = open_tickets_before - current_tickets
    if not closed:
        return set()

    pending = get_pending_trades()
    pending_by_ticket = {int(p["ticket"]): p for p in pending}
    now_utc = datetime.now(timezone.utc)
    from_time = now_utc - timedelta(hours=24)
    for ticket in closed:
        trade_info = pending_by_ticket.get(int(ticket))
        opened_at_str = trade_info.get("opened_at") if trade_info else None
        if not opened_at_str:
            continue
        try:
            opened_at = datetime.fromisoformat(opened_at_str)
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)
            from_time = min(from_time, opened_at - timedelta(hours=1))
        except Exception:
            continue
    max_lookback_days = int(getattr(cfg, "CLOSURE_HISTORY_LOOKBACK_DAYS", 30) or 30)
    earliest_allowed = now_utc - timedelta(days=max_lookback_days)
    if from_time < earliest_allowed:
        from_time = earliest_allowed

    history = mt5.history_deals_get(from_time, now_utc)
    if history is None:
        log.warning("[closures] No se pudo obtener historial de deals de MT5")
        return set(closed)

    closing_deals: dict = {}
    for d in history:
        if d.entry == 1:
            pid = int(d.position_id)
            if pid not in closing_deals or d.time > closing_deals[pid].time:
                closing_deals[pid] = d

    unresolved_tickets = set()
    for ticket in closed:
        _trail_stage.pop(ticket, None)
        trade_mode_cache.pop(ticket, None)
        _profit_candle_count.pop(ticket, None)
        _profit_candle_last_seen.pop(ticket, None)
        deal = closing_deals.get(ticket)

        if deal is None:
            for d in history:
                if int(d.position_id) == ticket and d.entry in (1, 3):
                    deal = d
                    if d.entry == 1:
                        break

        if deal is None:
            log.warning(f"[closures] No se encontró deal de cierre para ticket #{ticket}")
            unresolved_tickets.add(ticket)
            continue

        profit   = (float(deal.profit)
                    + float(getattr(deal, 'commission', 0) or 0)
                    + float(getattr(deal, 'swap', 0) or 0))
        symbol   = deal.symbol
        pips     = _calc_pips_instrument(deal, symbol)
        close_px = float(deal.price)
        direction = "BUY" if deal.type == 0 else "SELL"

        # ── FIX 12: clasificación con umbral más sensato ──
        # BE solo si el profit es absolutamente mínimo (spread/comisión)
        if profit > 1.0:
            result = "WIN"
        elif profit < -1.0:
            result = "LOSS"
        else:
            result = "BE"  # Ganancia/pérdida menor a $1 = breakeven

        trade_info    = pending_by_ticket.get(int(ticket))
        opened_at_str = trade_info["opened_at"] if trade_info else None
        open_price    = trade_info.get("open_price", close_px) if trade_info else close_px
        direction = trade_info.get("direction", direction) if trade_info else direction
        duration_min  = 0
        if opened_at_str:
            try:
                oa = datetime.fromisoformat(opened_at_str)
                duration_min = int(
                    (datetime.now(timezone.utc) - oa.replace(tzinfo=timezone.utc)
                    ).total_seconds() / 60
                )
            except Exception:
                pass

        update_trade_result(
            ticket=ticket, close_price=close_px,
            profit=profit, pips=pips,
            result=result, duration_min=duration_min,
        )

        # ── FIX 13: actualizar contadores — BE NO cuenta en WR ──
        daily_pnl    += profit
        if result == "WIN":
            wins_today   += 1
            trades_today += 1   # Solo WIN y LOSS aumentan trades_today
        elif result == "LOSS":
            losses_today += 1
            trades_today += 1   # Solo WIN y LOSS aumentan trades_today
        elif result == "BE":
            be_today += 1       # BE contado aparte, NO en trades_today

        daily_trades_log.append({
            "symbol":    symbol,
            "direction": direction,
            "result":    result,
            "profit":    profit,
            "pips":      pips,
            "duration":  duration_min,
        })

        notify_trade_closed(
            symbol=symbol, ticket=ticket, direction=direction,
            open_price=float(open_price), close_price=close_px,
            profit=profit, pips=pips, duration_min=duration_min,
            result=result, memory_learned=(result == "LOSS"),
        )
        last_action = f"Cerrada #{ticket} {symbol} {result} ${profit:+.2f}"
        log.info(
            f"[closures] ✅ #{ticket} {symbol} {direction} {result} "
            f"${profit:+.2f} | {pips:+.1f} pips | {duration_min}min"
        )
        # Limpiar estado de trail para el ticket cerrado
        _trail_stage.pop(ticket, None)
        be_activated.discard(ticket)
        tp_alerted.discard(ticket)
        tickets_en_memoria.discard(ticket)
    return unresolved_tickets


# ════════════════════════════════════════════════════════════════
#  RESUMEN DIARIO — INTERMEDIO Y EOD
# ════════════════════════════════════════════════════════════════

def maybe_send_daily_summary(balance: float, equity: float):
    global last_summary_date
    now   = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour == 17 and today != last_summary_date:
        notify_daily_summary(
            balance=balance, equity=equity, daily_profit=daily_pnl,
            trades_today=trades_today, wins=wins_today, losses=losses_today,
            memory_stats=get_memory_stats(),
        )
        last_summary_date = today
        log.info("[stats] 📊 Resumen intermedio 17:00 UTC enviado")


def maybe_send_eod_analysis(balance: float, equity: float):
    global last_eod_date, daily_pnl, trades_today, wins_today, losses_today
    global be_today, daily_trades_log

    now   = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour == 21 and today != last_eod_date:
        last_eod_date = today

        # FIX 13: WR = wins/(wins+losses) — sin BE
        wr     = (wins_today / trades_today * 100) if trades_today > 0 else 0.0
        profit = round(daily_pnl, 2)
        growth = ((balance - daily_start_balance) / daily_start_balance * 100) if daily_start_balance > 0 else 0

        loss_reasons = []
        if losses_today > wins_today:
            loss_trades = [t for t in daily_trades_log if t["result"] == "LOSS"]
            if loss_trades:
                symbols_lost = {}
                for t in loss_trades:
                    symbols_lost[t["symbol"]] = symbols_lost.get(t["symbol"], 0) + 1
                worst_sym = max(symbols_lost, key=symbols_lost.get)
                loss_reasons.append(f"Mayor concentración de pérdidas en {worst_sym} ({symbols_lost[worst_sym]} trades)")

            loss_durations = [t["duration"] for t in loss_trades if t["duration"] > 0]
            win_trades     = [t for t in daily_trades_log if t["result"] == "WIN"]
            win_durations  = [t["duration"] for t in win_trades if t["duration"] > 0]
            if loss_durations:
                avg_loss_dur = sum(loss_durations) / len(loss_durations)
                loss_reasons.append(f"Duración promedio en pérdidas: {avg_loss_dur:.0f} min")
            if win_durations:
                avg_win_dur = sum(win_durations) / len(win_durations)
                loss_reasons.append(f"Duración promedio en ganancias: {avg_win_dur:.0f} min")

        be_reason = ""
        if be_today > 0:
            be_pct = be_today / max(trades_today + be_today, 1) * 100
            be_reason = (
                f"⚡ {be_today} trades cerraron en breakeven ({be_pct:.0f}% del total). "
                "Si superan el 50%, el trailing está demasiado agresivo — "
                "considera aumentar be_atr_mult o reducir sl_atr_mult."
            )

        mem_stats = get_memory_stats()

        notify_eod_analysis(
            balance=balance, equity=equity,
            daily_profit=profit,
            trades_today=trades_today, wins=wins_today,
            losses=losses_today, be_count=be_today,
            win_rate=round(wr, 1),
            growth_pct=round(growth, 2),
            loss_reasons=loss_reasons,
            be_reason=be_reason,
            memory_stats=mem_stats,
            daily_trades=daily_trades_log,
        )

        log.info(
            f"[EOD] 📊 Análisis EOD enviado | "
            f"{trades_today} trades reales (excl. {be_today} BE) | "
            f"WR={wr:.0f}% | P&L=${profit:+.2f}"
        )

        # Resetear contadores
        daily_pnl    = 0.0
        trades_today = wins_today = losses_today = be_today = 0
        daily_trades_log.clear()


def _log_session_stats():
    """
    FIX 13: WR = wins/(wins+losses). BE excluido del denominador.
    Muestra las 3 métricas por separado: W / L / BE.
    """
    wr  = (wins_today / trades_today * 100) if trades_today > 0 else 0.0
    mem = get_memory_stats()
    log.info(
        f"[stats] Hoy: {trades_today} trades reales | "
        f"✅ {wins_today}W ❌ {losses_today}L ⚡ {be_today}BE (excluidos) | "
        f"WR={wr:.0f}% | P&L=${daily_pnl:+.2f} | "
        f"Mem: {mem['total']} trades WR={mem['win_rate']}% P&L=${mem['profit']:+.2f}"
    )


def _notify_calendar_pause_once(symbol: str, event, sym_cfg: dict):
    from modules.economic_calendar import CalendarEvent
    if isinstance(event, CalendarEvent) and event.event_id not in eco_calendar._notified_events:
        telegram_send(eco_calendar.format_for_telegram(symbol, event))
        eco_calendar.mark_notified(event)


def _notify_news_pause_once(symbol: str, reason: str, duration_min: int):
    global news_pause_notified
    now_ts = time.time()
    if now_ts - news_pause_notified.get(symbol, 0) >= NOTIF_COOLDOWN_SEC:
        notify_news_pause(symbol, reason, duration_min)
        news_pause_notified[symbol] = now_ts


def _build_memory_block_fingerprint(mem_check) -> str:
    regime = str(getattr(mem_check, "regime", "") or "").strip().upper() or "UNKNOWN"
    warning_msg = str(getattr(mem_check, "warning_msg", "") or "").strip()
    normalized = warning_msg.lower()

    if "modo calentamiento" in normalized:
        if "caótico" in normalized or "chaotic" in normalized:
            return f"WARMUP_CHAOTIC|{regime}"
        return f"WARMUP|{regime}"

    normalized = re.sub(r"\(\s*\d+\s*/\s*\d+\s*trades?\s*\)", "(n/trades)", normalized)
    normalized = re.sub(r"\b\d+(?:\.\d+)?%?\b", "#", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" |")
    return f"{regime}|{normalized[:160]}"


def _should_silence_memory_block_notification(mem_check) -> bool:
    similar_losses = int(getattr(mem_check, "similar_losses", 0) or 0)
    if similar_losses > 0:
        return False

    regime = str(getattr(mem_check, "regime", "") or "").strip().upper()
    warning_msg = str(getattr(mem_check, "warning_msg", "") or "").strip().lower()
    warmup_mode = bool(getattr(mem_check, "warmup_mode", False))

    return warmup_mode or regime == "CHAOTIC" or "modo calentamiento" in warning_msg


def _notify_memory_block_once(symbol: str, mem_check):
    global memory_block_notified
    if _should_silence_memory_block_notification(mem_check):
        return
    now_ts = time.time()
    cooldown_sec = max(
        int(getattr(cfg, "MEMORY_BLOCK_NOTIFY_COOLDOWN_SEC", NOTIF_COOLDOWN_SEC) or NOTIF_COOLDOWN_SEC),
        NOTIF_COOLDOWN_SEC,
    )
    previous_state = memory_block_notified.get(symbol, {})
    if isinstance(previous_state, dict):
        last_ts = float(previous_state.get("ts", 0) or 0.0)
        last_fingerprint = str(previous_state.get("fingerprint", "") or "")
    else:
        last_ts = float(previous_state or 0.0)
        last_fingerprint = ""

    current_fingerprint = _build_memory_block_fingerprint(mem_check)
    should_notify = (
        current_fingerprint != last_fingerprint
        or (now_ts - last_ts) >= cooldown_sec
    )
    if should_notify:
        notify_memory_block(symbol, "BUY/SELL", mem_check.similar_losses, mem_check.warning_msg)
        memory_block_notified[symbol] = {
            "ts": now_ts,
            "fingerprint": current_fingerprint,
        }


def _notify_equity_guard_once(symbol: str, equity: float, equity_floor: float, min_pct: float):
    global equity_guard_notified
    now_ts = time.time()
    if now_ts - equity_guard_notified.get(symbol, 0) >= NOTIF_COOLDOWN_SEC:
        telegram_send(
            "\n".join([
                f"🛡 <b>EQUITY GUARD ACTIVADO — {symbol}</b>",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                f"📉 Equity actual: <code>${equity:,.2f}</code>",
                f"🧱 Piso requerido: <code>${equity_floor:,.2f}</code> ({min_pct:.0f}% balance)",
                "⛔ Nuevas entradas bloqueadas temporalmente para este símbolo.",
                f"⏰ <code>{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC</code>",
            ])
        )
        equity_guard_notified[symbol] = now_ts


def _notify_daily_loss_guard_once(daily_pnl_now: float, balance_now: float, max_daily_loss_pct: float):
    global daily_loss_guard_notified
    now_ts = time.time()
    if now_ts - daily_loss_guard_notified >= NOTIF_COOLDOWN_SEC:
        daily_loss_cap = balance_now * max_daily_loss_pct if balance_now and balance_now > 0 else 0.0
        telegram_send(
            "\n".join([
                "🛑 <b>DAILY LOSS GUARD ACTIVADO</b>",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                f"📉 P&amp;L diario: <code>${daily_pnl_now:,.2f}</code>",
                f"🧱 Límite diario: <code>-${daily_loss_cap:,.2f}</code> ({max_daily_loss_pct*100:.1f}% balance)",
                "⛔ Nuevas entradas pausadas temporalmente en todos los símbolos.",
                f"⏰ <code>{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC</code>",
            ])
        )
        daily_loss_guard_notified = now_ts


# ════════════════════════════════════════════════════════════════
#  PROCESAMIENTO POR SÍMBOLO
# ════════════════════════════════════════════════════════════════

def _process_symbol(
    symbol: str, sym_cfg: dict,
    open_positions: list, balance: float, equity: float,
):
    global last_action, ind_cache, sr_cache, symbol_status_cache
    global shared_news_cache, shared_news_last_update

    equity_guard_min_pct = float(getattr(cfg, "EQUITY_GUARD_MIN_PCT", 70.0))
    equity_floor = balance * (equity_guard_min_pct / 100.0) if balance and balance > 0 else 0.0
    if equity_floor > 0 and equity < equity_floor:
        msg = (
            f"🛡 Equity guard: {equity:,.2f} < {equity_floor:,.2f} "
            f"({equity_guard_min_pct:.0f}% bal)"
        )
        _set_symbol_status(symbol, msg[:48])
        last_action = f"{msg} {symbol}"
        log.warning(f"[{symbol}] {msg} — nuevas entradas bloqueadas")
        _notify_equity_guard_once(symbol, equity, equity_floor, equity_guard_min_pct)
        return
    else:
        equity_guard_notified.pop(symbol, None)

    # FIX 15: los límites de entrada no deben impedir calcular indicadores.
    # Si no, el dashboard queda en "Calculando indicadores..." y el trailing
    # stop deja de gestionarse justo cuando hay posiciones abiertas.
    tradeable_basic, motivo_basic = is_market_tradeable(symbol, sym_cfg)
    if not tradeable_basic:
        _set_symbol_status(symbol, motivo_basic)
        return

    live_sym_positions = mt5.positions_get(symbol=symbol)
    if live_sym_positions is None:
        live_sym_positions = []
    bot_sym_positions = [p for p in live_sym_positions if p.magic == cfg.MAGIC_NUMBER]
    has_bot_position = len(bot_sym_positions) > 0
    max_per_symbol = getattr(cfg, "MAX_OPEN_PER_SYMBOL", 1)

    now_ts = time.time()
    last_open = last_trade_time.get(symbol, 0)
    cooldown_active = (now_ts - last_open) < SYMBOL_COOLDOWN_SEC
    cooldown_remaining = int(max(0, SYMBOL_COOLDOWN_SEC - (now_ts - last_open)))
    total_open = get_open_positions_count_realtime()

    df_entry = get_candles(symbol, cfg.TF_ENTRY, TF_CANDLES.get(cfg.TF_ENTRY, 300))
    df_h1    = get_candles(symbol, cfg.TF_TREND, TF_CANDLES.get(cfg.TF_TREND, 150))
    if df_entry is None or df_h1 is None or len(df_entry) < 60:
        _set_symbol_status(symbol, "⚠️ Datos insuficientes de MT5")
        return

    _now_h1 = time.time()
    _cached_h1 = _h1_cache.get(symbol)
    if _cached_h1 is not None and (_now_h1 - _cached_h1.get("timestamp", 0)) < H1_CACHE_TTL_SEC:
        # Reuse cached H1 indicators; update the fast-changing price fields in place.
        ind = _cached_h1["indicators"]
        ind["price"] = round(float(df_h1["close"].iloc[-1]), 4)
        if df_entry is not None and len(df_entry) > 0:
            ind["entry_price"] = round(float(df_entry["close"].iloc[-1]), 4)
            if "time" in df_entry.columns:
                ind["entry_candle_time"] = pd.Timestamp(df_entry["time"].iloc[-1]).isoformat()
        log.debug(f"[{symbol}] H1 cache hit — indicadores reutilizados")
    else:
        ind = compute_all(df_h1, symbol, sym_cfg, df_entry=df_entry)
        _h1_cache[symbol] = {"indicators": ind, "timestamp": _now_h1}
    ind_cache[symbol] = ind
    symbol_detail_cache[symbol] = {
        "enhanced_regime":   ind.get("enhanced_regime", "UNKNOWN"),
        "regime_confidence": ind.get("regime_confidence", "LOW"),
        "z_score":           ind.get("zscore_returns", {}).get("z_score", None),
    }

    if has_bot_position:
        _set_symbol_status(symbol, f"📂 Posiciones abiertas ({len(bot_sym_positions)}/{max_per_symbol})")

    if cooldown_active:
        _set_symbol_status(symbol, f"⏳ Cooldown activo — {cooldown_remaining}s")
        return

    if len(bot_sym_positions) >= max_per_symbol:
        _set_symbol_status(symbol, f"📂 Límite por símbolo alcanzado ({len(bot_sym_positions)}/{max_per_symbol})")
        return

    if total_open >= cfg.MAX_OPEN_TRADES:
        _set_symbol_status(symbol, f"⛔ Límite global alcanzado ({total_open}/{cfg.MAX_OPEN_TRADES})")
        return

    atr_now   = ind.get("atr", 0)
    price_now = ind.get("price", 0)
    tradeable_atr, motivo_atr = is_market_tradeable(symbol, sym_cfg, atr_now, price_now)
    if not tradeable_atr:
        log.info(f"[{symbol}] {motivo_atr}")
        last_action = f"📉 {motivo_atr[:55]}"
        _set_symbol_status(symbol, motivo_atr)
        return

    dfs_by_tf = {}
    for tf in sym_cfg.get("sr_timeframes", ["H1"]):
        df_tf = get_candles(symbol, tf, TF_CANDLES.get(tf, 150))
        if df_tf is not None:
            dfs_by_tf[tf] = df_tf
    sr_ctx = build_sr_context(dfs_by_tf, ind["price"], symbol, sym_cfg)
    sr_cache[symbol] = sr_ctx

    pause_cal, cal_reason, cal_event, already_cal_notified = \
        eco_calendar.should_pause_symbol(symbol, sym_cfg)
    if pause_cal:
        last_action = f"📅 Cal: {cal_reason[:55]}"
        _set_symbol_status(symbol, f"📅 {cal_reason[:48]}")
        if not already_cal_notified and cal_event is not None:
            _notify_calendar_pause_once(symbol, cal_event, sym_cfg)
        log.info(f"[{symbol}] ⏸ Cal: {cal_reason[:60]}")
        return

    cal_events_nearby = eco_calendar.get_events_for_symbol(symbol, sym_cfg, minutes_ahead=120)

    now_ts_news = time.time()
    if shared_news_cache is None or (now_ts_news - shared_news_last_update) > cfg.NEWS_REFRESH_MIN * 60:
        merged_topics = ",".join(sorted({
            topic.strip()
            for cfg_item in cfg.SYMBOLS.values()
            for topic in str(cfg_item.get("news_topics", "economy_monetary,economy_macro")).split(",")
            if topic.strip()
        }))
        shared_news_cache = build_shared_news_context(merged_topics)
        shared_news_last_update = now_ts_news

    if symbol not in news_cache or (now_ts_news - news_last_update.get(symbol, 0)) > cfg.NEWS_REFRESH_MIN * 60:
        news_ctx = derive_symbol_news_context(symbol, sym_cfg, shared_news_cache)
        news_cache[symbol] = news_ctx
        news_last_update[symbol] = now_ts_news
    else:
        news_ctx = news_cache[symbol]

    if news_ctx.should_pause:
        last_action = f"📰 News pausa {symbol}"
        _set_symbol_status(symbol, f"📰 {news_ctx.pause_reason[:48]}")
        _notify_news_pause_once(symbol, news_ctx.pause_reason, cfg.NEWS_PAUSE_MINUTES_BEFORE)
        return
    else:
        news_pause_notified.pop(symbol, None)

    # ── Store context for deterministic decision engine ───────────
    if symbol not in _symbol_state:
        _symbol_state[symbol] = {}
    _symbol_state[symbol]["last_sr_context"] = (
        sr_ctx.__dict__ if hasattr(sr_ctx, "__dict__") else sr_ctx
    )
    _symbol_state[symbol]["last_news_context"] = news_ctx

    hurst_val = ind.get("hurst", 0.5)
    min_hurst = sym_cfg.get("min_hurst", 0.40)
    hurst_allowed, hurst_reason = _should_allow_low_hurst_scalp(ind, sr_ctx, sym_cfg)
    if not hurst_allowed:
        if getattr(cfg, "SCALPING_ONLY", False):
            # In scalping mode, low Hurst is a score penalty, not a hard block
            hurst_penalty = round((min_hurst - hurst_val) * 5, 2)
            log.info(f"[{symbol}] Hurst {hurst_val:.3f} < {min_hurst:.3f} — penalty={hurst_penalty:.1f} (scalping mode)")
            _set_symbol_status(symbol, f"📉 Hurst scalp penalizado {hurst_val:.2f}")
            # Propagate penalty into indicators so decision_engine can apply it
            ind["hurst_penalty"] = hurst_penalty
            _symbol_state[symbol]["hurst_penalty"] = hurst_penalty
            # Continue processing (don't skip)
        else:
            log.info(f"[{symbol}] Hurst {hurst_val:.3f} < {min_hurst:.3f} — HOLD")
            last_action = f"📊 Hurst bajo {symbol} ({hurst_val:.2f}<{min_hurst:.2f})"
            _set_symbol_status(symbol, f"📊 Hurst bajo {hurst_val:.2f}<{min_hurst:.2f}")
            return
    else:
        ind["hurst_penalty"] = 0.0
        _symbol_state[symbol]["hurst_penalty"] = 0.0

    # Store updated indicators (with hurst_penalty) for decision engine
    _symbol_state[symbol]["last_indicators"] = ind
    if hurst_val < min_hurst:
        log.info(f"[{symbol}] Hurst {hurst_val:.3f} < {min_hurst:.3f} pero se permite scalp â€” {hurst_reason}")
        _set_symbol_status(symbol, f"ðŸ“‰ Hurst scalp OK {hurst_val:.2f}")

    h1_trend = ind.get("h1_trend", "LATERAL")
    candle_stamp = _get_last_candle_stamp(df_entry)
    if h1_trend in ("BAJISTA_FUERTE", "BAJISTA"):
        mem_direction = "SELL"
        _symbol_state[symbol]["mem_direction"] = mem_direction
    elif h1_trend in ("ALCISTA_FUERTE", "ALCISTA"):
        mem_direction = "BUY"
        _symbol_state[symbol]["mem_direction"] = mem_direction
    else:
        # LATERAL_ALCISTA, LATERAL_BAJISTA, LATERAL → evaluar ambas direcciones
        feat_buy  = build_features(symbol, "BUY",  ind, sr_ctx, news_ctx, sym_cfg)
        feat_sell = build_features(symbol, "SELL", ind, sr_ctx, news_ctx, sym_cfg)
        mem_buy   = check_memory(feat_buy,  symbol, "BUY",  sym_cfg)
        mem_sell  = check_memory(feat_sell, symbol, "SELL", sym_cfg)
        setup_buy = derive_setup_id(ind, "BUY")
        setup_sell = derive_setup_id(ind, "SELL")
        session_id = derive_session_from_ind(ind)
        score_buy = evaluate_scorecard(symbol, setup_buy, session_id, mem_buy.regime)
        score_sell = evaluate_scorecard(symbol, setup_sell, session_id, mem_sell.regime)
        policy_buy = evaluate_policy(symbol, "BUY", setup_buy, session_id, mem_buy.regime)
        policy_sell = evaluate_policy(symbol, "SELL", setup_sell, session_id, mem_sell.regime)
        if mem_buy.should_block and mem_sell.should_block:
            last_action = f"🧠 Memoria bloqueó {symbol}"
            _set_symbol_status(symbol, "🧠 Memoria bloqueó BUY y SELL")
            _notify_memory_block_once(symbol, mem_buy)
            return
        if score_buy.should_block and score_sell.should_block:
            msg = "🧮 Scorecard bloqueó BUY y SELL (historial pobre)"
            last_action = f"{msg} {symbol}"
            _set_symbol_status(symbol, msg[:48])
            log.info(
                f"[{symbol}] {msg} | BUY={score_buy.reason} | SELL={score_sell.reason}"
            )
            return
        candidates = [
            ("BUY", feat_buy, mem_buy, score_buy, policy_buy),
            ("SELL", feat_sell, mem_sell, score_sell, policy_sell),
        ]
        viable = [c for c in candidates if not c[2].should_block and not c[3].should_block and not c[4].should_block]
        if not viable:
            msg = "🧮 Policy/Scorecard bloqueó candidatos BUY y SELL"
            _set_symbol_status(symbol, msg[:48])
            last_action = f"{msg} {symbol}"
            log.info(
                f"[{symbol}] {msg} | "
                f"BUY={policy_buy.reason} | SELL={policy_sell.reason}"
            )
            return
        candidate_payloads = {}
        ready_actions = []
        for action, features, mem_check, scorecard, policy in viable:
            candidate_ok, candidate_reason, trade_plan = _is_groq_candidate_ready(
                symbol, action, ind, sr_ctx, sym_cfg
            )
            candidate_payloads[action] = {
                "features": features,
                "mem_check": mem_check,
                "scorecard": scorecard,
                "policy": policy,
                "trade_plan": trade_plan,
                "candidate_ok": candidate_ok,
                "candidate_reason": candidate_reason,
            }
            if candidate_ok:
                ready_actions.append(action)

        if not ready_actions:
            _bump_groq_metric("skipped_by_gate_total")
            reasons = " | ".join(
                f"{action}:{payload['candidate_reason']}" for action, payload in candidate_payloads.items()
            )
            _set_symbol_status(symbol, reasons[:48])
            last_action = f"🧪 Lateral sin candidato listo {symbol}"
            log.info(f"[{symbol}] Lateral sin candidato listo — {reasons}")
            return

        cache_key = _build_groq_cache_key(symbol, "LATERAL", candle_stamp, ind) + "|" + ",".join(sorted(ready_actions))
        # Store the first viable direction for the deterministic decision engine
        _symbol_state[symbol]["mem_direction"] = ready_actions[0]
        context = build_lateral_context(
            symbol=symbol,
            ind=ind,
            sr_ctx=sr_ctx,
            news_ctx=news_ctx,
            sym_cfg=sym_cfg,
            cal_events=cal_events_nearby,
            candidate_payloads=candidate_payloads,
        )
        decision = ask_groq(symbol, context, sym_cfg, cache_key=cache_key)
        base_payload = candidate_payloads[ready_actions[0]]
        _execute_decision(
            symbol, sym_cfg, decision, ind, sr_ctx, news_ctx,
            base_payload["mem_check"], base_payload["features"], balance, df_entry,
            candidate_payloads=candidate_payloads,
        )
        return

    features  = build_features(symbol, mem_direction, ind, sr_ctx, news_ctx, sym_cfg)
    mem_check = check_memory(features, symbol, mem_direction, sym_cfg)
    setup_id  = derive_setup_id(ind, mem_direction)
    session_id = derive_session_from_ind(ind)
    scorecard = evaluate_scorecard(
        symbol=symbol,
        setup_id=setup_id,
        session=session_id,
        regime=mem_check.regime,
    )
    policy = evaluate_policy(
        symbol=symbol,
        direction=mem_direction,
        setup_id=setup_id,
        session=session_id,
        regime=mem_check.regime,
    )

    if mem_check.should_block:
        last_action = f"🧠 Memoria bloqueó {symbol}"
        _set_symbol_status(symbol, f"🧠 {mem_check.warning_msg[:48]}")
        _notify_memory_block_once(symbol, mem_check)
        return
    if scorecard.should_block:
        msg = (
            f"🧮 Scorecard vetó {mem_direction} "
            f"(WR={scorecard.win_rate:.1f}% n={scorecard.sample_size})"
        )
        _set_symbol_status(symbol, msg[:48])
        last_action = f"{msg} {symbol}"
        log.info(f"[{symbol}] {msg} | {scorecard.reason}")
        return

    if policy.should_block:
        msg = (
            f"📉 Policy vetó {mem_direction} "
            f"(score={policy.policy_score:.3f} n={policy.sample_size})"
        )
        _set_symbol_status(symbol, msg[:48])
        last_action = f"{msg} {symbol}"
        log.info(f"[{symbol}] {msg} | {policy.reason}")
        return

    # ── FASE 3: Confluence Gate pre-Groq (directional path) ──────
    if _apply_confluence_gate_pre(symbol, ind, mem_direction):
        return

    candidate_ok, candidate_reason, trade_plan = _is_groq_candidate_ready(
        symbol, mem_direction, ind, sr_ctx, sym_cfg
    )
    if not candidate_ok:
        _bump_groq_metric("skipped_by_gate_total")
        _set_symbol_status(symbol, candidate_reason[:48])
        last_action = f"{candidate_reason} {symbol}"
        log.info(f"[{symbol}] {candidate_reason} — Groq no consultado")
        return

    cache_key = _build_groq_cache_key(symbol, mem_direction, candle_stamp, ind)
    context  = build_context(
        symbol, ind, sr_ctx, news_ctx, mem_check, sym_cfg,
        cal_events_nearby, scorecard, policy, trade_plan,
    )
    decision = ask_groq(symbol, context, sym_cfg, cache_key=cache_key)
    _execute_decision(symbol, sym_cfg, decision, ind, sr_ctx, news_ctx,
                      mem_check, features, balance, df_entry)


def _apply_confluence_gate_pre(symbol: str, ind: dict, direction: str) -> bool:
    """
    FASE 3 — Confluence Hard Gate (pre-Groq).

    Bloquea la llamada a Groq si la Confluencia de 3 Pilares contradice
    fuertemente la dirección esperada. Ahorra una llamada API y aplica el
    equivalente en código a REGLA 15 del system prompt.

    Umbral: conf_total < -(CONFLUENCE_MIN_SCORE × 2) para BUY
            conf_total > +(CONFLUENCE_MIN_SCORE × 2) para SELL

    Con los defaults (CONFLUENCE_MIN_SCORE=0.3), el umbral = ±0.6 sobre
    una escala de -3 a +3. Solo se bloquea cuando los 3 pilares apuntan
    claramente contra la dirección (no en neutrales).

    Retorna True si se debe bloquear (llamar 'return' en _process_symbol).
    """
    global last_action

    conf_total = ind.get("confluence", {}).get("total", 0.0)
    hard_thresh = (
        getattr(cfg, "CONFLUENCE_MIN_SCORE", 0.3)
        * getattr(cfg, "CONFLUENCE_HARD_GATE_MULT", 2)
    )

    if direction == "BUY" and conf_total < -hard_thresh:
        msg = f"⚡ Conf veta BUY ({conf_total:+.2f}) — pilares bajistas"
        _set_symbol_status(symbol, msg[:48])
        last_action = f"{msg} {symbol}"
        log.info(f"[{symbol}] {msg} — Groq no consultado")
        return True

    if direction == "SELL" and conf_total > hard_thresh:
        msg = f"⚡ Conf veta SELL ({conf_total:+.2f}) — pilares alcistas"
        _set_symbol_status(symbol, msg[:48])
        last_action = f"{msg} {symbol}"
        log.info(f"[{symbol}] {msg} — Groq no consultado")
        return True

    return False


def _execute_decision(
    symbol, sym_cfg, decision, ind, sr_ctx, news_ctx,
    mem_check, features, balance, df_entry,
    candidate_payloads=None,
):
    """Valida la decisión de Groq y aplica filtro anti-contra-tendencia."""
    global last_action

    if decision is None:
        log.warning(f"[{symbol}] Groq no respondió")
        _set_symbol_status(symbol, "🤖 Groq no respondió")
        return

    action     = decision.get("decision", "HOLD")
    confidence = int(decision.get("confidence", 0))
    reason     = decision.get("reason", "")

    if action in ("BUY", "SELL") and candidate_payloads is not None:
        payload = candidate_payloads.get(action)
        if payload is None or not payload.get("candidate_ok", False):
            msg = f"🧪 Candidato {action} no pasó gates internos"
            _set_symbol_status(symbol, msg[:48])
            last_action = f"{msg} {symbol}"
            log.info(f"[{symbol}] {msg} — decisión forzada a HOLD")
            return

    if candidate_payloads and action in candidate_payloads:
        payload = candidate_payloads[action]
        mem_check = payload["mem_check"]
        features = payload["features"]

    log.info(f"[{symbol}] 🤖 {action} conf={confidence} | {reason[:80]}")

    if action in ("BUY", "SELL"):
        setup_id = derive_setup_id(ind, action)
        session_id = derive_session_from_ind(ind)
        scorecard = evaluate_scorecard(
            symbol=symbol,
            setup_id=setup_id,
            session=session_id,
            regime=mem_check.regime,
        )
        policy = evaluate_policy(
            symbol=symbol,
            direction=action,
            setup_id=setup_id,
            session=session_id,
            regime=mem_check.regime,
        )
        if scorecard.should_block:
            msg = (
                f"🧮 Scorecard vetó {action} "
                f"(WR={scorecard.win_rate:.1f}% n={scorecard.sample_size})"
            )
            last_action = f"{msg} {symbol}"
            _set_symbol_status(symbol, msg[:48])
            log.info(f"[{symbol}] {msg} | {scorecard.reason}")
            return
        if policy.should_block:
            msg = (
                f"📉 Policy vetó {action} "
                f"(score={policy.policy_score:.3f} n={policy.sample_size})"
            )
            last_action = f"{msg} {symbol}"
            _set_symbol_status(symbol, msg[:48])
            log.info(f"[{symbol}] {msg} | {policy.reason}")
            return
    else:
        scorecard = None
        policy = None

    min_conf = sym_cfg.get("min_confidence", 6)
    if (
        scorecard is not None
        and scorecard.sample_size >= scorecard.min_sample
        and scorecard.win_rate < (scorecard.min_win_rate + 5.0)
    ):
        min_conf += int(getattr(cfg, "SCORECARD_MIN_CONF_BONUS", 1))
    if (
        policy is not None
        and policy.sample_size >= int(getattr(cfg, "POLICY_MIN_SAMPLE", 10))
        and policy.policy_score < (float(getattr(cfg, "POLICY_MIN_SCORE", 0.45)) + 0.1)
    ):
        min_conf += int(getattr(cfg, "POLICY_MIN_CONF_BONUS", 1))
    if action == "HOLD" or confidence < min_conf:
        last_action = f"HOLD {symbol} conf={confidence} (min={min_conf})"
        _set_symbol_status(symbol, f"🤖 {action} conf={confidence}/{min_conf}")
        return

    # ── FIX 14: Filtro anti-contra-tendencia ─────────────────────
    passes_filter, filter_reason = _passes_direction_filter(action, ind)
    if not passes_filter:
        log.info(f"[{symbol}] 🚫 Anti-contratendencia: {filter_reason}")
        last_action = f"🚫 {filter_reason[:60]}"
        _set_symbol_status(symbol, f"🚫 {filter_reason[:48]}")
        return

    # ── FASE 3: Confluence Hard Gate (safety net post-Groq) ─────
    # Veta la orden si la respuesta de Groq contradice la confluencia global.
    # Captura el caso LATERAL donde la dirección no se conocía antes de Groq.
    conf_total  = ind.get("confluence", {}).get("total", 0.0)
    conf_thresh = (
        getattr(cfg, "CONFLUENCE_MIN_SCORE", 0.3)
        * getattr(cfg, "CONFLUENCE_HARD_GATE_MULT", 2)
    )
    if action == "BUY" and conf_total < -conf_thresh:
        msg = f"⚡ Conf veta BUY post-Groq ({conf_total:+.2f})"
        log.info(f"[{symbol}] {msg}")
        last_action = f"{msg} {symbol}"
        _set_symbol_status(symbol, msg[:48])
        return
    if action == "SELL" and conf_total > conf_thresh:
        msg = f"⚡ Conf veta SELL post-Groq ({conf_total:+.2f})"
        log.info(f"[{symbol}] {msg}")
        last_action = f"{msg} {symbol}"
        _set_symbol_status(symbol, msg[:48])
        return

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        _set_symbol_status(symbol, "⚠️ Tick no disponible")
        return
    price  = tick.ask if action == "BUY" else tick.bid
    min_hurst = float(sym_cfg.get("min_hurst", 0.40) or 0.40)
    hurst_val = float(ind.get("hurst", 0.5) or 0.5)
    is_scalp_trade = bool(getattr(cfg, "SCALPING_ONLY", False)) or bool(hurst_val < min_hurst)
    if is_scalp_trade:
        # Scalping: usar ATR del TF de entrada (M1) para SL/TP proporcionales
        atr_entry = ind.get("atr_entry", ind["atr"])
        scalp_sl_mult = float(getattr(cfg, "SCALPING_SL_ATR_MULT", 3.0))
        scalp_tp_mult = float(getattr(cfg, "SCALPING_TP_ATR_MULT", 6.0))
        if action == "BUY":
            sl = round(price - scalp_sl_mult * atr_entry, 5)
            tp = round(price + scalp_tp_mult * atr_entry, 5)
        else:
            sl = round(price + scalp_sl_mult * atr_entry, 5)
            tp = round(price - scalp_tp_mult * atr_entry, 5)
    else:
        sl, tp = calc_sl_tp(action, price, ind["atr"], sym_cfg)

    rr = get_rr(price, sl, tp)
    min_rr = _get_entry_min_rr(sym_cfg)
    if not is_rr_valid(price, sl, tp, min_rr=min_rr):
        last_action = f"R:R inválido {symbol} ({rr:.2f})"
        _set_symbol_status(symbol, f"⚖️ R:R inválido ({rr:.2f})")
        return

    # ── FASE 7: Portfolio Risk — correlación entre activos ───────
    # Calcula riesgo efectivo del portafolio incluyendo el trade propuesto.
    # Bloquea si la exposición correlacionada supera el umbral.
    all_positions = mt5.positions_get()
    if all_positions is None:
        all_positions = []
    portfolio_risk_pct, risk_blocked, risk_reason = get_effective_portfolio_risk(
        open_positions=all_positions,
        new_symbol=symbol,
        new_direction=action,
    )
    if risk_blocked:
        msg = f"🔗 Riesgo portafolio {portfolio_risk_pct:.1f}%"
        last_action = f"{msg} {symbol}"
        _set_symbol_status(symbol, msg[:48])
        log.info(f"[{symbol}] {risk_reason}")
        return

    sym_info = mt5.symbol_info(symbol)
    point    = sym_info.point if sym_info else 0.00001
    sl_pips  = abs(price - sl) / point

    # ── FASE 2: Kelly position sizing ────────────────────────────
    # Usa Kelly fraccionado cuando hay suficientes trades históricos.
    # Si no, vuelve automáticamente al sizing estándar.
    mem_stats  = get_memory_stats(symbol)
    sym_trades = mem_stats.get("total", 0)
    win_rate   = mem_stats.get("win_rate", 0.0) / 100.0  # convertir % → fracción
    kelly_min_trades = getattr(cfg, "KELLY_MIN_TRADES", 30)
    kelly_active     = sym_trades >= kelly_min_trades and win_rate > 0
    vol = get_lot_size_kelly(
        balance=balance,
        sl_pips=sl_pips,
        symbol=symbol,
        win_rate=win_rate,
        avg_rr=rr,
        n_trades=sym_trades,
    )

    # ── Modo Calentamiento: reducir lote durante cold start ──────
    if getattr(mem_check, "warmup_mode", False):
        warmup_factor = float(getattr(cfg, "WARMUP_LOT_FACTOR", 0.5))
        original_vol  = vol
        vol = max(0.01, round(vol * warmup_factor, 2))
        log.info(
            f"[warmup] {symbol}: lote reducido {original_vol} → {vol} "
            f"(factor={warmup_factor}, trades={sym_trades})"
        )

    # ── FIX v7.0: GATE de profit mínimo esperado ─────────────────
    # Verifica que el lot size calculado + TP produzcan al menos
    # MIN_EXPECTED_PROFIT_USD. Evita trades de $0.50 que no cubren
    # el spread ni justifican el riesgo en scalping.
    min_profit_usd = float(getattr(cfg, "MIN_EXPECTED_PROFIT_USD", 5.0))
    if min_profit_usd > 0 and sym_info is not None:
        _tick_val = float(getattr(sym_info, "trade_tick_value", 0) or 0)
        _tick_sz  = float(getattr(sym_info, "trade_tick_size",  0) or 0)
        if _tick_val > 0 and _tick_sz > 0:
            _tp_diff = abs(tp - price)
            _expected_profit = (_tp_diff / _tick_sz) * _tick_val * vol
            if _expected_profit < min_profit_usd:
                msg = (
                    f"💵 Profit esperado ${_expected_profit:.2f} "
                    f"< ${min_profit_usd:.0f} mín (lote={vol})"
                )
                last_action = f"{msg} {symbol}"
                _set_symbol_status(symbol, f"💵 Profit ${_expected_profit:.2f}<${min_profit_usd:.0f}")
                log.info(f"[{symbol}] {msg} — HOLD (aumentar balance o lote mínimo)")
                return

    hilbert  = ind.get("hilbert", {})
    h_signal = hilbert.get("signal", "NEUTRAL")
    if action == "BUY"  and h_signal == "LOCAL_MAX":
        last_action = f"🌀 Hilbert bloqueó BUY {symbol}"
        _set_symbol_status(symbol, "🌀 Hilbert bloqueó BUY")
        return
    if action == "SELL" and h_signal == "LOCAL_MIN":
        last_action = f"🌀 Hilbert bloqueó SELL {symbol}"
        _set_symbol_status(symbol, "🌀 Hilbert bloqueó SELL")
        return

    ticket = open_order(symbol, action, sl, tp, vol)
    if ticket is None:
        last_action = f"❌ Orden fallida {symbol}"
        _set_symbol_status(symbol, "❌ Orden fallida")
        return

    # Registrar timestamp de apertura para cooldown
    last_trade_time[symbol] = time.time()

    # Inicializar estado de trail para este ticket
    _trail_stage[ticket] = 0
    _profit_candle_count[ticket] = 0
    _profit_candle_last_seen[ticket] = None
    trade_mode = get_adaptive_trail_params(sym_cfg, action)
    trade_mode["scalp_mode"] = is_scalp_trade
    trade_mode_cache[ticket] = trade_mode

    direction_features = build_features(symbol, action, ind, sr_ctx, news_ctx, sym_cfg)
    setup_id = derive_setup_id(ind, action)
    session_id = derive_session_from_ind(ind)
    risk_amount = balance * float(getattr(cfg, "RISK_PER_TRADE", 0.01))
    save_trade(
        ticket=ticket, symbol=symbol, direction=action,
        open_price=price, volume=vol,
        features=direction_features, reason_gemini=reason,
        hilbert_signal=h_signal, hurst_val=ind.get("hurst", 0.5),
        setup_id=setup_id,
        setup_score=ind.get("confluence", {}).get("total", 0.0),
        session=session_id,
        regime=mem_check.regime,
        risk_amount=risk_amount,
        sl=sl,
        tp=tp,
    )
    tickets_en_memoria.add(ticket)

    notify_trade_opened(
        symbol=symbol, direction=action, price=price, sl=sl, tp=tp,
        volume=vol, atr=ind["atr"], rr=rr, reason=reason,
        ind=ind, hilbert=hilbert,
        hurst=ind.get("hurst", 0.5),
        fisher=ind.get("fisher", 0),
        memory_warn=mem_check.warning_msg,
        kelly_active=kelly_active,
    )

    try:
        from modules.chart_generator import (
            generate_trade_chart, send_chart_to_telegram, build_telegram_caption,
        )
        image_bytes = generate_trade_chart(
            symbol=symbol, direction=action, df=df_entry,
            ind=ind, sr_ctx=sr_ctx, price=price, sl=sl, tp=tp,
            rr=rr, reason=reason, sym_cfg=sym_cfg, n_candles=80,
        )
        if image_bytes:
            caption = build_telegram_caption(
                symbol=symbol, direction=action, price=price, sl=sl, tp=tp,
                rr=rr, volume=vol, ind=ind, hilbert=hilbert,
                hurst=ind.get("hurst", 0.5), fisher=ind.get("fisher", 0),
                reason=reason, sym_cfg=sym_cfg,
            )
            send_chart_to_telegram(
                image_bytes=image_bytes, caption=caption,
                token=cfg.TELEGRAM_TOKEN, chat_id=cfg.TELEGRAM_CHAT_ID,
            )
    except Exception as chart_err:
        log.warning(f"[{symbol}] Gráfico: {chart_err}")

    last_action = f"✅ {action} {symbol} #{ticket} conf={confidence}"
    _set_symbol_status(symbol, f"✅ {action} #{ticket} conf={confidence}")
    log.info(f"[{symbol}] ✅ #{ticket} {action} p={price} sl={sl} tp={tp}")


# ════════════════════════════════════════════════════════════════
#  LOOP PRINCIPAL
# ════════════════════════════════════════════════════════════════

def run():
    global cycle_count, last_action, ind_cache, sr_cache, symbol_status_cache, daily_start_balance
    global daily_loss_guard_notified
    global pending_closure_tickets

    log.info("🚀 ZAR ULTIMATE BOT v6.5 — TRAILING STOP + WR FIX — Iniciando...")
    init_db()

    if not conectar_mt5():
        log.critical("No se pudo conectar a MT5.")
        sys.exit(1)

    balance, equity = get_account_info()
    daily_start_balance = balance or 0.0

    log.info("[calendar] 📅 Cargando calendario económico...")
    eco_calendar.refresh(force=True)

    mem_stats = get_memory_stats()
    notify_bot_started(balance, equity, mem_stats, list(cfg.SYMBOLS.keys()))
    web_host = str(getattr(cfg, "WEB_DASHBOARD_HOST", "127.0.0.1"))
    web_port = int(getattr(cfg, "WEB_DASHBOARD_PORT", 8765))
    start_web_dashboard(_get_web_status_snapshot, host=web_host, port=web_port, logger=log)
    _update_web_status_snapshot(balance, equity, [])
    log.info(
        f"✅ Bot v6.5 iniciado | Balance=${balance:,.2f} | "
        f"{len(cfg.SYMBOLS)} símbolos | Modo 24/5 | "
        f"Cooldown={SYMBOL_COOLDOWN_SEC}s | "
        f"Trail: proporcional por TP progress | Web: http://{web_host}:{web_port}"
    )

    for pending in get_pending_trades():
        tickets_en_memoria.add(pending["ticket"])

    while True:
        cycle_count += 1
        try:
            ind_cache = {}
            sr_cache = {}
            symbol_status_cache = {}

            balance, equity = get_account_info()
            if balance is None:
                log.warning("[main] Sin balance — reconectando...")
                if not conectar_mt5():
                    time.sleep(30); continue

            maybe_send_daily_summary(balance, equity)
            maybe_send_eod_analysis(balance, equity)

            if not is_daily_loss_ok(daily_pnl, balance):
                log.warning("[main] ⛔ Límite diario alcanzado")
                last_action = "⛔ Límite diario alcanzado"
                _notify_daily_loss_guard_once(daily_pnl, balance, float(getattr(cfg, "MAX_DAILY_LOSS", 0.05)))

                open_positions = get_open_positions()
                for pos in open_positions:
                    symbol = pos.get("symbol", "")
                    sym_cfg_data = cfg.SYMBOLS.get(symbol)
                    if not symbol or sym_cfg_data is None or symbol in ind_cache:
                        continue
                    df_entry = get_candles(symbol, cfg.TF_ENTRY, TF_CANDLES.get(cfg.TF_ENTRY, 300))
                    df_h1 = get_candles(symbol, cfg.TF_TREND, TF_CANDLES.get(cfg.TF_TREND, 150))
                    if df_entry is None or df_h1 is None or len(df_entry) < 60:
                        continue
                    ind_cache[symbol] = compute_all(df_h1, symbol, sym_cfg_data, df_entry=df_entry)

                manage_positions(open_positions, ind_cache)
                _update_web_status_snapshot(balance, equity, open_positions)
                _render_dashboard(balance, equity, open_positions)
                time.sleep(cfg.LOOP_SLEEP_SEC)
                continue
            else:
                daily_loss_guard_notified = 0.0

            open_positions = get_open_positions()
            open_tickets   = {p["ticket"] for p in open_positions}
            tickets_before = tickets_en_memoria.copy() | pending_closure_tickets
            pending_closure_tickets = watch_closures(tickets_before, open_positions)
            tickets_en_memoria.clear()
            tickets_en_memoria.update(open_tickets)

            if cycle_count % 10 == 0:
                _log_session_stats()

            for symbol, sym_cfg_data in cfg.SYMBOLS.items():
                try:
                    _process_symbol(symbol, sym_cfg_data, open_positions, balance, equity)
                except Exception as e:
                    log.error(f"[main] Error en {symbol}: {e}", exc_info=True)

            open_positions = get_open_positions()
            manage_positions(open_positions, ind_cache)
            _update_web_status_snapshot(balance, equity, open_positions)
            _render_dashboard(balance, equity, open_positions)

        except Exception as e:
            log.error(f"[main] Error ciclo #{cycle_count}: {e}", exc_info=True)
            notify_error(str(e)[:300])

        time.sleep(cfg.LOOP_SLEEP_SEC)


def _render_dashboard(balance: float, equity: float, open_positions: list):
    cal_info   = eco_calendar.format_for_dashboard()
    cal_status = eco_calendar.get_status()
    news_ctx   = next(iter(news_cache.values()), None)
    try:
        dashboard.render(
            symbols=list(cfg.SYMBOLS.keys()), indicators_by_sym=ind_cache,
            sr_by_sym=sr_cache, news_ctx=news_ctx, open_positions=open_positions,
            memory_stats=get_memory_stats(), balance=balance, equity=equity,
            daily_pnl=daily_pnl, cycle=cycle_count, last_action=last_action,
            calendar_info=cal_info, calendar_status=cal_status,
            status_by_sym=symbol_status_cache,
        )
    except TypeError:
        dashboard.render(
            symbols=list(cfg.SYMBOLS.keys()), indicators_by_sym=ind_cache,
            sr_by_sym=sr_cache, news_ctx=news_ctx, open_positions=open_positions,
            memory_stats=get_memory_stats(), balance=balance, equity=equity,
            daily_pnl=daily_pnl, cycle=cycle_count, last_action=last_action,
            status_by_sym=symbol_status_cache,
        )


if __name__ == "__main__":
    run()
