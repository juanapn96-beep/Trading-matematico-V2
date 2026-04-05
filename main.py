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

# ── New extracted modules ────────────────────────────────────────
from modules.bot_state import state
from modules.execution import (
    open_order, move_sl, close_position_market,
    calc_pips_instrument as _calc_pips_instrument,
    price_distance_to_pips as _price_distance_to_pips,
    get_candles, get_account_info, get_open_positions,
    get_open_positions_count_realtime,
    TF_MAP, TF_CANDLES,
)
from modules.trailing_manager import (
    trail_stage as _trail_stage,
    profit_candle_count as _profit_candle_count,
    profit_candle_last_seen as _profit_candle_last_seen,
    init_ticket, manage_trailing_stop, cleanup_ticket,
)
from modules.context_builder import build_context, build_lateral_context
from modules.position_manager import (
    be_activated, tp_alerted,
    manage_positions, watch_closures,
)
from modules.symbol_processor import (
    process_symbol,
    set_symbol_status as _set_symbol_status,
    set_last_decision_analysis as _set_last_decision_analysis,
    bump_decision_metric as _bump_decision_metric,
    ask_decision_engine,
    get_last_candle_stamp as _get_last_candle_stamp,
    build_decision_cache_key as _build_decision_cache_key,
)

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

# ── Module-level constants (unchanged from original) ─────────────
_EMERGENCY_CLOSE_DEVIATION = 50  # kept for backward compat (also in execution.py)

# ════════════════════════════════════════════════════════════════
#  ESTADO GLOBAL (encapsulado en BotState — state.xxx)
# ════════════════════════════════════════════════════════════════
# All mutable globals are now attributes of `state` (modules/bot_state.py).
# Convenience aliases for functions that have not yet been updated:

NOTIF_COOLDOWN_SEC  = 1800
SYMBOL_COOLDOWN_SEC = getattr(cfg, "SYMBOL_COOLDOWN_SEC", 300)
H1_CACHE_TTL_SEC    = 300



# ════════════════════════════════════════════════════════════════
#  STATE ALIASES (para compatibilidad mientras se migra a state.xxx)
# ════════════════════════════════════════════════════════════════
# El estado mutable vive en `state` (modules/bot_state.py).
# Las funciones que aún no han sido extraídas a submódulos pueden
# acceder a él directamente via state.xxx.

# Legacy alias for the lock (used in _update_web_status_snapshot, etc.)
state_lock = state.lock




def _get_decision_metrics_snapshot() -> dict:
    now      = datetime.now(timezone.utc)
    hour_key = now.strftime("%Y-%m-%d %H")
    day_key  = now.strftime("%Y-%m-%d")
    with state.lock:
        return {
            **state.decision_usage_stats,
            "api_calls_current_hour": int(state.decision_hourly_usage.get(hour_key, 0)),
            "api_calls_current_day":  int(state.decision_daily_usage.get(day_key, 0)),
            "engine": "deterministic",
        }


