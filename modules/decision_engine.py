"""
ZAR v7 — Decision Engine Determinista
Motor de decisión basado en reglas binarias repetibles (sin LLM).
"""
import logging
from datetime import datetime, timezone
from modules.sentiment_data import get_sentiment_for_symbol

log = logging.getLogger(__name__)


def _get_session_quality(sym_cfg: dict) -> float:
    """Retorna el factor de calidad de la sesión actual (0.0-1.0)."""
    session_quality = sym_cfg.get("session_quality")
    if not session_quality:
        return 1.0  # Sin configuración = calidad completa (retrocompatible)
    hour = datetime.now(timezone.utc).hour
    if 0 <= hour < 7:
        return float(session_quality.get("asian", 0.5))
    elif 7 <= hour < 13:
        return float(session_quality.get("london", 1.0))
    elif 13 <= hour < 21:
        return float(session_quality.get("ny", 1.0))
    else:
        return float(session_quality.get("dead", 0.3))


def deterministic_decision(
    symbol: str,
    direction: str,           # "BUY" o "SELL" (del mem_direction ya calculado)
    indicators: dict,         # resultado de compute_all()
    sr_context: dict,         # zonas S/R
    news_context,             # NewsContext object
    sym_cfg: dict,
) -> dict:
    """
    Motor de decisión determinista (sin LLM).

    Returns:
        {"decision": "BUY"|"SELL"|"HOLD", "confidence": int, "reason": str, "score": float}
    """
    # ═══ FILTRO: SPREAD MÁXIMO (hard block) ═══
    spread_pips = float(indicators.get("spread_pips", 0.0) or 0.0)
    max_spread = float(sym_cfg.get("max_spread_pips", 3.0))
    if spread_pips > 0 and spread_pips > max_spread:
        return {
            "decision": "HOLD",
            "confidence": 1,
            "reason": f"SPREAD={spread_pips:.1f}>{max_spread:.1f}pips",
            "score": 0.0,
        }

    score = 0.0
    reasons = []
    max_score = 10.0

    # ═══ PILAR 1: CONFLUENCIA TÉCNICA (max +4.0) ═══
    trend_votes = indicators.get("trend_votes", {})
    bull = trend_votes.get("bull", 0)
    bear = trend_votes.get("bear", 0)

    if direction == "BUY":
        vote_score = bull  # 0-6
    else:
        vote_score = bear

    if vote_score >= 5:
        score += 3.0
        reasons.append(f"votes:{vote_score}/6=FUERTE")
    elif vote_score >= 4:
        score += 2.0
        reasons.append(f"votes:{vote_score}/6=OK")
    elif vote_score >= 3:
        score += 0.5
        reasons.append(f"votes:{vote_score}/6=DÉBIL")
    else:
        score -= 2.0
        reasons.append(f"votes:{vote_score}/6=INSUFICIENTE")

    # Bonus si SuperTrend y Kalman confirman (ambos)
    # indicators["supertrend"]: 1 = alcista, -1 = bajista (entero)
    # indicators["kalman_trend"]: "ALCISTA" / "BAJISTA"
    st_val = indicators.get("supertrend", 0)
    kalman_trend = indicators.get("kalman_trend", "")
    if direction == "BUY":
        both_confirm = (st_val == 1) and (kalman_trend == "ALCISTA")
    else:
        both_confirm = (st_val == -1) and (kalman_trend == "BAJISTA")

    if both_confirm:
        score += 1.0
        reasons.append("ST+Kalman=OK")
    elif not both_confirm and vote_score < 5:
        score -= 1.0
        reasons.append("ST|Kalman=CONTRADICE")

    # ═══ PILAR 2: ESTADÍSTICO / CICLO (max +2.0) ═══
    hilbert = indicators.get("hilbert", {})
    hilbert_signal = hilbert.get("signal", "") if isinstance(hilbert, dict) else getattr(hilbert, "signal", "")

    # Hilbert LOCAL_MAX = techo → no BUY; LOCAL_MIN = piso → no SELL
    if direction == "BUY" and "LOCAL_MAX" in str(hilbert_signal):
        score -= 2.0
        reasons.append("Hilbert=TECHO(noBUY)")
    elif direction == "SELL" and "LOCAL_MIN" in str(hilbert_signal):
        score -= 2.0
        reasons.append("Hilbert=PISO(noSELL)")
    elif direction == "BUY" and "LOCAL_MIN" in str(hilbert_signal):
        score += 1.5
        reasons.append("Hilbert=PISO(BUY✓)")
    elif direction == "SELL" and "LOCAL_MAX" in str(hilbert_signal):
        score += 1.5
        reasons.append("Hilbert=TECHO(SELL✓)")
    else:
        score += 0.5
        reasons.append(f"Hilbert={hilbert_signal}")

    # RSI divergence
    rsi_div = indicators.get("rsi_div", "NONE")
    if direction == "BUY" and "BULL" in str(rsi_div).upper():
        score += 0.5
        reasons.append("RSI_div=BULL✓")
    elif direction == "SELL" and "BEAR" in str(rsi_div).upper():
        score += 0.5
        reasons.append("RSI_div=BEAR✓")

    # ═══ PILAR 2b: FISHER TRANSFORM + CONFLUENCIA (max +1.5) ═══
    fisher_val = indicators.get("fisher", 0.0)
    fisher_cross = indicators.get("fisher_cross", "NONE")
    if direction == "BUY" and fisher_val < -1.5:
        score += 0.75
        reasons.append(f"Fisher={fisher_val:.1f}(extremo_bajo✓)")
    elif direction == "SELL" and fisher_val > 1.5:
        score += 0.75
        reasons.append(f"Fisher={fisher_val:.1f}(extremo_alto✓)")
    elif direction == "BUY" and fisher_cross == "BULL_CROSS":
        score += 0.5
        reasons.append("Fisher=BULL_CROSS✓")
    elif direction == "SELL" and fisher_cross == "BEAR_CROSS":
        score += 0.5
        reasons.append("Fisher=BEAR_CROSS✓")

    # Confluencia 3 pilares (microestructura + estadístico + gráfico)
    confluence = indicators.get("confluence", {})
    conf_total = confluence.get("total", 0.0) if isinstance(confluence, dict) else 0.0
    if direction == "BUY" and conf_total > 0.5:
        score += 0.75
        reasons.append(f"conf={conf_total:+.2f}✓")
    elif direction == "SELL" and conf_total < -0.5:
        score += 0.75
        reasons.append(f"conf={conf_total:+.2f}✓")
    elif (direction == "BUY" and conf_total < -0.3) or (direction == "SELL" and conf_total > 0.3):
        score -= 0.5
        reasons.append(f"conf={conf_total:+.2f}✗")

    # ═══ PILAR 3: ZONAS S/R (max +2.0) ═══
    in_strong_zone = sr_context.get("in_strong_zone", False) if isinstance(sr_context, dict) else False

    # SRContext.__dict__ contains "supports" and "resistances" as lists of SRZone objects.
    # supports is sorted closest-first; resistances likewise.
    if isinstance(sr_context, dict):
        supports = sr_context.get("supports", [])
        resistances = sr_context.get("resistances", [])
        # Handles both SRZone dataclass objects (getattr) and plain dicts (.get)
        def _strength(zone) -> float:
            if isinstance(zone, dict):
                return zone.get("strength", 0)
            return getattr(zone, "strength", 0)
        nearest_sup_strength = _strength(supports[0]) if supports else 0
        nearest_res_strength = _strength(resistances[0]) if resistances else 0
    else:
        nearest_sup_strength = 0
        nearest_res_strength = 0

    if direction == "BUY" and nearest_sup_strength >= 5:
        score += 1.5
        reasons.append(f"S/R=soporte_fuerte({nearest_sup_strength:.0f})")
    elif direction == "SELL" and nearest_res_strength >= 5:
        score += 1.5
        reasons.append(f"S/R=resistencia_fuerte({nearest_res_strength:.0f})")

    if in_strong_zone:
        score += 0.5
        reasons.append("strong_zone=✓")

    # ═══ FILTRO: RSI EXTREMOS (hard block) ═══
    rsi_val = indicators.get("rsi", 50.0)
    overbought = sym_cfg.get("rsi_overbought", 70)
    oversold = sym_cfg.get("rsi_oversold", 30)

    if direction == "BUY" and rsi_val > overbought:
        return {"decision": "HOLD", "confidence": 2, "reason": f"RSI={rsi_val:.0f}>OB({overbought})", "score": 0.0}
    if direction == "SELL" and rsi_val < oversold:
        return {"decision": "HOLD", "confidence": 2, "reason": f"RSI={rsi_val:.0f}<OS({oversold})", "score": 0.0}

    # ═══ FILTRO: NOTICIAS / SENTIMIENTO (±1.5) ═══
    if news_context and hasattr(news_context, 'should_pause') and news_context.should_pause:
        return {"decision": "HOLD", "confidence": 1, "reason": "BREAKING_NEWS_PAUSE", "score": 0.0}

    if news_context and hasattr(news_context, 'currency_sentiment'):
        currencies = sym_cfg.get("currencies", ["USD"])
        net_sent = 0.0
        for i, cur in enumerate(currencies):
            s = news_context.currency_sentiment.get(cur, 0.0)
            if i == 0:  # base currency
                net_sent += s
            else:  # quote currency
                net_sent -= s

        if direction == "BUY" and net_sent > 0.30:
            score += 1.0
            reasons.append(f"news={net_sent:+.2f}✓")
        elif direction == "BUY" and net_sent < -0.50:
            score -= 1.5
            reasons.append(f"news={net_sent:+.2f}✗")
        elif direction == "SELL" and net_sent < -0.30:
            score += 1.0
            reasons.append(f"news={net_sent:+.2f}✓")
        elif direction == "SELL" and net_sent > 0.50:
            score -= 1.5
            reasons.append(f"news={net_sent:+.2f}✗")

    # ═══ FILTRO: MACRO (VIX, COT) (±1.0) ═══
    sentiment = get_sentiment_for_symbol(symbol)
    vix = sentiment.get("vix")
    cot_bias = sentiment.get("cot_bias")

    if vix is not None and vix > 30:
        if direction == "BUY" and any(idx in symbol for idx in ["US500", "USTEC", "US30", "DE40"]):
            score -= 1.0
            reasons.append(f"VIX={vix:.0f}>30(pánico)")
        elif "XAU" in symbol and direction == "BUY":
            score += 0.5
            reasons.append(f"VIX={vix:.0f}=safe_haven✓")

    if cot_bias:
        if direction == "BUY" and cot_bias == "BEARISH":
            score -= 0.5
            reasons.append("COT=BEAR✗")
        elif direction == "SELL" and cot_bias == "BULLISH":
            score -= 0.5
            reasons.append("COT=BULL✗")
        elif direction == "BUY" and cot_bias == "BULLISH":
            score += 0.5
            reasons.append("COT=BULL✓")
        elif direction == "SELL" and cot_bias == "BEARISH":
            score += 0.5
            reasons.append("COT=BEAR✓")

    # ═══ DECISIÓN FINAL ═══
    # Apply hurst_penalty if present (set by main.py in SCALPING_ONLY mode)
    hurst_penalty = float(indicators.get("hurst_penalty", 0.0) or 0.0)
    if hurst_penalty > 0:
        score -= hurst_penalty
        reasons.append(f"hurst_pen=-{hurst_penalty:.1f}")

    # min_decision_score: minimum score to open a trade (default 5.0 out of max 10.0).
    # Lower to 4.0 for more aggressive scalping; raise to 6.0 for higher quality signals.
    min_score = sym_cfg.get("min_decision_score", 5.0)

    # Ajuste por calidad de sesión: subir el umbral fuera de sesión óptima
    session_q = _get_session_quality(sym_cfg)
    if session_q <= 0:
        return {"decision": "HOLD", "confidence": 1, "reason": "session_q=0 (dead session)", "score": 0.0}
    if session_q < 1.0:
        min_score = min_score / session_q
        reasons.append(f"session_q={session_q:.1f}")

    confidence = max(1, min(10, int(score)))
    reason_str = " | ".join(reasons)

    if score >= min_score:
        log.info(f"[decision] {symbol} {direction} APROBADO score={score:.1f}/{max_score:.0f} → {reason_str}")
        return {"decision": direction, "confidence": confidence, "reason": reason_str, "score": round(score, 2)}
    else:
        log.info(f"[decision] {symbol} {direction} RECHAZADO score={score:.1f}<{min_score:.1f} → {reason_str}")
        return {"decision": "HOLD", "confidence": confidence, "reason": f"score={score:.1f}<{min_score} | {reason_str}", "score": round(score, 2)}
