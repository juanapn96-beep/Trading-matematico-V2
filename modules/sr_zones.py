"""
╔══════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — sr_zones.py                            ║
║                                                                  ║
║   6 MÉTODOS DE DETECCIÓN S/R:                                   ║
║   1. Fractales Williams (swing highs/lows validados)            ║
║   2. Volume Profile (Point of Control por precio)               ║
║   3. Fibonacci Retracements (últimos swing H/L)                 ║
║   4. CPR + Pivotes clásicos (R1,R2,R3,S1,S2,S3)               ║
║   5. Niveles psicológicos (números redondos por activo)         ║
║   6. Confluencia ponderada multi-timeframe                      ║
╚══════════════════════════════════════════════════════════════════╝
"""

import numpy as np
import pandas as pd
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional

log = logging.getLogger(__name__)


@dataclass
class SRZone:
    price:     float
    strength:  float        # 0-10 — peso de confluencia
    method:    str          # Qué método(s) la detectaron
    timeframe: str          # TF original
    zone_type: str          # "SUPPORT" / "RESISTANCE"
    touches:   int = 1      # Veces que el precio ha tocado esta zona
    last_touch: str = ""    # Fecha último toque


@dataclass
class SRContext:
    supports:      List[SRZone]   = field(default_factory=list)
    resistances:   List[SRZone]   = field(default_factory=list)
    nearest_sup:   Optional[float] = None
    nearest_res:   Optional[float] = None
    in_strong_zone: bool           = False
    dist_to_sup_pct: float         = 999.0
    dist_to_res_pct: float         = 999.0
    fib_levels:    Dict[str, float] = field(default_factory=dict)
    summary:       str             = ""


# ══════════════════════════════════════════════════════════════════
#  MÉTODO 1 — FRACTALES WILLIAMS
# ══════════════════════════════════════════════════════════════════

def find_fractals(df: pd.DataFrame, n: int = 2) -> tuple:
    """
    Detecta fractales de Williams: un máximo/mínimo local validado
    por N velas a cada lado.

    Fractal bajista (swing high): high[i] > max(high[i-n:i], high[i+1:i+n+1])
    Fractal alcista (swing low):  low[i]  < min(low[i-n:i],  low[i+1:i+n+1])
    """
    highs_idx = []
    lows_idx  = []

    for i in range(n, len(df) - n):
        h = df["high"].iloc[i]
        l = df["low"].iloc[i]
        left_h  = df["high"].iloc[i-n:i]
        right_h = df["high"].iloc[i+1:i+n+1]
        left_l  = df["low"].iloc[i-n:i]
        right_l = df["low"].iloc[i+1:i+n+1]

        if h > left_h.max() and h > right_h.max():
            highs_idx.append(i)
        if l < left_l.min() and l < right_l.min():
            lows_idx.append(i)

    return highs_idx, lows_idx


# ══════════════════════════════════════════════════════════════════
#  MÉTODO 2 — VOLUME PROFILE (Point of Control)
# ══════════════════════════════════════════════════════════════════

def volume_profile(df: pd.DataFrame, bins: int = 50) -> List[float]:
    """
    Detecta niveles de precio donde más volumen se ha negociado.
    El Point of Control (POC) es el precio con mayor volumen —
    actúa como imán y como S/R fuerte.
    """
    if "volume" not in df.columns or df["volume"].sum() == 0:
        return []

    price_range = df["high"].max() - df["low"].min()
    if price_range == 0:
        return []

    bin_edges = np.linspace(df["low"].min(), df["high"].max(), bins + 1)
    vol_by_bin = np.zeros(bins)

    for _, row in df.iterrows():
        # Distribuir el volumen de cada vela entre los bins que toca
        start_bin = np.searchsorted(bin_edges, row["low"])
        end_bin   = np.searchsorted(bin_edges, row["high"])
        start_bin = max(0, start_bin - 1)
        end_bin   = min(bins - 1, end_bin)

        if end_bin >= start_bin:
            bins_touched = end_bin - start_bin + 1
            vol_per_bin  = row["volume"] / bins_touched
            vol_by_bin[start_bin:end_bin+1] += vol_per_bin

    # Encontrar picos de volumen (POC y VAH/VAL)
    total_vol = vol_by_bin.sum()
    if total_vol == 0:
        return []

    threshold = vol_by_bin.max() * 0.7  # niveles con >70% del volumen máximo
    poc_levels = []
    for i, v in enumerate(vol_by_bin):
        if v >= threshold:
            price = (bin_edges[i] + bin_edges[i+1]) / 2
            poc_levels.append(round(price, 4))

    return poc_levels


# ══════════════════════════════════════════════════════════════════
#  MÉTODO 3 — FIBONACCI RETRACEMENTS
# ══════════════════════════════════════════════════════════════════

FIB_LEVELS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.618]