def _get_decision_metrics_snapshot_unlocked() -> dict:
    now      = datetime.now(timezone.utc)
    hour_key = now.strftime("%Y-%m-%d %H")
    day_key  = now.strftime("%Y-%m-%d")
    return {
        **state.decision_usage_stats,
        "api_calls_current_hour": int(state.decision_hourly_usage.get(hour_key, 0)),
        "api_calls_current_day":  int(state.decision_daily_usage.get(day_key, 0)),
        "engine": "deterministic",
    }


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
    news_ctx = next(iter(state.news_cache.values()), None)
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
        gross_wins   = avg_win  * wins   if wins   > 0 else 0.0
        gross_losses = abs(avg_loss) * losses if losses > 0 else 0.0
        pf = gross_wins / gross_losses if gross_losses > 0 else 0.0
        best  = float(mem_stats.get("best_trade",  0.0))
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
    bot_positions = [p for p in open_positions if p.get("magic", 0) == cfg.MAGIC_NUMBER]
    if bot_positions:
        positions_detail = []
        for p in bot_positions:
            positions_detail.append({
                "symbol": p.get("symbol", "?"),
                "direction": "LONG" if p.get("type", 0) == 0 else "SHORT",
            })
        from modules.portfolio_risk import _get_correlation
        corr_count = 0
        for i, p1 in enumerate(bot_positions):
            for p2 in bot_positions[i + 1:]:
                rho = _get_correlation(p1.get("symbol", ""), p2.get("symbol", ""))
                if abs(rho) >= 0.60:
                    corr_count += 1
        risk_per = getattr(cfg, "RISK_PER_TRADE", 0.01)
        n        = len(bot_positions)
        risk_sq  = risk_per ** 2
        variance = n * risk_sq
        for i, p1 in enumerate(bot_positions):
            for j in range(i + 1, n):
                p2       = bot_positions[j]
                rho      = _get_correlation(p1.get("symbol", ""), p2.get("symbol", ""))
                same_dir = (p1.get("type", 0) == p2.get("type", 0))
                sign     = 1.0 if same_dir else -1.0
                variance += 2 * rho * sign * risk_sq
        eff_risk = math.sqrt(max(0.0, variance)) * 100
        portfolio_payload = {
            "open_positions": n,
            "effective_risk_pct": round(eff_risk, 1),
            "max_risk_pct": getattr(cfg, "MAX_PORTFOLIO_RISK_PCT", 5.0),
            "correlated_pairs": corr_count,
            "positions_detail": positions_detail,
        }

    with state.lock:
        _now = time.time()
        state.web_status_snapshot = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "cycle": state.cycle_count,
            "balance": float(balance or 0.0),
            "equity": float(equity or 0.0),
            "daily_pnl": float(state.daily_pnl or 0.0),
            "last_action": state.last_action,
            "active_trades": [_serialize_position_for_web(pos) for pos in open_positions],
            "symbol_status": dict(state.symbol_status_cache),
            "symbol_details": dict(state.symbol_detail_cache),
            "memory": mem_stats,
            "news": news_payload,
            "shared_news_fetched_at": (
                getattr(state.shared_news_cache, "fetched_at", "")
                if state.shared_news_cache is not None else ""
            ),
            "decision_metrics": _get_decision_metrics_snapshot_unlocked(),
            "last_decision_analysis": dict(state.last_decision_analysis),
            "performance": perf_payload,
            "portfolio_risk": portfolio_payload,
            "tilt_guard": {
                "active": _now < state.tilt_active_until,
                "consecutive_losses": state.consecutive_losses,
                "remaining_minutes": max(0, int((state.tilt_active_until - _now) / 60)),
            },
        }


def _get_web_status_snapshot() -> dict:
    with state.lock:
        return dict(state.web_status_snapshot)


_GET_CANDLES_RETRIES = 3
_GET_CANDLES_RETRY_WAIT = 0.5

# Espera (segundos) después de pre-seleccionar símbolos en conectar_mt5().
# Da tiempo a MT5 para iniciar la descarga asíncrona de historial.
_SYMBOL_WARMUP_WAIT_SEC = 2

# ── Constantes de reconexión MT5 con backoff exponencial ──
_RECONNECT_BASE_DELAY_SEC = 30
_RECONNECT_MAX_EXPONENT = 4     # máx factor = 2^4 = 16 → 30*16 = 480s
_RECONNECT_MAX_DELAY_SEC = 300  # tope absoluto 5 min
_mt5_reconnect_fails = 0        # contador de fallos consecutivos (module-level)


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


def maybe_send_daily_summary(balance: float, equity: float):
    now   = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour == 17 and today != state.last_summary_date:
        notify_daily_summary(
            balance=balance, equity=equity, daily_profit=state.daily_pnl,
            trades_today=state.trades_today, wins=state.wins_today, losses=state.losses_today,
            memory_stats=get_memory_stats(),
        )
        state.last_summary_date = today
        log.info("[stats] 📊 Resumen intermedio 17:00 UTC enviado")


