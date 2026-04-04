"""
╔══════════════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v7 — config.py  (v7.0 — SCALPING PURO 5 SÍMBOLOS) ║
║                                                                          ║
║   CAMBIOS v6.8:                                                         ║
║   • TP asimétrico por dirección: tp_atr_mult_buy / tp_atr_mult_sell   ║
║   • BUY → TP largo | SELL → TP corto (ratio ~0.6× del BUY)            ║
║   • calc_sl_tp en risk_manager.py actualizado con fallback             ║
║     retrocompatible a tp_atr_mult si los nuevos campos no existen      ║
║                                                                          ║
║   CAMBIOS v6.7:                                                         ║
║   • Servidor MT5 cambiado a ICMarketsSC-MT5-2 (default)               ║
║   • ALPHA_VANTAGE_KEY y FINNHUB_KEY ahora opcionales (no bloquean)    ║
║   • US30 añadido a _NO_SUFFIX (índice sin sufijo de broker)            ║
║   • 8 nuevos símbolos TIER 1 IC Markets (13-20):                      ║
║     AUDUSD, USDCAD, USDCHF, NZDUSD, EURGBP, AUDJPY, ETHUSD, US30    ║
║   • Total: 12 → 20 activos                                             ║
║                                                                          ║
║   CAMBIOS v6.6:                                                         ║
║   • FIX CRÍTICO: Warmup ya no bloquea RANGING/MIXED/VOLATILE          ║
║   • should_block inicializado correctamente (evita UnboundLocalError)  ║
║   • XAGUSD memory_min_trades: 4→8, block_threshold: 0.88→0.92        ║
║   • EURUSD/XTIUSD memory_min_trades subido para estrategias rango     ║
║   • US500/USTEC/DE40 memory_min_trades: 4→6 (índices en MIXED)       ║
║   • WARMUP_TRADE_COUNT default: 100→50 (menos bloqueos en arranque)  ║
║                                                                          ║
║   CAMBIOS v6.5:                                                         ║
║   • FASE 0: Credenciales migradas a .env (python-dotenv)               ║
║   • BREAKEVEN_ATR_MULT: BE por ATR en vez de pips fijos                ║
║   • SYMBOL_COOLDOWN_SEC: cooldown entre trades del mismo símbolo       ║
║   • US500 min_hurst: 0.45→0.38 (índices operan con Hurst bajo)       ║
║   • XAGUSD min_hurst: 0.38→0.35 (casi siempre bajo el umbral)        ║
║   • USOIL min_hurst: 0.42→0.38                                        ║
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


def _split_env_csv(name: str) -> list:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _collect_numbered_env(base_name: str) -> list:
    items = []
    prefix = f"{base_name}_"
    for env_name, raw_value in os.environ.items():
        if not env_name.startswith(prefix):
            continue
        suffix = env_name[len(prefix):]
        if not suffix.isdigit():
            continue
        value = str(raw_value).strip()
        if value:
            items.append((int(suffix), value))
    return [value for _, value in sorted(items)]


# ================================================================
#  MT5 / BROKER
# ================================================================
MT5_LOGIN    = int(_require_env("MT5_LOGIN"))
MT5_PASSWORD = _require_env("MT5_PASSWORD")
MT5_SERVER   = os.environ.get("MT5_SERVER", "ICMarketsSC-MT5-2")

# Sufijo de símbolo del broker.  Exness usa "m" (XAUUSDm, EURUSDm…).
# IC Markets usa "" vacío (XAUUSD, EURUSD…).  Cambia en el .env para migrar.
# Nota: USTEC y DE40 en Exness no llevan sufijo; mantenlos en _NO_SUFFIX.
BROKER_SUFFIX = os.environ.get("BROKER_SUFFIX", "")

# Símbolos que NO llevan el sufijo en ningún broker (por convención del feed).
_NO_SUFFIX = {"USTEC", "DE40", "US30"}

# ================================================================
#  MOTOR DE DECISIÓN DETERMINISTA (sin LLM)
# ================================================================
# FIX: 120→45s — cooldown de decisión más agresivo para scalping M1 sniper.
DECISION_SYMBOL_COOLDOWN_SEC = int(os.environ.get("DECISION_SYMBOL_COOLDOWN_SEC", "45") or 45)
DECISION_MIN_ENTRY_QUALITY   = int(os.environ.get("DECISION_MIN_ENTRY_QUALITY", "3") or 3)
DECISION_ENTRY_STRONG_ONLY   = _env_flag("DECISION_ENTRY_STRONG_ONLY", True)
DECISION_ENTRY_CONF_MULT     = float(os.environ.get("DECISION_ENTRY_CONF_MULT", "2.0") or 2.0)
ENTRY_MIN_RR             = float(os.environ.get("ENTRY_MIN_RR", "1.50") or 1.50)
# Cuando es True, todos los trades se tratan como scalping (breakeven por pips, TP reducido).
SCALPING_ONLY            = _env_flag("SCALPING_ONLY", True)
SCALPING_ALLOW_LOW_HURST = _env_flag("SCALPING_ALLOW_LOW_HURST", True)
# FIX: 0.18→0.25 — Hurst < 0.25 es prácticamente random walk; no operar.
SCALPING_HURST_HARD_FLOOR = float(os.environ.get("SCALPING_HURST_HARD_FLOOR", "0.25") or 0.25)
SCALPING_HURST_SOFT_MARGIN = float(os.environ.get("SCALPING_HURST_SOFT_MARGIN", "0.20") or 0.20)
# FIX v7.0: 0.82→0.98 — SELL con TP_MULT=0.82 destruía el R:R en casi todos los SELL.
# Ejemplo con EURUSD SELL (sl=1.5×ATR, tp_sell=3.0×ATR):
#   R:R base = 3.0/1.5 = 2.00 → tras mult 0.82: 2.00×0.82=1.64 (pasa)
#   Pero con el antiguo tp_atr_mult_sell=1.8 y ENTRY_MIN_RR=1.20:
#   R:R base = 1.8/1.5 = 1.20 → tras mult 0.82: 1.20×0.82=0.984 < 1.20 → BLOQUEADO
# Con 0.98 y TP simétrico el R:R efectivo es 1.96, superando holgadamente el mín 1.50.
SCALPING_TP_MULT = float(os.environ.get("SCALPING_TP_MULT", "0.98") or 0.98)
# FIX v7.0: etapas de trail escaladas — 2→5 pips mínimos antes de mover SL.
# Evita que el trail se active en ruido de mercado (spread + 1 pip) y cierre
# ganancias de $0.10 en vez de $5+.
SCALPING_BE_PIPS_STAGE_1 = float(os.environ.get("SCALPING_BE_PIPS_STAGE_1",  "5.0") or  5.0)
SCALPING_BE_PIPS_STAGE_2 = float(os.environ.get("SCALPING_BE_PIPS_STAGE_2",  "8.0") or  8.0)
SCALPING_BE_PIPS_STAGE_3 = float(os.environ.get("SCALPING_BE_PIPS_STAGE_3", "12.0") or 12.0)
SCALPING_BE_PIPS_STAGE_4 = float(os.environ.get("SCALPING_BE_PIPS_STAGE_4", "18.0") or 18.0)
SCALPING_BE_MIN_PIPS     = float(os.environ.get("SCALPING_BE_MIN_PIPS",      "5.0") or  5.0)
# Ganancia mínima esperada en USD para abrir una operación.
# Si el lot size + TP calculado no pueden generar al menos este importe, se descarta.
# Esto evita trades de $0.50 que no justifican el riesgo ni el spread.
# FIX: 5.0→2.0 — umbral más bajo para capturar más oportunidades sniper rápidas.
# Con $2 el lot×TP sigue justificando el riesgo en cuentas desde $500.
MIN_EXPECTED_PROFIT_USD  = float(os.environ.get("MIN_EXPECTED_PROFIT_USD", "2.0") or 2.0)
# ── SCALPING: SL/TP proporcionales al ATR del TF de entrada ──
# Cuando SCALPING_ONLY=True, se usa atr_entry (ATR M1/M5) en vez de ATR H1
# para calcular SL y TP. Esto hace que los stops sean proporcionales al
# movimiento real del timeframe en que se opera.
SCALPING_SL_ATR_MULT = float(os.environ.get("SCALPING_SL_ATR_MULT", "3.0") or 3.0)
SCALPING_TP_ATR_MULT = float(os.environ.get("SCALPING_TP_ATR_MULT", "6.0") or 6.0)
CLOSURE_HISTORY_LOOKBACK_DAYS = int(os.environ.get("CLOSURE_HISTORY_LOOKBACK_DAYS", "30") or 30)

# ================================================================
#  TELEGRAM
# ================================================================
TELEGRAM_TOKEN   = _require_env("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = _require_env("TELEGRAM_CHAT_ID")

# ================================================================
#  APIs EXTERNAS
# ================================================================
ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_KEY", "")
FINNHUB_KEY       = os.environ.get("FINNHUB_KEY", "")


def _sym(base: str) -> str:
    """Retorna el nombre completo del símbolo según el broker configurado."""
    if base in _NO_SUFFIX:
        return base
    return f"{base}{BROKER_SUFFIX}"


# ================================================================
#  SÍMBOLOS — 5 ACTIVOS DE ALTA LIQUIDEZ, SCALPING PURO 24/5
#
#  FIX v7.0: Reducido de 20 a 5 símbolos para concentrar capital y
#  señales. TP simétrico (tp_atr_mult_buy == tp_atr_mult_sell) para
#  eliminar el sesgo BUY que generaba el TP asimétrico previo.
#  R:R mínimo ≥ 1.67 en todos los pares (con SCALPING_TP_MULT=0.98).
# ================================================================
SYMBOLS = {

    # ── 1. ORO — 24/5, mejor scalping activo ─────────────────────
    _sym("XAUUSD"): {
        "name":           "Oro (Gold)",
        "currencies":     ["USD"],
        "strategy_type":  "VOLATILITY_CYCLE",
        "strategy_extra_rules": (
            "ESTRATEGIA SCALPING VOLATILITY_CYCLE — ORO (XAUUSD):\n"
            "- Opera 24/5. Modo SCALPING PURO en M1. Objetivo: $5-30 USD/trade.\n"
            "- Sesión americana (13-21 UTC): máxima volatilidad — mejores scalps.\n"
            "- LOCAL_MIN en Hilbert M1 = señal BUY de alta probabilidad.\n"
            "- LOCAL_MAX en Hilbert M1 = señal SELL de alta probabilidad.\n"
            "- Fisher < -2.0 con precio en soporte → BUY scalp fuerte.\n"
            "- Fisher > +2.0 con precio en resistencia → SELL scalp fuerte.\n"
            "- TP SIMÉTRICO: misma distancia en BUY y SELL (sin sesgo de dirección).\n"
            "- HOLD si Kalman H1 + SuperTrend H1 ambos contradicen la dirección.\n"
            "- HOLD si noticias de alto impacto en próximos 30 min.\n"
            "- NUNCA abrir contra tendencia fuerte sin señal Hilbert extrema."
        ),
        "session_start":  0,
        "session_end":    24,
        "max_spread_pips": 5.0,    # Oro: spread natural más alto
        "session_quality": {       # Factor de calidad por sesión (0.0-1.0)
            "asian":  0.6,         # 0-7 UTC
            "london": 1.0,         # 7-13 UTC
            "ny":     1.0,         # 13-21 UTC
            "dead":   0.3,         # 21-24 UTC
        },
        "sl_atr_mult":    2.0,
        "tp_atr_mult_buy":  3.5,
        "tp_atr_mult_sell": 3.5,   # TP simétrico — R:R = 1.75
        "be_atr_mult":    2.0,
        "rsi_oversold":   30,
        "rsi_overbought": 70,
        "min_confidence": 6,
        "min_decision_score": 4.5,
        "min_hurst":      0.35,
        "sr_tolerance_pct": 0.40,
        "sr_lookback":    100,
        "sr_timeframes":  ["M5", "M15", "H1"],
        "tf_weights":     {"M5": 1, "M15": 2, "H1": 3},
        "atr_norm_factor":  30.0,
        "price_scale":    2500.0,
        "news_topics":    "economy_monetary,economy_macro,finance,forex",
        "memory_min_trades":      5,
        "memory_block_threshold": 0.88,
        "memory_warn_threshold":  0.78,
        "memory_decay_days":      30,
    },

    # ── 2. EUR/USD — 24/5, par más líquido del mundo ─────────────
    _sym("EURUSD"): {
        "name":           "Euro / Dólar",
        "currencies":     ["EUR", "USD"],
        "strategy_type":  "CYCLE_REVERSION",
        "strategy_extra_rules": (
            "ESTRATEGIA SCALPING CYCLE_REVERSION — EURUSD:\n"
            "- Opera 24/5. Modo SCALPING PURO en M1. Objetivo: $5-20 USD/trade.\n"
            "- Sesión europea (07-17 UTC): mayor liquidez y precisión de señales.\n"
            "- Reversiones desde S/R con confirmación de ciclo Hilbert extremo.\n"
            "- LOCAL_MIN en soporte S/R → BUY scalp óptimo.\n"
            "- LOCAL_MAX en resistencia S/R → SELL scalp óptimo.\n"
            "- Fisher extremo (>2.0 o <-2.0): señal de reversión muy confiable.\n"
            "- TP SIMÉTRICO — R:R = 2.0, igualmente válido para BUY y SELL.\n"
            "- ECB y Fed: el calendario pausará automáticamente."
        ),
        "session_start":  0,
        "session_end":    24,
        "max_spread_pips": 2.0,    # EUR/USD: par más líquido, spread bajo
        "session_quality": {       # Factor de calidad por sesión (0.0-1.0)
            "asian":  0.3,         # 0-7 UTC
            "london": 1.0,         # 7-13 UTC
            "ny":     0.8,         # 13-21 UTC
            "dead":   0.2,         # 21-24 UTC
        },
        "sl_atr_mult":    1.5,
        "tp_atr_mult_buy":  3.0,
        "tp_atr_mult_sell": 3.0,   # TP simétrico — R:R = 2.0
        "be_atr_mult":    1.8,
        "rsi_oversold":   30,
        "rsi_overbought": 70,
        "min_confidence": 6,
        "min_decision_score": 4.0,
        "min_hurst":      0.35,
        "sr_tolerance_pct": 0.10,
        "sr_lookback":    100,
        "sr_timeframes":  ["M5", "M15", "H1"],
        "tf_weights":     {"M5": 1, "M15": 2, "H1": 3},
        "atr_norm_factor":  0.0015,
        "price_scale":    1.10,
        "news_topics":    "economy_monetary,forex,economy_macro",
        "memory_min_trades":      5,
        "memory_block_threshold": 0.88,
        "memory_warn_threshold":  0.78,
        "memory_decay_days":      25,
    },

    # ── 3. GBP/USD — 24/5, alta volatilidad para scalping ────────
    _sym("GBPUSD"): {
        "name":           "Libra / Dólar (Cable)",
        "currencies":     ["GBP", "USD"],
        "strategy_type":  "MOMENTUM_SURGE",
        "strategy_extra_rules": (
            "ESTRATEGIA SCALPING MOMENTUM_SURGE — GBPUSD (Cable):\n"
            "- Opera 24/5. Modo SCALPING PURO en M1. Objetivo: $5-30 USD/trade.\n"
            "- Mejor hora: 7-12 UTC (Frankfurt-Londres apertura). El Cable se mueve 3+ pips/min.\n"
            "- MACD acelerando + HA 2+ velas consecutivas + Kalman alineado = entrada óptima.\n"
            "- Fisher extremo (>2.0 o <-2.0) es señal de reversión muy confiable en GBP.\n"
            "- TP SIMÉTRICO — R:R = 1.75, válido para BUY y SELL por igual.\n"
            "- BOE y UK CPI: el calendario pausará automáticamente.\n"
            "- HOLD si spread > 2.5 pips."
        ),
        "session_start":  0,
        "session_end":    24,
        "max_spread_pips": 3.0,    # GBP/USD: spreads moderados en Cable
        "session_quality": {       # Factor de calidad por sesión (0.0-1.0)
            "asian":  0.2,         # 0-7 UTC
            "london": 1.0,         # 7-13 UTC
            "ny":     0.7,         # 13-21 UTC
            "dead":   0.2,         # 21-24 UTC
        },
        "sl_atr_mult":    2.0,
        "tp_atr_mult_buy":  3.5,
        "tp_atr_mult_sell": 3.5,   # TP simétrico — R:R = 1.75
        "be_atr_mult":    2.0,
        "rsi_oversold":   30,
        "rsi_overbought": 70,
        "min_confidence": 6,
        "min_decision_score": 4.0,
        "min_hurst":      0.35,
        "sr_tolerance_pct": 0.12,
        "sr_lookback":    100,
        "sr_timeframes":  ["M5", "M15", "H1"],
        "tf_weights":     {"M5": 1, "M15": 2, "H1": 3},
        "atr_norm_factor":  0.0015,
        "price_scale":    1.27,
        "news_topics":    "economy_monetary,forex,economy_macro",
        "memory_min_trades":      5,
        "memory_block_threshold": 0.88,
        "memory_warn_threshold":  0.78,
        "memory_decay_days":      25,
    },

    # ── 4. S&P 500 — sesión americana, alta volatilidad ──────────
    _sym("US500"): {
        "name":           "S&P 500",
        "currencies":     ["USD"],
        "strategy_type":  "MOMENTUM_TREND",
        "strategy_extra_rules": (
            "ESTRATEGIA SCALPING MOMENTUM_TREND — S&P 500 (US500):\n"
            "- Opera casi 24h como CFD. Modo SCALPING PURO en M1. Objetivo: $5-40 USD/trade.\n"
            "- Sesión americana (13-21 UTC): máxima calidad de señal — priorizar esta ventana.\n"
            "- Pre-market (11-13 UTC): movimiento real, señales válidas con cautela extra.\n"
            "- El filtro ATR bloqueará automáticamente las horas sin movimiento suficiente.\n"
            "- MACD + SuperTrend + Kalman — los 3 deben confirmar para entrar.\n"
            "- Hurst en índices es naturalmente 0.35-0.45 — normal y esperable.\n"
            "- TP SIMÉTRICO — R:R = 1.67, BUY y SELL igualmente válidos.\n"
            "- NFP/CPI/FOMC: el calendario pausará automáticamente."
        ),
        "session_start":  0,
        "session_end":    24,
        "max_spread_pips": 4.0,    # S&P 500: CFD índice, spread variable
        "session_quality": {       # Factor de calidad por sesión (0.0-1.0)
            "asian":  0.3,         # 0-7 UTC
            "london": 0.5,         # 7-13 UTC
            "ny":     1.0,         # 13-21 UTC
            "dead":   0.2,         # 21-24 UTC
        },
        "sl_atr_mult":    1.8,
        "tp_atr_mult_buy":  3.0,
        "tp_atr_mult_sell": 3.0,   # TP simétrico — R:R = 1.67
        "be_atr_mult":    2.0,
        "rsi_oversold":   35,
        "rsi_overbought": 65,
        "min_confidence": 6,
        "min_decision_score": 4.5,
        "min_hurst":      0.35,
        "sr_tolerance_pct": 0.25,
        "sr_lookback":    100,
        "sr_timeframes":  ["M5", "M15", "H1"],
        "tf_weights":     {"M5": 1, "M15": 2, "H1": 3},
        "atr_norm_factor":  20.0,
        "price_scale":    5000.0,
        "news_topics":    "economy_monetary,economy_macro,finance,earnings",
        "memory_min_trades":      5,
        "memory_block_threshold": 0.86,
        "memory_warn_threshold":  0.76,
        "memory_decay_days":      25,
    },

    # ── 5. BITCOIN — 24/7, alta volatilidad ─────────────────────
    _sym("BTCUSD"): {
        "name":           "Bitcoin / Dólar",
        "currencies":     ["USD"],
        "strategy_type":  "CRYPTO_WAVE",
        "strategy_extra_rules": (
            "ESTRATEGIA SCALPING CRYPTO_WAVE — Bitcoin (BTCUSD):\n"
            "- Opera 7 días / 24 horas. Modo SCALPING PURO en M1. Objetivo: $10-60 USD/trade.\n"
            "- Mayor volatilidad: 14-22 UTC (Europa activa + Asia despertando).\n"
            "- ATR M1 de BTC muy alto — usar lot sizes pequeños (0.01-0.05).\n"
            "- Fisher extremo (>3.0 o <-3.0): reversiones de ciclo importantes.\n"
            "- Hilbert LOCAL_MIN/LOCAL_MAX son señales muy confiables en BTC.\n"
            "- TP SIMÉTRICO — R:R = 1.67, BUY y SELL igualmente válidos.\n"
            "- Hurst > 0.40 requerido. BTC aleatorio sin tendencia = trampa.\n"
            "- Fin de semana: oportunidades válidas pero spread mayor — usar cautela."
        ),
        "session_start":  0,
        "session_end":    24,
        "max_spread_pips": 8.0,    # BTC: crypto, spread alto es normal
        "session_quality": {       # Factor de calidad por sesión (0.0-1.0)
            "asian":  0.7,         # 0-7 UTC
            "london": 0.8,         # 7-13 UTC
            "ny":     1.0,         # 13-21 UTC
            "dead":   0.6,         # 21-24 UTC
        },
        "sl_atr_mult":    3.0,
        "tp_atr_mult_buy":  5.0,
        "tp_atr_mult_sell": 5.0,   # TP simétrico — R:R = 1.67
        "be_atr_mult":    3.0,
        "rsi_oversold":   28,
        "rsi_overbought": 72,
        "min_confidence": 7,
        "min_decision_score": 5.0,
        "min_hurst":      0.40,
        "sr_tolerance_pct": 0.50,
        "sr_lookback":    100,
        "sr_timeframes":  ["M5", "M15", "H1"],
        "tf_weights":     {"M5": 1, "M15": 2, "H1": 3},
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
RISK_PER_TRADE      = 0.02   # 2% del balance por trade (v7.0: 1%→2% para target $5 mínimo)
MAX_OPEN_TRADES     = 5      # máximo global — 1 por símbolo × 5 símbolos
MAX_OPEN_PER_SYMBOL = int(os.environ.get("MAX_OPEN_PER_SYMBOL", "1") or 1)
MAGIC_NUMBER        = 202606

# FIX v6.4: Breakeven basado en ATR, no en pips fijos
# be_atr_mult × ATR = distancia que debe moverse el precio antes de activar BE
# Valor por defecto usado si no está definido por símbolo
BREAKEVEN_ATR_MULT  = 1.5    # 1.0 = activar BE cuando precio se mueve 1×ATR en favor

# FIX v6.4: Cooldown entre trades del mismo símbolo
# Previene reabrir el mismo trade inmediatamente (300s = 5 minutos)
# FIX v7.0: Cooldown reducido de 180s a 30s para scalping M1 sniper agresivo
SYMBOL_COOLDOWN_SEC = int(os.environ.get("SYMBOL_COOLDOWN_SEC", "30") or 30)

# Cooldown específico para avisos de "bloqueo por memoria" en Telegram.
# Evita repetir el mismo warning una y otra vez cuando el símbolo sigue
# en warmup o en un régimen adverso durante varias horas.
MEMORY_BLOCK_NOTIFY_COOLDOWN_SEC = int(
    os.environ.get("MEMORY_BLOCK_NOTIFY_COOLDOWN_SEC", "14400")
)

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
# FIX v6.6: Reducido de 100 a 50. Con 100 trades de warmup, el bot bloqueaba
# demasiado tiempo en todos los símbolos que no sean tendencia pura.
# 50 trades es suficiente para tener una muestra estadística inicial.
# Configurable vía .env: WARMUP_TRADE_COUNT=50
WARMUP_TRADE_COUNT  = int(float(os.environ.get("WARMUP_TRADE_COUNT",  "50")))
WARMUP_LOT_FACTOR   = float(os.environ.get("WARMUP_LOT_FACTOR", "0.5"))

# Tiempo de espera entre iteraciones del ciclo principal (segundos)
# FIX: 10→5s — ciclo más rápido para entradas sniper M1
LOOP_SLEEP_SEC      = int(os.environ.get("LOOP_SLEEP_SEC", "5") or 5)

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
TF_TREND = "M15"

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
CONFLUENCE_MIN_SCORE         = 0.50  # Score mínimo absoluto para permitir entrada
# (0.0 = sin filtro, 0.5 = moderado, 1.0 = estricto sniper)
# Si el score total está entre -CONFLUENCE_MIN_SCORE y +CONFLUENCE_MIN_SCORE
# → el símbolo muestra "confluencia débil" pero aún puede ser evaluado por el motor.

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

# Multiplicador del umbral duro de confluencia (pre-gate + post-gate).
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
