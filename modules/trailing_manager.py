"""
ZAR v7 — Trailing Manager module
Extracted from main.py: trailing stop state and logic.

Module-level state (per-ticket):
    trail_stage            — current trailing stage reached
    profit_candle_count    — H1 candle count in profit (normal mode)
    profit_candle_last_seen — last candle stamp seen in profit

Exported functions:
    manage_trailing_stop(pos, sym_cfg, ind_cache, trade_mode_cache) -> str | None
    price_distance_to_pips(symbol, price_distance) -> float
    cleanup_ticket(ticket)
    init_ticket(ticket)
"""
import logging

import MetaTrader5 as mt5
import config as cfg

from modules.execution import move_sl, price_distance_to_pips
from modules.neural_brain import get_adaptive_trail_params
from modules.telegram_notifier import notify_breakeven

log = logging.getLogger(__name__)

# ── Per-ticket trailing state ────────────────────────────────────
trail_stage: dict = {}             # {ticket: int}  highest stage reached
profit_candle_count: dict = {}     # {ticket: int}  H1 candles in profit
profit_candle_last_seen: dict = {} # {ticket: str}  last candle stamp


def init_ticket(ticket: int) -> None:
    """Initialize trailing state for a newly opened ticket."""
    trail_stage[ticket] = 0
    profit_candle_count[ticket] = 0
    profit_candle_last_seen[ticket] = None


def cleanup_ticket(ticket: int) -> None:
    """Remove all trailing state for a closed ticket."""
    trail_stage.pop(ticket, None)
    profit_candle_count.pop(ticket, None)
    profit_candle_last_seen.pop(ticket, None)


def manage_trailing_stop(
    pos: dict,
    sym_cfg: dict,
    ind_cache: dict,
    trade_mode_cache: dict,
) -> "str | None":
    """
    Trailing proporcional por progreso de TP con ratchet y anti-SL-hunting.

    Devuelve el texto de last_action si se movió el SL, o None en caso contrario.
    El llamador (main.py o position_manager) es responsable de asignar last_action.
    """
    ticket    = pos["ticket"]
    symbol    = pos["symbol"]
    direction = "BUY" if pos["type"] == 0 else "SELL"
    open_p    = pos["price_open"]
    cur_p     = pos["price_current"]
    sl        = float(pos.get("sl", 0.0) or 0.0)
    tp        = float(pos.get("tp", 0.0) or 0.0)

    # Obtener ATR del cache de indicadores
    ind     = ind_cache.get(symbol, {})
    atr_val = ind.get("atr", 0)

    if atr_val <= 0:
        return None

    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        return None
    point = sym_info.point or 0.00001
    candle_stamp = str(ind.get("entry_candle_time", "") or "")

    favorable_move = (cur_p - open_p) if direction == "BUY" else (open_p - cur_p)
    if favorable_move <= 0:
        profit_candle_count[ticket] = 0
        profit_candle_last_seen.pop(ticket, None)
        return None
    profit_price = abs(cur_p - open_p)

    # Obtener trade_mode primero para saber si es scalp ANTES de aplicar los gates
    trade_mode = trade_mode_cache.get(ticket)
    if not trade_mode:
        trade_mode = get_adaptive_trail_params(sym_cfg, direction)
        trade_mode_cache[ticket] = trade_mode

    scalp_mode     = bool(trade_mode.get("scalp_mode", False))
    be_atr_mult    = float(trade_mode.get("be_atr_mult", sym_cfg.get("be_atr_mult", 2.0)))
    be_buffer_mult = float(trade_mode.get("be_buffer_mult", 0.5))

    if scalp_mode:
        gained_pips = price_distance_to_pips(symbol, profit_price)
        min_be_pips = float(getattr(cfg, "SCALPING_BE_MIN_PIPS", 2.0))
        if gained_pips < min_be_pips:
            return None
    else:
        if candle_stamp and profit_candle_last_seen.get(ticket) != candle_stamp:
            profit_candle_count[ticket] = profit_candle_count.get(ticket, 0) + 1
            profit_candle_last_seen[ticket] = candle_stamp
        if profit_candle_count.get(ticket, 0) < 2:
            return None
        be_threshold_price = atr_val * be_atr_mult
        if profit_price < be_threshold_price:
            return None

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
        stage_definitions = [
            (float(getattr(cfg, "SCALPING_BE_PIPS_STAGE_4")), 0.70, 5, "Scalp lock 70%"),
            (float(getattr(cfg, "SCALPING_BE_PIPS_STAGE_3")), 0.50, 4, "Scalp lock 50%"),
            (float(getattr(cfg, "SCALPING_BE_PIPS_STAGE_2")), 0.30, 3, "Scalp lock 30%"),
            (float(getattr(cfg, "SCALPING_BE_PIPS_STAGE_1")), 0.15, 2, "Scalp lock 15%"),
        ]
        lock_pct   = 0.0
        new_stage  = 1
        stage_label = "Scalp BE buffer"
        for min_pips, pct, stage_num, label in stage_definitions:
            if gained_pips >= min_pips:
                lock_pct   = pct
                new_stage  = stage_num
                stage_label = label
                break
    else:
        stage_definitions = [
            (0.85, 0.70, 5, "Lock 70%"),
            (0.70, 0.50, 4, "Lock 50%"),
            (0.50, 0.35, 3, "Lock 35%"),
            (0.30, 0.15, 2, "Lock 15%"),
        ]
        lock_pct   = 0.0
        new_stage  = 1
        stage_label = "BE buffer"
        for min_progress, pct, stage_num, label in stage_definitions:
            if tp_progress >= min_progress:
                lock_pct   = pct
                new_stage  = stage_num
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
            return None
    else:
        if sl > 0 and new_sl >= sl:
            return None

    sl_reference = sl if sl > 0 else open_p
    diff_pts = abs(new_sl - sl_reference) / point
    if diff_pts < 0.5:
        return None

    # ── Mover SL ──
    if move_sl(ticket, new_sl):
        prev_stage = trail_stage.get(ticket, 0)
        trail_stage[ticket] = new_stage

        profit_pts = profit_price / point
        locked_pts = (
            (new_sl - open_p) / point if direction == "BUY"
            else (open_p - new_sl) / point
        )

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
                    f"— {stage_label} | profit_cycles={profit_candle_count.get(ticket, 0)} "
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
                    f"— {stage_label} | cycles={profit_candle_count.get(ticket, 0)} | "
                    f"lock={locked_pts:.0f}pts | SL→{new_sl:.5f} | TP%={tp_progress:.0%}"
                )

        return f"Trail {stage_label} #{ticket} {symbol} lock={locked_pts:.0f}pts"

    return None
