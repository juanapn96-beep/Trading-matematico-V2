"""
╔══════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — sentiment_data.py  (FASE 7)            ║
║                                                                  ║
║   Datos de sentimiento de mercado de fuentes gratuitas:         ║
║   • Crypto Fear & Greed Index  (BTCUSDm)                       ║
║   • CBOE VIX — volatilidad implícita  (US500m, NAS100m)        ║
║                                                                  ║
║   Los datos se cachean con TTL para evitar exceder rate-limits. ║
╚══════════════════════════════════════════════════════════════════╝
"""

import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ── Cache ────────────────────────────────────────────────────────
_cache: dict = {}
_cache_ts: dict = {}
CACHE_TTL_SEC = 900  # 15 min


def _is_cache_valid(key: str) -> bool:
    return key in _cache and (time.time() - _cache_ts.get(key, 0)) < CACHE_TTL_SEC


# ── Crypto Fear & Greed Index ────────────────────────────────────
def get_crypto_fear_greed() -> Optional[float]:
    """
    Retorna el índice de Fear & Greed para crypto (0–100).
    <25 = Extreme Fear,  25-45 = Fear,  45-55 = Neutral,
    55-75 = Greed,  >75 = Extreme Greed.

    Fuente: alternative.me (gratuita, sin API key).
    """
    key = "crypto_fng"
    if _is_cache_valid(key):
        return _cache[key]

    try:
        resp = requests.get(
            "https://api.alternative.me/fng/",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        value = float(data["data"][0]["value"])
        _cache[key] = value
        _cache_ts[key] = time.time()
        log.info(f"[sentiment] Crypto Fear & Greed = {value:.0f}")
        return value
    except Exception as e:
        log.debug(f"[sentiment] Error obteniendo Fear & Greed: {e}")
        return _cache.get(key)


# ── CBOE VIX ─────────────────────────────────────────────────────
def get_vix() -> Optional[float]:
    """
    Retorna el último valor del VIX (CBOE Volatility Index).
    VIX < 15 = baja volatilidad,  15-25 = normal,
    25-35 = alta volatilidad,  > 35 = pánico.

    Fuente: CBOE delayed quotes (gratuita, JSON público).
    """
    key = "vix"
    if _is_cache_valid(key):
        return _cache[key]

    try:
        resp = requests.get(
            "https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/_VIX.json",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        # La última entrada tiene el valor más reciente
        if "data" in data and len(data["data"]) > 0:
            last_entry = data["data"][-1]
            vix_val = float(last_entry.get("close", last_entry.get("value", 0)))
        else:
            return _cache.get(key)

        _cache[key] = vix_val
        _cache_ts[key] = time.time()
        log.info(f"[sentiment] VIX = {vix_val:.2f}")
        return vix_val
    except Exception as e:
        log.debug(f"[sentiment] Error obteniendo VIX: {e}")
        return _cache.get(key)


# ── Mapa de símbolo → fuentes de sentimiento relevantes ─────────
SYMBOL_SENTIMENT_MAP = {
    "BTCUSDm": ["crypto_fng"],
    "US500m":  ["vix"],
    "NAS100m": ["vix"],
    "GER40m":  ["vix"],
    "XAUUSDm": ["vix"],
    "XAGUSDm": ["vix"],
    "USOILm":  ["vix"],
}


def get_sentiment_for_symbol(symbol: str) -> dict:
    """
    Retorna un diccionario con los datos de sentimiento relevantes
    para el símbolo dado.

    Ejemplo de retorno:
    {
        "crypto_fng": 72.0,        # solo si aplica
        "crypto_fng_label": "Greed",
        "vix": 18.5,               # solo si aplica
        "vix_label": "Normal",
        "sentiment_bias": "NEUTRAL" # FEAR / GREED / NEUTRAL
    }
    """
    result: dict = {}
    sources = SYMBOL_SENTIMENT_MAP.get(symbol, [])

    if "crypto_fng" in sources:
        fng = get_crypto_fear_greed()
        if fng is not None:
            result["crypto_fng"] = round(fng, 1)
            if fng < 25:
                result["crypto_fng_label"] = "Extreme Fear"
            elif fng < 45:
                result["crypto_fng_label"] = "Fear"
            elif fng < 55:
                result["crypto_fng_label"] = "Neutral"
            elif fng < 75:
                result["crypto_fng_label"] = "Greed"
            else:
                result["crypto_fng_label"] = "Extreme Greed"

    if "vix" in sources:
        vix = get_vix()
        if vix is not None:
            result["vix"] = round(vix, 2)
            if vix < 15:
                result["vix_label"] = "Baja volatilidad"
            elif vix < 25:
                result["vix_label"] = "Normal"
            elif vix < 35:
                result["vix_label"] = "Alta volatilidad"
            else:
                result["vix_label"] = "Pánico"

    # Sesgo general de sentimiento
    bias = "NEUTRAL"
    fng_val = result.get("crypto_fng")
    vix_val = result.get("vix")

    if fng_val is not None:
        if fng_val < 25:
            bias = "FEAR"
        elif fng_val > 75:
            bias = "GREED"
    elif vix_val is not None:
        if vix_val > 30:
            bias = "FEAR"
        elif vix_val < 14:
            bias = "GREED"

    result["sentiment_bias"] = bias
    return result
