"""
╔══════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — modules/real_volume.py  (FASE 9)        ║
║                                                                  ║
║   Volumen real de Dukascopy para pares forex donde               ║
║   tick_volume de MT5/Exness tiene menor precisión.               ║
║                                                                  ║
║   Fuente: https://datafeed.dukascopy.com/datafeed/              ║
║   (gratuita, sin API key)                                        ║
║                                                                  ║
║   DISEÑO:                                                        ║
║   • Descarga archivos .bi5 (LZMA compressed, 20 bytes/tick)     ║
║   • Agrega ticks en candles M5 con volumen real                  ║
║   • Cache en memoria con TTL configurable (default 15 min)       ║
║   • Fallback silencioso → retorna None si falla                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

import io
import logging
import lzma
import os
import struct
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd
import requests

log = logging.getLogger(__name__)

# ── Símbolo map — Exness (con sufijo "m") → Dukascopy (sin sufijo) ──
SYMBOL_MAP: dict = {
    "EURUSDm": "EURUSD",
    "GBPUSDm": "GBPUSD",
    "USDJPYm": "USDJPY",
    "EURJPYm": "EURJPY",
    "GBPJPYm": "GBPJPY",
}

# Pares con JPY usan 3 decimales (divisor 1000), resto 5 decimales (divisor 100000)
_JPY_PAIRS = {"USDJPY", "EURJPY", "GBPJPY"}

# Directorio de caché en disco (para backtester)
_CACHE_DIR = "volume_cache"

# ── Cache en memoria ─────────────────────────────────────────────
_mem_cache: dict = {}
_mem_cache_ts: dict = {}


def _is_cache_valid(key: str, ttl_sec: int) -> bool:
    return key in _mem_cache and (time.time() - _mem_cache_ts.get(key, 0)) < ttl_sec


# ════════════════════════════════════════════════════════════════
#  DESCARGA Y PARSEO DE TICKS DUKASCOPY
# ════════════════════════════════════════════════════════════════

def _download_hour_ticks(
    duka_symbol: str,
    dt_utc: datetime,
    timeout: int = 15,
) -> Optional[pd.DataFrame]:
    """
    Descarga los ticks de una hora específica desde Dukascopy.

    URL: https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM}/{DD}/{HH}h_ticks.bi5
    IMPORTANTE: Los meses en Dukascopy son 0-based (enero=0, febrero=1, etc.)

    Formato .bi5 (LZMA compressed), cada tick record = 20 bytes:
      - 4 bytes uint32: offset en milisegundos desde inicio de la hora
      - 4 bytes uint32: ask (en pippettes — dividir entre 100000 o 1000)
      - 4 bytes uint32: bid (en pippettes)
      - 4 bytes float32: ask_vol
      - 4 bytes float32: bid_vol

    Retorna DataFrame con columnas: time, ask, bid, ask_vol, bid_vol, volume
    """
    year  = dt_utc.year
    month = dt_utc.month - 1   # 0-based
    day   = dt_utc.day
    hour  = dt_utc.hour

    url = (
        f"https://datafeed.dukascopy.com/datafeed/"
        f"{duka_symbol}/{year:04d}/{month:02d}/{day:02d}/{hour:02d}h_ticks.bi5"
    )

    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code == 404:
            # No hay datos para esa hora (fin de semana, festivo, etc.)
            return None
        resp.raise_for_status()
    except requests.RequestException as e:
        log.debug(f"[real_volume] Error descargando {url}: {e}")
        return None

    raw_bytes = resp.content
    if not raw_bytes:
        return None

    # Descomprimir LZMA
    try:
        decompressed = lzma.decompress(raw_bytes)
    except lzma.LZMAError as e:
        log.debug(f"[real_volume] Error descomprimiendo LZMA {url}: {e}")
        return None

    # Parsear registros de 20 bytes
    record_size = 20
    n_records = len(decompressed) // record_size
    if n_records == 0:
        return None

    divisor = 1_000 if duka_symbol in _JPY_PAIRS else 100_000

    rows = []
    base_ms = int(dt_utc.replace(minute=0, second=0, microsecond=0).timestamp() * 1000)

    for i in range(n_records):
        offset = i * record_size
        chunk = decompressed[offset: offset + record_size]
        if len(chunk) < record_size:
            break
        ms_offset, ask_raw, bid_raw, ask_vol, bid_vol = struct.unpack(">IIIff", chunk)
        ts_ms    = base_ms + ms_offset
        ask_price = ask_raw / divisor
        bid_price = bid_raw / divisor
        rows.append((ts_ms, ask_price, bid_price, float(ask_vol), float(bid_vol)))

    if not rows:
        return None

    df = pd.DataFrame(rows, columns=["ts_ms", "ask", "bid", "ask_vol", "bid_vol"])
    df["time"]   = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    df["volume"] = df["ask_vol"] + df["bid_vol"]
    df["close"]  = (df["ask"] + df["bid"]) / 2
    return df[["time", "ask", "bid", "ask_vol", "bid_vol", "volume", "close"]]


def _aggregate_to_m5(ticks_df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """
    Agrega ticks a candles de 5 minutos.

    Retorna DataFrame con columnas: time, open, high, low, close, volume
    compatible con el formato que espera volume_profile() en microstructure.py.
    """
    if ticks_df is None or len(ticks_df) == 0:
        return None

    df = ticks_df.copy()
    df = df.set_index("time")

    resampled = df["close"].resample("5min").ohlc()
    resampled["volume"] = df["volume"].resample("5min").sum()
    resampled = resampled.dropna(subset=["open"])
    resampled = resampled.reset_index()
    resampled.columns = ["time", "open", "high", "low", "close", "volume"]

    # Convertir time de tz-aware a tz-naive para compatibilidad con MT5 df
    resampled["time"] = resampled["time"].dt.tz_localize(None)

    return resampled if len(resampled) > 0 else None


# ════════════════════════════════════════════════════════════════
#  FUNCIÓN PRINCIPAL — get_real_volume_profile()
# ════════════════════════════════════════════════════════════════

def get_real_volume_profile(
    symbol: str,
    lookback_hours: int = 4,
    cache_ttl_min: int  = 15,
) -> Optional[pd.DataFrame]:
    """
    Descarga ticks reales de Dukascopy para el símbolo dado y los agrega
    en candles M5 con volumen real (ask_vol + bid_vol).

    Solo aplica a pares forex en SYMBOL_MAP. Para otros símbolos
    (XAUUSDm, BTCUSDm, índices) retorna None → microstructure usa tick_volume.

    Cache en memoria con TTL configurable para evitar requests excesivos.

    Args:
        symbol:         Símbolo Exness (e.g. "EURUSDm")
        lookback_hours: Cuántas horas previas descargar (default 4)
        cache_ttl_min:  TTL del cache en minutos (default 15)

    Returns:
        DataFrame M5 con columnas: time, open, high, low, close, volume
        o None si el símbolo no está en SYMBOL_MAP o si falla la descarga.
    """
    duka_symbol = SYMBOL_MAP.get(symbol)
    if duka_symbol is None:
        return None

    cache_key = f"rv_{symbol}_{lookback_hours}"
    if _is_cache_valid(cache_key, cache_ttl_min * 60):
        log.debug(f"[real_volume] Cache hit para {symbol}")
        return _mem_cache[cache_key]

    now_utc = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    all_ticks = []

    for h in range(lookback_hours, 0, -1):
        dt_hour = (now_utc - timedelta(hours=h)).replace(minute=0)
        ticks = _download_hour_ticks(duka_symbol, dt_hour)
        if ticks is not None and len(ticks) > 0:
            all_ticks.append(ticks)

    if not all_ticks:
        log.debug(f"[real_volume] Sin ticks para {symbol} en las últimas {lookback_hours}h")
        _mem_cache[cache_key] = None
        _mem_cache_ts[cache_key] = time.time()
        return None

    combined = pd.concat(all_ticks, ignore_index=True)
    m5_df = _aggregate_to_m5(combined)

    if m5_df is None or len(m5_df) == 0:
        _mem_cache[cache_key] = None
        _mem_cache_ts[cache_key] = time.time()
        return None

    log.info(
        f"[real_volume] {symbol} → {len(m5_df)} candles M5 de Dukascopy "
        f"(vol total: {m5_df['volume'].sum():.0f})"
    )
    _mem_cache[cache_key] = m5_df
    _mem_cache_ts[cache_key] = time.time()
    return m5_df


# ════════════════════════════════════════════════════════════════
#  FUNCIÓN PARA BACKTESTER — get_dukascopy_volume_for_period()
# ════════════════════════════════════════════════════════════════

def get_dukascopy_volume_for_period(
    symbol: str,
    start: datetime,
    end: datetime,
    cache_dir: str = _CACHE_DIR,
) -> Optional[pd.DataFrame]:
    """
    Descarga volumen real de Dukascopy para un período histórico completo.
    Guarda el resultado en CSV para evitar re-descargas.

    Args:
        symbol:    Símbolo Exness (e.g. "EURUSDm")
        start:     Fecha/hora de inicio (UTC, naive o tz-aware)
        end:       Fecha/hora de fin (UTC, naive o tz-aware)
        cache_dir: Directorio para guardar CSVs (default "volume_cache/")

    Returns:
        DataFrame M5 con columnas: time, open, high, low, close, volume
        o None si el símbolo no está soportado o falla la descarga.
    """
    duka_symbol = SYMBOL_MAP.get(symbol)
    if duka_symbol is None:
        return None

    os.makedirs(cache_dir, exist_ok=True)

    # Convertir a UTC naive si es necesario
    if hasattr(start, "tzinfo") and start.tzinfo is not None:
        start = start.astimezone(timezone.utc).replace(tzinfo=None)
    if hasattr(end, "tzinfo") and end.tzinfo is not None:
        end = end.astimezone(timezone.utc).replace(tzinfo=None)

    date_tag  = f"{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}"
    cache_csv = os.path.join(cache_dir, f"{duka_symbol}_{date_tag}_m5.csv")

    if os.path.exists(cache_csv):
        log.info(f"[real_volume] Cargando desde CSV: {cache_csv}")
        df = pd.read_csv(cache_csv, parse_dates=["time"])
        return df if len(df) > 0 else None

    log.info(
        f"[real_volume] Descargando período {start.date()} → {end.date()} "
        f"para {duka_symbol}..."
    )

    current = start.replace(minute=0, second=0, microsecond=0)
    all_ticks = []

    while current <= end:
        dt_utc = current.replace(tzinfo=timezone.utc)
        ticks  = _download_hour_ticks(duka_symbol, dt_utc)
        if ticks is not None and len(ticks) > 0:
            all_ticks.append(ticks)
        current += timedelta(hours=1)

    if not all_ticks:
        log.warning(f"[real_volume] Sin ticks para {symbol} en el período solicitado")
        return None

    combined = pd.concat(all_ticks, ignore_index=True)
    m5_df    = _aggregate_to_m5(combined)

    if m5_df is None or len(m5_df) == 0:
        return None

    m5_df.to_csv(cache_csv, index=False)
    log.info(f"[real_volume] Guardado: {cache_csv} ({len(m5_df)} candles M5)")
    return m5_df
