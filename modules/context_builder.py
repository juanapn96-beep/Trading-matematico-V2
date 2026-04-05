"""
ZAR v7 — Context Builder module
Extracted from main.py: build_context() and build_lateral_context().

These are pure text-formatting functions — they only receive data as arguments
and return strings. No global state dependencies.
"""
import logging
from datetime import datetime, timezone

from modules.sr_zones import sr_for_prompt
from modules.news_engine import format_news_for_prompt
from modules.sentiment_data import get_sentiment_for_symbol

try:
    from modules.data_providers import get_twelve_data, get_polygon
    _DATA_PROVIDERS_AVAILABLE = True
except ImportError:
    _DATA_PROVIDERS_AVAILABLE = False

log = logging.getLogger(__name__)


def build_context(
    symbol: str, ind: dict, sr_ctx, news_ctx,
    mem_check, sym_cfg: dict,
    cal_events: list = None,
    scorecard_check=None,
    policy_check=None,
    trade_plan=None,
) -> str:
    import config as cfg

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
    ]

    # ── FASE 12: ADF + Z-score — régimen mejorado ──
    adf = ind.get("adf_test", {})
    zsc = ind.get("zscore_returns", {})
    enhanced_regime = ind.get("enhanced_regime", "UNKNOWN")
    regime_conf = ind.get("regime_confidence", "LOW")

    lines.append(f"Régimen mejorado: {enhanced_regime} (confianza: {regime_conf})")
    if adf.get("p_value", 1.0) < 1.0:
        lines.append(
            f"ADF: stat={adf.get('adf_statistic', 0):.3f} p={adf.get('p_value', 1):.3f} "
            f"{'✓ estacionario' if adf.get('is_stationary') else '✗ no estacionario'}"
        )
    if zsc.get("signal", "NEUTRAL") != "NEUTRAL":
        lines.append(
            f"Z-Score: {zsc.get('z_score', 0):.2f} → {zsc.get('signal')} "
            f"(fuerza: {zsc.get('strength', 0):.0%})"
        )

    lines += [
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
        lines += ["Sin scorecard disponible", ""]

    lines += ["── POLICY ENGINE ──"]
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
        lines += ["Sin policy check disponible", ""]

    lines += ["── PLAN DE TRADE PREVIO ──"]
    if trade_plan is not None:
        lines += [
            f"Dirección candidata: {trade_plan['action']}",
            f"Entry estimado: {trade_plan['price']:.5f} | SL: {trade_plan['sl']:.5f} | TP: {trade_plan['tp']:.5f}",
            f"R:R estimado: {trade_plan['rr']:.2f}",
            "",
        ]
    else:
        lines += ["Plan no disponible", ""]

    lines += [
        "── NOTICIAS RSS ──",
        format_news_for_prompt(news_ctx),
        "",
    ]

    # ── SENTIMIENTO DE MERCADO ────────────────────────────────────
    sentiment = get_sentiment_for_symbol(symbol)
    if sentiment:
        sent_lines = ["── SENTIMIENTO DE MERCADO ──"]
        if "crypto_fng" in sentiment:
            sent_lines.append(
                f"Crypto Fear & Greed: {sentiment['crypto_fng']:.0f} "
                f"({sentiment.get('crypto_fng_label', '?')})"
            )
        if "vix" in sentiment:
            sent_lines.append(
                f"VIX: {sentiment['vix']:.2f} ({sentiment.get('vix_label', '?')})"
            )
        if "cot_net_position" in sentiment:
            cot_date = sentiment.get("cot_report_date", "")
            date_str = f" ({cot_date})" if cot_date else ""
            sent_lines.append(
                f"COT Non-Commercial net: {sentiment['cot_net_position']:+,} "
                f"→ {sentiment.get('cot_bias', '?')}{date_str}"
            )
        sent_lines.append(f"Sesgo sentimiento: {sentiment.get('sentiment_bias', 'NEUTRAL')}")
        sent_lines.append("")
        lines += sent_lines

    # ── PILAR 3: MICROESTRUCTURA ──────────────────────────────────
    micro = ind.get("microstructure", {})
    conf  = ind.get("confluence", {})

    # ── DATOS EXTERNOS ────────────────────────────────────────────
    if _DATA_PROVIDERS_AVAILABLE:
        try:
            td_enabled   = getattr(cfg, "TWELVE_DATA_ENABLED",  False)
            poly_enabled = getattr(cfg, "POLYGON_ENABLED",      False)
            ext_data_source = ind.get("ext_data_source", "tick_volume")
            ext_lines = []

            if poly_enabled:
                try:
                    poly = get_polygon()
                    if symbol in poly.SYMBOL_MAP:
                        snap = poly.get_snapshot(symbol)
                        if snap:
                            ext_lines.append(
                                f"Polygon {symbol}: Vol={snap['day_volume']:,.0f} | "
                                f"VWAP={snap['day_vwap']:.2f} | Cambio={snap['change_pct']:+.2f}%"
                            )
                except Exception:
                    pass

            if td_enabled:
                try:
                    td = get_twelve_data()
                    if symbol in td.SYMBOL_MAP:
                        quote = td.get_quote(symbol)
                        if quote:
                            ext_lines.append(
                                f"Twelve Data {symbol}: Vol={quote['volume']:,.0f} | "
                                f"Precio={quote['close']:.2f} | Cambio={quote['percent_change']:+.2f}%"
                            )
                except Exception:
                    pass

            if ext_lines:
                lines += ["── DATOS EXTERNOS (FASE 11) ──"] + ext_lines + [
                    f"Fuente volumen activa: {ext_data_source}",
                    "",
                ]
        except Exception:
            pass

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


def build_lateral_context(
    symbol: str,
    ind: dict,
    sr_ctx,
    news_ctx,
    sym_cfg: dict,
    cal_events: list,
    candidate_payloads: dict,
) -> str:
    base_mem = next(iter(candidate_payloads.values()))["mem_check"]
    lines = [
        "── CANDIDATOS LATERALES ──",
        "Compara BUY y SELL como candidatos completos. Si ninguno domina claramente, responde HOLD.",
    ]

    for action in ("BUY", "SELL"):
        payload = candidate_payloads.get(action)
        if payload is None:
            continue
        scorecard = payload["scorecard"]
        policy    = payload["policy"]
        mem_check = payload["mem_check"]
        plan      = payload["trade_plan"]
        lines += [
            f"[{action}] listo_para_decision={payload['candidate_ok']}",
            f"  gate: {payload['candidate_reason'] or 'OK'}",
            f"  memory: block={mem_check.should_block} adj={mem_check.confidence_adj:+.1f} detail={mem_check.warning_msg}",
            f"  scorecard: block={scorecard.should_block} wr={scorecard.win_rate:.1f}% n={scorecard.sample_size} reason={scorecard.reason}",
            f"  policy: block={policy.should_block} score={policy.policy_score:.3f} n={policy.sample_size} reason={policy.reason}",
        ]
        if plan is not None:
            lines.append(
                f"  plan: entry={plan['price']:.5f} sl={plan['sl']:.5f} tp={plan['tp']:.5f} rr={plan['rr']:.2f}"
            )
        lines.append("")

    return build_context(
        symbol=symbol,
        ind=ind,
        sr_ctx=sr_ctx,
        news_ctx=news_ctx,
        mem_check=base_mem,
        sym_cfg=sym_cfg,
        cal_events=cal_events,
        scorecard_check=None,
        policy_check=None,
        trade_plan=None,
    ) + "\n\n" + "\n".join(lines)
