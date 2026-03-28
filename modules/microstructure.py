"""
╔══════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — modules/microstructure.py  (v1.0)      ║
║                                                                  ║
║   PILAR 3 — MICROESTRUCTURA                                     ║
║   (adaptada a tick_volume de Exness / MT5 — sin Nivel 2)        ║
║                                                                  ║
║   COMPONENTES:                                                   ║
║   • Volume Profile  — POC, VAH, VAL (tick_volume histogram)     ║
║   • Session VWAP    — VWAP anclado por sesión UTC               ║
║                       ASIAN(00-08) / EUROPEAN(07-17) /          ║
║                       AMERICAN(13-22)                           ║
║   • Fair Value Gaps — Desequilibrios/Imbalances de precio       ║
║   • Micro Score     — Score consolidado [-3 a +3]               ║
║                                                                  ║
║   NOTA TECNOLÓGICA:                                             ║
║   MT5/Exness provee tick_volume, no volumen real de Nivel 2.    ║
║   El Volume Profile usa esta proxy — válida para detectar       ║
║   niveles de interés (POC, VAH, VAL) con alta consistencia.     ║
╚══════════════════════════════════════════════════════════════════╝
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ── Session definitions — UTC hours [inclusive_start, exclusive_end) ──
SESSIONS: Dict[str, tuple] = {
    "ASIAN":    (0,  8),
    "EUROPEAN": (7,  17),
    "AMERICAN": (13, 22),
}


# ════════════════════════════════════════════════════════════════
#  DATA CLASSES
# ════════════════════════════════════════════════════════════════

@dataclass
class VolumeProfileResult:
    poc:           float  # Point of Control (price level with max tick-volume)
    vah:           float  # Value Area High  (70 % of total volume)
    val:           float  # Value Area Low   (70 % of total volume)
    above_poc:     bool   # True if current price is above POC
    in_value_area: bool   # True if price is between VAL and VAH
    poc_strength:  float  # Relative strength of POC bucket (0–1)


@dataclass
class SessionVWAPResult:
    session_name:      str    # ASIAN / EUROPEAN / AMERICAN / INTER_SESSION
    vwap:              float  # VWAP anchored from session open
    above_vwap:        bool   # True if current price > session VWAP
    deviation_pct:     float  # % deviation from session VWAP
    prev_session_vwap: float = 0.0  # Previous session VWAP for context


@dataclass
class FairValueGap:
    direction:   str    # BULLISH or BEARISH
    high:        float  # Top boundary of the gap
    low:         float  # Bottom boundary of the gap
    midpoint:    float  # Midpoint of the gap
    mitigated:   bool   # True if price subsequently closed inside the gap
    age_candles: int    # Candles elapsed since formation


@dataclass
class MicrostructureResult:
    volume_profile:   VolumeProfileResult
    session_vwap:     SessionVWAPResult
    fvgs:             List[FairValueGap] = field(default_factory=list)
    nearest_bull_fvg: Optional[FairValueGap] = None
    nearest_bear_fvg: Optional[FairValueGap] = None
    micro_score:      float = 0.0   # Consolidated score in [-3, +3]
    micro_bias:       str   = "NEUTRAL"
    score_breakdown:  Dict[str, float] = field(default_factory=dict)
    description:      str   = ""

    def to_dict(self) -> dict:
        nb = self.nearest_bull_fvg
        nd = self.nearest_bear_fvg
        return {
            "poc":               self.volume_profile.poc,
            "vah":               self.volume_profile.vah,
            "val":               self.volume_profile.val,
            "above_poc":         self.volume_profile.above_poc,
            "in_value_area":     self.volume_profile.in_value_area,
            "poc_strength":      self.volume_profile.poc_strength,
            "session":           self.session_vwap.session_name,
            "session_vwap":      self.session_vwap.vwap,
            "above_session_vwap": self.session_vwap.above_vwap,
            "session_vwap_dev":  self.session_vwap.deviation_pct,
            "fvg_bull": (
                {"high": nb.high, "low": nb.low,
                 "mid": nb.midpoint, "age": nb.age_candles}
                if nb else None
            ),
            "fvg_bear": (
                {"high": nd.high, "low": nd.low,
                 "mid": nd.midpoint, "age": nd.age_candles}
                if nd else None
            ),
            "micro_score":     self.micro_score,
            "micro_bias":      self.micro_bias,
            "score_breakdown": self.score_breakdown,
            "description":     self.description,
        }


# ════════════════════════════════════════════════════════════════
#  VOLUME PROFILE
# ════════════════════════════════════════════════════════════════

def volume_profile(
    df:             pd.DataFrame,
    n_candles:      int                    = 100,
    n_bins:         int                    = 50,
    price:          float                  = 0.0,
    real_volume_df: Optional[pd.DataFrame] = None,
) -> VolumeProfileResult:
    """
    Perfil de Volumen.

    Por defecto usa tick_volume de MT5/Exness.  Si se pasa real_volume_df
    (DataFrame M5 de Dukascopy con volumen real), se usa ese volumen en
    lugar del tick_volume del df de MT5.  En caso de fallo silencioso
    se vuelve al tick_volume normal.

    Distribuye el volumen de cada vela proporcionalmente sobre su rango
    high-low y agrupa en n_bins buckets de precio.
    Calcula POC (bucket de máximo volumen) y Value Area (70 % del
    volumen total alrededor del POC).
    """
    # Si hay volumen real disponible, intentar usarlo como fuente de volumen
    if real_volume_df is not None and len(real_volume_df) > 0:
        try:
            subset = df.tail(n_candles).copy().reset_index(drop=True)
            # Merge real volume onto OHLC candles by nearest time match
            rv = real_volume_df[["time", "volume"]].copy()
            rv = rv.rename(columns={"volume": "real_vol"})
            rv["time"] = pd.to_datetime(rv["time"])
            if "time" in subset.columns:
                subset["time"] = pd.to_datetime(subset["time"])
                merged = pd.merge_asof(
                    subset.sort_values("time"),
                    rv.sort_values("time"),
                    on="time",
                    direction="nearest",
                    tolerance=pd.Timedelta("10min"),
                )
                matched = merged["real_vol"].notna().sum()
                # Requiere al menos 5 candles con match O cobertura del 50 % del
                # subset — evita usar volumen real si hay muy pocos datos reales
                # (e.g. Dukascopy devolvió solo 1-2 horas de un lookback de 4h).
                if matched >= max(5, len(subset) // 2):
                    # Use real volume where available, fall back to tick_volume
                    merged["volume"] = merged["real_vol"].where(
                        merged["real_vol"].notna(), merged["volume"]
                    )
                    subset = merged.drop(columns=["real_vol"])
                    subset = subset.reset_index(drop=True)
                    log.debug(
                        f"[micro] volume_profile usando volumen real "
                        f"({matched}/{len(subset)} candles)"
                    )
        except Exception as rv_err:
            log.debug(f"[micro] Fallback a tick_volume: {rv_err}")
            subset = df.tail(n_candles).copy().reset_index(drop=True)
    else:
        subset = df.tail(n_candles).copy().reset_index(drop=True)
    cur    = price or float(df["close"].iloc[-1])

    if len(subset) < 5:
        return VolumeProfileResult(
            poc=cur, vah=cur, val=cur,
            above_poc=True, in_value_area=True, poc_strength=0.0,
        )

    price_min   = float(subset["low"].min())
    price_max   = float(subset["high"].max())
    price_range = price_max - price_min

    if price_range <= 0:
        return VolumeProfileResult(
            poc=cur, vah=cur, val=cur,
            above_poc=True, in_value_area=True, poc_strength=0.0,
        )

    bin_size = price_range / n_bins
    hist     = np.zeros(n_bins, dtype=float)

    for _, row in subset.iterrows():
        lo  = float(row["low"])
        hi  = float(row["high"])
        vol = max(float(row.get("volume", 1) or 1), 1.0)
        rng = hi - lo

        if rng <= 0:
            idx        = int((lo - price_min) / bin_size)
            idx        = max(0, min(n_bins - 1, idx))
            hist[idx] += vol
        else:
            lo_bin = max(0, int((lo - price_min) / bin_size))
            hi_bin = min(n_bins - 1, int((hi - price_min) / bin_size))
            if lo_bin == hi_bin:
                hist[lo_bin] += vol
            else:
                for b in range(lo_bin, hi_bin + 1):
                    b_lo    = price_min + b * bin_size
                    b_hi    = b_lo + bin_size
                    overlap = min(hi, b_hi) - max(lo, b_lo)
                    if overlap > 0:
                        hist[b] += vol * (overlap / rng)

    poc_bin  = int(np.argmax(hist))
    poc      = round(price_min + (poc_bin + 0.5) * bin_size, 5)

    # Value Area — expand from POC until 70 % of total volume is captured
    total_vol  = hist.sum()
    target_vol = total_vol * 0.70
    va_lo_bin  = poc_bin
    va_hi_bin  = poc_bin
    va_vol     = hist[poc_bin]

    while va_vol < target_vol and (va_lo_bin > 0 or va_hi_bin < n_bins - 1):
        add_lo = hist[va_lo_bin - 1] if va_lo_bin > 0     else 0.0
        add_hi = hist[va_hi_bin + 1] if va_hi_bin < n_bins - 1 else 0.0
        if add_hi >= add_lo and va_hi_bin < n_bins - 1:
            va_hi_bin += 1
            va_vol    += add_hi
        elif va_lo_bin > 0:
            va_lo_bin -= 1
            va_vol    += add_lo
        else:
            va_hi_bin += 1
            va_vol    += add_hi

    vah = round(price_min + (va_hi_bin + 1) * bin_size, 5)
    val = round(price_min + va_lo_bin * bin_size, 5)

    poc_strength = round(float(hist[poc_bin] / max(total_vol, 1e-10)), 3)

    return VolumeProfileResult(
        poc=poc, vah=vah, val=val,
        above_poc=cur > poc,
        in_value_area=val <= cur <= vah,
        poc_strength=poc_strength,
    )


# ════════════════════════════════════════════════════════════════
#  SESSION VWAP
# ════════════════════════════════════════════════════════════════

def session_vwap(df: pd.DataFrame, price: float = 0.0) -> SessionVWAPResult:
    """
    VWAP anclado al inicio de la sesión de trading UTC actual.

    Sesiones (con overlap intencionado):
    - ASIAN:    00:00 – 08:00 UTC
    - EUROPEAN: 07:00 – 17:00 UTC
    - AMERICAN: 13:00 – 22:00 UTC

    Prioridad en overlap: AMERICAN > EUROPEAN > ASIAN.
    Requiere columna 'time' en el DataFrame.
    """
    cur = price or float(df["close"].iloc[-1])
    fallback = SessionVWAPResult(
        session_name="UNKNOWN", vwap=cur,
        above_vwap=True, deviation_pct=0.0,
    )

    if "time" not in df.columns or len(df) < 2:
        return fallback

    try:
        last_ts = df["time"].iloc[-1]
        hour    = int(last_ts.hour) if hasattr(last_ts, "hour") else 0
    except Exception:
        return fallback

    # Determine active session (highest priority first)
    active_session    = "INTER_SESSION"
    session_start_hour = 0
    for sess, (s, e) in [
        ("AMERICAN", SESSIONS["AMERICAN"]),
        ("EUROPEAN",  SESSIONS["EUROPEAN"]),
        ("ASIAN",     SESSIONS["ASIAN"]),
    ]:
        if s <= hour < e:
            active_session     = sess
            session_start_hour = s
            break

    # Filter candles from session start (same UTC day context)
    df_sess = df[df["time"].dt.hour >= session_start_hour].copy()
    if len(df_sess) < 2:
        df_sess = df.tail(20)  # Fallback: last 20 bars

    tp   = (df_sess["high"] + df_sess["low"] + df_sess["close"]) / 3
    vol  = df_sess["volume"].replace(0, 1.0)
    vwap_val = round(float((tp * vol).sum() / vol.sum()), 5)

    dev_pct = round((cur - vwap_val) / max(abs(vwap_val), 1e-10) * 100, 3)

    # Previous session VWAP (8 hours before current session start)
    prev_start = (session_start_hour - 8) % 24
    df_prev    = df[
        (df["time"].dt.hour >= prev_start) &
        (df["time"].dt.hour <  session_start_hour)
    ]
    if len(df_prev) >= 2:
        tp_p   = (df_prev["high"] + df_prev["low"] + df_prev["close"]) / 3
        vol_p  = df_prev["volume"].replace(0, 1.0)
        prev_v = round(float((tp_p * vol_p).sum() / vol_p.sum()), 5)
    else:
        prev_v = vwap_val

    return SessionVWAPResult(
        session_name=active_session,
        vwap=vwap_val,
        above_vwap=cur > vwap_val,
        deviation_pct=dev_pct,
        prev_session_vwap=prev_v,
    )


# ════════════════════════════════════════════════════════════════
#  FAIR VALUE GAPS (IMBALANCES)
# ════════════════════════════════════════════════════════════════

def detect_fair_value_gaps(
    df:        pd.DataFrame,
    n_candles: int   = 50,
    price:     float = 0.0,
) -> List[FairValueGap]:
    """
    Detecta Fair Value Gaps (FVG / Imbalances) en las últimas n velas.

    BULLISH FVG: vela[i].low  > vela[i-2].high
        → El precio subió con impulso dejando un vacío inferior.
        → El gap actúa como soporte (precio tiende a volver a llenarlo).

    BEARISH FVG: vela[i].high < vela[i-2].low
        → El precio bajó con impulso dejando un vacío superior.
        → El gap actúa como resistencia.

    Un FVG está "mitigado" cuando el precio regresó y cerró dentro
    del gap (total o parcialmente) en alguna vela posterior.
    """
    subset = df.tail(n_candles + 10).reset_index(drop=True)
    n      = len(subset)
    cur    = price or float(df["close"].iloc[-1])
    fvgs: List[FairValueGap] = []

    for i in range(2, n):
        h_prev2 = float(subset.at[i - 2, "high"])
        l_prev2 = float(subset.at[i - 2, "low"])
        h_curr  = float(subset.at[i,     "high"])
        l_curr  = float(subset.at[i,     "low"])
        age     = n - 1 - i  # 0 = current candle

        # ── Bullish FVG ──────────────────────────────────────────
        if l_curr > h_prev2:
            gap_low  = h_prev2
            gap_high = l_curr
            mitigated = any(
                float(subset.at[j, "low"]) <= gap_high
                for j in range(i + 1, n)
            )
            fvgs.append(FairValueGap(
                direction="BULLISH",
                high=round(gap_high, 5), low=round(gap_low, 5),
                midpoint=round((gap_low + gap_high) / 2, 5),
                mitigated=mitigated, age_candles=age,
            ))

        # ── Bearish FVG ──────────────────────────────────────────
        elif h_curr < l_prev2:
            gap_high = l_prev2
            gap_low  = h_curr
            mitigated = any(
                float(subset.at[j, "high"]) >= gap_low
                for j in range(i + 1, n)
            )
            fvgs.append(FairValueGap(
                direction="BEARISH",
                high=round(gap_high, 5), low=round(gap_low, 5),
                midpoint=round((gap_low + gap_high) / 2, 5),
                mitigated=mitigated, age_candles=age,
            ))

    return fvgs


def _nearest_fvg(
    fvgs:     List[FairValueGap],
    direction: str,
    price:     float,
    max_age:   int = 20,
) -> Optional[FairValueGap]:
    """Retorna el FVG activo (no mitigado) más cercano al precio actual."""
    candidates = [
        f for f in fvgs
        if f.direction == direction
        and not f.mitigated
        and f.age_candles <= max_age
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda f: abs(f.midpoint - price))


# ════════════════════════════════════════════════════════════════
#  MICRO SCORE COMPUTATION
# ════════════════════════════════════════════════════════════════

def _micro_score_from_components(
    vp:    VolumeProfileResult,
    sv:    SessionVWAPResult,
    fvgs:  List[FairValueGap],
    price: float,
) -> tuple:
    """
    Computa el score del Pilar 3 en el rango [-3, +3].

    Componentes (cada uno aporta ±1):
    1. Volume Profile POC:  precio > POC → +1 | precio < POC → -1
    2. Value Area position: precio > VAH → +1 | precio < VAL → -1 | en VA → 0
    3. Session VWAP:        precio > S-VWAP → +1 | precio < S-VWAP → -1
    4. FVG proximity:       precio en zona bullish FVG activo → +1
                            precio en zona bearish FVG activo → -1
                            sin FVG cercano → 0

    Retorna (score: float, breakdown: dict)
    """
    breakdown: Dict[str, float] = {}
    score = 0.0

    # 1. Volume Profile POC
    poc_comp              = 1.0 if vp.above_poc else -1.0
    breakdown["vol_poc"]  = poc_comp
    score += poc_comp

    # 2. Value Area context
    if price > vp.vah:
        va_comp = 1.0    # Bullish breakout above value area
    elif price < vp.val:
        va_comp = -1.0   # Bearish breakdown below value area
    else:
        va_comp = 0.0    # Inside value area — no directional edge
    breakdown["value_area"] = va_comp
    score += va_comp

    # 3. Session VWAP
    vwap_comp              = 1.0 if sv.above_vwap else -1.0
    breakdown["sess_vwap"] = vwap_comp
    score += vwap_comp

    # 4. FVG proximity (only fresh, unmitigated FVGs within 0.3 % of price)
    fvg_comp  = 0.0
    prox_pct  = abs(price) * 0.003  # 0.3 % proximity threshold

    bull_fvgs = [
        f for f in fvgs
        if f.direction == "BULLISH" and not f.mitigated and f.age_candles <= 30
    ]
    bear_fvgs = [
        f for f in fvgs
        if f.direction == "BEARISH" and not f.mitigated and f.age_candles <= 30
    ]

    # Price sitting on / just above a bullish FVG → support → bullish
    for fvg in bull_fvgs:
        if price >= fvg.low and abs(price - fvg.high) <= prox_pct:
            fvg_comp = 1.0
            break

    # Price at / just below a bearish FVG → resistance → bearish
    if fvg_comp == 0.0:
        for fvg in bear_fvgs:
            if price <= fvg.high and abs(price - fvg.low) <= prox_pct:
                fvg_comp = -1.0
                break

    breakdown["fvg_prox"] = fvg_comp
    score += fvg_comp

    return max(-3.0, min(3.0, score)), breakdown


# ════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ════════════════════════════════════════════════════════════════

def compute_microstructure(
    df:             pd.DataFrame,
    price:          float                  = 0.0,
    real_volume_df: Optional[pd.DataFrame] = None,
) -> MicrostructureResult:
    """
    Calcula el Tercer Pilar (Microestructura) completo.

    Compatible con tick_volume de Exness / MT5 (sin Nivel 2 real).
    Si se pasa real_volume_df (Dukascopy M5), se usa volumen real en el
    Volume Profile.  Fallback silencioso a tick_volume si falla.
    Usa el DataFrame de velas recibido (funciona con cualquier TF).
    """
    cur = price or (float(df["close"].iloc[-1]) if df is not None and len(df) > 0 else 0.0)

    if df is None or len(df) < 10:
        dummy_vp = VolumeProfileResult(
            poc=cur, vah=cur, val=cur,
            above_poc=True, in_value_area=True, poc_strength=0.0,
        )
        dummy_sv = SessionVWAPResult(
            session_name="UNKNOWN", vwap=cur,
            above_vwap=True, deviation_pct=0.0,
        )
        return MicrostructureResult(
            volume_profile=dummy_vp, session_vwap=dummy_sv,
            micro_score=0.0, micro_bias="NEUTRAL",
            description="Datos insuficientes",
        )

    try:
        vp = volume_profile(
            df, n_candles=min(100, len(df)), n_bins=50, price=cur,
            real_volume_df=real_volume_df,
        )
    except Exception as exc:
        log.debug(f"[micro] volume_profile error: {exc}")
        vp = VolumeProfileResult(
            poc=cur, vah=cur, val=cur,
            above_poc=True, in_value_area=True, poc_strength=0.0,
        )

    try:
        sv = session_vwap(df, price=cur)
    except Exception as exc:
        log.debug(f"[micro] session_vwap error: {exc}")
        sv = SessionVWAPResult(
            session_name="UNKNOWN", vwap=cur,
            above_vwap=True, deviation_pct=0.0,
        )

    try:
        fvgs = detect_fair_value_gaps(df, n_candles=50, price=cur)
    except Exception as exc:
        log.debug(f"[micro] fvg error: {exc}")
        fvgs = []

    nearest_bull = _nearest_fvg(fvgs, "BULLISH", cur, max_age=20)
    nearest_bear = _nearest_fvg(fvgs, "BEARISH", cur, max_age=20)

    micro_score, breakdown = _micro_score_from_components(vp, sv, fvgs, cur)

    if micro_score >= 1.5:
        bias = "BULLISH"
    elif micro_score <= -1.5:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    # Human-readable description
    parts = []
    parts.append(f"POC={'↑' if vp.above_poc else '↓'}{vp.poc:.4f}")
    parts.append(f"VA={'IN' if vp.in_value_area else ('↑' if cur > vp.vah else '↓')}")
    parts.append(
        f"S-VWAP({sv.session_name[:3]})="
        f"{'↑' if sv.above_vwap else '↓'}{abs(sv.deviation_pct):.2f}%"
    )
    if nearest_bull:
        parts.append(f"FVG-Bull@{nearest_bull.high:.4f}")
    if nearest_bear:
        parts.append(f"FVG-Bear@{nearest_bear.low:.4f}")
    description = " | ".join(parts)

    return MicrostructureResult(
        volume_profile=vp,
        session_vwap=sv,
        fvgs=fvgs,
        nearest_bull_fvg=nearest_bull,
        nearest_bear_fvg=nearest_bear,
        micro_score=round(micro_score, 2),
        micro_bias=bias,
        score_breakdown=breakdown,
        description=description,
    )
