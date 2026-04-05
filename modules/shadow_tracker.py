"""
ZAR v7 — Shadow Mode Tracker (Mejora 15)

Tracks virtual/paper positions without sending real orders to MT5.
Each cycle, checks current market prices against SL/TP levels of open
shadow positions and simulates closures.

This module allows validating strategy parameters risk-free.
"""
import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import config as cfg
from modules.neural_brain import (
    save_shadow_trade, close_shadow_trade, get_shadow_stats,
)

log = logging.getLogger(__name__)

# ── In-memory registry of open shadow positions ─────────────────
# { shadow_id: { symbol, direction, entry_price, sl, tp, volume,
#                score, opened_at_ts } }
_open_shadow: Dict[int, dict] = {}


def open_shadow_trade(
    symbol: str,
    direction: str,
    entry_price: float,
    sl: float,
    tp: float,
    volume: float,
    score: float,
    reason: str,
    ind: Optional[dict] = None,
) -> int:
    """
    Registra un nuevo shadow trade en SQLite y en el cache en memoria.

    Returns:
        shadow_id (int) del nuevo trade virtual, o -1 si error.
    """
    ind = ind or {}

    h1_trend = ind.get("h1_trend", "")
    htf_trend = ind.get("htf_trend", "")
    hurst    = float(ind.get("hurst", 0.0))
    rsi      = float(ind.get("rsi", 0.0))
    atr_val  = float(ind.get("atr", 0.0))

    shadow_id = save_shadow_trade(
        symbol=symbol, direction=direction,
        entry_price=entry_price, sl=sl, tp=tp,
        volume=volume, score=score, reason=reason,
        h1_trend=h1_trend, htf_trend=htf_trend,
        hurst=hurst, rsi=rsi, atr=atr_val,
    )

    if shadow_id > 0:
        _open_shadow[shadow_id] = {
            "symbol":       symbol,
            "direction":    direction,
            "entry_price":  entry_price,
            "sl":           sl,
            "tp":           tp,
            "volume":       volume,
            "opened_at_ts": time.time(),
        }
        log.info(
            f"{cfg.SHADOW_LOG_PREFIX} [shadow] #{shadow_id} abierto: "
            f"{direction} {symbol} @ {entry_price:.5f} SL={sl:.5f} TP={tp:.5f}"
        )

    return shadow_id


def check_shadow_positions(symbol_prices: Dict[str, dict]) -> None:
    """
    Comprueba los precios actuales contra SL/TP de los shadow trades
    abiertos. Cierra los que han alcanzado su objetivo o stop.

    Args:
        symbol_prices: dict { symbol: {"bid": float, "ask": float} }
    """
    if not _open_shadow:
        return

    to_close: list = []

    for sid, pos in list(_open_shadow.items()):
        sym = pos["symbol"]
        prices = symbol_prices.get(sym)
        if not prices:
            continue

        direction   = pos["direction"]
        entry_price = pos["entry_price"]
        sl          = pos["sl"]
        tp          = pos["tp"]

        # Para BUY: chequeamos con bid; para SELL: con ask
        current = prices["bid"] if direction == "BUY" else prices["ask"]

        result: Optional[str] = None
        exit_price: float     = 0.0

        if direction == "BUY":
            if current <= sl:
                result, exit_price = "LOSS", sl
            elif current >= tp:
                result, exit_price = "WIN", tp
        else:  # SELL
            if current >= sl:
                result, exit_price = "LOSS", sl
            elif current <= tp:
                result, exit_price = "WIN", tp

        if result:
            to_close.append((sid, pos, result, exit_price))

    for sid, pos, result, exit_price in to_close:
        _close_shadow_position(sid, pos, result, exit_price)


def _close_shadow_position(
    shadow_id: int, pos: dict, result: str, exit_price: float,
) -> None:
    """Cierra un shadow trade en DB y notifica por Telegram si está configurado."""
    from modules.execution import price_distance_to_pips
    from modules.telegram_notifier import notify_shadow_trade_closed

    symbol    = pos["symbol"]
    direction = pos["direction"]
    entry     = pos["entry_price"]
    opened_ts = pos["opened_at_ts"]

    duration_min = int((time.time() - opened_ts) / 60)

    # Calcular profit en pips (positivo = ganancia, negativo = pérdida)
    price_diff  = (exit_price - entry) if direction == "BUY" else (entry - exit_price)
    signed_diff = price_diff if result == "WIN" else -abs(price_diff)
    profit_pips = price_distance_to_pips(symbol, abs(signed_diff))
    if signed_diff < 0:
        profit_pips = -profit_pips

    close_shadow_trade(
        shadow_id=shadow_id,
        exit_price=exit_price,
        result=result,
        profit_pips=profit_pips,
        duration_min=duration_min,
    )

    # Eliminar del cache en memoria
    _open_shadow.pop(shadow_id, None)

    emoji = "✅" if result == "WIN" else "❌"
    log.info(
        f"{cfg.SHADOW_LOG_PREFIX} [shadow] #{shadow_id} cerrado {result}: "
        f"{direction} {symbol} entry={entry:.5f} exit={exit_price:.5f} "
        f"pips={profit_pips:+.1f} dur={duration_min}min"
    )

    # Notificación Telegram
    if getattr(cfg, "SHADOW_NOTIFY_TELEGRAM", True):
        try:
            notify_shadow_trade_closed(
                shadow_id=shadow_id,
                symbol=symbol,
                direction=direction,
                entry_price=entry,
                exit_price=exit_price,
                result=result,
                profit_pips=profit_pips,
                duration_min=duration_min,
                emoji=emoji,
            )
        except Exception as exc:
            log.warning(f"[shadow] Error notificando cierre #{shadow_id}: {exc}")


def get_open_shadow_count() -> int:
    """Retorna el número de shadow positions actualmente abiertas."""
    return len(_open_shadow)


def get_open_shadow_positions() -> dict:
    """Retorna una copia del dict de shadow positions abiertas."""
    return dict(_open_shadow)


def load_open_from_db() -> None:
    """
    Carga shadow trades sin cerrar desde la DB (para recuperar estado
    tras un reinicio del bot).
    """
    try:
        import sqlite3
        from modules.neural_brain import DB_PATH
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            """SELECT id, symbol, direction, entry_price, sl, tp, volume, opened_at
               FROM shadow_trades WHERE result IS NULL"""
        ).fetchall()
        con.close()

        for row in rows:
            sid, sym, direction, entry, sl, tp, vol, opened_at_str = row
            if sid not in _open_shadow:
                # Reconstruir timestamp de apertura
                try:
                    opened_dt = datetime.fromisoformat(opened_at_str)
                    opened_ts = opened_dt.timestamp()
                except Exception:
                    opened_ts = time.time()

                _open_shadow[sid] = {
                    "symbol":       sym,
                    "direction":    direction,
                    "entry_price":  entry,
                    "sl":           sl,
                    "tp":           tp,
                    "volume":       vol,
                    "opened_at_ts": opened_ts,
                }
        if rows:
            log.info(f"{cfg.SHADOW_LOG_PREFIX} [shadow] {len(rows)} posiciones abiertas cargadas desde DB")
    except Exception as exc:
        log.warning(f"[shadow] Error cargando shadow trades desde DB: {exc}")
