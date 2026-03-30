"""
╔══════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — risk_manager.py  (v6.4 — 24/5)        ║
║                                                                  ║
║   CAMBIO v6.3 — Filtro dinámico de calidad de mercado:         ║
║                                                                  ║
║   ANTES: is_session_valid() → reloj fijo 07:00-21:00           ║
║   AHORA: is_market_tradeable() → métricas reales en vivo       ║
║                                                                  ║
║   CRITERIOS DINÁMICOS (todos deben cumplirse):                  ║
║   1. ATR actual ≥ spread mínimo × min_atr_spread_ratio         ║
║      → El movimiento esperado cubre el costo de entrada        ║
║   2. ATR actual ≥ atr_min_pct del precio                       ║
║      → El mercado tiene suficiente volatilidad para operar     ║
║   3. No es finde de semana (excepto BTC/cripto)                ║
║   4. No es la hora muerta 00:00 UTC viernes → domingo          ║
║      para instrumentos no-crypto                               ║
║                                                                  ║
║   Los índices (US500, NAS100, GER40, USOILm) tienen una hora   ║
║   de cierre real de mercado que SÍ se respeta.                 ║
║                                                                  ║
║   CAMBIO v6.4 — Circuit Breaker individual:                    ║
║   Si el drawdown de una posición supera                        ║
║   CIRCUIT_BREAKER_MAX_DRAWDOWN_PCT del balance,                ║
║   se fuerza el cierre inmediato (Cisne Negro).                 ║
╚══════════════════════════════════════════════════════════════════╝
"""

import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

import numpy as np
import config as cfg

log = logging.getLogger(__name__)

# ── Sufijo del broker (centralizado en config.py) ───────────────
_BROKER_SUFFIX  = cfg.BROKER_SUFFIX    # "m" para Exness, "" para IC Markets
_NO_SUFFIX_RM   = getattr(cfg, "_NO_SUFFIX", {"USTEC", "DE40"})


def _sym(base: str) -> str:
    """Retorna el nombre completo del símbolo según el broker configurado."""
    if base in _NO_SUFFIX_RM:
        return base
    return f"{base}{_BROKER_SUFFIX}"


# ── Horarios de cierre REAL de mercado (no de liquidez) ─────────
# Estos instrumentos literalmente no tienen precio fuera de estos rangos.
# El resto opera 24/5 y solo se filtra por calidad de spread/ATR.
HARD_CLOSE_HOURS = {
    # Índices americanos — cierre real 21:00-22:00 UTC, reapertura 22:00 UTC
    # En la práctica Exness los tiene 23h casi continuas (22:00-21:00 UTC)
    # Permitimos 22:00 en adelante (pre-market NY tiene movimiento)
    _sym("US500"):  {"hard_open": 0,  "hard_close": 24},   # 23h continuas en Exness
    _sym("USTEC"):  {"hard_open": 0,  "hard_close": 24},
    # Petróleo — cierre de 1h a las 23:00 UTC en Exness
    _sym("USOIL"):  {"hard_open": 1,  "hard_close": 23},
    # DAX — cierre real 22:00 UTC (mercado alemán cierra 17:00 Frankfurt = 16:00 UTC)
    # Pero Exness mantiene el CFD casi 24h, solo cierra 1h
    _sym("DE40"):   {"hard_open": 0,  "hard_close": 24},
    # Forex y metales — 24/5, sin cierre duro
    _sym("XAUUSD"): {"hard_open": 0,  "hard_close": 24},
    _sym("XAGUSD"): {"hard_open": 0,  "hard_close": 24},
    _sym("EURUSD"): {"hard_open": 0,  "hard_close": 24},
    _sym("GBPUSD"): {"hard_open": 0,  "hard_close": 24},
    _sym("USDJPY"): {"hard_open": 0,  "hard_close": 24},
    _sym("GBPJPY"): {"hard_open": 0,  "hard_close": 24},
    _sym("EURJPY"): {"hard_open": 0,  "hard_close": 24},
    # Cripto — 24/7 literal
    _sym("BTCUSD"): {"hard_open": 0,  "hard_close": 24},
}

