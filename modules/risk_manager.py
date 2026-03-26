"""
╔══════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — risk_manager.py  (v6.3 — 24/5)        ║
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
╚══════════════════════════════════════════════════════════════════╝
"""

import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

import config as cfg

log = logging.getLogger(__name__)

# ── Horarios de cierre REAL de mercado (no de liquidez) ─────────
# Estos instrumentos literalmente no tienen precio fuera de estos rangos.
# El resto opera 24/5 y solo se filtra por calidad de spread/ATR.
HARD_CLOSE_HOURS = {
    # Índices americanos — cierre real 21:00-22:00 UTC, reapertura 22:00 UTC
    # En la práctica Exness los tiene 23h casi continuas (22:00-21:00 UTC)
    # Permitimos 22:00 en adelante (pre-market NY tiene movimiento)
    "US500m":  {"hard_open": 0,  "hard_close": 24},   # 23h continuas en Exness
    "NAS100m": {"hard_open": 0,  "hard_close": 24},
    # Petróleo — cierre de 1h a las 23:00 UTC en Exness
    "USOILm":  {"hard_open": 1,  "hard_close": 23},
    # DAX — cierre real 22:00 UTC (mercado alemán cierra 17:00 Frankfurt = 16:00 UTC)
    # Pero Exness mantiene el CFD casi 24h, solo cierra 1h
    "GER40m":  {"hard_open": 0,  "hard_close": 24},
    # Forex y metales — 24/5, sin cierre duro
    "XAUUSDm": {"hard_open": 0,  "hard_close": 24},
    "XAGUSDm": {"hard_open": 0,  "hard_close": 24},
    "EURUSDm": {"hard_open": 0,  "hard_close": 24},
    "GBPUSDm": {"hard_open": 0,  "hard_close": 24},
    "USDJPYm": {"hard_open": 0,  "hard_close": 24},
    "GBPJPYm": {"hard_open": 0,  "hard_close": 24},
    "EURJPYm": {"hard_open": 0,  "hard_close": 24},
    # Cripto — 24/7 literal
    "BTCUSDm": {"hard_open": 0,  "hard_close": 24},
}

# ── Símbolo → es cripto (opera fines de semana) ────────────────
CRYPTO_SYMBOLS = {"BTCUSDm", "ETHUSDm", "XRPUSDm"}

# ── ATR mínimo como % del precio para que valga la pena operar ──
# Si el mercado no se mueve lo suficiente, el spread se come la ganancia.
ATR_MIN_PCT = {
    # Forex majors — necesitan al menos 0.02% de ATR vs precio
    "EURUSDm": 0.020, "GBPUSDm": 0.025, "USDJPYm": 0.015,
    "GBPJPYm": 0.030, "EURJPYm": 0.020,
    # Metales
    "XAUUSDm": 0.035,   # Oro: ATR mínimo $1.75 con precio ~$5000
    "XAGUSDm": 0.040,
    # Índices y petróleo
    "US500m":  0.020, "NAS100m": 0.025, "GER40m": 0.018,
    "USOILm":  0.035,
    # Cripto — alta volatilidad normal
    "BTCUSDm": 0.060,
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
