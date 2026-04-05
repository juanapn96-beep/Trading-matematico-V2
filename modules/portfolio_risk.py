"""
╔══════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — portfolio_risk.py  (FASE 7)            ║
║                                                                  ║
║   Gestión de correlación entre activos.                         ║
║                                                                  ║
║   PROBLEMA: El bot puede abrir simultáneamente posiciones en    ║
║   pares altamente correlacionados (ej: EURUSDm + GBPUSDm en    ║
║   la misma dirección, ρ ≈ 0.82).  Con 3 posiciones             ║
║   correlacionadas al 80%, el riesgo efectivo sube de 1% a      ║
║   ~2.7% del balance.                                            ║
║                                                                  ║
║   SOLUCIÓN: Calcular la exposición efectiva del portafolio      ║
║   antes de abrir cada trade y bloquear si supera el umbral.     ║
╚══════════════════════════════════════════════════════════════════╝
"""

import logging
import math
from datetime import datetime, timezone
from typing import List, Tuple, Optional

import numpy as np

import config as cfg

log = logging.getLogger(__name__)

# ── Sufijo del broker ────────────────────────────────────────────
_S = cfg.BROKER_SUFFIX
_NO_SUFFIX_PR = getattr(cfg, "_NO_SUFFIX", {"USTEC", "DE40"})


def _sym(base: str) -> str:
    """Retorna el nombre completo del símbolo según el broker configurado."""
    if base in _NO_SUFFIX_PR:
        return base
    return f"{base}{_S}"


# ── Matriz de correlación estática entre pares ───────────────────
# Valores basados en correlaciones históricas promedio (5 años).
# Solo se registran pares con |ρ| ≥ 0.60; el resto se asume ρ = 0.
CORRELATION_MATRIX = {
    # Forex majors
    (_sym("EURUSD"), _sym("GBPUSD")):  0.82,
    (_sym("EURUSD"), _sym("EURJPY")):  0.78,
    (_sym("GBPUSD"), _sym("GBPJPY")):  0.85,
    (_sym("EURUSD"), _sym("USDJPY")): -0.65,
    (_sym("GBPUSD"), _sym("USDJPY")): -0.60,
    (_sym("EURJPY"), _sym("GBPJPY")):  0.90,
    # Índices americanos
    (_sym("US500"),  _sym("USTEC")):   0.93,
    # Metales
    (_sym("XAUUSD"), _sym("XAGUSD")): 0.85,
    # Índices vs metales (baja pero relevante en crisis)
    (_sym("XAUUSD"), _sym("US500")): -0.30,
    (_sym("XAUUSD"), _sym("USTEC")): -0.25,
}

# Umbral máximo de riesgo efectivo del portafolio (% del balance).
# Con RISK_PER_TRADE = 1%, MAX_PORTFOLIO_RISK = 5% limita la
# exposición real incluyendo efecto de correlación.
MAX_PORTFOLIO_RISK_PCT = 5.0


def _get_correlation(sym_a: str, sym_b: str) -> float:
    """Retorna la correlación entre dos símbolos.  Orden-agnóstico."""
    if sym_a == sym_b:
        return 1.0
    return CORRELATION_MATRIX.get(
        (sym_a, sym_b),
        CORRELATION_MATRIX.get((sym_b, sym_a), 0.0),
    )


# ── Rolling Correlation Cache ────────────────────────────────────
_rolling_corr_cache: dict = {}  # {(sym_a, sym_b): {"corr": float, "ts": float}}

# Use config values with module-level fallbacks
_ROLLING_CORR_TTL_SEC = 300  # default; overridden by cfg at runtime
_ROLLING_CORR_WINDOW  = 20   # default; overridden by cfg at runtime


def _compute_rolling_correlation(sym_a: str, sym_b: str) -> Optional[float]:
    """
    Calculate rolling Pearson correlation between two symbols using
    the last ROLLING_CORR_WINDOW H1 close prices.

    Returns None if insufficient data available.
    Uses MT5 to fetch candle data (import at function level to avoid
    import errors in test/backtest contexts).
    """
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return None

    window = int(getattr(cfg, "ROLLING_CORR_WINDOW", _ROLLING_CORR_WINDOW))
    tf = mt5.TIMEFRAME_H1
    n  = window + 5  # extra buffer for safety

    rates_a = mt5.copy_rates_from_pos(sym_a, tf, 0, n)
    rates_b = mt5.copy_rates_from_pos(sym_b, tf, 0, n)

    if rates_a is None or rates_b is None:
        return None
    if len(rates_a) < window or len(rates_b) < window:
        return None

    # Align by timestamp — use only matching candles
    # MT5 structured arrays: field 'time' = timestamp, field 'close' = close price
    times_a = {r['time']: r['close'] for r in rates_a}
    times_b = {r['time']: r['close'] for r in rates_b}

    common_times = sorted(set(times_a.keys()) & set(times_b.keys()))
    if len(common_times) < window:
        return None

    # Take the last ROLLING_CORR_WINDOW common timestamps
    common_times = common_times[-window:]

    closes_a = np.array([times_a[t] for t in common_times])
    closes_b = np.array([times_b[t] for t in common_times])

    # Calculate returns
    returns_a = np.diff(closes_a) / closes_a[:-1]
    returns_b = np.diff(closes_b) / closes_b[:-1]

    if len(returns_a) < 5:
        return None

    # Pearson correlation
    corr = float(np.corrcoef(returns_a, returns_b)[0, 1])
    if np.isnan(corr):
        return None

    return round(corr, 3)