def maybe_send_eod_analysis(balance: float, equity: float):
    now   = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour == 21 and today != state.last_eod_date:
        state.last_eod_date = today

        # FIX 13: WR = wins/(wins+losses) — sin BE
        wr     = (state.wins_today / state.trades_today * 100) if state.trades_today > 0 else 0.0
        profit = round(state.daily_pnl, 2)
        growth = ((balance - state.daily_start_balance) / state.daily_start_balance * 100) if state.daily_start_balance > 0 else 0

        loss_reasons = []
        if state.losses_today > state.wins_today:
            loss_trades = [t for t in state.daily_trades_log if t["result"] == "LOSS"]
            if loss_trades:
                symbols_lost = {}
                for t in loss_trades:
                    symbols_lost[t["symbol"]] = symbols_lost.get(t["symbol"], 0) + 1
                worst_sym = max(symbols_lost, key=symbols_lost.get)
                loss_reasons.append(f"Mayor concentración de pérdidas en {worst_sym} ({symbols_lost[worst_sym]} trades)")

            loss_durations = [t["duration"] for t in loss_trades if t["duration"] > 0]
            win_trades     = [t for t in state.daily_trades_log if t["result"] == "WIN"]
            win_durations  = [t["duration"] for t in win_trades if t["duration"] > 0]
            if loss_durations:
                avg_loss_dur = sum(loss_durations) / len(loss_durations)
                loss_reasons.append(f"Duración promedio en pérdidas: {avg_loss_dur:.0f} min")
            if win_durations:
                avg_win_dur = sum(win_durations) / len(win_durations)
                loss_reasons.append(f"Duración promedio en ganancias: {avg_win_dur:.0f} min")

        be_reason = ""
        if state.be_today > 0:
            be_pct = state.be_today / max(state.trades_today + state.be_today, 1) * 100
            be_reason = (
                f"⚡ {state.be_today} trades cerraron en breakeven ({be_pct:.0f}% del total). "
                "Si superan el 50%, el trailing está demasiado agresivo — "
                "considera aumentar be_atr_mult o reducir sl_atr_mult."
            )

        mem_stats = get_memory_stats()

        notify_eod_analysis(
            balance=balance, equity=equity,
            daily_profit=profit,
            trades_today=state.trades_today, wins=state.wins_today,
            losses=state.losses_today, be_count=state.be_today,
            win_rate=round(wr, 1),
            growth_pct=round(growth, 2),
            loss_reasons=loss_reasons,
            be_reason=be_reason,
            memory_stats=mem_stats,
            daily_trades=state.daily_trades_log,
        )

        log.info(
            f"[EOD] 📊 Análisis EOD enviado | "
            f"{state.trades_today} trades reales (excl. {state.be_today} BE) | "
            f"WR={wr:.0f}% | P&L=${profit:+.2f}"
        )

        # Resetear contadores
        state.reset_daily_stats()
        # Set starting balance for next day's growth calculation
        state.daily_start_balance = balance


def _log_session_stats():
    """
    FIX 13: WR = wins/(wins+losses). BE excluido del denominador.
    Muestra las 3 métricas por separado: W / L / BE.
    """
    wr  = (state.wins_today / state.trades_today * 100) if state.trades_today > 0 else 0.0
    mem = get_memory_stats()
    _now = time.time()
    tilt_active = _now < state.tilt_active_until
    tilt_info = (
        f" | 🛑 TILT({state.consecutive_losses}racha, {max(0, int((state.tilt_active_until - _now) / 60))}min)"
        if tilt_active else
        f" | consec_losses={state.consecutive_losses}"
    )
    log.info(
        f"[stats] Hoy: {state.trades_today} trades reales | "
        f"✅ {state.wins_today}W ❌ {state.losses_today}L ⚡ {state.be_today}BE (excluidos) | "
        f"WR={wr:.0f}% | P&L=${state.daily_pnl:+.2f} | "
        f"Mem: {mem['total']} trades WR={mem['win_rate']}% P&L=${mem['profit']:+.2f}"
        f"{tilt_info}"
    )


