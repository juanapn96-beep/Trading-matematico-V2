"""
╔══════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — indicators.py  (v6.2 — FIXED)         ║
║                                                                  ║
║   GRUPO 1 — TENDENCIA (8 indicadores)                          ║
║   GRUPO 2 — MOMENTUM (8 indicadores)                           ║
║   GRUPO 3 — VOLATILIDAD (6 indicadores)                        ║
║   GRUPO 4 — VOLUMEN (5 indicadores)                            ║
║   GRUPO 5 — PATRONES DE VELAS (3 indicadores)                  ║
║   GRUPO 6 — ALGORITMOS MATEMÁTICOS AVANZADOS (7 modelos)       ║
║             • Transformada de Hilbert (senos/cosenos — TESIS)  ║
║             • Exponente de Hurst                               ║
║             • Filtro de Kalman                                 ║
║             • Ciclos de Fourier                                ║
║             • Transformada de Fisher                           ║
║             • Oscillator de Ciclo Adaptativo                   ║
║             • Regresión cuantílica adaptativa                  ║
║                                                                  ║
║   FIX v6.2 — h1_trend:                                         ║
║   Antes: ALCISTA_FUERTE requería Hurst > 0.55 → con Hurst      ║
║   0.40-0.55 todo caía en LATERAL y Gemini nunca operaba.       ║
║   Ahora: 5 niveles de tendencia según Hurst + Kalman:          ║
║   ALCISTA_FUERTE / ALCISTA / LATERAL_ALCISTA /                 ║
║   LATERAL / LATERAL_BAJISTA / BAJISTA / BAJISTA_FUERTE         ║
╚══════════════════════════════════════════════════════════════════╝
"""

import numpy as np
import pandas as pd
import logging
from dataclasses import dataclass, field
from typing import Optional

from modules.microstructure import compute_microstructure

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
#  GRUPO 1 — TENDENCIA
# ══════════════════════════════════════════════════════════════════

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def dema(series: pd.Series, period: int) -> pd.Series:
    """Double EMA — 50% menos lag."""
    e1 = series.ewm(span=period, adjust=False).mean()
    e2 = e1.ewm(span=period, adjust=False).mean()
    return 2 * e1 - e2

def tema(series: pd.Series, period: int) -> pd.Series:
    """Triple EMA — elimina lag casi completamente."""
    e1 = series.ewm(span=period, adjust=False).mean()
    e2 = e1.ewm(span=period, adjust=False).mean()
    e3 = e2.ewm(span=period, adjust=False).mean()
    return 3 * e1 - 3 * e2 + e3

def wma(series: pd.Series, period: int) -> pd.Series:
    weights = np.arange(1, period + 1, dtype=float)
    return series.rolling(period).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

def hma(series: pd.Series, period: int) -> pd.Series:
    """Hull Moving Average — suave y reactivo."""
    half  = wma(series, period // 2)
    full  = wma(series, period)
    raw   = 2 * half - full
    return wma(raw, int(np.sqrt(period)))

def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0):
    """SuperTrend — direccion de tendencia con banda dinámica."""
    atr_s = atr(df, period)
    hl2   = (df["high"] + df["low"]) / 2
    upper = hl2 + mult * atr_s
    lower = hl2 - mult * atr_s

    st    = pd.Series(index=df.index, dtype=float)
    trend = pd.Series(index=df.index, dtype=int)

    for i in range(1, len(df)):
        fu = upper.iloc[i]
        fl = lower.iloc[i]
        c  = df["close"].iloc[i]
        pc = df["close"].iloc[i - 1]

        if fu < upper.iloc[i - 1] or pc > upper.iloc[i - 1]:
            pass
        else:
            fu = upper.iloc[i - 1]

        if fl > lower.iloc[i - 1] or pc < lower.iloc[i - 1]:
            pass
        else:
            fl = lower.iloc[i - 1]

        if trend.iloc[i - 1] == -1:
            trend.iloc[i] = 1 if c > fu else -1
            st.iloc[i]    = fl if trend.iloc[i] == 1 else fu
        else:
            trend.iloc[i] = -1 if c < fl else 1
            st.iloc[i]    = fu if trend.iloc[i] == -1 else fl

    return st, trend

def ichimoku(df: pd.DataFrame):
    """Ichimoku Cloud — 5 líneas de soporte/resistencia dinámico."""
    tenkan   = (df["high"].rolling(9).max()  + df["low"].rolling(9).min())  / 2
    kijun    = (df["high"].rolling(26).max() + df["low"].rolling(26).min()) / 2
    senkou_a = ((tenkan + kijun) / 2).shift(26)
    senkou_b = ((df["high"].rolling(52).max() + df["low"].rolling(52).min()) / 2).shift(26)
    chikou   = df["close"].shift(-26)
    return tenkan, kijun, senkou_a, senkou_b, chikou

