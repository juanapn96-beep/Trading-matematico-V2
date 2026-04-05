"""
ZAR v7 — Execution module
Extracted from main.py: MT5 order execution functions + data utility helpers.
No shared state dependencies — pure MT5 + config calls.
"""
import logging
import time
from typing import Optional

import pandas as pd
import MetaTrader5 as mt5
import config as cfg

log = logging.getLogger(__name__)

# Desviación máxima de precio (pips) permitida en el cierre de emergencia.
_EMERGENCY_CLOSE_DEVIATION = 50


def _get_filling_type(symbol: str) -> int:
    """Auto-detect the filling mode supported by the symbol."""
    sym_info = mt5.symbol_info(symbol)
    if sym_info is not None:
        fm = sym_info.filling_mode
        if fm & mt5.SYMBOL_FILLING_IOC:
            return mt5.ORDER_FILLING_IOC
        if fm & mt5.SYMBOL_FILLING_FOK:
            return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN

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

# ── Candle fetch retry config ────────────────────────────────────
_GET_CANDLES_RETRIES    = 3
_GET_CANDLES_RETRY_WAIT = 0.5


def open_order(symbol: str, direction: str, sl: float, tp: float, volume: float) -> tuple:
    """Returns (ticket, executed_price) or (None, None)."""
    action = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None, None
    price = tick.ask if direction == "BUY" else tick.bid
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
        "type_filling": _get_filling_type(symbol),
    }
    result = mt5.order_send(request)
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        executed_price = float(result.price) if hasattr(result, 'price') and result.price else price
        log.info(
            f"[orden] ✅ #{result.order} {direction} {symbol} "
            f"vol={volume} req={price:.5f} exec={executed_price:.5f}"
        )
        return result.order, executed_price
    log.error(f"[orden] ❌ {result.comment} (retcode={result.retcode})")
    return None, None


def move_sl(ticket: int, new_sl: float) -> bool:
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        return False
    r = mt5.order_send({
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   pos[0].symbol,
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
    ticket   = pos.get("ticket")
    symbol   = pos.get("symbol")
    volume   = float(pos.get("volume", 0.01))
    pos_type = pos.get("type", 0)  # 0=BUY, 1=SELL

    if not ticket or not symbol or volume <= 0:
        log.error(f"[close_market] Datos de posición inválidos: {pos}")
        return False

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log.error(f"[close_market] No hay tick para {symbol}")
        return False

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
        "type_filling": _get_filling_type(symbol),
    }
    result = mt5.order_send(request)
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        log.warning(f"[close_market] ✅ Posición #{ticket} {symbol} cerrada ({comment})")
        return True
    log.error(
        f"[close_market] ❌ Error cerrando #{ticket} {symbol}: "
        f"{result.comment} (retcode={result.retcode})"
    )
    return False


def calc_pips_instrument(deal, symbol: str) -> float:
    """Calcula pips de ganancia/pérdida de un deal cerrado."""
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


def price_distance_to_pips(symbol: str, price_distance: float) -> float:
    """Convierte una distancia de precio en pips para el símbolo dado."""
    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        return 0.0
    tick_sz  = float(getattr(sym_info, "trade_tick_size", 0) or 0)
    point    = float(getattr(sym_info, "point", 0) or 0)
    pip_size = tick_sz * 10 if tick_sz > 0 else point * 10
    if pip_size <= 0:
        return 0.0
    return max(0.0, float(price_distance) / pip_size)


def get_candles(symbol: str, tf: str, n: int = 200) -> Optional[pd.DataFrame]:
    """Obtiene N velas históricas de MT5 para el símbolo y timeframe dados."""
    if not mt5.symbol_select(symbol, True):
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
    """Retorna (balance, equity) de la cuenta MT5 activa."""
    info = mt5.account_info()
    if info is None:
        return None, None
    return float(info.balance), float(info.equity)


def get_open_positions():
    """Retorna lista de posiciones abiertas del bot (filtradas por MAGIC_NUMBER)."""
    positions = mt5.positions_get()
    if positions is None:
        return []
    return [p._asdict() for p in positions if p.magic == cfg.MAGIC_NUMBER]


def get_open_positions_count_realtime() -> int:
    """Retorna la cantidad de posiciones abiertas del bot en tiempo real."""
    positions = mt5.positions_get()
    if positions is None:
        return 0
    return sum(1 for p in positions if p.magic == cfg.MAGIC_NUMBER)