# ── Símbolo → es cripto (opera fines de semana) ────────────────
CRYPTO_SYMBOLS = {_sym("BTCUSD"), _sym("ETHUSD"), _sym("XRPUSD")}

# ── ATR mínimo como % del precio para que valga la pena operar ──
# Si el mercado no se mueve lo suficiente, el spread se come la ganancia.
ATR_MIN_PCT = {
    # Forex majors — necesitan al menos 0.02% de ATR vs precio
    _sym("EURUSD"): 0.020, _sym("GBPUSD"): 0.025, _sym("USDJPY"): 0.015,
    _sym("GBPJPY"): 0.030, _sym("EURJPY"): 0.020,
    # Metales
    _sym("XAUUSD"): 0.035,   # Oro: ATR mínimo $1.75 con precio ~$5000
    _sym("XAGUSD"): 0.040,
    # Índices y petróleo
    _sym("US500"):  0.020, _sym("USTEC"):  0.025, _sym("DE40"):   0.018,
    _sym("USOIL"):  0.035,
    # Cripto — alta volatilidad normal
    _sym("BTCUSD"): 0.060,
}

# Valor por defecto si el símbolo no está en el mapa
DEFAULT_ATR_MIN_PCT = 0.020


def is_market_tradeable(
    symbol: str,
    sym_cfg: dict,
    atr_value: float = 0.0,
    price: float = 0.0,
) -> Tuple[bool, str]:
    """
    Determina si el mercado tiene condiciones aceptables para operar.

    Reemplaza is_session_valid() con un filtro basado en métricas reales:
    1. No es fin de semana (salvo cripto)
    2. Está dentro del horario de cierre real del instrumento
    3. ATR actual es suficiente para cubrir spread y generar profit

    Returns:
        (puede_operar: bool, motivo: str)
    """
    now = datetime.now(timezone.utc)
    weekday = now.weekday()   # 0=lunes, 5=sábado, 6=domingo
    hour    = now.hour

    # ── Regla 1: Fin de semana ────────────────────────────────────
    is_crypto = symbol in CRYPTO_SYMBOLS
    if not is_crypto:
        # Forex/indices cierran viernes ~22:00 UTC, reabren domingo ~22:00 UTC
        # weekday 4 (viernes) hora >= 22 → cerrado
        # weekday 5 (sábado) → cerrado
        # weekday 6 (domingo) hora < 22 → cerrado
        if weekday == 5:
            return False, "🚫 Fin de semana — mercado cerrado"
        if weekday == 6 and hour < 21:
            return False, "🚫 Domingo — mercado aún no abre (abre ~22:00 UTC)"
        if weekday == 4 and hour >= 22:
            return False, "🚫 Viernes cierre — mercado cerrado"

    # ── Regla 2: Horario duro del instrumento ─────────────────────
    hard = HARD_CLOSE_HOURS.get(symbol, {"hard_open": 0, "hard_close": 24})
    hard_open  = hard["hard_open"]
    hard_close = hard["hard_close"]

    if hard_close < 24:
        if not (hard_open <= hour < hard_close):
            return False, f"🚫 {symbol} fuera de horario ({hard_open:02d}:00-{hard_close:02d}:00 UTC)"

    # ── Regla 3: ATR mínimo (calidad de movimiento) ───────────────
    if atr_value > 0 and price > 0:
        atr_pct = (atr_value / price) * 100
        min_pct  = ATR_MIN_PCT.get(symbol, DEFAULT_ATR_MIN_PCT)

        if atr_pct < min_pct:
            return False, (
                f"📉 Mercado inactivo — ATR={atr_pct:.3f}% < mínimo={min_pct:.3f}% "
                f"({symbol} no se mueve suficiente)"
            )

    return True, "✅ Mercado operable"


def is_session_valid(symbol: str, sym_cfg: dict) -> bool:
    """
    Compatibilidad con código antiguo.
    Ahora solo verifica el horario duro (fin de semana + cierre real).
    El filtro de ATR se aplica por separado en _process_symbol con datos reales.
    """
    tradeable, _ = is_market_tradeable(symbol, sym_cfg)
    return tradeable