def _notify_daily_loss_guard_once(daily_pnl_now: float, balance_now: float, max_daily_loss_pct: float):
    now_ts = time.time()
    if now_ts - state.daily_loss_guard_notified >= NOTIF_COOLDOWN_SEC:
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
        state.daily_loss_guard_notified = now_ts

# ════════════════════════════════════════════════════════════════
#  LOOP PRINCIPAL
# ════════════════════════════════════════════════════════════════

def run():
    log.info("🚀 ZAR ULTIMATE BOT v6.5 — TRAILING STOP + WR FIX — Iniciando...")
    init_db()

    if not conectar_mt5():
        log.critical("No se pudo conectar a MT5.")
        sys.exit(1)

    balance, equity = get_account_info()
    state.daily_start_balance = balance or 0.0

    log.info("[calendar] 📅 Cargando calendario económico...")
    try:
        eco_calendar.refresh(force=True)
    except Exception as e:
        log.warning(f"[calendar] ⚠️ Error cargando calendario económico (el bot continuará sin él): {e}")

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

    # ── Shadow Mode: banner de advertencia prominente ─────────────
    if getattr(cfg, "SHADOW_MODE_ENABLED", False):
        log.warning("👻👻👻 SHADOW MODE ACTIVO — NO se ejecutarán órdenes reales 👻👻👻")
        from modules.shadow_tracker import load_open_from_db
        load_open_from_db()
        from modules.telegram_notifier import telegram_send
        telegram_send(
            "👻👻👻 <b>SHADOW MODE ACTIVO</b>\n"
            "El bot está corriendo en modo simulación.\n"
            "<i>NO se ejecutarán órdenes reales.</i> 👻👻👻"
        )

    for pending in get_pending_trades():
        state.tickets_en_memoria.add(pending["ticket"])

    while True:
        state.cycle_count += 1
        try:
            state.ind_cache = {}
            state.sr_cache  = {}
            state.symbol_status_cache = {}

            balance, equity = get_account_info()
            if balance is None:
                global _mt5_reconnect_fails
                log.warning("[main] Sin balance — reconectando...")
                if not conectar_mt5():
                    _mt5_reconnect_fails += 1
                    wait = min(
                        _RECONNECT_BASE_DELAY_SEC * (2 ** min(_mt5_reconnect_fails - 1, _RECONNECT_MAX_EXPONENT)),
                        _RECONNECT_MAX_DELAY_SEC,
                    )
                    log.warning(f"[main] Reconexión fallida #{_mt5_reconnect_fails}, espera {wait}s")
                    time.sleep(wait)
                    continue
                _mt5_reconnect_fails = 0

            maybe_send_daily_summary(balance, equity)
            maybe_send_eod_analysis(balance, equity)

            if not is_daily_loss_ok(state.daily_pnl, balance):
                log.warning("[main] ⛔ Límite diario alcanzado")
                state.last_action = "⛔ Límite diario alcanzado"
                _notify_daily_loss_guard_once(state.daily_pnl, balance, float(getattr(cfg, "MAX_DAILY_LOSS", 0.05)))

                open_positions = get_open_positions()
                for pos in open_positions:
                    symbol = pos.get("symbol", "")
                    sym_cfg_data = cfg.SYMBOLS.get(symbol)
                    if not symbol or sym_cfg_data is None or symbol in state.ind_cache:
                        continue
                    df_entry = get_candles(symbol, cfg.TF_ENTRY, TF_CANDLES.get(cfg.TF_ENTRY, 300))
                    df_h1 = get_candles(symbol, cfg.TF_TREND, TF_CANDLES.get(cfg.TF_TREND, 150))
                    if df_entry is None or df_h1 is None or len(df_entry) < 60:
                        continue
                    state.ind_cache[symbol] = compute_all(df_h1, symbol, sym_cfg_data, df_entry=df_entry)

                manage_positions(open_positions, state.ind_cache)

                # ── Shadow Mode: verificar SL/TP (incluso cuando daily loss bloquea) ──
                if getattr(cfg, "SHADOW_MODE_ENABLED", False):
                    from modules.shadow_tracker import check_shadow_positions
                    _shadow_prices = {}
                    for _sym in cfg.SYMBOLS:
                        _tick = mt5.symbol_info_tick(_sym)
                        if _tick:
                            _shadow_prices[_sym] = {"bid": _tick.bid, "ask": _tick.ask}
                    check_shadow_positions(_shadow_prices)

                _update_web_status_snapshot(balance, equity, open_positions)
                _render_dashboard(balance, equity, open_positions)
                time.sleep(cfg.LOOP_SLEEP_SEC)
                continue
            else:
                state.daily_loss_guard_notified = 0.0

            open_positions = get_open_positions()
            open_tickets   = {p["ticket"] for p in open_positions}
            tickets_before = state.tickets_en_memoria.copy() | state.pending_closure_tickets
            state.pending_closure_tickets = watch_closures(tickets_before, open_positions)
            state.tickets_en_memoria.clear()
            state.tickets_en_memoria.update(open_tickets)

            if state.cycle_count % 10 == 0:
                _log_session_stats()

            for symbol, sym_cfg_data in cfg.SYMBOLS.items():
                try:
                    process_symbol(symbol, sym_cfg_data, open_positions, balance, equity)
                except Exception as e:
                    log.error(f"[main] Error en {symbol}: {e}", exc_info=True)

            open_positions = get_open_positions()
            manage_positions(open_positions, state.ind_cache)

            # ── Shadow Mode: verificar SL/TP de posiciones virtuales ──
            if getattr(cfg, "SHADOW_MODE_ENABLED", False):
                from modules.shadow_tracker import check_shadow_positions
                current_prices = {}
                for sym in cfg.SYMBOLS:
                    tick = mt5.symbol_info_tick(sym)
                    if tick:
                        current_prices[sym] = {"bid": tick.bid, "ask": tick.ask}
                check_shadow_positions(current_prices)

            _update_web_status_snapshot(balance, equity, open_positions)
            _render_dashboard(balance, equity, open_positions)

        except Exception as e:
            log.error(f"[main] Error ciclo #{state.cycle_count}: {e}", exc_info=True)
            notify_error(str(e)[:300])

        time.sleep(cfg.LOOP_SLEEP_SEC)


def _render_dashboard(balance: float, equity: float, open_positions: list):
    cal_info   = eco_calendar.format_for_dashboard()
    cal_status = eco_calendar.get_status()
    news_ctx   = next(iter(state.news_cache.values()), None)
    try:
        dashboard.render(
            symbols=list(cfg.SYMBOLS.keys()), indicators_by_sym=state.ind_cache,
            sr_by_sym=state.sr_cache, news_ctx=news_ctx, open_positions=open_positions,
            memory_stats=get_memory_stats(), balance=balance, equity=equity,
            daily_pnl=state.daily_pnl, cycle=state.cycle_count, last_action=state.last_action,
            calendar_info=cal_info, calendar_status=cal_status,
            status_by_sym=state.symbol_status_cache,
        )
    except TypeError:
        dashboard.render(
            symbols=list(cfg.SYMBOLS.keys()), indicators_by_sym=state.ind_cache,
            sr_by_sym=state.sr_cache, news_ctx=news_ctx, open_positions=open_positions,
            memory_stats=get_memory_stats(), balance=balance, equity=equity,
            daily_pnl=state.daily_pnl, cycle=state.cycle_count, last_action=state.last_action,
            status_by_sym=state.symbol_status_cache,
        )


if __name__ == "__main__":
    run()
