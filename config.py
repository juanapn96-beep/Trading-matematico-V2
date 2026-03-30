"""
╔══════════════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — config.py  (v6.5 — ENV SECURIZATION)          ║
║                                                                          ║
║   CAMBIOS v6.5:                                                         ║
║   • FASE 0: Credenciales migradas a .env (python-dotenv)               ║
║   • BREAKEVEN_ATR_MULT: BE por ATR en vez de pips fijos                ║
║   • SYMBOL_COOLDOWN_SEC: cooldown entre trades del mismo símbolo       ║
║   • US500m min_hurst: 0.45→0.38 (índices operan con Hurst bajo)       ║
║   • XAGUSDm min_hurst: 0.38→0.35 (casi siempre bajo el umbral)        ║
║   • USOILm min_hurst: 0.42→0.38                                        ║
║   • be_atr_mult por símbolo (personalizable)                           ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import os
import sys
from dotenv import load_dotenv

# Carga el archivo .env desde la raíz del proyecto (si existe)
load_dotenv()


def _require_env(name: str) -> str:
    """Devuelve el valor de la variable de entorno o termina con error claro."""
    value = os.environ.get(name, "")
    if not value:
        print(
            f"❌  Variable de entorno requerida no configurada: {name}\n"
            "    Copia .env.example a .env y rellena todas las credenciales.",
            file=sys.stderr,
        )
        sys.exit(1)
    return value


# ================================================================
#  MT5 / BROKER
# ================================================================
MT5_LOGIN    = int(_require_env("MT5_LOGIN"))
MT5_PASSWORD = _require_env("MT5_PASSWORD")
MT5_SERVER   = os.environ.get("MT5_SERVER", "Exness-MT5Trial11")

# Sufijo de símbolo del broker.  Exness usa "m" (XAUUSDm, EURUSDm…).
# IC Markets usa "" vacío (XAUUSD, EURUSD…).  Cambia en el .env para migrar.
# Nota: USTEC y DE40 en Exness no llevan sufijo; mantenlos en _NO_SUFFIX.
BROKER_SUFFIX = os.environ.get("BROKER_SUFFIX", "m")

# Símbolos que NO llevan el sufijo en ningún broker (por convención del feed).
_NO_SUFFIX = {"USTEC", "DE40"}

# ================================================================
#  GROQ
# ================================================================
GROQ_API_KEY   = _require_env("GROQ_API_KEY")
GROQ_MODEL     = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_MAX_CALLS_PER_HOUR = int(os.environ.get("GROQ_MAX_CALLS_PER_HOUR", "0") or 0)
GROQ_MAX_CALLS_PER_DAY  = int(os.environ.get("GROQ_MAX_CALLS_PER_DAY",  "0") or 0)

# ================================================================
#  TELEGRAM
# ================================================================
TELEGRAM_TOKEN   = _require_env("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = _require_env("TELEGRAM_CHAT_ID")

# ================================================================
#  APIs EXTERNAS
# ================================================================
ALPHA_VANTAGE_KEY = _require_env("ALPHA_VANTAGE_KEY")
FINNHUB_KEY       = _require_env("FINNHUB_KEY")


def _sym(base: str) -> str:
    """Retorna el nombre completo del símbolo según el broker configurado."""
    if base in _NO_SUFFIX:
        return base
    return f"{base}{BROKER_SUFFIX}"


# ================================================================
#  SÍMBOLOS — 12 ACTIVOS, SESIONES EXPANDIDAS 24/5
# ================================================================
SYMBOLS = {

    # ── 1. ORO — 24/5 ──────────────────────────────────────────
    _sym("XAUUSD"): {
        "name":           "Oro (Gold)",
        "currencies":     ["USD"],
        "strategy_type":  "VOLATILITY_CYCLE",
        "strategy_extra_rules": (
            "ESTRATEGIA VOLATILITY_CYCLE — ORO (XAUUSDm):\n"
            "- Opera 24 horas, 5 días. El oro tiene volatilidad incluso en sesión asiática.\n"
            "- Sesión asiática (00-07 UTC): movimientos más lentos pero ciclos Hilbert muy limpios.\n"
            "- Sesión europea (07-13 UTC): volatilidad media, buenos entries con S/R.\n"
            "- Sesión americana (13-21 UTC): máxima volatilidad, seguir momentum agresivamente.\n"
            "- LOCAL_MIN = señal BUY de alta probabilidad. LOCAL_MAX = señal SELL.\n"
            "- Fisher < -2.5 con Hurst > 0.42: oportunidad estadística fuerte.\n"
            "- El oro es refugio: en risk-off global sube fuerte sin importar la hora.\n"
            "- IMPORTANTE: NO abrir más de 1 posición simultánea. Respetar el cooldown.\n"
            "- Si Hilbert muestra LOCAL_MIN pero el precio lleva 5+ velas bajistas → HOLD."
        ),
        "session_start":  0,
        "session_end":    24,
        "sl_atr_mult":    2.0,
        "tp_atr_mult":    4.0,
        "be_atr_mult":    2.8,
        "rsi_oversold":   30,
        "rsi_overbought": 70,
        "min_confidence": 7,
        "min_hurst":      0.42,
        "sr_tolerance_pct": 0.40,
        "sr_lookback":    150,
        "sr_timeframes":  ["M15", "H1", "H4", "D1"],
        "tf_weights":     {"M15": 1, "H1": 2, "H4": 3, "D1": 4},
        "atr_norm_factor":  30.0,
        "price_scale":    5000.0,
        "news_topics":    "economy_monetary,economy_macro,finance,forex",
        "memory_min_trades":      5,
        "memory_block_threshold": 0.90,
        "memory_warn_threshold":  0.80,
        "memory_decay_days":      45,
    },

    # ── 2. S&P 500 — ampliado ──────────────────────────────────
    _sym("US500"): {
        "name":           "S&P 500",
        "currencies":     ["USD"],
        "strategy_type":  "MOMENTUM_TREND",
        "strategy_extra_rules": (
            "ESTRATEGIA MOMENTUM_TREND — S&P 500 (US500m):\n"
            "- Opera casi 24h en Exness (CFD). Sesión americana 13-21 UTC es la de mayor volatilidad.\n"
            "- Pre-market (11-13 UTC): hay movimiento, con cautela extra en S/R de día anterior.\n"
            "- After-hours (21-23 UTC): movimiento reducido, solo entrar con señal muy clara.\n"
            "- Madrugada (23-11 UTC): el filtro ATR bloqueará automáticamente si no hay movimiento.\n"
            "- MACD histograma es la señal primaria. SuperTrend + Kalman deben confirmar.\n"
            "- Hurst en índices es naturalmente más bajo (0.35-0.45). Esto es normal.\n"
            "- Eventos macro NFP, CPI, FOMC: el calendario pausará automáticamente."
        ),
        "session_start":  0,
        "session_end":    24,
        "sl_atr_mult":    1.6,
        "tp_atr_mult":    3.2,
        "be_atr_mult":    2.3,
        "rsi_oversold":   35,
        "rsi_overbought": 65,
        "min_confidence": 6,
        # FIX v6.4: Bajado de 0.45 a 0.38 — los índices tienen Hurst más bajo por naturaleza
        "min_hurst":      0.38,
        "sr_tolerance_pct": 0.25,
        "sr_lookback":    120,
        "sr_timeframes":  ["M5", "M15", "H1", "H4"],
        "tf_weights":     {"M5": 1, "M15": 2, "H1": 3, "H4": 4},
        "atr_norm_factor":  20.0,
        "price_scale":    6700.0,
        "news_topics":    "economy_monetary,economy_macro,finance,earnings",
        "memory_min_trades":      4,
        "memory_block_threshold": 0.88,
        "memory_warn_threshold":  0.78,
        "memory_decay_days":      30,
    },

    # ── 3. EUR/USD — 24/5 ──────────────────────────────────────
    _sym("EURUSD"): {
        "name":           "Euro / Dólar",
        "currencies":     ["EUR", "USD"],
        "strategy_type":  "CYCLE_REVERSION",
        "strategy_extra_rules": (
            "ESTRATEGIA CYCLE_REVERSION — EURUSD (EURUSDm):\n"
            "- Opera 24h. Par más líquido del mundo — hay movimiento a cualquier hora.\n"
            "- Sesión asiática (00-07 UTC): rango más estrecho, ideal para reversiones S/R.\n"
            "- Sesión europea (07-17 UTC): máxima actividad, seguir momentum.\n"
            "- Sesión americana (13-21 UTC): volatilidad alta por datos USD.\n"
            "- Con Hurst 0.38-0.50: estrategia de reversión desde S/R. Con Hurst>0.55: momentum.\n"
            "- ECB y Fed: el calendario pausará automáticamente 1min antes."
        ),
        "session_start":  0,
        "session_end":    24,
        "sl_atr_mult":    1.5,
        "tp_atr_mult":    3.0,
        "be_atr_mult":    2.2,
        "rsi_oversold":   35,
        "rsi_overbought": 65,
        "min_confidence": 6,
        "min_hurst":      0.38,
        "sr_tolerance_pct": 0.10,
        "sr_lookback":    120,
        "sr_timeframes":  ["M15", "H1", "H4"],
        "tf_weights":     {"M15": 1, "H1": 2, "H4": 3},
        "atr_norm_factor":  0.0012,
        "price_scale":    1.10,
        "news_topics":    "economy_monetary,forex,economy_macro",
        "memory_min_trades":      5,
        "memory_block_threshold": 0.88,
        "memory_warn_threshold":  0.78,
        "memory_decay_days":      30,
    },

    # ── 4. GBP/USD — 24/5 ──────────────────────────────────────
    _sym("GBPUSD"): {
        "name":           "Libra / Dólar (Cable)",
        "currencies":     ["GBP", "USD"],
        "strategy_type":  "MOMENTUM_SURGE",
        "strategy_extra_rules": (
            "ESTRATEGIA MOMENTUM_SURGE — GBPUSD / Cable (GBPUSDm):\n"
            "- Opera 24h. Hora pico: 7-12 UTC (Frankfurt-Londres). Segunda ventana: 13-17 UTC.\n"
            "- Sesión asiática (00-07 UTC): movimiento reducido, priorizar reversiones S/R claras.\n"
            "- Buscar MACD histograma acelerándose + HA en dirección por 3+ velas.\n"
            "- Fisher extremo (>2.0 o <-2.0) es señal de reversión altamente confiable en GBP.\n"
            "- BOE y UK CPI: el calendario pausará automáticamente.\n"
            "- IMPORTANTE: Si el precio está en resistencia fuerte (strength>7) → NO BUY."
        ),
        "session_start":  0,
        "session_end":    24,
        "sl_atr_mult":    2.0,
        "tp_atr_mult":    5.0,
        "be_atr_mult":    2.3,
        "rsi_oversold":   32,
        "rsi_overbought": 68,
        "min_confidence": 6,
        "min_hurst":      0.42,
        "sr_tolerance_pct": 0.12,
        "sr_lookback":    130,
        "sr_timeframes":  ["M15", "H1", "H4"],
        "tf_weights":     {"M15": 1, "H1": 2, "H4": 3},
        "atr_norm_factor":  0.0015,
        "price_scale":    1.27,
        "news_topics":    "economy_monetary,forex,economy_macro",
        "memory_min_trades":      5,
        "memory_block_threshold": 0.90,
        "memory_warn_threshold":  0.80,
        "memory_decay_days":      35,
    },

    # ── 5. USD/JPY — 24/5 ──────────────────────────────────────
    _sym("USDJPY"): {
        "name":           "Dólar / Yen (Ninja)",
        "currencies":     ["USD", "JPY"],
        "strategy_type":  "TREND_KALMAN",
        "strategy_extra_rules": (
            "ESTRATEGIA TREND_KALMAN — USDJPY / Ninja (USDJPYm):\n"
            "- Opera 24h. Sesión asiática (00-09 UTC) es CLAVE para USDJPY — Japón activo.\n"
            "- Kalman slope es el indicador primario. Sin slope claro → HOLD.\n"
            "- Sesión asiática: seguir tendencia con Kalman. Sesión europea/americana: momentum.\n"
            "- BOJ mueve el par 100+ pips. El calendario pausará automáticamente.\n"
            "- Si hay posición SELL abierta → NO abrir BUY en el mismo ciclo (cooldown protege).\n"
            "- Cuando precio en soporte fuerte (strength>7) + tendencia BAJISTA → SELL desde S/R."
        ),
        "session_start":  0,
        "session_end":    24,
        "sl_atr_mult":    1.8,
        "tp_atr_mult":    3.6,
        "be_atr_mult":    2.2,
        "rsi_oversold":   35,
        "rsi_overbought": 65,
        "min_confidence": 6,
        "min_hurst":      0.44,
        "sr_tolerance_pct": 0.15,
        "sr_lookback":    140,
        "sr_timeframes":  ["M15", "H1", "H4"],
        "tf_weights":     {"M15": 1, "H1": 2, "H4": 3},
        "atr_norm_factor":  0.20,
        "price_scale":    150.0,
        "news_topics":    "economy_monetary,forex,economy_macro",
        "memory_min_trades":      5,
        "memory_block_threshold": 0.90,
        "memory_warn_threshold":  0.80,
        "memory_decay_days":      35,
    },

    # ── 6. GBP/JPY — 24/5, con cautela extra ───────────────────
    _sym("GBPJPY"): {
        "name":           "Libra / Yen (El Dragón)",
        "currencies":     ["GBP", "JPY"],
        "strategy_type":  "DRAGON_EXPLOSION",
        "strategy_extra_rules": (
            "ESTRATEGIA DRAGON_EXPLOSION — GBPJPY / El Dragón (GBPJPYm):\n"
            "- Opera 24h PERO con la máxima selectividad. El Dragón es peligroso a cualquier hora.\n"
            "- Hora óptima: 7-12 UTC (overlap Europa-Londres). Segunda ventana: 0-4 UTC (Tokio activo).\n"
            "- REQUIERE todos los primarios alineados: h1_trend + MACD + Hilbert + SuperTrend + Kalman + HA.\n"
            "- Hurst mínimo 0.50 — sin tendencia clara este par destruye cuentas.\n"
            "- El filtro ATR es especialmente importante aquí."
        ),
        "session_start":  0,
        "session_end":    24,
        "sl_atr_mult":    3.0,
        "tp_atr_mult":    6.0,
        "be_atr_mult":    2.8,
        "rsi_oversold":   28,
        "rsi_overbought": 72,
        "min_confidence": 7,
        "min_hurst":      0.50,
        "sr_tolerance_pct": 0.20,
        "sr_lookback":    150,
        "sr_timeframes":  ["M15", "H1", "H4"],
        "tf_weights":     {"M15": 1, "H1": 3, "H4": 4},
        "atr_norm_factor":  0.35,
        "price_scale":    195.0,
        "news_topics":    "economy_monetary,forex,economy_macro",
        "memory_min_trades":      3,
        "memory_block_threshold": 0.85,
        "memory_warn_threshold":  0.75,
        "memory_decay_days":      60,
    },

    # ── 7. XAG/USD — 24/5 ──────────────────────────────────────
    _sym("XAGUSD"): {
        "name":           "Plata / Dólar (Silver)",
        "currencies":     ["USD"],
        "strategy_type":  "GOLD_BETA_REVERSION",
        "strategy_extra_rules": (
            "ESTRATEGIA GOLD_BETA_REVERSION — Plata (XAGUSDm):\n"
            "- Opera 24h siguiendo al oro. Cuando el oro tiene volatilidad, la plata también.\n"
            "- Sesión americana (13-21 UTC): máxima liquidez, spread más bajo.\n"
            "- Fisher < -2.5 con oro alcista → fuerte señal BUY en plata.\n"
            "- La plata tiene beta 1.5-3x del oro. Sus movimientos son más amplios."
        ),
        "session_start":  0,
        "session_end":    24,
        "sl_atr_mult":    2.5,
        "tp_atr_mult":    5.0,
        "be_atr_mult":    2.8,
        "rsi_oversold":   30,
        "rsi_overbought": 70,
        "min_confidence": 7,
        # FIX v6.4: Bajado de 0.38 a 0.35 — la plata estaba casi siempre bajo 0.38
        "min_hurst":      0.35,
        "sr_tolerance_pct": 0.35,
        "sr_lookback":    130,
        "sr_timeframes":  ["M15", "H1", "H4"],
        "tf_weights":     {"M15": 1, "H1": 2, "H4": 3},
        "atr_norm_factor":  0.60,
        "price_scale":    32.0,
        "news_topics":    "economy_monetary,economy_macro,finance",
        "memory_min_trades":      4,
        "memory_block_threshold": 0.88,
        "memory_warn_threshold":  0.78,
        "memory_decay_days":      40,
    },

    # ── 8. WTI CRUDE OIL — casi 24/5 ───────────────────────────
    _sym("USOIL"): {
        "name":           "Petróleo WTI (Crude Oil)",
        "currencies":     ["USD"],
        "strategy_type":  "RANGE_BREAKOUT_OIL",
        "strategy_extra_rules": (
            "ESTRATEGIA RANGE_BREAKOUT_OIL — Petróleo WTI (USOILm):\n"
            "- En Exness opera 23h (1h de cierre a las 23:00 UTC).\n"
            "- Mayor volatilidad: 13-18 UTC (apertura NY + overlap con Europa).\n"
            "- Miércoles 14:00-15:30 UTC: datos EIA inventarios → CALENDARIO pausará.\n"
            "- Volume Profile es CRÍTICO para identificar S/R reales.\n"
            "- Geopolítica (OPEC) puede dar movimiento brusco a cualquier hora."
        ),
        "session_start":  0,
        "session_end":    24,
        "sl_atr_mult":    2.0,
        "tp_atr_mult":    4.0,
        "be_atr_mult":    2.3,
        "rsi_oversold":   32,
        "rsi_overbought": 68,
        "min_confidence": 7,
        # FIX v6.4: Bajado de 0.42 a 0.38 — el petróleo era bloqueado continuamente
        "min_hurst":      0.38,
        "sr_tolerance_pct": 0.30,
        "sr_lookback":    120,
        "sr_timeframes":  ["M15", "H1", "H4"],
        "tf_weights":     {"M15": 1, "H1": 2, "H4": 3},
        "atr_norm_factor":  1.50,
        "price_scale":    75.0,
        "news_topics":    "economy_macro,energy,commodities",
        "memory_min_trades":      4,
        "memory_block_threshold": 0.88,
        "memory_warn_threshold":  0.78,
        "memory_decay_days":      30,
    },

    # ── 9. NASDAQ 100 — ampliado ────────────────────────────────
    _sym("USTEC"): {
        "name":           "Nasdaq 100",
        "currencies":     ["USD"],
        "strategy_type":  "TECH_MOMENTUM",
        "strategy_extra_rules": (
            "ESTRATEGIA TECH_MOMENTUM — Nasdaq 100 (USTEC):\n"
            "- En Exness como CFD opera casi 24h.\n"
            "- Pre-market americano (11-13 UTC) tiene movimiento real.\n"
            "- Sesión americana 13-21 UTC: máxima volatilidad y calidad de señal.\n"
            "- El filtro ATR descarta las horas sin movimiento suficiente automáticamente.\n"
            "- Fed, CPI, earnings mega-cap: el calendario pausará automáticamente.\n"
            "- Hurst en índices es naturalmente 0.35-0.45. Esto es normal y esperable."
        ),
        "session_start":  0,
        "session_end":    24,
        "sl_atr_mult":    1.8,
        "tp_atr_mult":    3.6,
        "be_atr_mult":    2.3,
        "rsi_oversold":   33,
        "rsi_overbought": 67,
        "min_confidence": 6,
        # FIX v6.4: Bajado de 0.45 a 0.38 — NAS100 tiene Hurst naturalmente bajo
        "min_hurst":      0.38,
        "sr_tolerance_pct": 0.20,
        "sr_lookback":    120,
        "sr_timeframes":  ["M5", "M15", "H1", "H4"],
        "tf_weights":     {"M5": 1, "M15": 2, "H1": 3, "H4": 4},
        "atr_norm_factor":  60.0,
        "price_scale":    21000.0,
        "news_topics":    "economy_monetary,economy_macro,technology,earnings",
        "memory_min_trades":      4,
        "memory_block_threshold": 0.88,
        "memory_warn_threshold":  0.78,
        "memory_decay_days":      30,
    },

    # ── 10. DAX 40 — ampliado ───────────────────────────────────
    _sym("DE40"): {
        "name":           "DAX 40 (Alemania)",
        "currencies":     ["EUR"],
        "strategy_type":  "FRANKFURT_BREAKOUT",
        "strategy_extra_rules": (
            "ESTRATEGIA FRANKFURT_BREAKOUT — DAX 40 (DE40):\n"
            "- En Exness opera casi 24h como CFD. El filtro ATR descarta horas sin movimiento.\n"
            "- MODO MADRUGADA (00-06 UTC): movimiento reducido, solo reversiones S/R muy claras.\n"
            "- MODO APERTURA (06-09 UTC): buscar ruptura del rango nocturno con volume profile.\n"
            "- MODO EUROPEO (09-17 UTC): seguimiento de tendencia estándar, máxima liquidez.\n"
            "- MODO AMERICANO (13-21 UTC): correlación con S&P.\n"
            "- BCE y datos alemanes pausarán automáticamente por el calendario.\n"
            "- Hurst en índices es naturalmente 0.35-0.45. Esto es normal y esperable."
        ),
        "session_start":  0,
        "session_end":    24,
        "sl_atr_mult":    2.0,
        "tp_atr_mult":    4.0,
        "be_atr_mult":    2.3,
        "rsi_oversold":   35,
        "rsi_overbought": 65,
        "min_confidence": 6,
        # FIX v6.4: Bajado de 0.42 a 0.38
        "min_hurst":      0.38,
        "sr_tolerance_pct": 0.20,
        "sr_lookback":    120,
        "sr_timeframes":  ["M5", "M15", "H1", "H4"],
        "tf_weights":     {"M5": 1, "M15": 2, "H1": 3, "H4": 4},
        "atr_norm_factor":  50.0,
        "price_scale":    22000.0,
        "news_topics":    "economy_monetary,economy_macro,forex",
        "memory_min_trades":      4,
        "memory_block_threshold": 0.88,
        "memory_warn_threshold":  0.78,
        "memory_decay_days":      30,
    },

    # ── 11. EUR/JPY — 24/5 ─────────────────────────────────────
    _sym("EURJPY"): {
        "name":           "Euro / Yen (Yuro)",
        "currencies":     ["EUR", "JPY"],
        "strategy_type":  "RISK_CARRY",
        "strategy_extra_rules": (
            "ESTRATEGIA RISK_CARRY — EURJPY / Yuro (EURJPYm):\n"
            "- Opera 24h. Sesión asiática (00-09 UTC) es MUY activa para EURJPY por Tokio.\n"
            "- Risk-ON global → EURJPY sube a cualquier hora. Risk-OFF → cae.\n"
            "- Hilbert cycle + Kalman slope en combinación dan las mejores señales.\n"
            "- ECB y BOJ: el calendario pausará automáticamente los eventos de ambos."
        ),
        "session_start":  0,
        "session_end":    24,
        "sl_atr_mult":    2.2,
        "tp_atr_mult":    4.4,
        "be_atr_mult":    2.2,
        "rsi_oversold":   32,
        "rsi_overbought": 68,
        "min_confidence": 6,
        "min_hurst":      0.40,
        "sr_tolerance_pct": 0.15,
        "sr_lookback":    130,
        "sr_timeframes":  ["M15", "H1", "H4"],
        "tf_weights":     {"M15": 1, "H1": 2, "H4": 3},
        "atr_norm_factor":  0.22,
        "price_scale":    165.0,
        "news_topics":    "economy_monetary,forex,economy_macro",
        "memory_min_trades":      4,
        "memory_block_threshold": 0.88,
        "memory_warn_threshold":  0.78,
        "memory_decay_days":      35,
    },

    # ── 12. BITCOIN — 24/7 ─────────────────────────────────────
    _sym("BTCUSD"): {
        "name":           "Bitcoin / Dólar",
        "currencies":     ["USD"],
        "strategy_type":  "CRYPTO_WAVE",
        "strategy_extra_rules": (
            "ESTRATEGIA CRYPTO_WAVE — Bitcoin (BTCUSDm):\n"
            "- Único activo que opera 7 días a la semana, 24 horas.\n"
            "- Fin de semana: puede haber buenas oportunidades, pero con mayor spread.\n"
            "- El filtro ATR (0.06% mínimo) descartará automáticamente los momentos dormidos.\n"
            "- Hurst > 0.46 requerido. Bitcoin aleatorio = trampa.\n"
            "- Fisher extremo (>3.0 o <-3.0) señala reversiones importantes de ciclo.\n"
            "- Mejor ventana: 14-22 UTC (Europa activa + Asia despertando)."
        ),
        "session_start":  0,
        "session_end":    24,
        "sl_atr_mult":    4.0,
        "tp_atr_mult":    8.0,
        "be_atr_mult":    3.3,
        "rsi_oversold":   28,
        "rsi_overbought": 72,
        "min_confidence": 7,
        "min_hurst":      0.46,
        "sr_tolerance_pct": 0.50,
        "sr_lookback":    150,
        "sr_timeframes":  ["M15", "H1", "H4", "D1"],
        "tf_weights":     {"M15": 1, "H1": 2, "H4": 3, "D1": 4},
        "atr_norm_factor":  2000.0,
        "price_scale":    85000.0,
        "news_topics":    "blockchain,technology,economy_monetary",
        "memory_min_trades":      3,
        "memory_block_threshold": 0.85,
        "memory_warn_threshold":  0.75,
        "memory_decay_days":      20,
    },
}

# ================================================================
#  GESTIÓN DE RIESGO GLOBAL
# ================================================================
RISK_PER_TRADE      = 0.01   # 1% del balance por trade
MAX_OPEN_TRADES     = 25     # máximo global simultáneo
MAX_OPEN_PER_SYMBOL = 1      # máximo 1 trade por símbolo
MAGIC_NUMBER        = 202606

# FIX v6.4: Breakeven basado en ATR, no en pips fijos
# be_atr_mult × ATR = distancia que debe moverse el precio antes de activar BE
# Valor por defecto usado si no está definido por símbolo
BREAKEVEN_ATR_MULT  = 1.5    # 1.0 = activar BE cuando precio se mueve 1×ATR en favor

# FIX v6.4: Cooldown entre trades del mismo símbolo
# Previene reabrir el mismo trade inmediatamente (300s = 5 minutos)
SYMBOL_COOLDOWN_SEC = 180

# ── Circuit Breaker individual de posición (Cisne Negro) ─────────
# Si el drawdown de una posición abierta supera este % del balance,
# se ejecuta un cierre forzado de emergencia independiente del SL/TP.
# 3.0% = cerrar si la posición pierde más del 3% del balance total.
# 0.0 = desactivado.
CIRCUIT_BREAKER_MAX_DRAWDOWN_PCT = float(
    os.environ.get("CIRCUIT_BREAKER_MAX_DRAWDOWN_PCT", "3.0")
)

# ── Modo Calentamiento (Cold-Start Mitigation) ───────────────────
# Mientras el bot tenga menos de WARMUP_TRADE_COUNT trades en memoria,
# opera en "Modo Calentamiento": lot reducido + filtros más estrictos.
# Configurable vía .env para ajuste sin tocar el código.
WARMUP_TRADE_COUNT  = int(float(os.environ.get("WARMUP_TRADE_COUNT",  "100")))
WARMUP_LOT_FACTOR   = float(os.environ.get("WARMUP_LOT_FACTOR", "0.5"))

# Tiempo de espera entre iteraciones del ciclo principal (segundos)
LOOP_SLEEP_SEC      = 30

MAX_DAILY_LOSS      = 0.05   # 5% pérdida máxima diaria

# ================================================================
#  PARÁMETROS DE INDICADORES (compartidos)
# ================================================================
RSI_PERIOD   = 14
MACD_FAST    = 12
MACD_SLOW    = 26
MACD_SIGNAL  = 9
DEMA_FAST    = 21
DEMA_SLOW    = 55
ATR_PERIOD   = 14
BB_PERIOD    = 20
BB_STD       = 2.0
STOCH_K      = 5
STOCH_D      = 3
CCI_PERIOD   = 20
VWAP_ENABLED = True
HA_FILTER    = True

HILBERT_ENABLED = True
HURST_ENABLED   = True
KALMAN_ENABLED  = True
FOURIER_ENABLED = True
FISHER_ENABLED  = True

# ================================================================
#  TIMEFRAMES
# ================================================================
TF_ENTRY = "M1"
TF_TREND = "H1"

# ================================================================
#  NOTICIAS
# ================================================================
NEWS_REFRESH_MIN          = 30
MIN_NEWS_IMPACT_TO_PAUSE  = 1
NEWS_PAUSE_MINUTES_BEFORE = 30
NEWS_PAUSE_MINUTES_AFTER  = 20

# ================================================================
#  CALENDARIO ECONÓMICO EN TIEMPO REAL
# ================================================================
CALENDAR_REFRESH_HOURS        = 4
CALENDAR_PAUSE_MINUTES_BEFORE = 1
CALENDAR_RESUME_MINUTES_AFTER = 20
CALENDAR_HIGH_IMPACT_ONLY     = True

# ================================================================
#  FILTRO DINÁMICO DE CALIDAD DE MERCADO
# ================================================================
MARKET_ATR_MIN_PCT_OVERRIDE = {}

# ================================================================
#  FASE 1 — TERCER PILAR: MICROESTRUCTURA
# ================================================================

# Volume Profile: número de velas históricas y resolución del histograma
MICROSTRUCTURE_VP_CANDLES    = 100   # Velas para construir el Volume Profile
MICROSTRUCTURE_VP_BINS       = 50    # Buckets de precio del histograma

# Fair Value Gaps: antigüedad máxima de FVGs considerados activos
MICROSTRUCTURE_FVG_CANDLES   = 50    # Velas de lookback para detectar FVGs
MICROSTRUCTURE_FVG_MAX_AGE   = 20    # FVGs más viejos que esto → ignorados

# Confluence Matrix: umbrales para el score ponderado de 3 pilares
# [-3, +3] — mayor número = requisito más estricto para operar
CONFLUENCE_MIN_SCORE         = 0.25  # Score mínimo absoluto para permitir entrada
# (0.0 = sin filtro, 0.5 = moderado, 1.0 = estricto sniper)
# Si el score total está entre -CONFLUENCE_MIN_SCORE y +CONFLUENCE_MIN_SCORE
# → el símbolo muestra "confluencia débil" pero aún se pregunta a Groq.

# ================================================================
#  FASE 2 — NEURAL BRAIN v3 + KELLY POSITION SIZING
# ================================================================

# Criterio de Kelly Fraccionado — para sizing dinámico basado en win rate
# El bot usa Kelly solo cuando tiene suficientes trades históricos por símbolo.
KELLY_FRACTION    = 0.25   # 25% del Kelly óptimo (cuarto de Kelly = conservador)
                            # 0.0 = desactivar Kelly (usar always fixed risk)
                            # 0.50 = half Kelly (más agresivo)
                            # 0.25 = quarter Kelly (recomendado, resiste errores de estimación)
KELLY_MIN_TRADES  = 100    # Mínimo de trades históricos (por símbolo) para activar Kelly
                            # Con menos trades, el intervalo de confianza del win_rate
                            # es demasiado amplio para que Kelly sea confiable

# Multiplicador del umbral duro de confluencia (pre-Groq + post-Groq gate).
# Umbral efectivo = CONFLUENCE_MIN_SCORE × CONFLUENCE_HARD_GATE_MULT
# Ejemplo con defaults: 0.3 × 2 = 0.6 sobre escala [-3, +3]
# Aumentar → más permisivo (solo bloquea señales muy contradictorias)
# Reducir  → más estricto (bloquea con menor contradicción de pilares)
CONFLUENCE_HARD_GATE_MULT = 2   # Recomendado: 2 (balance permisividad/seguridad)

# ================================================================
#  FASE 3 — SCORECARD JERÁRQUICO POR ACTIVO
# ================================================================
# Historial máximo (últimos trades cerrados por símbolo) para evaluar setup.
SCORECARD_LOOKBACK_TRADES = 300
# Muestra mínima (WIN+LOSS, BE excluido) para considerar estadística confiable.
SCORECARD_MIN_SAMPLE      = 6
# Win rate mínimo (%) exigido para permitir el setup (si hay muestra suficiente).
SCORECARD_MIN_WIN_RATE    = 50.0
# Endurecimiento dinámico: +1 punto de confianza mínima si scorecard es débil.
SCORECARD_MIN_CONF_BONUS  = 1

# ================================================================
#  FASE 4 — POLICY ENGINE (RANKING DE CANDIDATOS)
# ================================================================
# Ventana histórica para métricas de policy por activo/setup.
POLICY_LOOKBACK_TRADES    = 300
# Muestra mínima para considerar estable el policy score.
POLICY_MIN_SAMPLE         = 8
# Pesos del ranking (deben sumar ~1.0).
POLICY_WEIGHT_WR          = 0.40
POLICY_WEIGHT_PF          = 0.25
POLICY_WEIGHT_REWARD      = 0.20
POLICY_WEIGHT_SAMPLE      = 0.15
# Umbral de bloqueo duro cuando hay muestra suficiente.
POLICY_MIN_SCORE          = 0.40
# Endurecimiento de confianza si policy score es débil.
POLICY_MIN_CONF_BONUS     = 1

# ================================================================
#  FASE 5 — EQUITY GUARD (PROTECCIÓN DE CAPITAL)
# ================================================================
# Bloquea NUEVAS entradas cuando la equity cae por debajo de este
# porcentaje del balance actual. No afecta la gestión de posiciones
# ya abiertas (trailing/SL/TP siguen activos).
EQUITY_GUARD_MIN_PCT      = 70.0

# ================================================================
#  FASE 7 — PORTFOLIO RISK (CORRELACIÓN ENTRE ACTIVOS)
# ================================================================
# Riesgo máximo efectivo del portafolio (% del balance) considerando
# la correlación entre posiciones abiertas.
# Con RISK_PER_TRADE=1%, 5% limita ~5 posiciones no correlacionadas
# o ~3 posiciones altamente correlacionadas.
MAX_PORTFOLIO_RISK_PCT    = 5.0

# ================================================================
#  FASE 6 — DAILY LOSS GUARD (PROTECCIÓN INTRADÍA GLOBAL)
# ================================================================
# Reutiliza MAX_DAILY_LOSS como umbral de protección global.
# Cuando se supera la pérdida diaria permitida:
# - se pausan nuevas entradas en TODOS los símbolos
# - se mantiene la gestión de posiciones abiertas
# - se envía notificación Telegram con cooldown anti-spam

# ================================================================
#  FASE 9 — REAL VOLUME + COT POSITIONING
# ================================================================
# Volumen real de Dukascopy para pares forex (mejora Volume Profile)
REAL_VOLUME_ENABLED        = True   # False → siempre usar tick_volume
REAL_VOLUME_LOOKBACK_HOURS = 4      # Horas de historial a descargar
REAL_VOLUME_CACHE_TTL_MIN  = 15     # TTL del cache en memoria (minutos)

# CFTC Commitment of Traders (COT) — posicionamiento institucional
COT_ENABLED         = True    # False → no descargar COT
# El reporte se actualiza semanalmente (viernes); 24 h de TTL permite
# detectar cada nuevo reporte sin saturar el servidor de la CFTC.
COT_CACHE_TTL_HOURS = 24      # TTL del cache en horas


# ================================================================
#  WEB DASHBOARD
# ================================================================
WEB_DASHBOARD_HOST = "127.0.0.1"
WEB_DASHBOARD_PORT = 8765

# ================================================================
#  FASE 10 — DASHBOARD AVANZADO (visualizaciones gráficas)
# ================================================================
# Balance inicial usado para reconstruir la equity curve en el
# dashboard (no afecta al bot en vivo — solo lectura de SQLite).
DASHBOARD_EQUITY_INITIAL      = 10000.0   # Balance inicial de la equity curve
# Ventana deslizante para el cálculo del Rolling Win Rate.
DASHBOARD_ROLLING_WR_WINDOW   = 20        # Últimos N trades para Rolling WR
# Número de trades recientes mostrados en la tabla del dashboard.
DASHBOARD_RECENT_TRADES_LIMIT = 50        # Últimos N trades en la tabla
# Intervalo de refresco de los gráficos Chart.js (más lento que el status
# principal porque las queries son más pesadas).
DASHBOARD_CHART_REFRESH_SEC   = 30        # Refresh de gráficos (segundos)

# ================================================================
#  FASE 8 — BACKTESTING HISTÓRICO
# ================================================================
# Balance inicial para la simulación de trades en el backtester.
# No afecta al bot en vivo — solo se usa en run_backtest.py y
# modules/backtester.py para calcular métricas relativas al capital.
BACKTEST_INITIAL_BALANCE = 10000.0

# Rango de fechas por defecto cuando se ejecuta sin --start / --end.
BACKTEST_DEFAULT_START = "2023-01-01"
BACKTEST_DEFAULT_END   = "2025-12-31"

# Walk-Forward Testing: parámetros de la ventana deslizante.
# Lógica: [train_months] de datos IS → evaluar [test_months] OOS →
# avanzar [step_months] → repetir hasta cubrir el período completo.
# Un step_months pequeño (1) genera más ventanas y mayor robustez
# estadística, pero tarda más en correr.
BACKTEST_WALK_FORWARD_TRAIN_MONTHS = 6   # Meses de ventana in-sample
BACKTEST_WALK_FORWARD_TEST_MONTHS  = 2   # Meses de ventana out-of-sample
BACKTEST_WALK_FORWARD_STEP_MONTHS  = 1   # Avance por iteración

# ================================================================
#  PRESUPUESTO GROQ (CONTROL DURO DE COSTO)
# ================================================================
# Configurado vía variables de entorno GROQ_MAX_CALLS_PER_HOUR y
# GROQ_MAX_CALLS_PER_DAY (ver sección GROQ al inicio de este archivo).
# 0 = desactivado. Si se define >0, el bot no hará más llamadas una vez
# alcanzado el límite y usará fallback HOLD para evitar gasto excedente.


# ================================================================
#  FASE 11 — EXTERNAL DATA PROVIDERS
# ================================================================
# Twelve Data (gratuito: 800 calls/día, 8 calls/min)
TWELVE_DATA_KEY              = os.environ.get("TWELVE_DATA_KEY", "")
TWELVE_DATA_ENABLED          = bool(TWELVE_DATA_KEY)
TWELVE_DATA_CACHE_TTL_MIN    = 5
TWELVE_DATA_MAX_CALLS_PER_MIN = 8

# Polygon.io (gratuito: 5 req/min)
POLYGON_KEY                  = os.environ.get("POLYGON_KEY", "")
POLYGON_ENABLED              = bool(POLYGON_KEY)
POLYGON_CACHE_TTL_MIN        = 5
POLYGON_MAX_CALLS_PER_MIN    = 5

# TrueFX (datos locales, sin API)
TRUEFX_DATA_DIR              = os.path.join(os.path.dirname(__file__), "data", "truefx")
TRUEFX_ENABLED               = os.path.isdir(TRUEFX_DATA_DIR)

# ================================================================
#  FASE 12 — ADF STATIONARITY + Z-SCORE FILTER
# ================================================================
ADF_ENABLED              = True    # Activar test ADF como complemento de Hurst
ADF_PVALUE_THRESHOLD     = 0.05    # p-value máximo para considerar estacionario
ZSCORE_LOOKBACK          = 50      # Ventana de lookback para Z-score
ZSCORE_ENTRY_THRESHOLD   = 1.5     # Z-score mínimo para señal de reversal
HURST_GREY_ZONE_LOW      = 0.42    # Límite inferior de zona gris de Hurst
HURST_GREY_ZONE_HIGH     = 0.58    # Límite superior de zona gris de Hurst
RANDOM_WALK_PENALTY      = 0.70    # Factor de penalización en confluencia para random walk
ZSCORE_CONFLUENCE_BONUS  = 0.5     # Bonus máximo al score por Z-score alignment