def fibonacci_levels(df: pd.DataFrame, lookback: int = 100) -> Dict[str, float]:
    """
    Calcula niveles de Fibonacci entre el swing máximo y mínimo
    de los últimos N períodos.

    Los niveles de 38.2%, 50% y 61.8% son los más respetados.
    """
    recent = df.iloc[-lookback:]
    swing_high = float(recent["high"].max())
    swing_low  = float(recent["low"].min())
    rng        = swing_high - swing_low

    if rng == 0:
        return {}

    levels = {}
    for fib in FIB_LEVELS:
        key          = f"fib_{int(fib*1000)}".replace("_0", "_")
        levels[key]  = round(swing_high - fib * rng, 4)

    levels["swing_high"] = swing_high
    levels["swing_low"]  = swing_low
    return levels


# ══════════════════════════════════════════════════════════════════
#  MÉTODO 4 — CPR + PIVOTES CLÁSICOS
# ══════════════════════════════════════════════════════════════════

def classic_pivots(df: pd.DataFrame) -> Dict[str, float]:
    """
    Pivotes clásicos calculados sobre el día anterior.
    R1, R2, R3, S1, S2, S3 — niveles institucionales clave.
    """
    prev  = df.iloc[-2] if len(df) > 1 else df.iloc[-1]
    H, L, C = float(prev["high"]), float(prev["low"]), float(prev["close"])
    P  = (H + L + C) / 3
    R1 = 2 * P - L
    R2 = P + (H - L)
    R3 = H + 2 * (P - L)
    S1 = 2 * P - H
    S2 = P - (H - L)
    S3 = L - 2 * (H - P)
    BC = (H + L) / 2
    TC = 2 * P - BC
    return dict(P=round(P,4), R1=round(R1,4), R2=round(R2,4), R3=round(R3,4),
                S1=round(S1,4), S2=round(S2,4), S3=round(S3,4),
                BC=round(BC,4), TC=round(TC,4))


# ══════════════════════════════════════════════════════════════════
#  MÉTODO 5 — NIVELES PSICOLÓGICOS
# ══════════════════════════════════════════════════════════════════

def psychological_levels(price: float, symbol: str) -> List[float]:
    """
    Los grandes operadores acumulan órdenes en números redondos.
    El bot los detecta dinámicamente según el precio actual.
    """
    levels = []

    if "XAU" in symbol or "GOLD" in symbol:
        # Oro: niveles en $50 y $100
        base = round(price / 100) * 100
        for mult in range(-3, 4):
            levels.append(base + mult * 100)
            levels.append(base + mult * 100 + 50)
    elif "500" in symbol or "NAS" in symbol or "DAX" in symbol:
        # Índices: niveles en 50 y 100 puntos
        base = round(price / 100) * 100
        for mult in range(-3, 4):
            levels.append(base + mult * 100)
            levels.append(base + mult * 100 + 50)
    else:
        # Forex: niveles en pips redondos
        magnitude = 10 ** (len(str(int(price))) - 1)
        base = round(price / magnitude) * magnitude
        for mult in range(-3, 4):
            levels.append(base + mult * magnitude)
            levels.append(base + mult * magnitude * 0.5)

    return [round(l, 4) for l in levels if abs(l - price) / price < 0.03]


# ══════════════════════════════════════════════════════════════════
#  MÉTODO 6 — CONFLUENCIA MULTI-TIMEFRAME PONDERADA
# ══════════════════════════════════════════════════════════════════

