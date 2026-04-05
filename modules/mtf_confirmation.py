"""
ZAR v7 — Multi-Timeframe Confirmation (Mejora 12)

Verifica que el timeframe superior (H1 por defecto) confirme la dirección
de la señal de entrada. Calcula la tendencia del HTF usando los mismos
indicadores que compute_all():
  - DEMA fast vs slow
  - MACD histograma
  - Kalman trend
  - SuperTrend
  - Precio vs VWAP
  - Heiken Ashi

Retorna un dict con:
  - htf_trend:    "ALCISTA" | "BAJISTA" | "LATERAL" | ...
  - htf_agrees:   bool (True si HTF confirma la dirección de entrada)
  - htf_bull_votes: int (0-6)
  - htf_bear_votes: int (0-6)
  - htf_timeframe: str (e.g. "H1")
"""
import logging
import time
from typing import Optional

import config as cfg

log = logging.getLogger(__name__)

# ── Cache de resultados HTF por símbolo ──────────────────────────
# { symbol: {"result": dict, "timestamp": float} }
_mtf_cache: dict = {}

# Tendencias que confirman dirección alcista
_BULL_TRENDS = frozenset({"ALCISTA_FUERTE", "ALCISTA", "LATERAL_ALCISTA"})
# Tendencias que confirman dirección bajista
_BEAR_TRENDS = frozenset({"BAJISTA_FUERTE", "BAJISTA", "LATERAL_BAJISTA"})


def check_mtf_confirmation(symbol: str, direction: str, sym_cfg: dict) -> Optional[dict]:
    """
    Comprueba si el timeframe superior (H1 por defecto) confirma la
    dirección propuesta.

    Args:
        symbol:    Símbolo a evaluar (e.g. "EURUSD")
        direction: "BUY" o "SELL"
        sym_cfg:   Configuración del símbolo (dict de cfg.SYMBOLS)

    Returns:
        dict con htf_trend, htf_agrees, htf_bull_votes, htf_bear_votes,
        htf_timeframe — o None si no hay datos disponibles.
    """
    # Lazy imports para evitar dependencias circulares en tests
    from modules.execution import get_candles
    from modules.indicators import compute_all

    htf        = getattr(cfg, "MTF_HIGHER_TF", "H1")
    n_candles  = int(getattr(cfg, "MTF_CANDLES", 100))
    cache_ttl  = int(getattr(cfg, "MTF_CACHE_TTL_SEC", 300))

    # ── Verificar cache ──────────────────────────────────────────
    now = time.time()
    cached = _mtf_cache.get(symbol)
    if cached is not None and (now - cached["timestamp"]) < cache_ttl:
        result = cached["result"].copy()
        # Recalcular htf_agrees con la dirección actual
        result["htf_agrees"] = _agrees(result["htf_trend"], direction)
        log.debug(
            f"[MTF] {symbol} cache hit — HTF={result['htf_trend']} "
            f"dir={direction} agrees={result['htf_agrees']}"
        )
        return result

    # ── Pedir velas del HTF ──────────────────────────────────────
    df_htf = get_candles(symbol, htf, n_candles)
    if df_htf is None or len(df_htf) < 50:
        log.warning(f"[MTF] {symbol}: no hay datos suficientes de {htf} ({df_htf is None or 0 if df_htf is None else len(df_htf)} velas)")
        return None

    # ── Calcular indicadores en el HTF ───────────────────────────
    try:
        ind = compute_all(df_htf, symbol, sym_cfg)
    except Exception as exc:
        log.warning(f"[MTF] {symbol}: error compute_all en {htf}: {exc}")
        return None

    htf_trend = ind.get("h1_trend", "LATERAL")
    tv        = ind.get("trend_votes", {})
    bull_v    = int(tv.get("bull", 0))
    bear_v    = int(tv.get("bear", 0))

    agrees = _agrees(htf_trend, direction)

    result = {
        "htf_trend":      htf_trend,
        "htf_agrees":     agrees,
        "htf_bull_votes": bull_v,
        "htf_bear_votes": bear_v,
        "htf_timeframe":  htf,
    }

    # Guardar en cache (sin el campo htf_agrees que depende de direction)
    _mtf_cache[symbol] = {
        "result":    {k: v for k, v in result.items() if k != "htf_agrees"},
        "timestamp": now,
    }

    log.debug(
        f"[MTF] {symbol} {htf}: trend={htf_trend} bull={bull_v} bear={bear_v} "
        f"dir={direction} agrees={agrees}"
    )
    return result


def _agrees(htf_trend: str, direction: str) -> bool:
    """Determina si el HTF confirma la dirección de entrada."""
    if direction == "BUY":
        return htf_trend in _BULL_TRENDS
    if direction == "SELL":
        return htf_trend in _BEAR_TRENDS
    return False  # dirección desconocida → no confirma


def invalidate_cache(symbol: str) -> None:
    """Invalida el cache MTF de un símbolo (útil en tests)."""
    _mtf_cache.pop(symbol, None)
