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
from typing import List, Tuple, Optional

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
            rho = _get_correlation(sym_i, sym_j)
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