def parabolic_sar(df: pd.DataFrame, af_start=0.02, af_max=0.2):
    """Parabolic SAR."""
    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values
    sar    = np.zeros(len(df))
    trend  = np.ones(len(df), dtype=int)
    ep     = lows[0]
    af     = af_start

    sar[0] = highs[0]
    for i in range(1, len(df)):
        if trend[i-1] == 1:
            sar[i] = sar[i-1] + af * (ep - sar[i-1])
            sar[i] = min(sar[i], lows[i-1], lows[i-2] if i > 1 else lows[i-1])
            if lows[i] < sar[i]:
                trend[i] = -1; sar[i] = ep; ep = highs[i]; af = af_start
            else:
                trend[i] = 1
                if highs[i] > ep:
                    ep = highs[i]; af = min(af + af_start, af_max)
        else:
            sar[i] = sar[i-1] + af * (ep - sar[i-1])
            sar[i] = max(sar[i], highs[i-1], highs[i-2] if i > 1 else highs[i-1])
            if highs[i] > sar[i]:
                trend[i] = 1; sar[i] = ep; ep = lows[i]; af = af_start
            else:
                trend[i] = -1
                if lows[i] < ep:
                    ep = lows[i]; af = min(af + af_start, af_max)

    return pd.Series(sar, index=df.index), pd.Series(trend, index=df.index)

# ══════════════════════════════════════════════════════════════════
#  GRUPO 2 — MOMENTUM
# ══════════════════════════════════════════════════════════════════

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta  = series.diff()
    gain   = delta.clip(lower=0)
    loss   = -delta.clip(upper=0)
    avg_g  = gain.ewm(com=period - 1, adjust=False).mean()
    avg_l  = loss.ewm(com=period - 1, adjust=False).mean()
    rs     = avg_g / avg_l.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def macd(series: pd.Series, fast=12, slow=26, signal=9):
    ef   = series.ewm(span=fast,   adjust=False).mean()
    es   = series.ewm(span=slow,   adjust=False).mean()
    line = ef - es
    sig  = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig

def stochastic(df: pd.DataFrame, k=5, d=3):
    low_k  = df["low"].rolling(k).min()
    high_k = df["high"].rolling(k).max()
    fast_k = 100 * (df["close"] - low_k) / (high_k - low_k).replace(0, np.nan)
    slow_k = fast_k.rolling(3).mean()
    slow_d = slow_k.rolling(d).mean()
    return slow_k, slow_d

def cci(df: pd.DataFrame, period: int = 20) -> pd.Series:
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    mad = tp.rolling(period).apply(lambda x: np.mean(np.abs(x - np.mean(x))), raw=True)
    return (tp - tp.rolling(period).mean()) / (0.015 * mad.replace(0, np.nan))

