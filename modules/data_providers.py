"""
╔══════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — modules/data_providers.py  (FASE 11)    ║
║                                                                  ║
║   Módulo unificado de proveedores externos de datos de mercado.  ║
║                                                                  ║
║   Proveedores implementados:                                     ║
║   • TwelveDataProvider  — datos OHLCV de índices via API REST    ║
║   • PolygonProvider     — datos US (SPX/NDX) via API REST        ║
║   • TrueFXLoader        — tick data CSV local en data/truefx/    ║
║                                                                  ║
║   DISEÑO:                                                        ║
║   • Fallback silencioso: cualquier fallo retorna None             ║
║   • Cache en memoria con TTL configurable                        ║
║   • Rate limiting estricto (Twelve Data: 8/min, Polygon: 5/min)  ║
║   • TrueFX: lectura local, sin red, sin API key                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import calendar
import glob as _glob
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────
#  Helpers de caché en memoria
# ──────────────────────────────────────────────────────────────────

class _MemCache:
    """Cache en memoria thread-safe con TTL en segundos."""

    def __init__(self) -> None:
        self._store: dict = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            val, exp = entry
            if time.monotonic() > exp:
                del self._store[key]
                return None
            return val

    def set(self, key: str, value, ttl_sec: float) -> None:
        with self._lock:
            self._store[key] = (value, time.monotonic() + ttl_sec)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)


# ──────────────────────────────────────────────────────────────────
#  Rate limiter simple (llamadas por minuto)
# ──────────────────────────────────────────────────────────────────

class _RateLimiter:
    """Limita el número de llamadas en una ventana deslizante de 60 s."""

    def __init__(self, max_per_minute: int) -> None:
        self._max = max_per_minute
        self._timestamps: list = []
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        """Retorna True si se puede hacer la llamada ahora; False si se excedería el límite."""
        with self._lock:
            now = time.monotonic()
            window_start = now - 60.0
            self._timestamps = [t for t in self._timestamps if t > window_start]
            if len(self._timestamps) >= self._max:
                return False
            self._timestamps.append(now)
            return True


# ══════════════════════════════════════════════════════════════════
#  TWELVE DATA PROVIDER
# ══════════════════════════════════════════════════════════════════

class TwelveDataProvider:
    """
    Datos OHLCV en tiempo real de índices y forex via Twelve Data API.

    Tier gratuito: 800 llamadas/día, 8 llamadas/minuto.
    API key desde .env: TWELVE_DATA_KEY
    """

    BASE_URL = "https://api.twelvedata.com"

    # Mapeo de símbolos Exness → Twelve Data
    SYMBOL_MAP: dict[str, str] = {
        "US500m":  "SPX",       # S&P 500
        "NAS100m": "NDX",       # Nasdaq 100
        "GER40m":  "DAX",       # DAX 40
        "USOILm":  "CL",        # Crude Oil
        "XAUUSDm": "XAU/USD",   # Gold
        "BTCUSDm": "BTC/USD",   # Bitcoin
    }

    def __init__(
        self,
        api_key: str = "",
        cache_ttl_min: int = 5,
        max_calls_per_min: int = 8,
        timeout: int = 10,
    ) -> None:
        self._api_key = api_key
        self._cache_ttl_sec = cache_ttl_min * 60
        self._cache = _MemCache()
        self._rate = _RateLimiter(max_calls_per_min)
        self._timeout = timeout

    # ── Helpers privados ──────────────────────────────────────────

    def _get(self, endpoint: str, params: dict) -> Optional[dict]:
        """Realiza GET con rate limiting y retorna JSON o None."""
        if not self._api_key:
            log.debug("[TwelveData] API key no configurada")
            return None
        if not self._rate.acquire():
            log.debug("[TwelveData] Rate limit alcanzado, omitiendo llamada")
            return None
        params["apikey"] = self._api_key
        url = f"{self.BASE_URL}/{endpoint}"
        try:
            resp = requests.get(url, params=params, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "error":
                log.debug(f"[TwelveData] Error API: {data.get('message', data)}")
                return None
            return data
        except Exception as exc:
            log.debug(f"[TwelveData] Fallo GET {endpoint}: {exc}")
            return None

    # ── API pública ───────────────────────────────────────────────

    def get_realtime_ohlcv(
        self,
        symbol: str,
        interval: str = "5min",
        outputsize: int = 100,
    ) -> Optional[pd.DataFrame]:
        """
        Retorna DataFrame con columnas open, high, low, close, volume
        ordenado de más antiguo a más reciente.

        Args:
            symbol:     Símbolo Exness (e.g. "US500m")
            interval:   Intervalo ("1min", "5min", "15min", "1h", "1day")
            outputsize: Número de barras a obtener (max 5000)
        """
        td_sym = self.SYMBOL_MAP.get(symbol)
        if not td_sym:
            return None

        cache_key = f"ohlcv:{td_sym}:{interval}:{outputsize}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        data = self._get(
            "time_series",
            {"symbol": td_sym, "interval": interval, "outputsize": outputsize},
        )
        if data is None:
            return None

        values = data.get("values")
        if not values:
            log.debug(f"[TwelveData] Sin datos 'values' para {td_sym}")
            return None

        try:
            df = pd.DataFrame(values)
            df["time"] = pd.to_datetime(df["datetime"], utc=True)
            for col in ("open", "high", "low", "close", "volume"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            cols = ["time"] + [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
            df = df[cols].sort_values("time").reset_index(drop=True)
            self._cache.set(cache_key, df, self._cache_ttl_sec)
            log.debug(f"[TwelveData] {td_sym} {interval}: {len(df)} barras obtenidas")
            return df
        except Exception as exc:
            log.debug(f"[TwelveData] Error parseando datos de {td_sym}: {exc}")
            return None

    def get_quote(self, symbol: str) -> Optional[dict]:
        """
        Retorna dict con precio actual, volumen y cambio % del día.

        Claves: symbol, name, exchange, currency, datetime, open, high, low,
                close, volume, previous_close, change, percent_change
        """
        td_sym = self.SYMBOL_MAP.get(symbol)
        if not td_sym:
            return None

        cache_key = f"quote:{td_sym}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        data = self._get("quote", {"symbol": td_sym})
        if data is None:
            return None

        try:
            result = {
                "symbol":        td_sym,
                "exness_symbol": symbol,
                "name":          data.get("name", ""),
                "close":         float(data.get("close", 0) or 0),
                "open":          float(data.get("open", 0) or 0),
                "high":          float(data.get("high", 0) or 0),
                "low":           float(data.get("low", 0) or 0),
                "volume":        float(data.get("volume", 0) or 0),
                "percent_change": float(data.get("percent_change", 0) or 0),
                "change":        float(data.get("change", 0) or 0),
                "datetime":      data.get("datetime", ""),
            }
            self._cache.set(cache_key, result, self._cache_ttl_sec)
            return result
        except Exception as exc:
            log.debug(f"[TwelveData] Error parseando quote de {td_sym}: {exc}")
            return None


# ══════════════════════════════════════════════════════════════════
#  POLYGON.IO PROVIDER
# ══════════════════════════════════════════════════════════════════

class PolygonProvider:
    """
    Datos de mercado US (S&P 500 / Nasdaq 100) via Polygon.io.

    Tier gratuito: 5 req/min.
    API key desde .env: POLYGON_KEY
    """

    BASE_URL = "https://api.polygon.io"

    # Mapeo a tickers de Polygon
    SYMBOL_MAP: dict[str, str] = {
        "US500m":  "I:SPX",   # S&P 500 index
        "NAS100m": "I:NDX",   # Nasdaq 100 index
    }

    def __init__(
        self,
        api_key: str = "",
        cache_ttl_min: int = 5,
        max_calls_per_min: int = 5,
        timeout: int = 10,
    ) -> None:
        self._api_key = api_key
        self._cache_ttl_sec = cache_ttl_min * 60
        self._cache = _MemCache()
        self._rate = _RateLimiter(max_calls_per_min)
        self._timeout = timeout

    # ── Helpers privados ──────────────────────────────────────────

    def _get(self, path: str, params: dict = None) -> Optional[dict]:
        """Realiza GET con rate limiting y retorna JSON o None."""
        if not self._api_key:
            log.debug("[Polygon] API key no configurada")
            return None
        if not self._rate.acquire():
            log.debug("[Polygon] Rate limit alcanzado, omitiendo llamada")
            return None
        if params is None:
            params = {}
        params["apiKey"] = self._api_key
        url = f"{self.BASE_URL}{path}"
        try:
            resp = requests.get(url, params=params, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status", "")
            if status not in ("OK", "DELAYED", ""):
                log.debug(f"[Polygon] Status inesperado: {status} — {data.get('message','')}")
                return None
            return data
        except Exception as exc:
            log.debug(f"[Polygon] Fallo GET {path}: {exc}")
            return None

    # ── API pública ───────────────────────────────────────────────

    def get_aggregates(
        self,
        symbol: str,
        timespan: str = "minute",
        multiplier: int = 5,
        from_date: str = None,
        to_date: str = None,
    ) -> Optional[pd.DataFrame]:
        """
        Retorna DataFrame con columnas: time, open, high, low, close, volume, vwap, transactions.

        Args:
            symbol:     Símbolo Exness (e.g. "US500m")
            timespan:   "minute", "hour", "day"
            multiplier: Multiplicador del intervalo (5 → barras de 5 min)
            from_date:  Fecha inicio "YYYY-MM-DD" (default: hoy - 3 días)
            to_date:    Fecha fin   "YYYY-MM-DD" (default: hoy)
        """
        poly_sym = self.SYMBOL_MAP.get(symbol)
        if not poly_sym:
            return None

        today = datetime.now(timezone.utc)
        if to_date is None:
            to_date = today.strftime("%Y-%m-%d")
        if from_date is None:
            from_date = today.strftime("%Y-%m-%d")

        cache_key = f"aggs:{poly_sym}:{multiplier}:{timespan}:{from_date}:{to_date}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        path = f"/v2/aggs/ticker/{poly_sym}/range/{multiplier}/{timespan}/{from_date}/{to_date}"
        data = self._get(path, {"adjusted": "true", "sort": "asc", "limit": 50000})
        if data is None:
            return None

        results = data.get("results")
        if not results:
            log.debug(f"[Polygon] Sin resultados para {poly_sym}")
            return None

        try:
            df = pd.DataFrame(results)
            # Polygon timestamps en milisegundos UNIX
            df["time"] = pd.to_datetime(df["t"], unit="ms", utc=True)
            rename = {"o": "open", "h": "high", "l": "low", "c": "close",
                      "v": "volume", "vw": "vwap", "n": "transactions"}
            df = df.rename(columns=rename)
            cols = ["time"] + [c for c in ("open", "high", "low", "close", "volume", "vwap", "transactions") if c in df.columns]
            df = df[cols].reset_index(drop=True)
            self._cache.set(cache_key, df, self._cache_ttl_sec)
            log.debug(f"[Polygon] {poly_sym} {multiplier}{timespan}: {len(df)} barras obtenidas")
            return df
        except Exception as exc:
            log.debug(f"[Polygon] Error parseando aggregates de {poly_sym}: {exc}")
            return None

    def get_snapshot(self, symbol: str) -> Optional[dict]:
        """
        Retorna snapshot actual: precio, volumen del día, prev_close.

        Claves: symbol, day_close, day_volume, day_vwap, prev_close, todaysChangePerc
        """
        poly_sym = self.SYMBOL_MAP.get(symbol)
        if not poly_sym:
            return None

        cache_key = f"snapshot:{poly_sym}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Índices usan el endpoint de indices, no stocks
        path = f"/v3/snapshot?ticker.any_of={poly_sym}"
        data = self._get(path)
        if data is None:
            return None

        try:
            results = data.get("results") or []
            if not results:
                return None
            r = results[0]
            day = r.get("session", {})
            result = {
                "symbol":           poly_sym,
                "exness_symbol":    symbol,
                "day_close":        float(day.get("close", 0) or 0),
                "day_open":         float(day.get("open", 0) or 0),
                "day_high":         float(day.get("high", 0) or 0),
                "day_low":          float(day.get("low", 0) or 0),
                "day_volume":       float(day.get("volume", 0) or 0),
                "day_vwap":         float(day.get("volume_weighted", 0) or 0),
                "prev_close":       float(r.get("prevDay", {}).get("close", 0) or 0),
                "change_pct":       float(r.get("todaysChangePerc", 0) or 0),
            }
            self._cache.set(cache_key, result, self._cache_ttl_sec)
            return result
        except Exception as exc:
            log.debug(f"[Polygon] Error parseando snapshot de {poly_sym}: {exc}")
            return None


# ══════════════════════════════════════════════════════════════════
#  TRUEFX CSV LOADER
# ══════════════════════════════════════════════════════════════════

class TrueFXLoader:
    """
    Carga tick data histórico de TrueFX desde CSVs locales en data/truefx/.

    Los archivos tienen formato (sin header):
        EUR/USD,20251101 00:00:00.123,1.08234,1.08237

    Columnas: symbol, timestamp, bid, ask — sin header row.
    Los archivos son >25 MB y no están en el repo (ver .gitignore).
    """

    # Mapeo Exness → nombre en archivo TrueFX
    SYMBOL_MAP: dict[str, str] = {
        "EURUSDm": "EURUSD",
        "GBPUSDm": "GBPUSD",
        "USDJPYm": "USDJPY",
        "EURJPYm": "EURJPY",
        "GBPJPYm": "GBPJPY",
        "XAUUSDm": "XAUUSD",
    }

    # Formato de timestamp en los archivos TrueFX
    TS_FORMAT = "%Y%m%d %H:%M:%S.%f"

    def __init__(self, data_dir: str = None) -> None:
        if data_dir is None:
            data_dir = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "data", "truefx"
            )
        self._data_dir = data_dir

    # ── Helpers privados ──────────────────────────────────────────

    def _csv_path(self, truefx_symbol: str, year: int, month: int) -> str:
        """Construye la ruta del CSV: data/truefx/EURUSD-2025-11.csv"""
        return os.path.join(
            self._data_dir, f"{truefx_symbol}-{year:04d}-{month:02d}.csv"
        )

    def _parse_csv(self, filepath: str) -> Optional[pd.DataFrame]:
        """Carga y parsea un CSV TrueFX sin header. Retorna DataFrame con columnas time, bid, ask, mid, spread."""
        try:
            df = pd.read_csv(
                filepath,
                header=None,
                names=["symbol", "timestamp", "bid", "ask"],
                dtype={"bid": float, "ask": float},
            )
            df["time"] = pd.to_datetime(
                df["timestamp"], format=self.TS_FORMAT, errors="coerce"
            )
            df = df.dropna(subset=["time"])
            df["time"] = df["time"].dt.tz_localize("UTC")
            df["mid"] = (df["bid"] + df["ask"]) / 2.0
            df["spread"] = df["ask"] - df["bid"]
            df = df[["time", "bid", "ask", "mid", "spread"]].reset_index(drop=True)
            log.debug(f"[TrueFX] Cargados {len(df):,} ticks de {os.path.basename(filepath)}")
            return df
        except Exception as exc:
            log.debug(f"[TrueFX] Error leyendo {filepath}: {exc}")
            return None

    # ── API pública ───────────────────────────────────────────────

    def load_ticks(self, symbol: str, year: int, month: int) -> Optional[pd.DataFrame]:
        """
        Carga el CSV TrueFX para el símbolo/año/mes indicado.

        Args:
            symbol: Símbolo Exness (e.g. "EURUSDm") o directamente "EURUSD"
            year:   Año (e.g. 2025)
            month:  Mes (1-12)

        Returns:
            DataFrame con columnas: time, bid, ask, mid, spread — o None si no existe.
        """
        truefx_sym = self.SYMBOL_MAP.get(symbol, symbol)
        path = self._csv_path(truefx_sym, year, month)
        if not os.path.isfile(path):
            log.debug(f"[TrueFX] Archivo no encontrado: {path}")
            return None
        return self._parse_csv(path)

    def load_and_aggregate_m5(
        self, symbol: str, year: int, month: int
    ) -> Optional[pd.DataFrame]:
        """
        Carga ticks del CSV y los agrega en candles M5 con volumen real (tick count).

        El volumen resultante es el número de ticks en cada ventana de 5 min,
        que representa el volumen real de transacciones — de mayor calidad que
        el tick_volume de MT5/Exness para pares forex.

        Returns:
            DataFrame con columnas: time, open, high, low, close, volume
            (compatible con lo que backtester.py y microstructure.py esperan)
        """
        ticks = self.load_ticks(symbol, year, month)
        if ticks is None or len(ticks) == 0:
            return None

        try:
            ticks = ticks.set_index("time").sort_index()
            mid = ticks["mid"]
            agg = mid.resample("5min").agg(
                open="first",
                high="max",
                low="min",
                close="last",
            )
            vol = mid.resample("5min").count().rename("volume")
            df = pd.concat([agg, vol], axis=1).dropna(subset=["open"]).reset_index()
            df["volume"] = df["volume"].astype(int)
            log.debug(
                f"[TrueFX] {symbol} {year}/{month:02d}: "
                f"{len(df)} candles M5 a partir de {len(ticks):,} ticks"
            )
            return df
        except Exception as exc:
            log.debug(f"[TrueFX] Error agregando ticks a M5 para {symbol}: {exc}")
            return None

    def load_range(
        self, symbol: str, start_date: str, end_date: str
    ) -> Optional[pd.DataFrame]:
        """
        Carga múltiples meses de ticks, los concatena y filtra por rango de fechas.

        Args:
            symbol:     Símbolo Exness (e.g. "EURUSDm")
            start_date: "YYYY-MM-DD"
            end_date:   "YYYY-MM-DD"

        Returns:
            DataFrame concatenado con columnas: time, bid, ask, mid, spread
            filtrado al rango pedido, o None si no hay datos.
        """
        start_dt = pd.Timestamp(start_date, tz="UTC")
        end_dt   = pd.Timestamp(end_date,   tz="UTC")

        # Determinar qué año/mes abarca el rango
        frames = []
        year, month = start_dt.year, start_dt.month
        while (year, month) <= (end_dt.year, end_dt.month):
            df_m = self.load_ticks(symbol, year, month)
            if df_m is not None:
                frames.append(df_m)
            # Avanzar un mes
            if month == 12:
                year, month = year + 1, 1
            else:
                month += 1

        if not frames:
            log.debug(f"[TrueFX] No hay archivos disponibles para {symbol} en {start_date}–{end_date}")
            return None

        combined = pd.concat(frames, ignore_index=True).sort_values("time")
        mask = (combined["time"] >= start_dt) & (combined["time"] <= end_dt)
        combined = combined.loc[mask].reset_index(drop=True)
        log.debug(f"[TrueFX] {symbol}: {len(combined):,} ticks en rango {start_date}–{end_date}")
        return combined if len(combined) > 0 else None

    def load_range_m5(
        self, symbol: str, start_date: str, end_date: str
    ) -> Optional[pd.DataFrame]:
        """
        Carga múltiples meses como candles M5 (agrega ticks) y los concatena.

        Returns:
            DataFrame con columnas: time, open, high, low, close, volume
            compatible con backtester.py, o None si no hay datos.
        """
        start_dt = pd.Timestamp(start_date, tz="UTC")
        end_dt   = pd.Timestamp(end_date,   tz="UTC")

        frames = []
        year, month = start_dt.year, start_dt.month
        while (year, month) <= (end_dt.year, end_dt.month):
            df_m = self.load_and_aggregate_m5(symbol, year, month)
            if df_m is not None:
                frames.append(df_m)
            if month == 12:
                year, month = year + 1, 1
            else:
                month += 1

        if not frames:
            return None

        combined = pd.concat(frames, ignore_index=True).sort_values("time")
        mask = (combined["time"] >= start_dt) & (combined["time"] <= end_dt)
        combined = combined.loc[mask].reset_index(drop=True)
        return combined if len(combined) > 0 else None

    def list_available_data(self) -> dict:
        """
        Escanea data/truefx/ y retorna un dict {exness_symbol: [months]}.

        Months es una lista de strings "YYYY-MM".
        Ejemplo: {"EURUSDm": ["2025-11", "2025-12", "2026-01"], ...}
        """
        if not os.path.isdir(self._data_dir):
            return {}

        # Invertir el mapa para buscar símbolo Exness por nombre TrueFX
        inv_map = {v: k for k, v in self.SYMBOL_MAP.items()}

        result: dict = {}
        pattern = os.path.join(self._data_dir, "*.csv")
        for filepath in sorted(_glob.glob(pattern)):
            basename = os.path.splitext(os.path.basename(filepath))[0]
            parts = basename.split("-")
            if len(parts) != 3:
                continue
            truefx_sym, year_s, month_s = parts
            try:
                int(year_s)
                int(month_s)
            except ValueError:
                continue
            exness_sym = inv_map.get(truefx_sym, truefx_sym)
            month_label = f"{year_s}-{month_s}"
            result.setdefault(exness_sym, []).append(month_label)

        return result


# ══════════════════════════════════════════════════════════════════
#  INSTANCIAS GLOBALES (singleton por proceso)
# ══════════════════════════════════════════════════════════════════

def _build_providers():
    """Construye las instancias globales leyendo config."""
    try:
        import config as cfg  # type: ignore
        td_key    = getattr(cfg, "TWELVE_DATA_KEY",            "")
        td_ttl    = getattr(cfg, "TWELVE_DATA_CACHE_TTL_MIN",  5)
        td_rpm    = getattr(cfg, "TWELVE_DATA_MAX_CALLS_PER_MIN", 8)
        poly_key  = getattr(cfg, "POLYGON_KEY",                "")
        poly_ttl  = getattr(cfg, "POLYGON_CACHE_TTL_MIN",      5)
        poly_rpm  = getattr(cfg, "POLYGON_MAX_CALLS_PER_MIN",  5)
        tfx_dir   = getattr(cfg, "TRUEFX_DATA_DIR",            None)
    except Exception:
        td_key = poly_key = ""
        td_ttl = poly_ttl = 5
        td_rpm = 8
        poly_rpm = 5
        tfx_dir = None

    td   = TwelveDataProvider(api_key=td_key, cache_ttl_min=td_ttl, max_calls_per_min=td_rpm)
    poly = PolygonProvider(api_key=poly_key, cache_ttl_min=poly_ttl, max_calls_per_min=poly_rpm)
    tfx  = TrueFXLoader(data_dir=tfx_dir)
    return td, poly, tfx


_twelve_data:   Optional[TwelveDataProvider]  = None
_polygon:       Optional[PolygonProvider]     = None
_truefx_loader: Optional[TrueFXLoader]        = None

def _lazy_init():
    global _twelve_data, _polygon, _truefx_loader
    if _twelve_data is None:
        _twelve_data, _polygon, _truefx_loader = _build_providers()


def get_twelve_data() -> TwelveDataProvider:
    _lazy_init()
    return _twelve_data


def get_polygon() -> PolygonProvider:
    _lazy_init()
    return _polygon


def get_truefx_loader() -> TrueFXLoader:
    _lazy_init()
    return _truefx_loader