def _get_correlation_dynamic(sym_a: str, sym_b: str) -> float:
    """
    Get correlation between two symbols.
    Tries rolling correlation first (cached with TTL), falls back to static matrix.
    """
    if sym_a == sym_b:
        return 1.0

    if not getattr(cfg, "ROLLING_CORR_ENABLED", True):
        return _get_correlation(sym_a, sym_b)

    # Normalize key order for cache
    key = tuple(sorted([sym_a, sym_b]))
    now = datetime.now(timezone.utc).timestamp()
    ttl = int(getattr(cfg, "ROLLING_CORR_TTL_SEC", _ROLLING_CORR_TTL_SEC))

    cached = _rolling_corr_cache.get(key)
    if cached and (now - cached["ts"]) < ttl:
        return cached["corr"]

    # Try computing rolling correlation
    rolling = _compute_rolling_correlation(sym_a, sym_b)
    if rolling is not None:
        _rolling_corr_cache[key] = {"corr": rolling, "ts": now}
        return rolling

    # Fallback to static
    static = _get_correlation(sym_a, sym_b)
    _rolling_corr_cache[key] = {"corr": static, "ts": now}
    return static


def get_effective_portfolio_risk(
    open_positions: list,
    new_symbol: str,
    new_direction: str,
    risk_per_trade: float = None,
) -> Tuple[float, bool, str]:
    """
    Calcula el riesgo efectivo del portafolio considerando correlaciones
    entre las posiciones abiertas y el nuevo trade propuesto.

    Fórmula simplificada (varianza del portafolio):
        σ²_p = Σᵢ σᵢ² + 2 × Σᵢ<j ρᵢⱼ × σᵢ × σⱼ × sign_factor
    donde sign_factor = +1 si misma dirección, -1 si opuesta (cobertura),
    y σᵢ = risk_per_trade para cada posición.

    Returns:
        (risk_pct, should_block, reason)
    """
    if risk_per_trade is None:
        risk_per_trade = getattr(cfg, "RISK_PER_TRADE", 0.01)

    max_risk_pct = getattr(cfg, "MAX_PORTFOLIO_RISK_PCT", MAX_PORTFOLIO_RISK_PCT)

    # Construir lista de (symbol, direction) incluyendo el nuevo trade
    positions: List[Tuple[str, str]] = []

    for pos in open_positions:
        sym = getattr(pos, "symbol", "")
        # MT5: type 0 = BUY, type 1 = SELL
        direction = "BUY" if getattr(pos, "type", 0) == 0 else "SELL"
        magic = getattr(pos, "magic", 0)
        if magic == getattr(cfg, "MAGIC_NUMBER", 0) and sym:
            positions.append((sym, direction))

    # Añadir la posición propuesta
    positions.append((new_symbol, new_direction))

    n = len(positions)
    if n <= 1:
        # Solo el trade nuevo, sin correlación que calcular
        return risk_per_trade * 100, False, "Sin posiciones correlacionadas"

    # Calcular varianza del portafolio
    risk_sq = risk_per_trade ** 2
    variance = 0.0
    for i in range(n):
        variance += risk_sq
        for j in range(i + 1, n):
            sym_i, dir_i = positions[i]
            sym_j, dir_j = positions[j]
            rho = _get_correlation_dynamic(sym_i, sym_j)
            # Si ambas posiciones van en la misma dirección, la correlación
            # positiva AUMENTA el riesgo.  Si van en dirección opuesta con
            # correlación positiva, REDUCE el riesgo (cobertura parcial).
            same_direction = (dir_i == dir_j)
            sign_factor = 1.0 if same_direction else -1.0
            variance += 2 * rho * sign_factor * risk_sq

    # Riesgo efectivo = desviación estándar del portafolio
    effective_risk = math.sqrt(max(0.0, variance))
    effective_risk_pct = effective_risk * 100

    should_block = effective_risk_pct > max_risk_pct
    if should_block:
        reason = (
            f"Riesgo portafolio {effective_risk_pct:.1f}% > {max_risk_pct:.1f}% "
            f"(correlaciones entre {n} posiciones)"
        )
        log.warning(f"[portfolio_risk] {reason}")
    else:
        reason = f"Riesgo portafolio {effective_risk_pct:.1f}% OK ({n} posiciones)"

    return effective_risk_pct, should_block, reason
