"""
╔══════════════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — main.py  (v6.5 — TRAILING STOP + WR FIX)     ║
║                                                                          ║
║   FIXES v6.5 (sobre v6.4):                                             ║
║   ✅ FIX 11 — Trailing stop progresivo (no BE inmediato)               ║
║       • Etapa 1 (0–1.5×ATR):   SL intacto — dejar respirar             ║
║       • Etapa 2 (1.5–2.5×ATR): SL→breakeven + buffer 0.1×ATR          ║
║       • Etapa 3 (2.5–4×ATR):   SL trail a 1.5×ATR del precio actual   ║
║       • Etapa 4 (>4×ATR):      SL trail a 1.0×ATR del precio actual   ║
║       • Etapa 5 (>80% hacia TP): SL trail a 0.5×ATR (máxima captura)  ║
║   ✅ FIX 12 — WR = wins/(wins+losses) — BE excluido del cálculo        ║
║   ✅ FIX 13 — BE no cuenta como trade en WR ni en EOD                  ║
║   ✅ FIX 14 — Filtro anti-contra-tendencia: bloquear cuando 4+         ║
║               indicadores primarios contradicen la dirección           ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import sys
import time
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd
import requests

try:
    import MetaTrader5 as mt5
except ImportError:
    print("❌  pip install MetaTrader5"); sys.exit(1)

try:
    from google import genai
except ImportError:
    print("❌  pip install google-genai"); sys.exit(1)

import config as cfg
from modules.indicators        import compute_all
from modules.sr_zones          import build_sr_context, sr_for_prompt
from modules.news_engine       import build_news_context, format_news_for_prompt
from modules.economic_calendar import calendar as eco_calendar
from modules.neural_brain import (
    init_db, save_trade, update_trade_result,
    build_features, check_memory,
    get_learning_report, get_pending_trades, get_memory_stats,
    derive_setup_id, derive_session_from_ind, evaluate_scorecard,
    evaluate_policy,
)
from modules.risk_manager import (
    get_lot_size, get_lot_size_kelly, calc_sl_tp, is_rr_valid,
    is_session_valid, is_daily_loss_ok, get_rr,
    is_market_tradeable,
)
from modules.telegram_notifier import (
    notify_bot_started, notify_trade_opened, notify_breakeven,
    notify_near_tp, notify_trade_closed, notify_news_pause,
    notify_memory_block, notify_daily_summary, notify_error,
    notify_eod_analysis,
    _send as telegram_send,
)
from modules import dashboard

# ── Logging ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("zar_v6.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Gemini ───────────────────────────────────────────────────────
gemini_client = genai.Client(api_key=cfg.GEMINI_API_KEY)

# ── TF Map MT5 ───────────────────────────────────────────────────
TF_MAP = {
    "M1":  mt5.TIMEFRAME_M1,  "M5":  mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15, "H1":  mt5.TIMEFRAME_H1,
    "H4":  mt5.TIMEFRAME_H4,  "D1":  mt5.TIMEFRAME_D1,
}
TF_CANDLES = {
    "M1": 300, "M5": 250, "M15": 200,
    "H1": 150, "H4": 100, "D1":  60,
}

# ════════════════════════════════════════════════════════════════
#  ESTADO GLOBAL
# ════════════════════════════════════════════════════════════════

tickets_en_memoria:  set   = set()
news_cache:          dict  = {}
news_last_update:    dict  = {}
daily_start_balance: float = 0.0
daily_pnl:           float = 0.0
trades_today:        int   = 0
wins_today:          int   = 0
losses_today:        int   = 0
be_today:            int   = 0
last_summary_date:   str   = ""
last_eod_date:       str   = ""
cycle_count:         int   = 0
last_action:         str   = ""
ind_cache:           dict  = {}
sr_cache:            dict  = {}
symbol_status_cache: dict  = {}

news_pause_notified:   dict = {}
memory_block_notified: dict = {}
NOTIF_COOLDOWN_SEC = 1800

# Cooldown por símbolo
last_trade_time: dict = {}
SYMBOL_COOLDOWN_SEC = getattr(cfg, "SYMBOL_COOLDOWN_SEC", 300)

# Registro diario de trades para EOD
daily_trades_log: list = []


def _set_symbol_status(symbol: str, status: str):
    global symbol_status_cache
    symbol_status_cache[symbol] = status

# ════════════════════════════════════════════════════════════════
#  MT5 — CONEXIÓN Y DATOS
# ════════════════════════════════════════════════════════════════

def conectar_mt5() -> bool:
    if not mt5.initialize():
        log.error("[MT5] initialize() falló"); return False
    ok = mt5.login(cfg.MT5_LOGIN, password=cfg.MT5_PASSWORD, server=cfg.MT5_SERVER)
    if not ok:
        log.error(f"[MT5] login falló: {mt5.last_error()}"); return False
    info = mt5.account_info()
    log.info(f"[MT5] Conectado — Balance: ${info.balance:,.2f} | Server: {cfg.MT5_SERVER}")
    return True


def get_candles(symbol: str, tf: str, n: int = 200) -> Optional[pd.DataFrame]:
    if not mt5.symbol_select(symbol, True):
        log.debug(f"[MT5] symbol_select falló para {symbol}")
    rates = mt5.copy_rates_from_pos(symbol, TF_MAP[tf], 0, n)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df.rename(columns={"tick_volume": "volume"})


def get_account_info():
    info = mt5.account_info()
    if info is None:
        return None, None
    return float(info.balance), float(info.equity)


def get_open_positions():
    positions = mt5.positions_get()
    if positions is None:
        return []
    return [p._asdict() for p in positions if p.magic == cfg.MAGIC_NUMBER]


def get_open_positions_count_realtime() -> int:
    positions = mt5.positions_get()
    if positions is None:
        return 0
    return sum(1 for p in positions if p.magic == cfg.MAGIC_NUMBER)


# ════════════════════════════════════════════════════════════════
#  GEMINI — CON RETRY EXPONENCIAL
# ════════════════════════════════════════════════════════════════

SYSTEM_PROMPT_BASE = """
Eres ZAR — un algoritmo de trading institucional de precisión matemática.
Tu única función es analizar el contexto de mercado y decidir: BUY, SELL o HOLD.
Responde SIEMPRE con JSON exacto, nada más.

REGLA 1 — TENDENCIA (7 niveles — lee con cuidado):
  ALCISTA_FUERTE   → BUY fuertemente favorecido. Solo SELL si hay señal extrema contraria.
  ALCISTA          → BUY es la dirección correcta. Confirmar con S/R o momentum.
  LATERAL_ALCISTA  → Señales mayoritariamente alcistas. BUY desde soporte fuerte o con 3+ confirmaciones.
  LATERAL          → Sin dirección clara. BUY/SELL SOLO si estás EN soporte/resistencia fuerte (strength>5).
                     Si no hay S/R fuerte cercano → HOLD.
  LATERAL_BAJISTA  → Señales mayoritariamente bajistas. SELL desde resistencia fuerte o con 3+ confirmaciones.
  BAJISTA          → SELL es la dirección correcta. Confirmar con S/R o momentum.
  BAJISTA_FUERTE   → SELL fuertemente favorecido. Solo BUY si hay señal extrema contraria.

  trend_votes muestra cuántos de 6 indicadores votan alcista (bull) vs bajista (bear).

REGLA 2 — ZONAS S/R FUERTES:
  En soporte (strength>5): BUY si hay confirmación de momentum
  En resistencia (strength>5): SELL si hay confirmación de momentum
  in_strong_zone=True: zona de alta probabilidad — aumenta confianza

REGLA 3 — CONFLUENCIA MÍNIMA (al menos 4 de 6 — AUMENTADO a 4):
  ✓ DEMA cross o posición (fast>slow = alcista)
  ✓ MACD histograma en dirección correcta
  ✓ RSI no en zona opuesta extrema
  ✓ Heiken Ashi en dirección correcta
  ✓ SuperTrend en dirección correcta
  ✓ Kalman trend en dirección correcta

  IMPORTANTE: Si SuperTrend y Kalman CONTRADICEN la dirección → HOLD obligatorio.
  Ambos deben confirmar la dirección del trade.

REGLA 4 — RSI EXTREMOS:
  RSI > overbought → NO BUY. RSI < oversold → NO SELL.
  Excepción: RSI extremo en S/R fuerte puede confirmar divergencia.

REGLA 5 — NOTICIAS Y CALENDARIO:
  Si should_pause=True → HOLD siempre. Sin excepción.

REGLA 6 — MEMORIA NEURAL:
  Si should_block=True → HOLD.
  confidence_adj > 0 → patrón ganador similar.
  confidence_adj < 0 → patrón perdedor similar.

REGLA 7 — R:R MÍNIMO:
  R:R < 1.5 → HOLD. R:R 1.5-2.0 = aceptable. R:R > 2.5 = excelente.

REGLA 8 — HILBERT TRANSFORM:
  LOCAL_MAX (sine > 0.85) → NO BUY. Techo del ciclo.
  LOCAL_MIN (sine < -0.85) → NO SELL. Suelo del ciclo.
  BUY_CYCLE → Favorece BUY.
  SELL_CYCLE → Favorece SELL.

REGLA 9 — FISHER TRANSFORM:
  Fisher > +2.0 → sobrecomprado → precaución BUY.
  Fisher < -2.0 → sobrevendido → precaución SELL.

