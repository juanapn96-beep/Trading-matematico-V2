"""
ZAR v7 — Position Manager module
Extracted from main.py: manage_positions() and watch_closures().

Module-level state:
    be_activated  — set of tickets that reached at least stage 1 trailing
    tp_alerted    — set of tickets that received a TP-near alert
"""
import logging
from datetime import datetime, timezone, timedelta

import MetaTrader5 as mt5
import config as cfg

from modules.bot_state import state
from modules.trailing_manager import trail_stage, manage_trailing_stop, cleanup_ticket
from modules.execution import close_position_market, calc_pips_instrument
from modules.risk_manager import check_circuit_breaker
from modules.neural_brain import (
    get_pending_trades, update_trade_result,
)
from modules.telegram_notifier import (
    notify_near_tp, notify_trade_closed,
)

log = logging.getLogger(__name__)

# ── Module-level state ───────────────────────────────────────────
be_activated: set = set()  # tickets that passed at least trail stage 1
tp_alerted:   set = set()  # tickets that already received TP-near alert


def manage_positions(positions: list, ind_cache: dict) -> None:
    """
    Gestión de posiciones abiertas: Circuit Breaker, trailing stop, alertas TP.
    Actualiza state.last_action donde corresponde.
    """
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
                state.last_action = f"🚨 CB {symbol} #{ticket}"
                from modules.telegram_notifier import _send as telegram_send
                telegram_send(f"🚨 <b>CIRCUIT BREAKER</b>\n{cb_reason}")
            continue

        # FIX 11: trailing stop progresivo
        action_str = manage_trailing_stop(
            pos, sym_cfg_data, ind_cache, state.trade_mode_cache
        )
        if action_str:
            state.last_action = action_str

        # Marcar tickets que están en trail
        if trail_stage.get(ticket, 0) >= 1:
            be_activated.add(ticket)

        # Alerta TP cercano (15% restante del camino)
        if ticket not in tp_alerted and tp != 0:
            open_p  = pos["price_open"]
            dist_tp = abs(tp - cur_p)
            total   = abs(tp - open_p)
            if total > 0 and dist_tp / total <= 0.15:
                notify_near_tp(symbol, ticket, cur_p, tp, profit)
                tp_alerted.add(ticket)
                state.last_action = f"TP cercano #{ticket} {symbol} ${profit:.2f}"


def watch_closures(open_tickets_before: set, open_positions_now: list) -> set:
    """
    Detecta posiciones cerradas y registra el resultado.

    FIX 12/13: Win Rate = wins/(wins+losses). BE NO cuenta en WR.
    El WR solo mide trades con resultado claro. BE se muestra separado.

    Returns:
        set of unresolved tickets (no closing deal found yet)
    """
    current_tickets = {p["ticket"] for p in open_positions_now}
    closed          = open_tickets_before - current_tickets
    if not closed:
        return set()

    pending           = get_pending_trades()
    pending_by_ticket = {int(p["ticket"]): p for p in pending}
    now_utc   = datetime.now(timezone.utc)
    from_time = now_utc - timedelta(hours=24)

    for ticket in closed:
        trade_info    = pending_by_ticket.get(int(ticket))
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
    earliest_allowed  = now_utc - timedelta(days=max_lookback_days)
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
        # Clean up per-ticket state in all modules
        cleanup_ticket(ticket)
        state.trade_mode_cache.pop(ticket, None)

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

        profit  = (
            float(deal.profit)
            + float(getattr(deal, 'commission', 0) or 0)
            + float(getattr(deal, 'swap', 0) or 0)
        )
        symbol    = deal.symbol
        pips      = calc_pips_instrument(deal, symbol)
        close_px  = float(deal.price)
        direction = "BUY" if deal.type == 0 else "SELL"

        if profit > 1.0:
            result = "WIN"
        elif profit < -1.0:
            result = "LOSS"
        else:
            result = "BE"

        trade_info    = pending_by_ticket.get(int(ticket))
        opened_at_str = trade_info["opened_at"] if trade_info else None
        open_price    = trade_info.get("open_price", close_px) if trade_info else close_px
        direction     = trade_info.get("direction", direction) if trade_info else direction
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

        # FIX 13: actualizar contadores — BE NO cuenta en WR
        state.daily_pnl += profit
        if result == "WIN":
            state.wins_today   += 1
            state.trades_today += 1
        elif result == "LOSS":
            state.losses_today += 1
            state.trades_today += 1
        elif result == "BE":
            state.be_today += 1

        state.daily_trades_log.append({
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
        state.last_action = f"Cerrada #{ticket} {symbol} {result} ${profit:+.2f}"
        log.info(
            f"[closures] ✅ #{ticket} {symbol} {direction} {result} "
            f"${profit:+.2f} | {pips:+.1f} pips | {duration_min}min"
        )

        # Limpiar estado de trail para el ticket cerrado
        cleanup_ticket(ticket)
        be_activated.discard(ticket)
        tp_alerted.discard(ticket)
        state.tickets_en_memoria.discard(ticket)

    return unresolved_tickets