def williams_r(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hh = df["high"].rolling(period).max()
    ll = df["low"].rolling(period).min()
    return -100 * (hh - df["close"]) / (hh - ll).replace(0, np.nan)

def roc(series: pd.Series, period: int = 10) -> pd.Series:
    return 100 * (series - series.shift(period)) / series.shift(period).replace(0, np.nan)

def rsi_divergence(price: pd.Series, rsi_s: pd.Series, lookback: int = 12) -> str:
    if len(price) < lookback:
        return "NONE"
    p = price.iloc[-lookback:]
    r = rsi_s.iloc[-lookback:]
    if price.iloc[-1] < p.min() and rsi_s.iloc[-1] > r.min():
        return "BULLISH_DIV"
    if price.iloc[-1] > p.max() and rsi_s.iloc[-1] < r.max():
        return "BEARISH_DIV"
    return "NONE"

def momentum(series: pd.Series, period: int = 10) -> pd.Series:
    return series - series.shift(period)

# ══════════════════════════════════════════════════════════════════
#  GRUPO 3 — VOLATILIDAD
# ══════════════════════════════════════════════════════════════════

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h  = df["high"]
    l  = df["low"]
    cp = df["close"].shift(1)
    tr = pd.concat([h - l, (h - cp).abs(), (l - cp).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def bollinger_bands(close: pd.Series, period=20, std_mult=2.0):
    mid  = close.rolling(period).mean()
    std  = close.rolling(period).std()
    return mid + std_mult * std, mid, mid - std_mult * std

def keltner_channel(df: pd.DataFrame, ema_period=20, atr_mult=2.0, atr_period=10):
    mid   = df["close"].ewm(span=ema_period, adjust=False).mean()
    atr_v = atr(df, atr_period)
    return mid + atr_mult * atr_v, mid, mid - atr_mult * atr_v

def donchian(df: pd.DataFrame, period: int = 20):
    upper = df["high"].rolling(period).max()
    lower = df["low"].rolling(period).min()
    mid   = (upper + lower) / 2
    return upper, mid, lower

def historical_volatility(close: pd.Series, period: int = 20) -> pd.Series:
    log_ret = np.log(close / close.shift(1))
    return log_ret.rolling(period).std() * np.sqrt(252)

def bb_squeeze(upper: pd.Series, lower: pd.Series, period=20) -> bool:
    width = upper - lower
    return bool(width.iloc[-1] <= width.rolling(period).min().iloc[-1] * 1.05)

# ══════════════════════════════════════════════════════════════════
#  GRUPO 4 — VOLUMEN
# ══════════════════════════════════════════════════════════════════

def vwap(df: pd.DataFrame) -> float:
    today    = df["time"].dt.date.iloc[-1]
    df_today = df[df["time"].dt.date == today]
    if len(df_today) < 2:
        df_today = df
    tp  = (df_today["high"] + df_today["low"] + df_today["close"]) / 3
    vol = df_today["volume"]
    if vol.sum() == 0:
        return float(df["close"].iloc[-1])
    return round(float((tp * vol).sum() / vol.sum()), 4)

def obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["close"].diff())
    return (direction * df["volume"]).cumsum()

def cmf(df: pd.DataFrame, period: int = 20) -> pd.Series:
    mfv = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / \
          (df["high"] - df["low"]).replace(0, np.nan) * df["volume"]
    return mfv.rolling(period).sum() / df["volume"].rolling(period).sum().replace(0, np.nan)

def mfi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tp    = (df["high"] + df["low"] + df["close"]) / 3
    rmf   = tp * df["volume"]
    pmf   = rmf.where(tp > tp.shift(1), 0).rolling(period).sum()
    nmf   = rmf.where(tp < tp.shift(1), 0).rolling(period).sum()
    mr    = pmf / nmf.replace(0, np.nan)
    return 100 - (100 / (1 + mr))

def vwma(close: pd.Series, volume: pd.Series, period: int = 20) -> pd.Series:
    return (close * volume).rolling(period).sum() / volume.rolling(period).sum().replace(0, np.nan)

# ══════════════════════════════════════════════════════════════════
#  GRUPO 5 — PATRONES DE VELAS
# ══════════════════════════════════════════════════════════════════

def heiken_ashi(df: pd.DataFrame) -> pd.DataFrame:
    ha = df.copy()
    ha["close"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    ha["open"]  = ((df["open"].shift(1) + df["close"].shift(1)) / 2).fillna(df["open"])
    ha["high"]  = ha[["high", "open", "close"]].max(axis=1)
    ha["low"]   = ha[["low",  "open", "close"]].min(axis=1)
    return ha

def cpr(df: pd.DataFrame):
    """Central Pivot Range — pivote central del día anterior."""
    prev  = df.iloc[-2] if len(df) > 1 else df.iloc[-1]
    pivot = (prev["high"] + prev["low"] + prev["close"]) / 3
    bc    = (prev["high"] + prev["low"]) / 2
    tc    = 2 * pivot - bc
    r1    = 2 * pivot - prev["low"]
    s1    = 2 * pivot - prev["high"]
    r2    = pivot + (prev["high"] - prev["low"])
    s2    = pivot - (prev["high"] - prev["low"])
    return dict(pivot=pivot, bc=bc, tc=tc, r1=r1, s1=s1, r2=r2, s2=s2)

def detect_candle_pattern(df: pd.DataFrame) -> str:
    if len(df) < 3:
        return "NONE"
    c = df.iloc[-1]
    p = df.iloc[-2]
    body   = abs(c["close"] - c["open"])
    range_ = c["high"] - c["low"]
    if range_ == 0:
        return "NONE"
    if body / range_ < 0.1:
        return "DOJI"
    upper_wick = c["high"] - max(c["close"], c["open"])
    lower_wick = min(c["close"], c["open"]) - c["low"]
    if lower_wick > 2 * body and upper_wick < 0.3 * body:
        return "HAMMER"
    if upper_wick > 2 * body and lower_wick < 0.3 * body:
        return "SHOOTING_STAR"
    if (c["close"] > c["open"] and p["close"] < p["open"]
            and c["open"] < p["close"] and c["close"] > p["open"]):
        return "BULLISH_ENGULFING"
    if (c["close"] < c["open"] and p["close"] > p["open"]
            and c["open"] > p["close"] and c["close"] < p["open"]):
        return "BEARISH_ENGULFING"
    return "NONE"

# ══════════════════════════════════════════════════════════════════
#  GRUPO 6 — ALGORITMOS MATEMÁTICOS AVANZADOS
# ══════════════════════════════════════════════════════════════════

@dataclass
class HilbertResult:
    dominant_period: float
    phase:           float
    sine:            float
    lead_sine:       float
    in_phase:        float
    quadrature:      float
    signal:          str
    cycle_strength:  float
    description:     str

def hilbert_transform(close: pd.Series, min_period: int = 6, max_period: int = 50) -> HilbertResult:
    """Transformada de Hilbert de Ehlers — detecta ciclos de precio."""
    closes = close.values.astype(float)
    n      = len(closes)

    if n < 60:
        return HilbertResult(
            dominant_period=20, phase=0, sine=0, lead_sine=0,
            in_phase=0, quadrature=0, signal="NEUTRAL",
            cycle_strength=0, description="Datos insuficientes"
        )

    smooth    = np.zeros(n)
    detrender = np.zeros(n)
    for i in range(4, n):
        smooth[i] = (closes[i] + 2*closes[i-1] + 2*closes[i-2] + closes[i-3]) / 6
        detrender[i] = (0.0962*smooth[i] + 0.5769*smooth[i-2]
                       - 0.5769*smooth[i-4] - 0.0962*smooth[i-6]
                       if i >= 6 else 0.0)

    I = np.zeros(n)
    Q = np.zeros(n)
    for i in range(6, n):
        I[i] = detrender[i]
        Q[i] = (0.0962*detrender[i] + 0.5769*detrender[i-2]
               - 0.5769*detrender[i-4] - 0.0962*detrender[i-6]
               if i >= 6 else 0.0)

    jI = np.zeros(n)
    jQ = np.zeros(n)
    for i in range(6, n):
        jI[i] = (0.0962*I[i] + 0.5769*I[i-2] - 0.5769*I[i-4] - 0.0962*I[i-6])
        jQ[i] = (0.0962*Q[i] + 0.5769*Q[i-2] - 0.5769*Q[i-4] - 0.0962*Q[i-6])

    I2      = np.zeros(n)
    Q2      = np.zeros(n)
    Re      = np.zeros(n)
    Im      = np.zeros(n)
    per     = np.zeros(n)
    smo_per = np.zeros(n)

    for i in range(1, n):
        I2[i] =  I[i] - jQ[i]
        Q2[i] =  Q[i] + jI[i]
        Re[i] =  I2[i]*I2[i-1] + Q2[i]*Q2[i-1]
        Im[i] =  I2[i]*Q2[i-1] - Q2[i]*I2[i-1]
        if Im[i] != 0 and Re[i] != 0:
            raw_per = 2 * np.pi / np.arctan(Im[i] / Re[i])
        else:
            raw_per = 0
        raw_per = min(1.5 * smo_per[i-1] if smo_per[i-1] > 0 else max_period, raw_per)
        raw_per = max(0.67 * smo_per[i-1] if smo_per[i-1] > 0 else min_period, raw_per)
        raw_per = max(min_period, min(max_period, raw_per))
        per[i]     = raw_per
        smo_per[i] = 0.2*per[i] + 0.8*(smo_per[i-1] if i > 0 else per[i])

    dom_period    = float(np.clip(smo_per[-1], min_period, max_period))
    phase_rad     = 2 * np.pi / dom_period
    phase_deg     = np.degrees(phase_rad) * len(closes) % 360
    sine_val      = float(np.sin(np.radians(phase_deg)))
    lead_sine_val = float(np.sin(np.radians(phase_deg + 45)))
    in_phase_val  = float(I[-1])
    quad_val      = float(Q[-1])

    signal    = "NEUTRAL"
    desc      = "Ciclo en curso"
    cycle_str = abs(sine_val)

    if sine_val > 0.85:
        signal = "LOCAL_MAX"
        desc   = f"⚠️ Máximo local detectado (fase={phase_deg:.0f}°) — No comprar"
    elif sine_val < -0.85:
        signal = "LOCAL_MIN"
        desc   = f"✅ Mínimo local detectado (fase={phase_deg:.0f}°) — Oportunidad de compra"
    elif lead_sine_val > sine_val and sine_val > 0:
        signal = "SELL_CYCLE"
        desc   = f"📉 Fase bajista del ciclo (sine={sine_val:.2f})"
    elif lead_sine_val < sine_val and sine_val < 0:
        signal = "BUY_CYCLE"
        desc   = f"📈 Fase alcista del ciclo (sine={sine_val:.2f})"
    elif abs(sine_val) < 0.15:
        signal = "CYCLE_CROSS"
        desc   = f"🔄 Cruce del ciclo — posible cambio de dirección"

    return HilbertResult(
        dominant_period=round(dom_period, 1),
        phase=round(phase_deg % 360, 1),
        sine=round(sine_val, 4),
        lead_sine=round(lead_sine_val, 4),
        in_phase=round(in_phase_val, 4),
        quadrature=round(quad_val, 4),
        signal=signal,
        cycle_strength=round(cycle_str, 3),
        description=desc,
    )


def hurst_exponent(close: pd.Series, min_lag: int = 2, max_lag: int = 20) -> float:
    """Exponente de Hurst — H>0.6=tendencia, H≈0.5=aleatorio, H<0.4=reversión."""
    prices = close.dropna().values.astype(float)
    if len(prices) < max_lag * 2:
        return 0.5
    lags = range(min_lag, max_lag)
    tau  = [np.std(np.subtract(prices[lag:], prices[:-lag])) for lag in lags]
    tau  = [t if t > 0 else 1e-10 for t in tau]
    poly = np.polyfit(np.log(list(lags)), np.log(tau), 1)
    return float(np.clip(poly[0], 0.01, 1.0))


def kalman_filter(close: pd.Series, R: float = 0.01, Q: float = 0.0001):
    """Filtro de Kalman — precio estimado sin lag."""
    prices = close.values.astype(float)
    n      = len(prices)
    kf     = np.zeros(n)
    P      = 1.0
    x      = prices[0]
    for i in range(n):
        P    = P + Q
        K    = P / (P + R)
        x    = x + K * (prices[i] - x)
        P    = (1 - K) * P
        kf[i] = x
    return pd.Series(kf, index=close.index)


def fourier_dominant_cycle(close: pd.Series, top_n: int = 3) -> dict:
    """FFT para detectar el período dominante del mercado."""
    prices = close.dropna().values.astype(float)
    if len(prices) < 32:
        return {"dominant_period": 20, "cycles": [], "strength": 0.0}
    detrended = prices - np.linspace(prices[0], prices[-1], len(prices))
    fft_vals  = np.fft.rfft(detrended)
    freqs     = np.fft.rfftfreq(len(detrended))
    amps      = np.abs(fft_vals)
    amps[0]   = 0
    valid     = (freqs > 0) & (freqs < 0.3)
    amps_v    = np.where(valid, amps, 0)
    top_idx   = np.argsort(amps_v)[-top_n:][::-1]
    cycles    = []
    for idx in top_idx:
        if freqs[idx] > 0:
            period = round(1.0 / freqs[idx], 1)
            amp    = round(amps_v[idx] / (amps_v.max() + 1e-10), 3)
            cycles.append({"period": period, "amplitude": amp})
    dom_period = cycles[0]["period"] if cycles else 20.0
    strength   = cycles[0]["amplitude"] if cycles else 0.0
    return {"dominant_period": dom_period, "cycles": cycles, "strength": round(float(strength), 3)}


def fisher_transform(high: pd.Series, low: pd.Series, period: int = 10):
    """Fisher transform — convierte precios a distribución Gaussiana."""
    hl2    = (high + low) / 2
    hh     = hl2.rolling(period).max()
    ll     = hl2.rolling(period).min()
    value  = 2 * ((hl2 - ll) / (hh - ll).replace(0, np.nan)) - 1
    value  = value.clip(-0.999, 0.999)
    fisher = 0.5 * np.log((1 + value) / (1 - value).replace(0, np.nan))
    signal_line = fisher.shift(1)
    return fisher.fillna(0), signal_line.fillna(0)


def adaptive_cycle_oscillator(close: pd.Series) -> pd.Series:
    """Oscilador adaptativo combinando Hilbert + Fourier. Oscila -1 a +1."""
    hilbert_res = hilbert_transform(close)
    period      = int(max(6, hilbert_res.dominant_period))
    prices      = close.values.astype(float)
    n           = len(prices)
    osc         = np.zeros(n)
    for i in range(period, n):
        window = prices[i-period:i]
        mn     = np.min(window)
        mx     = np.max(window)
        rng    = mx - mn
        osc[i] = 2 * (prices[i] - mn) / rng - 1 if rng > 0 else 0.0
    return pd.Series(osc, index=close.index)


def adaptive_linear_regression(close: pd.Series, period: int = 50):
    """Regresión lineal deslizante — pendiente e R² de la tendencia."""
    prices = close.values.astype(float)
    n      = len(prices)
    slope  = np.zeros(n)
    r2     = np.zeros(n)
    for i in range(period, n):
        y        = prices[i-period:i]
        x        = np.arange(period, dtype=float)
        coeffs   = np.polyfit(x, y, 1)
        slope[i] = coeffs[0]
        y_pred   = np.polyval(coeffs, x)
        ss_res   = np.sum((y - y_pred) ** 2)
        ss_tot   = np.sum((y - np.mean(y)) ** 2)
        r2[i]    = 1 - (ss_res / (ss_tot + 1e-10))
    return pd.Series(slope, index=close.index), pd.Series(r2, index=close.index)


# ══════════════════════════════════════════════════════════════════
#  FUNCIÓN PRINCIPAL — compute_all()
# ══════════════════════════════════════════════════════════════════

def compute_all(df: pd.DataFrame, symbol: str, sym_cfg: dict, df_entry: pd.DataFrame = None) -> dict:
    """
    Calcula TODOS los indicadores y retorna un diccionario con los
    valores actuales (última vela).
    """
    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"]
    micro_df = df_entry if df_entry is not None and len(df_entry) >= 10 else df

    ctx = {}

    try:
        # ── TENDENCIA ──
        ctx["dema_fast"]   = round(float(dema(close, 21).iloc[-1]), 4)
        ctx["dema_slow"]   = round(float(dema(close, 55).iloc[-1]), 4)
        ctx["tema_fast"]   = round(float(tema(close, 21).iloc[-1]), 4)
        ctx["ema_200"]     = round(float(ema(close, 200).iloc[-1]), 4)
        ctx["hma_21"]      = round(float(hma(close, 21).iloc[-1]), 4)
        ctx["wma_50"]      = round(float(wma(close, 50).iloc[-1]), 4)

        ten, kij, sA, sB, chi = ichimoku(df)
        ctx["ichi_tenkan"]      = round(float(ten.iloc[-1]), 4)
        ctx["ichi_kijun"]       = round(float(kij.iloc[-1]), 4)
        ctx["ichi_above_cloud"] = bool(close.iloc[-1] > max(
            sA.iloc[-1] if not np.isnan(sA.iloc[-1]) else 0,
            sB.iloc[-1] if not np.isnan(sB.iloc[-1]) else 0
        ))

        sar_vals, sar_trend = parabolic_sar(df)
        ctx["sar_trend"]   = int(sar_trend.iloc[-1])
        ctx["sar_value"]   = round(float(sar_vals.iloc[-1]), 4)

        _, st_trend = supertrend(df)
        ctx["supertrend"]  = int(st_trend.iloc[-1])

        dema_f = dema(close, 21)
        dema_s = dema(close, 55)
        if dema_f.iloc[-1] > dema_s.iloc[-1] and dema_f.iloc[-2] <= dema_s.iloc[-2]:
            ctx["dema_cross"] = "GOLDEN_CROSS"
        elif dema_f.iloc[-1] < dema_s.iloc[-1] and dema_f.iloc[-2] >= dema_s.iloc[-2]:
            ctx["dema_cross"] = "DEATH_CROSS"
        else:
            ctx["dema_cross"] = "NONE"

        # ── MOMENTUM ──
        rsi_s           = rsi(close, 14)
        ctx["rsi"]      = round(float(rsi_s.iloc[-1]), 2)
        ctx["rsi_div"]  = rsi_divergence(close, rsi_s)

        ml, ms, mh      = macd(close)
        ctx["macd"]     = round(float(ml.iloc[-1]), 4)
        ctx["macd_sig"] = round(float(ms.iloc[-1]), 4)
        ctx["macd_hist"]= round(float(mh.iloc[-1]), 4)
        ctx["macd_dir"] = "ALCISTA" if mh.iloc[-1] > 0 else "BAJISTA"

        sk, sd          = stochastic(df)
        ctx["stoch_k"]  = round(float(sk.iloc[-1]), 2)
        ctx["stoch_d"]  = round(float(sd.iloc[-1]), 2)

        ctx["cci"]      = round(float(cci(df).iloc[-1]), 2)
        ctx["williams"] = round(float(williams_r(df).iloc[-1]), 2)
        ctx["roc"]      = round(float(roc(close).iloc[-1]), 4)
        ctx["momentum"] = round(float(momentum(close).iloc[-1]), 4)

        # ── VOLATILIDAD ──
        atr_s            = atr(df, 14)
        ctx["atr"]       = round(float(atr_s.iloc[-1]), 4)
        ctx["atr_pct"]   = round(float(atr_s.iloc[-1] / close.iloc[-1] * 100), 3)

        bb_u, bb_m, bb_l = bollinger_bands(close)
        ctx["bb_upper"]  = round(float(bb_u.iloc[-1]), 4)
        ctx["bb_mid"]    = round(float(bb_m.iloc[-1]), 4)
        ctx["bb_lower"]  = round(float(bb_l.iloc[-1]), 4)
        ctx["bb_squeeze"]= bool(bb_squeeze(bb_u, bb_l))
        if close.iloc[-1] >= bb_u.iloc[-1]:    ctx["bb_pos"] = "SOBRE_SUPERIOR"
        elif close.iloc[-1] <= bb_l.iloc[-1]:  ctx["bb_pos"] = "BAJO_INFERIOR"
        elif close.iloc[-1] > bb_m.iloc[-1]:   ctx["bb_pos"] = "ZONA_ALTA"
        else:                                   ctx["bb_pos"] = "ZONA_BAJA"

        kc_u, kc_m, kc_l = keltner_channel(df)
        ctx["kc_upper"]   = round(float(kc_u.iloc[-1]), 4)
        ctx["kc_lower"]   = round(float(kc_l.iloc[-1]), 4)
        ctx["kc_squeeze"] = bool(bb_u.iloc[-1] < kc_u.iloc[-1] and bb_l.iloc[-1] > kc_l.iloc[-1])

        dc_u, dc_m, dc_l  = donchian(df)
        ctx["donchian_upper"] = round(float(dc_u.iloc[-1]), 4)
        ctx["donchian_lower"] = round(float(dc_l.iloc[-1]), 4)
        ctx["hist_vol"]   = round(float(historical_volatility(close).iloc[-1]), 4)

        # ── VOLUMEN ──
        ctx["vwap"]      = round(vwap(df), 4)
        ctx["obv_trend"] = "ALCISTA" if obv(df).diff().iloc[-3:].mean() > 0 else "BAJISTA"
        ctx["cmf"]       = round(float(cmf(df).iloc[-1]), 4)
        ctx["mfi"]       = round(float(mfi(df).iloc[-1]), 2)
        ctx["vwma"]      = round(float(vwma(close, volume, 20).iloc[-1]), 4)

        # ── PATRONES DE VELAS ──
        ha_df            = heiken_ashi(df)
        ctx["ha_trend"]  = "ALCISTA" if ha_df["close"].iloc[-1] > ha_df["open"].iloc[-1] else "BAJISTA"
        ctx["ha_streak"] = int(sum(1 for i in range(1, min(5, len(ha_df)))
                                   if (ha_df["close"].iloc[-i] > ha_df["open"].iloc[-i])
                                   == (ha_df["close"].iloc[-1] > ha_df["open"].iloc[-1])))
        cpr_vals             = cpr(df)
        ctx["cpr"]           = cpr_vals
        ctx["candle_pattern"]= detect_candle_pattern(df)

        # ── ALGORITMOS MATEMÁTICOS AVANZADOS (parámetros adaptativos) ──
        #
        #  FASE 1: Los parámetros de Hilbert, Hurst y Kalman se ajustan
        #  dinámicamente según la volatilidad reciente del activo (atr_pct).
        #  Mercado volátil → ventanas más cortas/rápidas para capturar cambios.
        #  Mercado tranquilo → ventanas más largas/estables para menor ruido.

        atr_pct_now: float = ctx.get("atr_pct", 0.3)

        # Hilbert: períodos mín/máx adaptativos
        hil_min_p = 4  if atr_pct_now > 1.0 else 6
        hil_max_p = 30 if atr_pct_now > 0.8 else 50
        hilbert = hilbert_transform(close, min_period=hil_min_p, max_period=hil_max_p)
        ctx["hilbert"] = {
            "period":      hilbert.dominant_period,
            "phase":       hilbert.phase,
            "sine":        hilbert.sine,
            "lead_sine":   hilbert.lead_sine,
            "signal":      hilbert.signal,
            "strength":    hilbert.cycle_strength,
            "description": hilbert.description,
        }

        # Hurst: ventana max_lag adaptativa a la volatilidad
        hurst_max_lag = 15 if atr_pct_now > 0.8 else (20 if atr_pct_now > 0.4 else 30)
        h_exp = hurst_exponent(close, min_lag=2, max_lag=hurst_max_lag)
        ctx["hurst"] = round(h_exp, 3)
        ctx["hurst_regime"] = (
            "TENDENCIA" if h_exp > 0.6 else
            "ALEATORIO" if h_exp > 0.45 else
            "REVERSION"
        )

        # Kalman: ruido de observación R y proceso Q adaptativos
        # Mayor ATR% → mercado más ruidoso → R más alto → filtro más suave
        kalman_R = max(0.005, min(0.10, atr_pct_now / 100.0))
        kalman_Q = kalman_R * 0.01
        kalman_s = kalman_filter(close, R=kalman_R, Q=kalman_Q)
        ctx["kalman_price"] = round(float(kalman_s.iloc[-1]), 4)
        ctx["kalman_trend"] = "ALCISTA" if kalman_s.iloc[-1] > kalman_s.iloc[-2] else "BAJISTA"
        ctx["kalman_slope"] = round(float(kalman_s.iloc[-1] - kalman_s.iloc[-5]), 4)

        fourier = fourier_dominant_cycle(close)
        ctx["fourier"] = {
            "dominant_period": fourier["dominant_period"],
            "strength":        fourier["strength"],
            "top_cycles":      fourier["cycles"][:2],
        }

        fish, fish_sig = fisher_transform(high, low, 10)
        ctx["fisher"]        = round(float(fish.iloc[-1]), 3)
        ctx["fisher_signal"] = round(float(fish_sig.iloc[-1]), 3)
        ctx["fisher_cross"]  = (
            "BULLISH" if fish.iloc[-1] > fish_sig.iloc[-1] and fish.iloc[-2] <= fish_sig.iloc[-2] else
            "BEARISH" if fish.iloc[-1] < fish_sig.iloc[-1] and fish.iloc[-2] >= fish_sig.iloc[-2] else
            "NONE"
        )

        aco = adaptive_cycle_oscillator(close)
        ctx["cycle_osc"]   = round(float(aco.iloc[-1]), 3)
        ctx["cycle_phase"] = (
            "TECHO" if aco.iloc[-1] > 0.7 else
            "SUELO" if aco.iloc[-1] < -0.7 else
            "TRANSICION"
        )

        slope_s, r2_s = adaptive_linear_regression(close, 50)
        ctx["lr_slope"] = round(float(slope_s.iloc[-1]), 6)
        ctx["lr_r2"]    = round(float(r2_s.iloc[-1]), 3)
        ctx["lr_trend"] = "ALCISTA" if slope_s.iloc[-1] > 0 else "BAJISTA"

        # ══════════════════════════════════════════════════════════
        #  DIAGNÓSTICO DE TENDENCIA H1 — VERSIÓN CORREGIDA v6.2
        #
        #  BUG ORIGINAL: requería Hurst > 0.55 para ALCISTA/BAJISTA
        #  → Con Hurst 0.40-0.55 (sesión asiática normal) todo caía
        #    en LATERAL y Gemini nunca operaba.
        #
        #  SOLUCIÓN: 7 niveles de tendencia que cubren todos los
        #  regímenes de Hurst. Gemini recibe contexto completo y
        #  puede decidir BUY/SELL incluso con Hurst moderado.
        #
        #  NIVELES:
        #  ALCISTA_FUERTE   — DEMA + MACD + VWAP + Kalman + Hurst>0.55
        #  ALCISTA          — DEMA + MACD + Kalman (Hurst cualquiera)
        #  LATERAL_ALCISTA  — señales mixtas con sesgo alcista
        #  LATERAL          — sin dirección clara
        #  LATERAL_BAJISTA  — señales mixtas con sesgo bajista
        #  BAJISTA          — DEMA + MACD + Kalman (Hurst cualquiera)
        #  BAJISTA_FUERTE   — DEMA + MACD + VWAP + Kalman + Hurst>0.55
        # ══════════════════════════════════════════════════════════

        dema_f_v   = ctx["dema_fast"]
        dema_s_v   = ctx["dema_slow"]
        macd_v     = ctx["macd_hist"]
        h_v        = ctx["hurst"]
        kalman_t   = ctx["kalman_trend"]   # "ALCISTA" / "BAJISTA"
        price_now  = close.iloc[-1]
        vwap_v     = ctx["vwap"]

        # Contar votos alcistas y bajistas de los indicadores clave
        bullish_votes = sum([
            dema_f_v > dema_s_v,                # DEMA rápida > lenta
            macd_v > 0,                          # MACD histograma positivo
            kalman_t == "ALCISTA",               # Kalman al alza
            price_now > vwap_v,                  # Precio sobre VWAP
            ctx["supertrend"] == 1,              # SuperTrend alcista
            ctx["ha_trend"] == "ALCISTA",        # Heiken Ashi alcista
        ])
        bearish_votes = sum([
            dema_f_v < dema_s_v,
            macd_v < 0,
            kalman_t == "BAJISTA",
            price_now < vwap_v,
            ctx["supertrend"] == -1,
            ctx["ha_trend"] == "BAJISTA",
        ])

        # Clasificación por votos + Hurst
        if bullish_votes >= 5 and h_v > 0.55:
            ctx["h1_trend"] = "ALCISTA_FUERTE"
        elif bullish_votes >= 4:
            ctx["h1_trend"] = "ALCISTA"
        elif bullish_votes >= 3 and bullish_votes > bearish_votes:
            ctx["h1_trend"] = "LATERAL_ALCISTA"
        elif bearish_votes >= 5 and h_v > 0.55:
            ctx["h1_trend"] = "BAJISTA_FUERTE"
        elif bearish_votes >= 4:
            ctx["h1_trend"] = "BAJISTA"
        elif bearish_votes >= 3 and bearish_votes > bullish_votes:
            ctx["h1_trend"] = "LATERAL_BAJISTA"
        else:
            ctx["h1_trend"] = "LATERAL"

        # Guardar votos para el prompt de Gemini (transparencia)
        ctx["trend_votes"] = {"bull": bullish_votes, "bear": bearish_votes}

        micro_price_now = float(micro_df["close"].iloc[-1]) if len(micro_df) > 0 else float(price_now)
        ctx["price"] = round(float(price_now), 4)
        ctx["entry_price"] = round(float(micro_price_now), 4)
        if len(micro_df) > 0 and "time" in micro_df.columns:
            ctx["entry_candle_time"] = pd.Timestamp(micro_df["time"].iloc[-1]).isoformat()
        else:
            ctx["entry_candle_time"] = ""

        # ══════════════════════════════════════════════════════════
        #  PILAR 3 — MICROESTRUCTURA (FASE 1)
        # ══════════════════════════════════════════════════════════
        try:
            micro     = compute_microstructure(micro_df, price=float(micro_price_now))
            ctx["microstructure"] = micro.to_dict()
            ctx["microstructure"]["source_tf"] = "ENTRY" if micro_df is df_entry else "TREND"
        except Exception as micro_err:
            log.debug(f"[indicators] Microstructure error en {symbol}: {micro_err}")
            ctx["microstructure"] = {
                "micro_score": 0.0, "micro_bias": "NEUTRAL",
                "description": "Error en cálculo",
                "source_tf": "UNKNOWN",
            }

        # ══════════════════════════════════════════════════════════
        #  CONFLUENCIA — MATRIZ DE 3 PILARES (FASE 1)
        #
        #  Pesos: P1 (Estadístico) 40 % | P2 (Matemático) 30 % |
        #          P3 (Microestructura)  30 %
        #
        #  Score total en [-3, +3]:
        #  >= +1.0 → BULLISH | <= -1.0 → BEARISH | resto → NEUTRAL
        #
        #  sniper_aligned=True cuando los 3 pilares apuntan en la
        #  misma dirección → entrada de alta probabilidad.
        # ══════════════════════════════════════════════════════════
        try:
            # Pilar 1: Estadístico/Tendencia (de trend_votes)
            p1_score = max(-3.0, min(3.0, float(bullish_votes - bearish_votes) / 2.0))

            # Pilar 2: Matemático/Ciclos
            hil_sig  = ctx.get("hilbert", {}).get("signal", "NEUTRAL")
            p2_bull  = sum([
                hil_sig in ("BUY_CYCLE", "LOCAL_MIN"),
                ctx.get("kalman_trend")   == "ALCISTA",
                ctx.get("fisher",   0.0)  <  -1.5,
                ctx.get("cycle_phase")    == "SUELO",
                ctx.get("hurst_regime")   == "TENDENCIA" and ctx.get("lr_trend") == "ALCISTA",
                ctx.get("lr_r2",    0.0)  >   0.6 and ctx.get("lr_trend") == "ALCISTA",
            ])
            p2_bear  = sum([
                hil_sig in ("SELL_CYCLE", "LOCAL_MAX"),
                ctx.get("kalman_trend")   == "BAJISTA",
                ctx.get("fisher",   0.0)  >   1.5,
                ctx.get("cycle_phase")    == "TECHO",
                ctx.get("hurst_regime")   == "TENDENCIA" and ctx.get("lr_trend") == "BAJISTA",
                ctx.get("lr_r2",    0.0)  >   0.6 and ctx.get("lr_trend") == "BAJISTA",
            ])
            p2_score = max(-3.0, min(3.0, float(p2_bull - p2_bear) / 2.0))

            # Pilar 3: Microestructura
            p3_score = float(ctx.get("microstructure", {}).get("micro_score", 0.0))

            # Weighted confluence
            conf_total = round(0.40 * p1_score + 0.30 * p2_score + 0.30 * p3_score, 2)
            conf_bias  = (
                "BULLISH" if conf_total >=  1.0 else
                "BEARISH" if conf_total <= -1.0 else
                "NEUTRAL"
            )

            # Sniper alignment: all 3 pillars in same direction
            sniper_aligned = (
                (p1_score > 0 and p2_score > 0 and p3_score > 0) or
                (p1_score < 0 and p2_score < 0 and p3_score < 0)
            )

            ctx["confluence"] = {
                "p1_score":      round(p1_score,   2),
                "p2_score":      round(p2_score,   2),
                "p3_score":      round(p3_score,   2),
                "total":         conf_total,
                "bias":          conf_bias,
                "sniper_aligned": sniper_aligned,
            }
        except Exception as conf_err:
            log.debug(f"[indicators] Confluence error en {symbol}: {conf_err}")
            ctx["confluence"] = {
                "p1_score": 0.0, "p2_score": 0.0, "p3_score": 0.0,
                "total": 0.0, "bias": "NEUTRAL", "sniper_aligned": False,
            }

    except Exception as e:
        log.error(f"[indicators] Error en {symbol}: {e}", exc_info=True)

    return ctx