REGLA 10 — HURST EXPONENT:
  Hurst > 0.6 → tendencia persistente → seguir agresivamente.
  Hurst 0.45-0.6 → mixto → requerir S/R adicional.
  Hurst < 0.45 → reversión → usar S/R como criterio principal.

REGLA 11 — CICLO ADAPTATIVO:
  cycle_phase = TECHO → favorece SELL.
  cycle_phase = SUELO → favorece BUY.
  cycle_phase = TRANSICION → esperar confirmación.

REGLA 12 — REGRESIÓN LINEAL:
  lr_r2 > 0.7 → tendencia estadísticamente robusta.
  lr_r2 < 0.4 → usar S/R, no la tendencia lineal.

REGLA 13 — ANTI-CONTRA-TENDENCIA (NUEVO — CRÍTICO):
  Si la dirección propuesta CONTRADICE a SuperTrend + Kalman simultáneamente → HOLD.
  Si la dirección propuesta CONTRADICE a 4 o más indicadores primarios → HOLD.
  El bot tuvo el 97% de trades en breakeven por entrar contra la tendencia.
  Solo operar cuando la dirección está ALINEADA con la mayoría de indicadores.

REGLA 14 — PILAR 3: MICROESTRUCTURA (NUEVO — FASE 1):
  Volume Profile (POC/VAH/VAL basado en tick-volume de Exness):
    Precio > POC → sesgo alcista. Precio < POC → sesgo bajista.
    Precio > VAH → breakout bullish (fuerte). Precio < VAL → breakdown bearish (fuerte).
    Precio dentro del Value Area (VAL–VAH) → zona de equilibrio, menor edge.
  Session VWAP (VWAP anclado por sesión UTC):
    Precio sobre S-VWAP → sesgo alcista. Precio bajo S-VWAP → sesgo bajista.
    Desviación S-VWAP > 0.5 % → posible reversión a VWAP antes del TP.
  Fair Value Gaps (FVG / Imbalances):
    FVG Bullish activo (fvg_bull): precio entrando en el gap desde arriba → SOPORTE. Favorece BUY.
    FVG Bearish activo (fvg_bear): precio llegando al gap desde abajo → RESISTENCIA. Favorece SELL.
    FVG mitigado: ya NO es soporte/resistencia válido.

REGLA 15 — CONFLUENCIA DE 3 PILARES (SNIPER — NUEVO — FASE 1):
  El bot evalúa 3 pilares independientes para cada señal:
    P1 (Estadístico): votos de tendencia de indicadores técnicos.
    P2 (Matemático):  Hilbert + Hurst + Kalman + Fisher + Ciclo Adaptativo.
    P3 (Microestructura): POC + Session VWAP + FVG (calculado arriba).
  confluence.total en [-3, +3]:
    >= +1.0 → BULLISH fuerte. <= -1.0 → BEARISH fuerte. Entre → neutral/débil.
  confluence.sniper_aligned = True → los 3 pilares apuntan en la MISMA dirección.
    → Alta probabilidad de éxito. Aumentar confianza en 1 punto.
  confluence.sniper_aligned = False → pilares en desacuerdo.
    → Si conf.total < 0.5, reducir confianza. Si conf.total < 0.0 en la dirección → HOLD.
  NUNCA ejecutar BUY si confluence.total < -0.5 (todos los pilares en contra).
  NUNCA ejecutar SELL si confluence.total > 0.5 (todos los pilares en contra).

