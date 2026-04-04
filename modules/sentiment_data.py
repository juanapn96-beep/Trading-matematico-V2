"""
╔══════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — sentiment_data.py  (FASE 7 + FASE 9)   ║
║                                                                  ║
║   Datos de sentimiento de mercado de fuentes gratuitas:         ║
║   • Crypto Fear & Greed Index  (BTCUSDm)                       ║
║   • CBOE VIX — volatilidad implícita  (US500m, USTEC)         ║
║   • CFTC COT Report — posicionamiento institucional             ║
║     (forex, commodities, índices, crypto)                       ║
║                                                                  ║
║   Los datos se cachean con TTL para evitar exceder rate-limits. ║
╚══════════════════════════════════════════════════════════════════╝
"""

import csv
import io
import logging
import time
from typing import Optional

import requests
import config as cfg

log = logging.getLogger(__name__)


def _sym(base: str) -> str:
    """Retorna el nombre completo del símbolo según el broker configurado."""
    if base in cfg._NO_SUFFIX:
        return base
    return f"{base}{cfg.BROKER_SUFFIX}"

# ── Cache ────────────────────────────────────────────────────────
_cache: dict = {}
_cache_ts: dict = {}
CACHE_TTL_SEC     = 900    # 15 min  — Fear & Greed, VIX
COT_CACHE_TTL_SEC = 86400  # 24 horas — COT (actualiza solo los viernes)


def _is_cache_valid(key: str, ttl: int = CACHE_TTL_SEC) -> bool:
    return key in _cache and (time.time() - _cache_ts.get(key, 0)) < ttl


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
            close_val = last_entry.get("close")
            value_val = last_entry.get("value")
            vix_val = float(close_val if close_val is not None else (value_val or 0))
        else:
            return _cache.get(key)

        _cache[key] = vix_val
        _cache_ts[key] = time.time()
        log.info(f"[sentiment] VIX = {vix_val:.2f}")
        return vix_val
    except Exception as e:
        log.debug(f"[sentiment] Error obteniendo VIX: {e}")
        return _cache.get(key)


# ════════════════════════════════════════════════════════════════
#  CFTC COT REPORT  (FASE 9)
# ════════════════════════════════════════════════════════════════

# Nombres de mercado en el reporte CFTC → símbolo interno
_COT_MARKET_MAP = {
    "EURO FX":              "EUR",
    "BRITISH POUND":        "GBP",
    "JAPANESE YEN":         "JPY",
    "GOLD":                 "GOLD",
    "SILVER":               "SILVER",
    "CRUDE OIL, LIGHT SWEET": "OIL",
    "S&P 500 STOCK INDEX":  "SP500",
    "BITCOIN":              "BTC",
}

# Símbolo interno COT → símbolos del broker configurado que aplican
_COT_SYMBOL_COVERAGE = {
    "EUR":    [_sym("EURUSD"), _sym("EURJPY")],
    "GBP":    [_sym("GBPUSD"), _sym("GBPJPY")],
    "JPY":    [_sym("USDJPY"), _sym("EURJPY"), _sym("GBPJPY")],
    "GOLD":   [_sym("XAUUSD")],
    "SILVER": [_sym("XAGUSD")],
    "OIL":    [_sym("USOIL")],
    "SP500":  [_sym("US500")],
    "BTC":    [_sym("BTCUSD")],
}

# URL del reporte COT (archivo de texto plano, sin key)
_COT_URL = "https://www.cftc.gov/dea/newcot/deafut.txt"


def _parse_cot_row(row: dict) -> Optional[dict]:
    """
    Extrae posición neta Non-Commercial de una fila del reporte COT.

    El reporte deafut.txt tiene columnas:
    'Market and Exchange Names', 'As of Date in Form YYYY-MM-DD',
    'NonComm_Positions_Long_All', 'NonComm_Positions_Short_All', ...
    """
    try:
        long_nc  = float(row.get("NonComm_Positions_Long_All",  0) or 0)
        short_nc = float(row.get("NonComm_Positions_Short_All", 0) or 0)
        net      = long_nc - short_nc
        total    = long_nc + short_nc
        return {
            "net_position": net,
            "long":         long_nc,
            "short":        short_nc,
            "total":        total,
            "report_date":  row.get("As of Date in Form YYYY-MM-DD", ""),
        }
    except Exception:
        return None