def get_lot_size(
    balance: float,
    sl_pips: float,
    symbol: str,
    point_value: float = 1.0,
) -> float:
    """
    Calcula el tamaño de lote para arriesgar exactamente RISK_PER_TRADE % del balance.
    Fórmula: Lote = (Balance × Riesgo%) / (SL_pips × Valor_por_pip)
    """
    risk_amount = balance * cfg.RISK_PER_TRADE
    if sl_pips <= 0 or point_value <= 0:
        return 0.01
    raw_lot = risk_amount / (sl_pips * point_value)
    lot = round(max(0.01, raw_lot), 2)
    log.debug(f"[risk] Lote: {lot} (balance={balance}, sl={sl_pips}, risk={risk_amount:.2f})")
    return lot


def get_lot_size_kelly(
    balance:      float,
    sl_pips:      float,
    symbol:       str,
    win_rate:     float,
    avg_rr:       float,
    n_trades:     int,
    point_value:  float = 1.0,
) -> float:
    """
    FASE 2 — Tamaño de lote con Criterio de Kelly Fraccionado.

    El Criterio de Kelly calcula la fracción óptima del capital a arriesgar:
        f* = (b×p - q) / b
    donde:
        p   = tasa de victorias (win_rate, 0.0–1.0)
        q   = 1 - p  (tasa de derrotas)
        b   = ratio recompensa/riesgo esperado (RR ratio del trade)

    Se usa Kelly Fraccionado (f_actual = KELLY_FRACTION × f*) que:
        • Reduce la volatilidad del equity vs. Kelly completo
        • Evita ruina incluso con errores de estimación en p y b
        • KELLY_FRACTION=0.25 = 25% del Kelly óptimo (conservador)

    SALVAGUARDAS:
        • Kelly negativo (f*<0) → usar sizing estándar (señal: no operar)
        • Lote mínimo: 0.01
        • Lote máximo: RISK_PER_TRADE × 3 del balance (3× el riesgo base)
        • Si n_trades < KELLY_MIN_TRADES → vuelve a sizing estándar

    Args:
        balance:     Balance actual de la cuenta
        sl_pips:     Distancia del Stop Loss en pips
        symbol:      Símbolo para logging
        win_rate:    Tasa de victorias histórica (0.0–1.0)
        avg_rr:      Ratio R:R promedio del trade actual (e.g. 2.0)
        n_trades:    Número de trades en el historial para este símbolo
        point_value: Valor en dinero por pip (por defecto 1.0)

    Returns:
        Tamaño de lote redondeado a 2 decimales
    """
    min_trades = getattr(cfg, "KELLY_MIN_TRADES", 30)
    fraction   = getattr(cfg, "KELLY_FRACTION",   0.25)
    # Cap: nunca arriesgar más de 3× el riesgo base por trade.
    # Con RISK_PER_TRADE=0.01 (1%): techo = 3% del balance por trade.
    # Esto protege contra estimaciones de p/b demasiado optimistas.
    max_risk   = cfg.RISK_PER_TRADE * 3.0

    # Insuficientes datos históricos → sizing estándar
    if n_trades < min_trades or win_rate <= 0 or avg_rr <= 0:
        return get_lot_size(balance, sl_pips, symbol, point_value)

    p = float(np.clip(win_rate, 0.01, 0.99))
    q = 1.0 - p
    b = float(max(avg_rr, 0.1))

    # Kelly óptimo
    f_star = (b * p - q) / b

    if f_star <= 0:
        # Kelly negativo → edge estadístico insuficiente → sizing estándar
        log.info(
            f"[risk/kelly] {symbol}: f*={f_star:.3f} ≤ 0 "
            f"(WR={win_rate:.1%} RR={avg_rr:.2f}) → sizing estándar"
        )
        return get_lot_size(balance, sl_pips, symbol, point_value)

    # Kelly fraccionado
    f_actual    = float(np.clip(fraction * f_star, 0.0, max_risk))
    risk_amount = balance * f_actual

    if sl_pips <= 0 or point_value <= 0:
        return 0.01

    raw_lot = risk_amount / (sl_pips * point_value)
    lot = round(max(0.01, raw_lot), 2)

    log.info(
        f"[risk/kelly] {symbol}: f*={f_star:.3f} → f_act={f_actual:.4f} "
        f"(WR={win_rate:.1%} RR={avg_rr:.2f} trades={n_trades}) → lote={lot}"
    )
    return lot