def build_sr_context(
    dfs_by_tf: Dict[str, pd.DataFrame],
    current_price: float,
    symbol: str,
    sym_cfg: dict,
) -> SRContext:
    """
    Detecta S/R en múltiples timeframes y pondera por confluencia.
    H4 y D1 tienen mayor peso que M5/M15 — representan zonas institucionales.

    Un nivel es FUERTE si es detectado por 2+ métodos o TFs distintos.
    """
    tolerance_pct = sym_cfg.get("sr_tolerance_pct", 0.3) / 100
    tf_weights    = sym_cfg.get("tf_weights", {})
    raw_levels: Dict[str, list] = {}  # price → [(weight, method, tf)]

    def add_level(price: float, weight: float, method: str, tf: str):
        key = None
        for existing in raw_levels:
            if abs(float(existing) - price) / (current_price + 1e-8) <= tolerance_pct:
                key = existing
                break
        if key is None:
            key = str(round(price, 4))
        raw_levels.setdefault(key, []).append((weight, method, tf))

    for tf, df in dfs_by_tf.items():
        if df is None or len(df) < 20:
            continue
        w = tf_weights.get(tf, 1)

        # Fractales
        try:
            h_idx, l_idx = find_fractals(df, n=3)
            for i in h_idx[-8:]:
                add_level(float(df["high"].iloc[i]), w * 1.0, "FRACTAL_HIGH", tf)
            for i in l_idx[-8:]:
                add_level(float(df["low"].iloc[i]),  w * 1.0, "FRACTAL_LOW",  tf)
        except Exception:
            pass

        # Volume Profile
        try:
            for lvl in volume_profile(df):
                add_level(lvl, w * 0.8, "VOL_PROFILE", tf)
        except Exception:
            pass

        # Fibonacci (solo en TFs mayores)
        if tf in ["H1", "H4", "D1"]:
            try:
                fibs = fibonacci_levels(df)
                for fname, fval in fibs.items():
                    if "fib" in fname and abs(fval - current_price) / current_price < 0.05:
                        w_fib = w * (1.5 if fname in ["fib_382","fib_500","fib_618"] else 0.8)
                        add_level(fval, w_fib, f"FIB_{fname.upper()}", tf)
            except Exception:
                pass

        # Pivotes clásicos
        try:
            pivs = classic_pivots(df)
            for pname, pval in pivs.items():
                if abs(pval - current_price) / current_price < 0.05:
                    add_level(pval, w * 1.2, f"PIVOT_{pname}", tf)
        except Exception:
            pass

    # Niveles psicológicos (no dependen de TF)
    try:
        for psych in psychological_levels(current_price, symbol):
            add_level(psych, 1.0, "PSYCHOLOGICAL", "ALL")
    except Exception:
        pass

    # ── Construir zonas finales ──
    supports    = []
    resistances = []

    for price_key, touches in raw_levels.items():
        price   = float(price_key)
        total_w = sum(t[0] for t in touches)
        methods = list(set(t[1] for t in touches))
        tfs     = list(set(t[2] for t in touches))
        n_touch = len(touches)
        strength = min(10.0, total_w * (1 + 0.3 * (n_touch - 1)))

        zone = SRZone(
            price     = round(price, 4),
            strength  = round(strength, 2),
            method    = ", ".join(methods),
            timeframe = ", ".join(tfs),
            zone_type = "RESISTANCE" if price > current_price else "SUPPORT",
            touches   = n_touch,
        )
        if price > current_price:
            resistances.append(zone)
        else:
            supports.append(zone)

    # Ordenar y filtrar
    supports    = sorted(supports,    key=lambda z: z.price, reverse=True)[:8]
    resistances = sorted(resistances, key=lambda z: z.price)[:8]

    # Zonas más cercanas
    nearest_sup = supports[0].price    if supports    else None
    nearest_res = resistances[0].price if resistances else None

    dist_sup = (abs(current_price - nearest_sup) / current_price * 100) if nearest_sup else 999.0
    dist_res = (abs(current_price - nearest_res) / current_price * 100) if nearest_res else 999.0
    tol_pct  = sym_cfg.get("sr_tolerance_pct", 0.3)

    in_strong = (
        (dist_sup < tol_pct and any(s.strength >= 5 for s in supports[:2]))
        or
        (dist_res < tol_pct and any(r.strength >= 5 for r in resistances[:2]))
    )

    # Fibonacci del contexto mayor
    main_tf = [tf for tf in ["D1", "H4", "H1"] if tf in dfs_by_tf]
    fib_ctx = {}
    if main_tf:
        fib_ctx = fibonacci_levels(dfs_by_tf[main_tf[0]])

    # Resumen legible
    lines = []
    if resistances:
        top3r = resistances[:3]
        lines.append(f"🔴 Resistencias: " + " | ".join(f"{r.price} (fuerza {r.strength:.1f})" for r in top3r))
    if supports:
        top3s = supports[:3]
        lines.append(f"🟢 Soportes: " + " | ".join(f"{s.price} (fuerza {s.strength:.1f})" for s in top3s))
    if in_strong:
        lines.append("⚠️ PRECIO EN ZONA S/R FUERTE")
    if nearest_res:
        lines.append(f"📏 Distancia a R más cercana: {dist_res:.2f}%")
    if nearest_sup:
        lines.append(f"📏 Distancia a S más cercana: {dist_sup:.2f}%")

    return SRContext(
        supports        = supports,
        resistances     = resistances,
        nearest_sup     = nearest_sup,
        nearest_res     = nearest_res,
        in_strong_zone  = in_strong,
        dist_to_sup_pct = round(dist_sup, 3),
        dist_to_res_pct = round(dist_res, 3),
        fib_levels      = fib_ctx,
        summary         = "\n".join(lines),
    )


def sr_for_prompt(ctx: SRContext) -> str:
    """Formatea el contexto S/R para el motor de decisión."""
    lines = [ctx.summary, ""]

    if ctx.fib_levels:
        fib_str = []
        for k in ["fib_382", "fib_500", "fib_618"]:
            if k in ctx.fib_levels:
                fib_str.append(f"{k.replace('fib_','')}: {ctx.fib_levels[k]}")
        if fib_str:
            lines.append("📐 Fibonacci clave: " + " | ".join(fib_str))

    if ctx.supports:
        lines.append("\n🟢 SOPORTES ACTIVOS:")
        for s in ctx.supports[:5]:
            lines.append(f"  {s.price:>10.4f}  fuerza={s.strength:.1f}  [{s.method}] [{s.timeframe}]")

    if ctx.resistances:
        lines.append("\n🔴 RESISTENCIAS ACTIVAS:")
        for r in ctx.resistances[:5]:
            lines.append(f"  {r.price:>10.4f}  fuerza={r.strength:.1f}  [{r.method}] [{r.timeframe}]")

    return "\n".join(lines)