def get_cot_data() -> Optional[dict]:
    """
    Descarga y parsea el reporte COT semanal de la CFTC.
    Cache de 24 horas (el reporte se actualiza solo los viernes).

    Retorna dict anidado:
    {
        "EUR":  {"net_position": 12345, "long": ..., "short": ...,
                 "pct_change_weekly": ..., "positioning_bias": "BULLISH"},
        "GBP":  {...},
        ...
    }
    o None si falla la descarga.
    """
    key = "cot_data"
    if _is_cache_valid(key, COT_CACHE_TTL_SEC):
        return _cache[key]

    try:
        resp = requests.get(_COT_URL, timeout=20)
        resp.raise_for_status()
        content = resp.text
    except Exception as e:
        log.debug(f"[sentiment] Error descargando COT: {e}")
        return _cache.get(key)

    result = {}
    try:
        reader = csv.DictReader(io.StringIO(content))
        # Mantener solo la entrada más reciente por mercado
        latest: dict = {}
        for row in reader:
            market_raw = (row.get("Market and Exchange Names") or "").strip().upper()
            for keyword, cot_key in _COT_MARKET_MAP.items():
                if keyword in market_raw:
                    parsed = _parse_cot_row(row)
                    if parsed is None:
                        continue
                    if cot_key not in latest:
                        latest[cot_key] = parsed
                    # El CSV viene ordenado más reciente primero —
                    # si ya tenemos entrada para este mercado, ignorar las siguientes.
                    break

        # Calcular sesgo de posicionamiento
        for cot_key, data in latest.items():
            net   = data["net_position"]
            total = max(data["total"], 1)
            pct   = round(net / total * 100, 1)

            if pct > 10:
                bias = "BULLISH"
            elif pct < -10:
                bias = "BEARISH"
            else:
                bias = "NEUTRAL"

            result[cot_key] = {
                "net_position":       round(net),
                "long":               round(data["long"]),
                "short":              round(data["short"]),
                "pct_net":            pct,
                "positioning_bias":   bias,
                "report_date":        data["report_date"],
                # pct_change_weekly requiere historial de semanas anteriores;
                # se expone como 0.0 hasta que se implemente cache persistente.
                "pct_change_weekly":  0.0,
            }

    except Exception as e:
        log.debug(f"[sentiment] Error parseando COT: {e}")
        return _cache.get(key)

    if result:
        _cache[key] = result
        _cache_ts[key] = time.time()
        log.info(f"[sentiment] COT cargado: {list(result.keys())}")

    return result or None


def _get_cot_for_symbol(symbol: str) -> Optional[dict]:
    """
    Retorna el dato COT aplicable al símbolo Exness dado.
    Si varios COT aplican (e.g. EURJPYm → EUR y JPY), retorna el más reciente.
    Respeta la bandera COT_ENABLED en config.py.
    """
    try:
        import config as cfg
        if not getattr(cfg, "COT_ENABLED", True):
            return None
    except Exception:
        pass

    cot_all = get_cot_data()
    if cot_all is None:
        return None

    relevant = {}
    for cot_key, symbols in _COT_SYMBOL_COVERAGE.items():
        if symbol in symbols and cot_key in cot_all:
            relevant[cot_key] = cot_all[cot_key]

    if not relevant:
        return None

    # Para pares cruzados (e.g. EURJPY), devolver el COT con mayor |net|
    return max(relevant.values(), key=lambda d: abs(d["net_position"]))


# ── Mapa de símbolo → fuentes de sentimiento relevantes ─────────
SYMBOL_SENTIMENT_MAP = {
    _sym("BTCUSD"): ["crypto_fng", "cot"],
    _sym("US500"):  ["vix", "cot"],
    _sym("USTEC"):  ["vix"],
    _sym("DE40"):   ["vix"],
    _sym("XAUUSD"): ["vix", "cot"],
    _sym("XAGUSD"): ["vix", "cot"],
    _sym("USOIL"):  ["vix", "cot"],
    _sym("EURUSD"): ["cot"],
    _sym("GBPUSD"): ["cot"],
    _sym("USDJPY"): ["cot"],
    _sym("EURJPY"): ["cot"],
    _sym("GBPJPY"): ["cot"],
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
        "cot_net_position": 12345, # solo si aplica
        "cot_bias": "BULLISH",
        "cot_change_pct": 0.0,
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

    if "cot" in sources:
        cot = _get_cot_for_symbol(symbol)
        if cot is not None:
            result["cot_net_position"] = cot["net_position"]
            result["cot_bias"]         = cot["positioning_bias"]
            result["cot_change_pct"]   = cot["pct_change_weekly"]
            result["cot_report_date"]  = cot.get("report_date", "")

    # Sesgo general de sentimiento
    bias = "NEUTRAL"
    fng_val = result.get("crypto_fng")
    vix_val = result.get("vix")
    cot_bias = result.get("cot_bias")

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
    elif cot_bias is not None and cot_bias != "NEUTRAL":
        # Usar COT como señal de sesgo si no hay otra fuente
        bias = "GREED" if cot_bias == "BULLISH" else "FEAR"

    result["sentiment_bias"] = bias
    return result