def calc_sl_tp(
    direction: str,
    price: float,
    atr: float,
    sym_cfg: dict,
) -> Tuple[float, float]:
    """SL y TP basados en ATR — dinámicos según volatilidad actual."""
    sl_mult = sym_cfg.get("sl_atr_mult", 2.0)
    tp_mult = sym_cfg.get("tp_atr_mult", 4.0)
    if direction == "BUY":
        sl = round(price - sl_mult * atr, 5)
        tp = round(price + tp_mult * atr, 5)
    else:
        sl = round(price + sl_mult * atr, 5)
        tp = round(price - tp_mult * atr, 5)
    return sl, tp


def is_rr_valid(price: float, sl: float, tp: float, min_rr: float = 1.5) -> bool:
    """Verifica R:R mínimo."""
    risk   = abs(price - sl)
    reward = abs(tp - price)
    if risk == 0:
        return False
    rr = reward / risk
    valid = rr >= min_rr
    if not valid:
        log.info(f"[risk] R:R {rr:.2f} < mínimo {min_rr} — trade rechazado")
    return valid


def is_daily_loss_ok(daily_pnl: float, balance: float) -> bool:
    """Para el bot si la pérdida diaria supera el límite configurado."""
    if balance <= 0:
        return True
    loss_pct = abs(min(0, daily_pnl)) / balance
    ok = loss_pct < cfg.MAX_DAILY_LOSS
    if not ok:
        log.warning(f"[risk] Límite diario: {loss_pct*100:.1f}% > {cfg.MAX_DAILY_LOSS*100:.1f}%")
    return ok


def get_rr(price: float, sl: float, tp: float) -> float:
    risk   = abs(price - sl)
    reward = abs(tp - price)
    return round(reward / risk, 2) if risk > 0 else 0.0


# ════════════════════════════════════════════════════════════════
#  CIRCUIT BREAKER INDIVIDUAL (Cisne Negro)
# ════════════════════════════════════════════════════════════════

def check_circuit_breaker(pos: dict, balance: float) -> Tuple[bool, str]:
    """
    Verifica si una posición abierta debe cerrarse de emergencia por superar
    el drawdown máximo permitido (CIRCUIT_BREAKER_MAX_DRAWDOWN_PCT del balance).

    Este mecanismo es INDEPENDIENTE del trailing stop y del equity guard global.
    Se activa ante eventos de "Cisne Negro" donde el precio colapsa violentamente
    antes de que el SL normal pueda ejecutarse al precio esperado.

    Args:
        pos:     Diccionario de posición (campos: profit, ticket, symbol).
        balance: Balance actual de la cuenta en divisa de la cuenta.

    Returns:
        (debe_cerrar: bool, motivo: str)
    """
    max_dd_pct = getattr(cfg, "CIRCUIT_BREAKER_MAX_DRAWDOWN_PCT", 3.0)
    if max_dd_pct <= 0 or balance <= 0:
        return False, "Circuit Breaker desactivado"

    profit = float(pos.get("profit", 0.0))
    if profit >= 0:
        return False, "Posición en ganancia"

    loss_pct = abs(profit) / balance * 100.0
    if loss_pct >= max_dd_pct:
        symbol = pos.get("symbol", "?")
        ticket = pos.get("ticket", 0)
        reason = (
            f"🚨 CIRCUIT BREAKER: #{ticket} {symbol} "
            f"drawdown {loss_pct:.2f}% ≥ límite {max_dd_pct:.2f}%"
        )
        log.warning(f"[risk/circuit_breaker] {reason}")
        return True, reason

    return False, f"Drawdown {loss_pct:.2f}% < límite {max_dd_pct:.2f}%"