FORMATO (JSON exacto, sin markdown, sin texto extra):
{
  "decision": "BUY" | "SELL" | "HOLD",
  "confidence": 1-10,
  "reason": "explicación concisa en español (máx 150 palabras)",
  "key_signals": ["señal1", "señal2", "señal3"],
  "main_risk": "principal riesgo de este trade"
}
"""


def get_system_prompt(sym_cfg: dict) -> str:
    extra         = sym_cfg.get("strategy_extra_rules", "")
    strategy_type = sym_cfg.get("strategy_type", "HYBRID")
    if not extra:
        return SYSTEM_PROMPT_BASE
    return SYSTEM_PROMPT_BASE + (
        f"\n\n{'='*60}\n"
        f"ESTRATEGIA ESPECÍFICA — {strategy_type}:\n"
        f"{'='*60}\n"
        f"{extra}\n"
        f"{'='*60}\n"
        f"Aplica estas reglas ADICIONALES junto con las reglas base.\n"
    )


def ask_gemini(context: str, sym_cfg: dict) -> Optional[dict]:
    system_prompt = get_system_prompt(sym_cfg)
    max_attempts  = 3
    wait_secs     = [0, 5, 15]

    for attempt in range(max_attempts):
        if wait_secs[attempt] > 0:
            log.info(f"[gemini] Reintentando en {wait_secs[attempt]}s (intento {attempt+1}/{max_attempts})...")
            time.sleep(wait_secs[attempt])
        try:
            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    {"role": "user",  "parts": [{"text": system_prompt}]},
                    {"role": "model", "parts": [{"text": "Entendido. Analizaré y responderé en JSON exacto."}]},
                    {"role": "user",  "parts": [{"text": context}]},
                ],
            )
            raw = response.text.strip()
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].strip()
            return json.loads(raw)

        except json.JSONDecodeError as e:
            log.error(f"[gemini] JSON inválido (intento {attempt+1}): {e}")
            return None

        except Exception as e:
            err_str = str(e)
            if "503" in err_str or "UNAVAILABLE" in err_str:
                if attempt < max_attempts - 1:
                    log.warning(f"[gemini] 503 UNAVAILABLE — reintentando...")
                    continue
            elif "404" in err_str or "NOT_FOUND" in err_str:
                log.error(f"[gemini] Modelo no disponible: {e}")
                return None
            log.error(f"[gemini] Error (intento {attempt+1}): {e}")
            if attempt == max_attempts - 1:
                return None
    return None


def build_context(
    symbol: str, ind: dict, sr_ctx, news_ctx,
    mem_check, sym_cfg: dict,
    cal_events: list = None,
    scorecard_check = None,
    policy_check = None,
) -> str:
    price   = ind.get("price", 0)
    hilbert = ind.get("hilbert", {})
    fourier = ind.get("fourier", {})
    hurst   = ind.get("hurst", 0.5)
    votes   = ind.get("trend_votes", {"bull": 0, "bear": 0})

    lines = [
        f"=== ANÁLISIS {symbol} — {sym_cfg.get('name', symbol)} ===",
        f"Estrategia activa: {sym_cfg.get('strategy_type', '?')}",
        f"Hora UTC actual:   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}",
        f"Precio actual:     {price}",
        f"Tendencia H1:      {ind.get('h1_trend', '?')}  "
        f"(votos: bull={votes.get('bull',0)}/6  bear={votes.get('bear',0)}/6)",
        "",
        "── ALGORITMOS MATEMÁTICOS AVANZADOS ──",
        f"HILBERT TRANSFORM:",
        f"  Señal:          {hilbert.get('signal', '?')}",
        f"  Descripción:    {hilbert.get('description', '?')}",
        f"  Seno (sine):    {hilbert.get('sine', 0):.4f}",
        f"  Lead sine (+45°): {hilbert.get('lead_sine', 0):.4f}",
        f"  Fase:           {hilbert.get('phase', 0):.1f}°",
        f"  Período ciclo:  {hilbert.get('period', 0):.1f} velas",
        f"  Fuerza ciclo:   {hilbert.get('strength', 0):.3f}",
        f"HURST: {hurst:.3f} → {ind.get('hurst_regime','?')} | min={sym_cfg.get('min_hurst',0.5):.2f}",
        f"KALMAN: precio={ind.get('kalman_price',0):.4f} | tend={ind.get('kalman_trend','?')} | slope={ind.get('kalman_slope',0):.5f}",
        f"FISHER: {ind.get('fisher',0):.3f} | señal={ind.get('fisher_signal',0):.3f} | cruce={ind.get('fisher_cross','?')}",
        f"FOURIER: período={fourier.get('dominant_period','?')}v | fuerza={fourier.get('strength',0):.3f}",
        f"CICLO: osc={ind.get('cycle_osc',0):.3f} | fase={ind.get('cycle_phase','?')}",
        f"LR: slope={ind.get('lr_slope',0):.6f} | R²={ind.get('lr_r2',0):.3f} | tend={ind.get('lr_trend','?')}",
        "",
        "── INDICADORES TÉCNICOS ──",
        f"RSI: {ind.get('rsi',0):.1f} (OS={sym_cfg.get('rsi_oversold',30)} / OB={sym_cfg.get('rsi_overbought',70)}) | Div: {ind.get('rsi_div','NONE')}",
        f"MACD hist: {ind.get('macd_hist',0):.4f} | Dir: {ind.get('macd_dir','?')}",
        f"Stoch K={ind.get('stoch_k',0):.1f} D={ind.get('stoch_d',0):.1f}",
        f"CCI: {ind.get('cci',0):.1f} | Williams %R: {ind.get('williams',0):.1f}",
        f"ATR: {ind.get('atr',0):.4f} ({ind.get('atr_pct',0):.2f}%)",
        f"BB: {ind.get('bb_pos','?')} | Squeeze: {ind.get('bb_squeeze',False)} | KC: {ind.get('kc_squeeze',False)}",
        f"DEMA cross: {ind.get('dema_cross','?')} | SuperTrend: {'ALCISTA' if ind.get('supertrend',0)==1 else 'BAJISTA'}",
        f"Heiken Ashi: {ind.get('ha_trend','?')} ({ind.get('ha_streak',0)} velas)",
        f"VWAP: {ind.get('vwap',0):.4f} | Precio {'sobre' if price > ind.get('vwap',price) else 'bajo'} VWAP",
        f"OBV: {ind.get('obv_trend','?')} | CMF: {ind.get('cmf',0):.4f} | MFI: {ind.get('mfi',0):.1f}",
        f"SAR: {'alcista' if ind.get('sar_trend',0)==1 else 'bajista'} @ {ind.get('sar_value',0):.4f}",
        f"Ichimoku: {'sobre nube' if ind.get('ichi_above_cloud',False) else 'bajo nube'}",
        f"Vela: {ind.get('candle_pattern','NONE')} | Momentum: {ind.get('momentum',0):.4f}",
        "",
        "── SOPORTE / RESISTENCIA ──",
        sr_for_prompt(sr_ctx),
        "",
        "── CALENDARIO ECONÓMICO ──",
    ]

    if cal_events:
        for ev in cal_events[:5]:
            lines.append(
                f"  {'🚨' if ev.minutes_until() <= 1 else '📅'} "
                f"[{ev.currency}] {ev.title} {ev.time_label()} | {ev.impact}"
                + (f" | Forecast: {ev.forecast}" if ev.forecast else "")
            )
    else:
        lines.append("  ✅ Sin eventos de alto impacto próximos (2h)")

    lines += [
        "",
        "── MEMORIA NEURAL ──",
        f"Bloquear: {mem_check.should_block} | Ajuste: {mem_check.confidence_adj:+.1f}",
        f"Pérdidas similares: {mem_check.similar_losses} | Wins: {mem_check.similar_wins}",
        f"Detalle: {mem_check.warning_msg}",
        "",
        "── SCORECARD JERÁRQUICO ──",
    ]
    if scorecard_check is not None:
        lines += [
            f"Setup: {scorecard_check.setup_id}",
            f"Sesión: {scorecard_check.session} | Régimen: {scorecard_check.regime}",
            f"Nivel: {scorecard_check.level} | Sample: {scorecard_check.sample_size}",
            f"WR: {scorecard_check.win_rate:.1f}% | Mín WR: {scorecard_check.min_win_rate:.1f}%",
            f"Bloquear setup: {scorecard_check.should_block} | {scorecard_check.reason}",
            "",
        ]
    else:
        lines += [
            "Sin scorecard disponible",
            "",
        ]

    lines += [
        "── POLICY ENGINE ──",
    ]
    if policy_check is not None:
        lines += [
            f"Dirección candidata: {policy_check.direction}",
            f"Policy score: {policy_check.policy_score:.3f} | Block: {policy_check.should_block}",
            f"WR: {policy_check.win_rate:.1f}% | PF: {policy_check.profit_factor:.2f} | "
            f"AvgR: {policy_check.avg_reward:+.3f} | n={policy_check.sample_size}",
            f"Detalle: {policy_check.reason}",
            "",
        ]
    else:
        lines += [
            "Sin policy check disponible",
            "",
        ]

    lines += [
        "── NOTICIAS RSS ──",
        format_news_for_prompt(news_ctx),
        "",
    ]

    # ── PILAR 3: MICROESTRUCTURA ─────────────────────────────────
    micro = ind.get("microstructure", {})
    conf  = ind.get("confluence", {})

    fvg_bull = micro.get("fvg_bull")
    fvg_bear = micro.get("fvg_bear")
    sess_vwap_dev = micro.get("session_vwap_dev", 0.0)
    above_svwap   = micro.get("above_session_vwap", True)

    lines += [
        "── PILAR 3: MICROESTRUCTURA ──",
        f"Volume Profile:  POC={micro.get('poc',0):.4f} | VAH={micro.get('vah',0):.4f} | VAL={micro.get('val',0):.4f}",
        f"  Precio {'SOBRE' if micro.get('above_poc') else 'BAJO'} POC | "
        f"{'DENTRO' if micro.get('in_value_area') else ('SOBRE VAH' if price > micro.get('vah', price) else 'BAJO VAL')} del Value Area",
        f"Session VWAP ({micro.get('session','?')}): {micro.get('session_vwap',0):.4f} | "
        f"Precio {'sobre' if above_svwap else 'bajo'} S-VWAP ({sess_vwap_dev:+.2f}%)",
        f"FVG Bullish: " + (
            f"activo en {fvg_bull['low']:.4f}–{fvg_bull['high']:.4f} (hace {fvg_bull['age']} velas)"
            if fvg_bull else "ninguno activo cercano"
        ),
        f"FVG Bearish: " + (
            f"activo en {fvg_bear['low']:.4f}–{fvg_bear['high']:.4f} (hace {fvg_bear['age']} velas)"
            if fvg_bear else "ninguno activo cercano"
        ),
        f"Micro Score: {micro.get('micro_score', 0):+.1f} | Sesgo: {micro.get('micro_bias','NEUTRAL')}",
        f"  Detalle: {micro.get('description','')}",
        "",
        "── CONFLUENCIA 3 PILARES ──",
        f"P1 (Estadístico):  {conf.get('p1_score', 0):+.2f}",
        f"P2 (Matemático):   {conf.get('p2_score', 0):+.2f}",
        f"P3 (Microestr.):   {conf.get('p3_score', 0):+.2f}",
        f"TOTAL (ponderado): {conf.get('total', 0):+.2f} → {conf.get('bias','NEUTRAL')}",
        f"Sniper aligned:    {'✅ SÍ — todos los pilares alineados' if conf.get('sniper_aligned') else '⚠️  NO — pilares en desacuerdo'}",
        "",
        "── PARÁMETROS ──",
        f"SL={sym_cfg.get('sl_atr_mult','?')}×ATR | TP={sym_cfg.get('tp_atr_mult','?')}×ATR",
        f"Min Hurst: {sym_cfg.get('min_hurst',0.5):.2f} | Min confianza: {sym_cfg.get('min_confidence',6)}",
        f"Estrategia: {sym_cfg.get('strategy_type','HYBRID')}",
    ]
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
#  ÓRDENES MT5
# ════════════════════════════════════════════════════════════════

def open_order(symbol: str, direction: str, sl: float, tp: float, volume: float) -> Optional[int]:
    action  = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    tick    = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    price   = tick.ask if direction == "BUY" else tick.bid
    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       volume,
        "type":         action,
        "price":        price,
        "sl":           sl,
        "tp":           tp,
        "deviation":    20,
        "magic":        cfg.MAGIC_NUMBER,
        "comment":      "ZAR v6",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"[orden] ✅ #{result.order} {direction} {symbol} vol={volume}")
        return result.order
    log.error(f"[orden] ❌ {result.comment} (retcode={result.retcode})")
    return None


def move_sl(ticket: int, new_sl: float) -> bool:
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        return False
    r = mt5.order_send({
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "sl":       new_sl,
        "tp":       pos[0].tp,
    })
    return r.retcode == mt5.TRADE_RETCODE_DONE


# ════════════════════════════════════════════════════════════════
#  FIX 6: CÁLCULO DE PIPS POR INSTRUMENTO
# ════════════════════════════════════════════════════════════════

def _calc_pips_instrument(deal, symbol: str) -> float:
    profit = float(deal.profit)
    volume = float(deal.volume)
    if volume <= 0:
        return 0.0
    sym_info = mt5.symbol_info(symbol)
    if sym_info is not None:
        tick_val = float(getattr(sym_info, 'trade_tick_value', 0) or 0)
        tick_sz  = float(getattr(sym_info, 'trade_tick_size',  0) or 0)
        if tick_val > 0 and tick_sz > 0:
            pip_size = tick_sz * 10
            pip_val  = tick_val * 10 * volume
            if pip_val > 0:
                return round(profit / pip_val, 1)
    s = symbol.upper()
    if "XAU" in s or "GOLD" in s or "BTC" in s or "ETH" in s:
        pip_value = volume * 100
    elif "XAG" in s or "OIL" in s or "WTI" in s:
        pip_value = volume * 50
    elif "500" in s or "NAS" in s or "GER" in s or "DAX" in s:
        pip_value = volume * 10
    else:
        pip_value = volume * 10
    return round(profit / pip_value, 1) if pip_value != 0 else 0.0


# ════════════════════════════════════════════════════════════════
#  FIX 11 — TRAILING STOP PROGRESIVO (reemplaza _check_breakeven)
# ════════════════════════════════════════════════════════════════

# Estado del trailing por ticket: qué etapa alcanzó
_trail_stage: dict = {}  # ticket → int (0=sin activar, 1=BE, 2=trail1.5, 3=trail1.0, 4=tight)


def _manage_trailing_stop(pos: dict, sym_cfg: dict):
    """
    FIX 11 — Trailing stop progresivo en 5 etapas:

    ETAPA 0 (0 – 1.5×ATR mov favorable):
        NO tocar el SL. Dejar que el precio respire.
        El error anterior era activar BE demasiado pronto (1.0×ATR),
        lo que causaba que el ruido normal cerrara el 97% de trades en BE.

    ETAPA 1 (1.5 – 2.5×ATR mov favorable):
        Mover SL a breakeven + buffer de 0.1×ATR.
        El capital queda protegido pero con margen para respirar.

    ETAPA 2 (2.5 – 4×ATR mov favorable):
        Trail dinámico: SL se mueve a 1.5×ATR del precio actual.
        Nunca retrocede. Lockea ~1×ATR de ganancia.

    ETAPA 3 (>4×ATR mov favorable):
        Trail más ajustado: SL a 1.0×ATR del precio actual.
        Lockea ~3×ATR de ganancia.

    ETAPA 4 (>80% del camino hacia TP):
        Trail muy ajustado: SL a 0.5×ATR del precio actual.
        Maximiza captura cuando estamos cerca del TP.

    NUNCA retrocede el SL (solo se mueve en dirección favorable).
    NUNCA mueve el SL por debajo del precio de apertura (siempre en ganancia).
    """
    global last_action, symbol_status_cache

    ticket    = pos["ticket"]
    symbol    = pos["symbol"]
    direction = "BUY" if pos["type"] == 0 else "SELL"
    open_p    = pos["price_open"]
    cur_p     = pos["price_current"]
    sl        = pos["sl"]
    tp        = pos["tp"]

    # Obtener ATR del cache de indicadores
    ind     = ind_cache.get(symbol, {})
    atr_val = ind.get("atr", 0)

    if atr_val <= 0:
        return  # Sin ATR disponible, no actuar

    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        return
    point = sym_info.point or 0.00001

    atr_pts = atr_val / point

    # ── Calcular movimiento favorable en puntos ──
    if direction == "BUY":
        move_pts   = (cur_p - open_p) / point
        tp_total   = (tp - open_p) / point  if tp > open_p else 0
        tp_remaining = (tp - cur_p) / point if tp > cur_p  else 0
    else:
        move_pts   = (open_p - cur_p) / point
        tp_total   = (open_p - tp) / point  if tp < open_p else 0
        tp_remaining = (cur_p - tp) / point if tp < cur_p  else 0

    # Si el precio está en pérdida o exactamente en entrada → no hacer nada
    if move_pts <= 0:
        return

    # Calcular progreso hacia TP (0.0 = entrada, 1.0 = TP)
    tp_progress = 1.0 - (tp_remaining / max(tp_total, 1))
    tp_progress = max(0.0, min(1.0, tp_progress))

    # ── be_atr_mult del símbolo: cuánto necesita moverse para activar BE ──
    # FIX: ahora el mínimo es 1.5 (no 1.0). Configurado en config.py por símbolo.
    be_atr_mult = sym_cfg.get("be_atr_mult", getattr(cfg, "BREAKEVEN_ATR_MULT", 1.5))
    be_threshold_pts = be_atr_mult * atr_pts

    current_stage = _trail_stage.get(ticket, 0)

    # ── ETAPA 0: Demasiado pronto — no tocar ──
    if move_pts < be_threshold_pts:
        return

    # ── Determinar nueva etapa y trail distance ──
    new_sl = None
    new_stage = current_stage

    if tp_progress >= 0.80:
        # ETAPA 4: Cerca del TP — trail muy ajustado (0.5×ATR)
        trail_pts = 0.5 * atr_pts
        new_stage = 4
        if direction == "BUY":
            new_sl = cur_p - trail_pts * point
        else:
            new_sl = cur_p + trail_pts * point

    elif move_pts >= 4.0 * atr_pts:
        # ETAPA 3: >4×ATR de ganancia — trail 1.0×ATR
        trail_pts = 1.0 * atr_pts
        new_stage = 3
        if direction == "BUY":
            new_sl = cur_p - trail_pts * point
        else:
            new_sl = cur_p + trail_pts * point

    elif move_pts >= 2.5 * atr_pts:
        # ETAPA 2: >2.5×ATR — trail 1.5×ATR
        trail_pts = 1.5 * atr_pts
        new_stage = 2
        if direction == "BUY":
            new_sl = cur_p - trail_pts * point
        else:
            new_sl = cur_p + trail_pts * point

    else:
        # ETAPA 1: Activar breakeven (abierto tras 1.5×ATR)
        # Mover SL a precio de apertura + pequeño buffer (0.1×ATR)
        buffer_pts = max(1.0, atr_pts * 0.10)
        new_stage  = 1
        if direction == "BUY":
            new_sl = open_p + buffer_pts * point
        else:
            new_sl = open_p - buffer_pts * point

    if new_sl is None:
        return

    new_sl = round(new_sl, 5)

    # ── Regla crítica: NUNCA retroceder el SL ──
    if direction == "BUY":
        if new_sl <= sl:
            return  # Nuevo SL está peor o igual → no mover
        # Nunca mover por debajo de la apertura en etapas >= 2
        if new_stage >= 2:
            min_sl = open_p + (atr_pts * 0.05) * point
            new_sl = max(new_sl, min_sl)
    else:
        if new_sl >= sl:
            return  # Nuevo SL está peor o igual → no mover
        # Nunca mover por encima de la apertura en etapas >= 2
        if new_stage >= 2:
            max_sl = open_p - (atr_pts * 0.05) * point
            new_sl = min(new_sl, max_sl)

    # ── Verificar que sigue siendo diferente al SL actual ──
    diff_pts = abs(new_sl - sl) / point
    if diff_pts < 0.5:
        return  # Diferencia mínima insignificante

    # ── Mover SL ──
    if move_sl(ticket, new_sl):
        prev_stage = _trail_stage.get(ticket, 0)
        _trail_stage[ticket] = new_stage

        profit_pips = move_pts
        stage_names = {
            1: "BE activado",
            2: "Trail 1.5×ATR",
            3: "Trail 1.0×ATR",
            4: "Trail 0.5×ATR (TP cercano)",
        }
        stage_label = stage_names.get(new_stage, f"Etapa {new_stage}")
        locked_pts  = (new_sl - open_p) / point if direction == "BUY" else (open_p - new_sl) / point

        if new_stage == 1 and prev_stage < 1:
            # Primera vez que se activa BE — notificar
            notify_breakeven(symbol, ticket, new_sl, profit_pips)
            log.info(
                f"[Trail] 🛡 #{ticket} {symbol} {direction} "
                f"— {stage_label} | mov={move_pts:.0f}pts | SL→{new_sl:.5f}"
            )
        else:
            # Trailing update — log sin notificación Telegram
            log.info(
                f"[Trail] 📈 #{ticket} {symbol} {direction} "
                f"— {stage_label} | mov={move_pts:.0f}pts | "
                f"lock={locked_pts:.0f}pts | SL→{new_sl:.5f} | TP%={tp_progress:.0%}"
            )

        last_action = f"Trail {stage_label} #{ticket} {symbol} lock={locked_pts:.0f}pts"


# ════════════════════════════════════════════════════════════════
#  FIX 14 — FILTRO ANTI-CONTRA-TENDENCIA
# ════════════════════════════════════════════════════════════════

def _passes_direction_filter(action: str, ind: dict) -> tuple:
    """
    FIX 14: Bloquea trades cuando los indicadores primarios contradicen la dirección.

    Análisis del DB muestra el patrón de pérdida más frecuente:
    - XAUUSDm BUY con supertrend=-1 y kalman=BAJISTA → reversion
    - GBPUSDm SELL con kalman=ALCISTA y ha=ALCISTA → contratendencia

    Regla: Si Kalman + SuperTrend AMBOS contradicen la dirección → HOLD forzado.
    """
    kalman_trend = ind.get("kalman_trend", "NEUTRAL")
    supertrend   = ind.get("supertrend", 0)
    ha_trend     = ind.get("ha_trend", "NEUTRAL")
    macd_dir     = ind.get("macd_dir", "NEUTRAL")

    if action == "BUY":
        kalman_ok     = kalman_trend == "ALCISTA"
        supertrend_ok = supertrend == 1
        ha_ok         = ha_trend == "ALCISTA"
        macd_ok       = macd_dir == "ALCISTA"
    else:  # SELL
        kalman_ok     = kalman_trend == "BAJISTA"
        supertrend_ok = supertrend == -1
        ha_ok         = ha_trend == "BAJISTA"
        macd_ok       = macd_dir == "BAJISTA"

    # BLOQUEO DURO: Si Kalman Y SuperTrend ambos contradicen → bloquear
    if not kalman_ok and not supertrend_ok:
        return False, f"Kalman+SuperTrend contradicen {action} → HOLD forzado"

    # Contar indicadores primarios que contradicen
    contradictions = sum([
        not kalman_ok,
        not supertrend_ok,
        not ha_ok,
        not macd_ok,
    ])

    if contradictions >= 3:
        return False, f"{contradictions}/4 indicadores primarios contradicen {action} → HOLD"

    return True, ""


# ════════════════════════════════════════════════════════════════
#  GESTIÓN DE POSICIONES
# ════════════════════════════════════════════════════════════════

# be_activated: set de tickets que ya pasaron al menos a etapa 1 de trail
be_activated: set = set()
tp_alerted:   set = set()


def manage_positions(positions: list, ind_by_sym: dict):
    global last_action
    for pos in positions:
        ticket = pos["ticket"]
        symbol = pos["symbol"]
        cur_p  = pos["price_current"]
        tp     = pos["tp"]
        profit = pos["profit"]
        sym_cfg_data = cfg.SYMBOLS.get(symbol, {})

        # FIX 11: trailing stop progresivo (reemplaza _check_breakeven)
        _manage_trailing_stop(pos, sym_cfg_data)

        # Marcar tickets que están en trail
        if _trail_stage.get(ticket, 0) >= 1:
            be_activated.add(ticket)

        # Alerta TP cercano (15% restante del camino)
        if ticket not in tp_alerted and tp != 0:
            open_p  = pos["price_open"]
            dist_tp = abs(tp - cur_p)
            total   = abs(tp - open_p)
            if total > 0 and dist_tp / total <= 0.15:
                notify_near_tp(symbol, ticket, cur_p, tp, profit)
                tp_alerted.add(ticket)
                last_action = f"TP cercano #{ticket} {symbol} ${profit:.2f}"


def watch_closures(open_tickets_before: set, open_positions_now: list):
    """
    Detecta posiciones cerradas y registra el resultado.

    FIX 12/13: Win Rate = wins/(wins+losses). BE NO cuenta en WR.
    El WR solo mide trades con resultado claro. BE se muestra separado.
    """
    global daily_pnl, trades_today, wins_today, losses_today, be_today, last_action

    current_tickets = {p["ticket"] for p in open_positions_now}
    closed          = open_tickets_before - current_tickets
    if not closed:
        return

    from_time = datetime.now(timezone.utc) - timedelta(hours=24)
    history   = mt5.history_deals_get(from_time, datetime.now(timezone.utc))
    if history is None:
        log.warning("[closures] No se pudo obtener historial de deals de MT5")
        return

    closing_deals: dict = {}
    for d in history:
        if d.entry == 1:
            pid = int(d.position_id)
            if pid not in closing_deals or d.time > closing_deals[pid].time:
                closing_deals[pid] = d

    for ticket in closed:
        deal = closing_deals.get(ticket)

        if deal is None:
            for d in history:
                if int(d.position_id) == ticket and d.entry == 1:
                    deal = d
                    break

        if deal is None:
            log.warning(f"[closures] No se encontró deal de cierre para ticket #{ticket}")
            update_trade_result(
                ticket=ticket, close_price=0.0,
                profit=0.0, pips=0.0,
                result="UNKNOWN", duration_min=0,
            )
            continue

        profit   = (float(deal.profit)
                    + float(getattr(deal, 'commission', 0) or 0)
                    + float(getattr(deal, 'swap', 0) or 0))
        symbol   = deal.symbol
        pips     = _calc_pips_instrument(deal, symbol)
        close_px = float(deal.price)
        direction = "BUY" if deal.type == 0 else "SELL"

        # ── FIX 12: clasificación con umbral más sensato ──
        # BE solo si el profit es absolutamente mínimo (spread/comisión)
        if profit > 1.0:
            result = "WIN"
        elif profit < -1.0:
            result = "LOSS"
        else:
            result = "BE"  # Ganancia/pérdida menor a $1 = breakeven

        pending       = get_pending_trades()
        trade_info    = next((p for p in pending if p["ticket"] == ticket), None)
        opened_at_str = trade_info["opened_at"] if trade_info else None
        open_price    = trade_info.get("open_price", close_px) if trade_info else close_px
        duration_min  = 0
        if opened_at_str:
            try:
                oa = datetime.fromisoformat(opened_at_str)
                duration_min = int(
                    (datetime.now(timezone.utc) - oa.replace(tzinfo=timezone.utc)
                    ).total_seconds() / 60
                )
            except Exception:
                pass

        update_trade_result(
            ticket=ticket, close_price=close_px,
            profit=profit, pips=pips,
            result=result, duration_min=duration_min,
        )

        # ── FIX 13: actualizar contadores — BE NO cuenta en WR ──
        daily_pnl    += profit
        if result == "WIN":
            wins_today   += 1
            trades_today += 1   # Solo WIN y LOSS aumentan trades_today
        elif result == "LOSS":
            losses_today += 1
            trades_today += 1   # Solo WIN y LOSS aumentan trades_today
        elif result == "BE":
            be_today += 1       # BE contado aparte, NO en trades_today

        daily_trades_log.append({
            "symbol":    symbol,
            "direction": direction,
            "result":    result,
            "profit":    profit,
            "pips":      pips,
            "duration":  duration_min,
        })

        notify_trade_closed(
            symbol=symbol, ticket=ticket, direction=direction,
            open_price=float(open_price), close_price=close_px,
            profit=profit, pips=pips, duration_min=duration_min,
            result=result, memory_learned=(result == "LOSS"),
        )
        last_action = f"Cerrada #{ticket} {symbol} {result} ${profit:+.2f}"
        log.info(
            f"[closures] ✅ #{ticket} {symbol} {direction} {result} "
            f"${profit:+.2f} | {pips:+.1f} pips | {duration_min}min"
        )
        # Limpiar estado de trail para el ticket cerrado
        _trail_stage.pop(ticket, None)
        be_activated.discard(ticket)
        tp_alerted.discard(ticket)
        tickets_en_memoria.discard(ticket)


# ════════════════════════════════════════════════════════════════
#  RESUMEN DIARIO — INTERMEDIO Y EOD
# ════════════════════════════════════════════════════════════════

def maybe_send_daily_summary(balance: float, equity: float):
    global last_summary_date
    now   = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour == 17 and today != last_summary_date:
        notify_daily_summary(
            balance=balance, equity=equity, daily_profit=daily_pnl,
            trades_today=trades_today, wins=wins_today, losses=losses_today,
            memory_stats=get_memory_stats(),
        )
        last_summary_date = today
        log.info("[stats] 📊 Resumen intermedio 17:00 UTC enviado")


def maybe_send_eod_analysis(balance: float, equity: float):
    global last_eod_date, daily_pnl, trades_today, wins_today, losses_today
    global be_today, daily_trades_log

    now   = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour == 21 and today != last_eod_date:
        last_eod_date = today

        # FIX 13: WR = wins/(wins+losses) — sin BE
        wr     = (wins_today / trades_today * 100) if trades_today > 0 else 0.0
        profit = round(daily_pnl, 2)
        growth = ((balance - daily_start_balance) / daily_start_balance * 100) if daily_start_balance > 0 else 0

        loss_reasons = []
        if losses_today > wins_today:
            loss_trades = [t for t in daily_trades_log if t["result"] == "LOSS"]
            if loss_trades:
                symbols_lost = {}
                for t in loss_trades:
                    symbols_lost[t["symbol"]] = symbols_lost.get(t["symbol"], 0) + 1
                worst_sym = max(symbols_lost, key=symbols_lost.get)
                loss_reasons.append(f"Mayor concentración de pérdidas en {worst_sym} ({symbols_lost[worst_sym]} trades)")

            loss_durations = [t["duration"] for t in loss_trades if t["duration"] > 0]
            win_trades     = [t for t in daily_trades_log if t["result"] == "WIN"]
            win_durations  = [t["duration"] for t in win_trades if t["duration"] > 0]
            if loss_durations:
                avg_loss_dur = sum(loss_durations) / len(loss_durations)
                loss_reasons.append(f"Duración promedio en pérdidas: {avg_loss_dur:.0f} min")
            if win_durations:
                avg_win_dur = sum(win_durations) / len(win_durations)
                loss_reasons.append(f"Duración promedio en ganancias: {avg_win_dur:.0f} min")

        be_reason = ""
        if be_today > 0:
            be_pct = be_today / max(trades_today + be_today, 1) * 100
            be_reason = (
                f"⚡ {be_today} trades cerraron en breakeven ({be_pct:.0f}% del total). "
                "Si superan el 50%, el trailing está demasiado agresivo — "
                "considera aumentar be_atr_mult o reducir sl_atr_mult."
            )

        mem_stats = get_memory_stats()

        notify_eod_analysis(
            balance=balance, equity=equity,
            daily_profit=profit,
            trades_today=trades_today, wins=wins_today,
            losses=losses_today, be_count=be_today,
            win_rate=round(wr, 1),
            growth_pct=round(growth, 2),
            loss_reasons=loss_reasons,
            be_reason=be_reason,
            memory_stats=mem_stats,
            daily_trades=daily_trades_log,
        )

        log.info(
            f"[EOD] 📊 Análisis EOD enviado | "
            f"{trades_today} trades reales (excl. {be_today} BE) | "
            f"WR={wr:.0f}% | P&L=${profit:+.2f}"
        )

        # Resetear contadores
        daily_pnl    = 0.0
        trades_today = wins_today = losses_today = be_today = 0
        daily_trades_log.clear()


def _log_session_stats():
    """
    FIX 13: WR = wins/(wins+losses). BE excluido del denominador.
    Muestra las 3 métricas por separado: W / L / BE.
    """
    wr  = (wins_today / trades_today * 100) if trades_today > 0 else 0.0
    mem = get_memory_stats()
    log.info(
        f"[stats] Hoy: {trades_today} trades reales | "
        f"✅ {wins_today}W ❌ {losses_today}L ⚡ {be_today}BE (excluidos) | "
        f"WR={wr:.0f}% | P&L=${daily_pnl:+.2f} | "
        f"Mem: {mem['total']} trades WR={mem['win_rate']}% P&L=${mem['profit']:+.2f}"
    )


def _notify_calendar_pause_once(symbol: str, event, sym_cfg: dict):
    from modules.economic_calendar import CalendarEvent
    if isinstance(event, CalendarEvent) and event.event_id not in eco_calendar._notified_events:
        telegram_send(eco_calendar.format_for_telegram(symbol, event))
        eco_calendar.mark_notified(event)


def _notify_news_pause_once(symbol: str, reason: str, duration_min: int):
    global news_pause_notified
    now_ts = time.time()
    if now_ts - news_pause_notified.get(symbol, 0) >= NOTIF_COOLDOWN_SEC:
        notify_news_pause(symbol, reason, duration_min)
        news_pause_notified[symbol] = now_ts


def _notify_memory_block_once(symbol: str, mem_check):
    global memory_block_notified
    now_ts = time.time()
    if now_ts - memory_block_notified.get(symbol, 0) >= NOTIF_COOLDOWN_SEC:
        notify_memory_block(symbol, "BUY/SELL", mem_check.similar_losses, mem_check.warning_msg)
        memory_block_notified[symbol] = now_ts


# ════════════════════════════════════════════════════════════════
#  PROCESAMIENTO POR SÍMBOLO
# ════════════════════════════════════════════════════════════════

def _process_symbol(
    symbol: str, sym_cfg: dict,
    open_positions: list, balance: float, equity: float,
):
    global last_action, ind_cache, sr_cache, symbol_status_cache

    equity_guard_min_pct = float(getattr(cfg, "EQUITY_GUARD_MIN_PCT", 70.0))
    equity_floor = balance * (equity_guard_min_pct / 100.0) if balance and balance > 0 else 0.0
    if equity_floor > 0 and equity < equity_floor:
        msg = (
            f"🛡 Equity guard: {equity:,.2f} < {equity_floor:,.2f} "
            f"({equity_guard_min_pct:.0f}% bal)"
        )
        _set_symbol_status(symbol, msg[:48])
        last_action = f"{msg} {symbol}"
        log.warning(f"[{symbol}] {msg} — nuevas entradas bloqueadas")
        return

    # FIX 15: los límites de entrada no deben impedir calcular indicadores.
    # Si no, el dashboard queda en "Calculando indicadores..." y el trailing
    # stop deja de gestionarse justo cuando hay posiciones abiertas.
    tradeable_basic, motivo_basic = is_market_tradeable(symbol, sym_cfg)
    if not tradeable_basic:
        _set_symbol_status(symbol, motivo_basic)
        return

    live_sym_positions = mt5.positions_get(symbol=symbol)
    if live_sym_positions is None:
        live_sym_positions = []
    bot_sym_positions = [p for p in live_sym_positions if p.magic == cfg.MAGIC_NUMBER]
    has_bot_position = len(bot_sym_positions) > 0
    max_per_symbol = getattr(cfg, "MAX_OPEN_PER_SYMBOL", 1)

    now_ts = time.time()
    last_open = last_trade_time.get(symbol, 0)
    cooldown_active = (now_ts - last_open) < SYMBOL_COOLDOWN_SEC
    cooldown_remaining = int(max(0, SYMBOL_COOLDOWN_SEC - (now_ts - last_open)))
    total_open = get_open_positions_count_realtime()

    df_entry = get_candles(symbol, cfg.TF_ENTRY, TF_CANDLES.get(cfg.TF_ENTRY, 300))
    df_h1    = get_candles(symbol, cfg.TF_TREND, TF_CANDLES.get(cfg.TF_TREND, 150))
    if df_entry is None or df_h1 is None or len(df_entry) < 60:
        _set_symbol_status(symbol, "⚠️ Datos insuficientes de MT5")
        return

    ind = compute_all(df_h1, symbol, sym_cfg)
    ind_cache[symbol] = ind

    if has_bot_position:
        _set_symbol_status(symbol, f"📂 Posición abierta ({len(bot_sym_positions)}/{max_per_symbol}) — gestionando")
        return

    if cooldown_active:
        _set_symbol_status(symbol, f"⏳ Cooldown activo — {cooldown_remaining}s")
        return

    if len(bot_sym_positions) >= max_per_symbol:
        _set_symbol_status(symbol, f"📂 Límite por símbolo alcanzado ({len(bot_sym_positions)}/{max_per_symbol})")
        return

    if total_open >= cfg.MAX_OPEN_TRADES:
        _set_symbol_status(symbol, f"⛔ Límite global alcanzado ({total_open}/{cfg.MAX_OPEN_TRADES})")
        return

    atr_now   = ind.get("atr", 0)
    price_now = ind.get("price", 0)
    tradeable_atr, motivo_atr = is_market_tradeable(symbol, sym_cfg, atr_now, price_now)
    if not tradeable_atr:
        log.info(f"[{symbol}] {motivo_atr}")
        last_action = f"📉 {motivo_atr[:55]}"
        _set_symbol_status(symbol, motivo_atr)
        return

    dfs_by_tf = {}
    for tf in sym_cfg.get("sr_timeframes", ["H1"]):
        df_tf = get_candles(symbol, tf, TF_CANDLES.get(tf, 150))
        if df_tf is not None:
            dfs_by_tf[tf] = df_tf
    sr_ctx = build_sr_context(dfs_by_tf, ind["price"], symbol, sym_cfg)
    sr_cache[symbol] = sr_ctx

    pause_cal, cal_reason, cal_event, already_cal_notified = \
        eco_calendar.should_pause_symbol(symbol, sym_cfg)
    if pause_cal:
        last_action = f"📅 Cal: {cal_reason[:55]}"
        _set_symbol_status(symbol, f"📅 {cal_reason[:48]}")
        if not already_cal_notified and cal_event is not None:
            _notify_calendar_pause_once(symbol, cal_event, sym_cfg)
        log.info(f"[{symbol}] ⏸ Cal: {cal_reason[:60]}")
        return

    cal_events_nearby = eco_calendar.get_events_for_symbol(symbol, sym_cfg, minutes_ahead=120)

    now_ts_news = time.time()
    if symbol not in news_cache or (now_ts_news - news_last_update.get(symbol, 0)) > cfg.NEWS_REFRESH_MIN * 60:
        news_ctx = build_news_context(symbol, sym_cfg)
        news_cache[symbol]       = news_ctx
        news_last_update[symbol] = now_ts_news
    else:
        news_ctx = news_cache[symbol]

    if news_ctx.should_pause:
        last_action = f"📰 News pausa {symbol}"
        _set_symbol_status(symbol, f"📰 {news_ctx.pause_reason[:48]}")
        _notify_news_pause_once(symbol, news_ctx.pause_reason, cfg.NEWS_PAUSE_MINUTES_BEFORE)
        return
    else:
        news_pause_notified.pop(symbol, None)

    hurst_val = ind.get("hurst", 0.5)
    min_hurst = sym_cfg.get("min_hurst", 0.40)
    if hurst_val < min_hurst:
        log.info(f"[{symbol}] Hurst {hurst_val:.3f} < {min_hurst:.3f} — HOLD")
        last_action = f"📊 Hurst bajo {symbol} ({hurst_val:.2f}<{min_hurst:.2f})"
        _set_symbol_status(symbol, f"📊 Hurst bajo {hurst_val:.2f}<{min_hurst:.2f}")
        return

    h1_trend = ind.get("h1_trend", "LATERAL")
    if "BAJISTA" in h1_trend:
        mem_direction = "SELL"
    elif "ALCISTA" in h1_trend:
        mem_direction = "BUY"
    else:
        feat_buy  = build_features(symbol, "BUY",  ind, sr_ctx, news_ctx, sym_cfg)
        feat_sell = build_features(symbol, "SELL", ind, sr_ctx, news_ctx, sym_cfg)
        mem_buy   = check_memory(feat_buy,  symbol, "BUY",  sym_cfg)
        mem_sell  = check_memory(feat_sell, symbol, "SELL", sym_cfg)
        setup_buy = derive_setup_id(ind, "BUY")
        setup_sell = derive_setup_id(ind, "SELL")
        session_id = derive_session_from_ind(ind)
        score_buy = evaluate_scorecard(symbol, setup_buy, session_id, mem_buy.regime)
        score_sell = evaluate_scorecard(symbol, setup_sell, session_id, mem_sell.regime)
        policy_buy = evaluate_policy(symbol, "BUY", setup_buy, session_id, mem_buy.regime)
        policy_sell = evaluate_policy(symbol, "SELL", setup_sell, session_id, mem_sell.regime)
        if mem_buy.should_block and mem_sell.should_block:
            last_action = f"🧠 Memoria bloqueó {symbol}"
            _set_symbol_status(symbol, "🧠 Memoria bloqueó BUY y SELL")
            _notify_memory_block_once(symbol, mem_buy)
            return
        if score_buy.should_block and score_sell.should_block:
            msg = "🧮 Scorecard bloqueó BUY y SELL (historial pobre)"
            last_action = f"{msg} {symbol}"
            _set_symbol_status(symbol, msg[:48])
            log.info(
                f"[{symbol}] {msg} | BUY={score_buy.reason} | SELL={score_sell.reason}"
            )
            return
        candidates = [
            ("BUY", feat_buy, mem_buy, score_buy, policy_buy),
            ("SELL", feat_sell, mem_sell, score_sell, policy_sell),
        ]
        viable = [c for c in candidates if not c[2].should_block and not c[3].should_block and not c[4].should_block]
        if not viable:
            msg = "🧮 Policy/Scorecard bloqueó candidatos BUY y SELL"
            _set_symbol_status(symbol, msg[:48])
            last_action = f"{msg} {symbol}"
            log.info(
                f"[{symbol}] {msg} | "
                f"BUY={policy_buy.reason} | SELL={policy_sell.reason}"
            )
            return
        selected = max(viable, key=lambda x: (x[4].policy_score, x[2].confidence_adj))
        _, features, mem_check, selected_scorecard, selected_policy = selected
        if mem_check.should_block:
            last_action = f"🧠 Memoria bloqueó {symbol}"
            _set_symbol_status(symbol, f"🧠 {mem_check.warning_msg[:48]}")
            _notify_memory_block_once(symbol, mem_check)
            return
        else:
            memory_block_notified.pop(symbol, None)
        # Scorecard en ruta LATERAL: se evaluará después de la respuesta Gemini
        # porque la dirección final (BUY/SELL) aún no está definida.
        # LATERAL path: direction unknown before Gemini — the post-Gemini gate
        # in _execute_decision will apply the confluence veto after Gemini responds.
        context  = build_context(
            symbol, ind, sr_ctx, news_ctx, mem_check, sym_cfg,
            cal_events_nearby, selected_scorecard, selected_policy,
        )
        decision = ask_gemini(context, sym_cfg)
        _execute_decision(symbol, sym_cfg, decision, ind, sr_ctx, news_ctx,
                          mem_check, features, balance, df_entry)
        return

    features  = build_features(symbol, mem_direction, ind, sr_ctx, news_ctx, sym_cfg)
    mem_check = check_memory(features, symbol, mem_direction, sym_cfg)
    setup_id  = derive_setup_id(ind, mem_direction)
    session_id = derive_session_from_ind(ind)
    scorecard = evaluate_scorecard(
        symbol=symbol,
        setup_id=setup_id,
        session=session_id,
        regime=mem_check.regime,
    )
    policy = evaluate_policy(
        symbol=symbol,
        direction=mem_direction,
        setup_id=setup_id,
        session=session_id,
        regime=mem_check.regime,
    )

    if mem_check.should_block:
        last_action = f"🧠 Memoria bloqueó {symbol}"
        _set_symbol_status(symbol, f"🧠 {mem_check.warning_msg[:48]}")
        _notify_memory_block_once(symbol, mem_check)
        return
    else:
        memory_block_notified.pop(symbol, None)

    if scorecard.should_block:
        msg = (
            f"🧮 Scorecard vetó {mem_direction} "
            f"(WR={scorecard.win_rate:.1f}% n={scorecard.sample_size})"
        )
        _set_symbol_status(symbol, msg[:48])
        last_action = f"{msg} {symbol}"
        log.info(f"[{symbol}] {msg} | {scorecard.reason}")
        return

    if policy.should_block:
        msg = (
            f"📉 Policy vetó {mem_direction} "
            f"(score={policy.policy_score:.3f} n={policy.sample_size})"
        )
        _set_symbol_status(symbol, msg[:48])
        last_action = f"{msg} {symbol}"
        log.info(f"[{symbol}] {msg} | {policy.reason}")
        return

    # ── FASE 3: Confluence Gate pre-Gemini (directional path) ──────
    if _apply_confluence_gate_pre(symbol, ind, mem_direction):
        return

    context  = build_context(
        symbol, ind, sr_ctx, news_ctx, mem_check, sym_cfg,
        cal_events_nearby, scorecard, policy,
    )
    decision = ask_gemini(context, sym_cfg)
    _execute_decision(symbol, sym_cfg, decision, ind, sr_ctx, news_ctx,
                      mem_check, features, balance, df_entry)


def _apply_confluence_gate_pre(symbol: str, ind: dict, direction: str) -> bool:
    """
    FASE 3 — Confluence Hard Gate (pre-Gemini).

    Bloquea la llamada a Gemini si la Confluencia de 3 Pilares contradice
    fuertemente la dirección esperada. Ahorra una llamada API y aplica el
    equivalente en código a REGLA 15 del system prompt.

    Umbral: conf_total < -(CONFLUENCE_MIN_SCORE × 2) para BUY
            conf_total > +(CONFLUENCE_MIN_SCORE × 2) para SELL

    Con los defaults (CONFLUENCE_MIN_SCORE=0.3), el umbral = ±0.6 sobre
    una escala de -3 a +3. Solo se bloquea cuando los 3 pilares apuntan
    claramente contra la dirección (no en neutrales).

    Retorna True si se debe bloquear (llamar 'return' en _process_symbol).
    """
    global last_action

    conf_total = ind.get("confluence", {}).get("total", 0.0)
    hard_thresh = (
        getattr(cfg, "CONFLUENCE_MIN_SCORE", 0.3)
        * getattr(cfg, "CONFLUENCE_HARD_GATE_MULT", 2)
    )

    if direction == "BUY" and conf_total < -hard_thresh:
        msg = f"⚡ Conf veta BUY ({conf_total:+.2f}) — pilares bajistas"
        _set_symbol_status(symbol, msg[:48])
        last_action = f"{msg} {symbol}"
        log.info(f"[{symbol}] {msg} — Gemini no consultado")
        return True

    if direction == "SELL" and conf_total > hard_thresh:
        msg = f"⚡ Conf veta SELL ({conf_total:+.2f}) — pilares alcistas"
        _set_symbol_status(symbol, msg[:48])
        last_action = f"{msg} {symbol}"
        log.info(f"[{symbol}] {msg} — Gemini no consultado")
        return True

    return False


def _execute_decision(
    symbol, sym_cfg, decision, ind, sr_ctx, news_ctx,
    mem_check, features, balance, df_entry,
):
    """Valida la decisión de Gemini y aplica filtro anti-contra-tendencia."""
    global last_action

    if decision is None:
        log.warning(f"[{symbol}] Gemini no respondió")
        _set_symbol_status(symbol, "🤖 Gemini no respondió")
        return

    action     = decision.get("decision", "HOLD")
    confidence = int(decision.get("confidence", 0))
    reason     = decision.get("reason", "")

    log.info(f"[{symbol}] 🤖 {action} conf={confidence} | {reason[:80]}")

    if action in ("BUY", "SELL"):
        setup_id = derive_setup_id(ind, action)
        session_id = derive_session_from_ind(ind)
        scorecard = evaluate_scorecard(
            symbol=symbol,
            setup_id=setup_id,
            session=session_id,
            regime=mem_check.regime,
        )
        policy = evaluate_policy(
            symbol=symbol,
            direction=action,
            setup_id=setup_id,
            session=session_id,
            regime=mem_check.regime,
        )
        if scorecard.should_block:
            msg = (
                f"🧮 Scorecard vetó {action} "
                f"(WR={scorecard.win_rate:.1f}% n={scorecard.sample_size})"
            )
            last_action = f"{msg} {symbol}"
            _set_symbol_status(symbol, msg[:48])
            log.info(f"[{symbol}] {msg} | {scorecard.reason}")
            return
        if policy.should_block:
            msg = (
                f"📉 Policy vetó {action} "
                f"(score={policy.policy_score:.3f} n={policy.sample_size})"
            )
            last_action = f"{msg} {symbol}"
            _set_symbol_status(symbol, msg[:48])
            log.info(f"[{symbol}] {msg} | {policy.reason}")
            return
    else:
        scorecard = None
        policy = None

    min_conf = sym_cfg.get("min_confidence", 6)
    if (
        scorecard is not None
        and scorecard.sample_size >= scorecard.min_sample
        and scorecard.win_rate < (scorecard.min_win_rate + 5.0)
    ):
        min_conf += int(getattr(cfg, "SCORECARD_MIN_CONF_BONUS", 1))
    if (
        policy is not None
        and policy.sample_size >= int(getattr(cfg, "POLICY_MIN_SAMPLE", 10))
        and policy.policy_score < (float(getattr(cfg, "POLICY_MIN_SCORE", 0.45)) + 0.1)
    ):
        min_conf += int(getattr(cfg, "POLICY_MIN_CONF_BONUS", 1))
    if action == "HOLD" or confidence < min_conf:
        last_action = f"HOLD {symbol} conf={confidence} (min={min_conf})"
        _set_symbol_status(symbol, f"🤖 {action} conf={confidence}/{min_conf}")
        return

    # ── FIX 14: Filtro anti-contra-tendencia ─────────────────────
    passes_filter, filter_reason = _passes_direction_filter(action, ind)
    if not passes_filter:
        log.info(f"[{symbol}] 🚫 Anti-contratendencia: {filter_reason}")
        last_action = f"🚫 {filter_reason[:60]}"
        _set_symbol_status(symbol, f"🚫 {filter_reason[:48]}")
        return

    # ── FASE 3: Confluence Hard Gate (safety net post-Gemini) ─────
    # Veta la orden si la respuesta de Gemini contradice la confluencia global.
    # Captura el caso LATERAL donde la dirección no se conocía antes de Gemini.
    conf_total  = ind.get("confluence", {}).get("total", 0.0)
    conf_thresh = (
        getattr(cfg, "CONFLUENCE_MIN_SCORE", 0.3)
        * getattr(cfg, "CONFLUENCE_HARD_GATE_MULT", 2)
    )
    if action == "BUY" and conf_total < -conf_thresh:
        msg = f"⚡ Conf veta BUY post-Gemini ({conf_total:+.2f})"
        log.info(f"[{symbol}] {msg}")
        last_action = f"{msg} {symbol}"
        _set_symbol_status(symbol, msg[:48])
        return
    if action == "SELL" and conf_total > conf_thresh:
        msg = f"⚡ Conf veta SELL post-Gemini ({conf_total:+.2f})"
        log.info(f"[{symbol}] {msg}")
        last_action = f"{msg} {symbol}"
        _set_symbol_status(symbol, msg[:48])
        return

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        _set_symbol_status(symbol, "⚠️ Tick no disponible")
        return
    price  = tick.ask if action == "BUY" else tick.bid
    sl, tp = calc_sl_tp(action, price, ind["atr"], sym_cfg)

    rr = get_rr(price, sl, tp)
    if not is_rr_valid(price, sl, tp):
        last_action = f"R:R inválido {symbol} ({rr:.2f})"
        _set_symbol_status(symbol, f"⚖️ R:R inválido ({rr:.2f})")
        return

    sym_info = mt5.symbol_info(symbol)
    point    = sym_info.point if sym_info else 0.00001
    sl_pips  = abs(price - sl) / point

    # ── FASE 2: Kelly position sizing ────────────────────────────
    # Usa Kelly fraccionado cuando hay suficientes trades históricos.
    # Si no, vuelve automáticamente al sizing estándar.
    mem_stats  = get_memory_stats()
    sym_trades = mem_stats.get("total", 0)
    win_rate   = mem_stats.get("win_rate", 0.0) / 100.0  # convertir % → fracción
    kelly_min_trades = getattr(cfg, "KELLY_MIN_TRADES", 30)
    kelly_active     = sym_trades >= kelly_min_trades and win_rate > 0
    vol = get_lot_size_kelly(
        balance=balance,
        sl_pips=sl_pips,
        symbol=symbol,
        win_rate=win_rate,
        avg_rr=rr,
        n_trades=sym_trades,
    )

    hilbert  = ind.get("hilbert", {})
    h_signal = hilbert.get("signal", "NEUTRAL")
    if action == "BUY"  and h_signal == "LOCAL_MAX":
        last_action = f"🌀 Hilbert bloqueó BUY {symbol}"
        _set_symbol_status(symbol, "🌀 Hilbert bloqueó BUY")
        return
    if action == "SELL" and h_signal == "LOCAL_MIN":
        last_action = f"🌀 Hilbert bloqueó SELL {symbol}"
        _set_symbol_status(symbol, "🌀 Hilbert bloqueó SELL")
        return

    ticket = open_order(symbol, action, sl, tp, vol)
    if ticket is None:
        last_action = f"❌ Orden fallida {symbol}"
        _set_symbol_status(symbol, "❌ Orden fallida")
        return

    # Registrar timestamp de apertura para cooldown
    last_trade_time[symbol] = time.time()

    # Inicializar estado de trail para este ticket
    _trail_stage[ticket] = 0

    direction_features = build_features(symbol, action, ind, sr_ctx, news_ctx, sym_cfg)
    setup_id = derive_setup_id(ind, action)
    session_id = derive_session_from_ind(ind)
    risk_amount = balance * float(getattr(cfg, "RISK_PER_TRADE", 0.01))
    save_trade(
        ticket=ticket, symbol=symbol, direction=action,
        open_price=price, volume=vol,
        features=direction_features, reason_gemini=reason,
        hilbert_signal=h_signal, hurst_val=ind.get("hurst", 0.5),
        setup_id=setup_id,
        setup_score=ind.get("confluence", {}).get("total", 0.0),
        session=session_id,
        regime=mem_check.regime,
        risk_amount=risk_amount,
        sl=sl,
        tp=tp,
    )
    tickets_en_memoria.add(ticket)

    notify_trade_opened(
        symbol=symbol, direction=action, price=price, sl=sl, tp=tp,
        volume=vol, atr=ind["atr"], rr=rr, reason=reason,
        ind=ind, hilbert=hilbert,
        hurst=ind.get("hurst", 0.5),
        fisher=ind.get("fisher", 0),
        memory_warn=mem_check.warning_msg,
        kelly_active=kelly_active,
    )

    try:
        from modules.chart_generator import (
            generate_trade_chart, send_chart_to_telegram, build_telegram_caption,
        )
        image_bytes = generate_trade_chart(
            symbol=symbol, direction=action, df=df_entry,
            ind=ind, sr_ctx=sr_ctx, price=price, sl=sl, tp=tp,
            rr=rr, reason=reason, sym_cfg=sym_cfg, n_candles=80,
        )
        if image_bytes:
            caption = build_telegram_caption(
                symbol=symbol, direction=action, price=price, sl=sl, tp=tp,
                rr=rr, volume=vol, ind=ind, hilbert=hilbert,
                hurst=ind.get("hurst", 0.5), fisher=ind.get("fisher", 0),
                reason=reason, sym_cfg=sym_cfg,
            )
            send_chart_to_telegram(
                image_bytes=image_bytes, caption=caption,
                token=cfg.TELEGRAM_TOKEN, chat_id=cfg.TELEGRAM_CHAT_ID,
            )
    except Exception as chart_err:
        log.warning(f"[{symbol}] Gráfico: {chart_err}")

    last_action = f"✅ {action} {symbol} #{ticket} conf={confidence}"
    _set_symbol_status(symbol, f"✅ {action} #{ticket} conf={confidence}")
    log.info(f"[{symbol}] ✅ #{ticket} {action} p={price} sl={sl} tp={tp}")


# ════════════════════════════════════════════════════════════════
#  LOOP PRINCIPAL
# ════════════════════════════════════════════════════════════════

def run():
    global cycle_count, last_action, ind_cache, sr_cache, symbol_status_cache, daily_start_balance

    log.info("🚀 ZAR ULTIMATE BOT v6.5 — TRAILING STOP + WR FIX — Iniciando...")
    init_db()

    if not conectar_mt5():
        log.critical("No se pudo conectar a MT5.")
        sys.exit(1)

    balance, equity = get_account_info()
    daily_start_balance = balance or 0.0

    log.info("[calendar] 📅 Cargando calendario económico...")
    eco_calendar.refresh(force=True)

    mem_stats = get_memory_stats()
    notify_bot_started(balance, equity, mem_stats, list(cfg.SYMBOLS.keys()))
    log.info(
        f"✅ Bot v6.5 iniciado | Balance=${balance:,.2f} | "
        f"{len(cfg.SYMBOLS)} símbolos | Modo 24/5 | "
        f"Cooldown={SYMBOL_COOLDOWN_SEC}s | "
        f"Trail: BE@1.5×ATR → Trail@2.5× → Trail@4× → Tight@TP80%"
    )

    for pending in get_pending_trades():
        tickets_en_memoria.add(pending["ticket"])

    while True:
        cycle_count += 1
        try:
            ind_cache = {}
            sr_cache = {}
            symbol_status_cache = {}

            balance, equity = get_account_info()
            if balance is None:
                log.warning("[main] Sin balance — reconectando...")
                if not conectar_mt5():
                    time.sleep(30); continue

            maybe_send_daily_summary(balance, equity)
            maybe_send_eod_analysis(balance, equity)

            if not is_daily_loss_ok(daily_pnl, balance):
                log.warning("[main] ⛔ Límite diario alcanzado")
                last_action = "⛔ Límite diario alcanzado"
                time.sleep(cfg.LOOP_SLEEP_SEC * 5); continue

            open_positions = get_open_positions()
            open_tickets   = {p["ticket"] for p in open_positions}
            watch_closures(tickets_en_memoria.copy(), open_positions)
            tickets_en_memoria.clear()
            tickets_en_memoria.update(open_tickets)

            if cycle_count % 10 == 0:
                _log_session_stats()

            for symbol, sym_cfg_data in cfg.SYMBOLS.items():
                try:
                    _process_symbol(symbol, sym_cfg_data, open_positions, balance, equity)
                except Exception as e:
                    log.error(f"[main] Error en {symbol}: {e}", exc_info=True)

            open_positions = get_open_positions()
            manage_positions(open_positions, ind_cache)
            _render_dashboard(balance, equity, open_positions)

        except Exception as e:
            log.error(f"[main] Error ciclo #{cycle_count}: {e}", exc_info=True)
            notify_error(str(e)[:300])

        time.sleep(cfg.LOOP_SLEEP_SEC)


def _render_dashboard(balance: float, equity: float, open_positions: list):
    cal_info   = eco_calendar.format_for_dashboard()
    cal_status = eco_calendar.get_status()
    news_ctx   = next(iter(news_cache.values()), None)
    try:
        dashboard.render(
            symbols=list(cfg.SYMBOLS.keys()), indicators_by_sym=ind_cache,
            sr_by_sym=sr_cache, news_ctx=news_ctx, open_positions=open_positions,
            memory_stats=get_memory_stats(), balance=balance, equity=equity,
            daily_pnl=daily_pnl, cycle=cycle_count, last_action=last_action,
            calendar_info=cal_info, calendar_status=cal_status,
            status_by_sym=symbol_status_cache,
        )
    except TypeError:
        dashboard.render(
            symbols=list(cfg.SYMBOLS.keys()), indicators_by_sym=ind_cache,
            sr_by_sym=sr_cache, news_ctx=news_ctx, open_positions=open_positions,
            memory_stats=get_memory_stats(), balance=balance, equity=equity,
            daily_pnl=daily_pnl, cycle=cycle_count, last_action=last_action,
            status_by_sym=symbol_status_cache,
        )


if __name__ == "__main__":
    run()
