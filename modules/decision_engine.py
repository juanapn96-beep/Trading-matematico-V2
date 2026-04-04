"""
ZAR v7 — Decision Engine Determinista
Reemplaza ask_groq() con reglas binarias repetibles.
"""
import logging
from modules.sentiment_data import get_sentiment_for_symbol

log = logging.getLogger(__name__)


def deterministic_decision(
    symbol: str,
    direction: str,           # "BUY" o "SELL" (del mem_direction ya calculado)
    indicators: dict,         # resultado de compute_all()
    sr_context: dict,         # zonas S/R
    news_context,             # NewsContext object
    sym_cfg: dict,
) -> dict:
    """
    Motor de decisión determinista que reemplaza a Groq.

    Returns:
        {"decision": "BUY"|"SELL"|"HOLD", "confidence": int, "reason": str, "score": float}
    """
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
    st_dir = indicators.get("supertrend_dir", "")
    kalman_trend = indicators.get("kalman_trend", "")
    if direction == "BUY":
        both_confirm = (
            (st_dir == "UP" or st_dir == "BUY") and
            ("UP" in str(kalman_trend).upper() or "BUY" in str(kalman_trend).upper() or "BULL" in str(kalman_trend).upper())
        )
    else:
        both_confirm = (
            (st_dir == "DOWN" or st_dir == "SELL") and
            ("DOWN" in str(kalman_trend).upper() or "SELL" in str(kalman_trend).upper() or "BEAR" in str(kalman_trend).upper())
        )

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
    rsi_div = indicators.get("rsi_divergence", "NONE")
    if direction == "BUY" and "BULL" in str(rsi_div).upper():
        score += 0.5
        reasons.append("RSI_div=BULL✓")
    elif direction == "SELL" and "BEAR" in str(rsi_div).upper():
        score += 0.5
        reasons.append("RSI_div=BEAR✓")

    # ═══ PILAR 3: ZONAS S/R (max +2.0) ═══
    nearest_sr = sr_context.get("nearest", {}) if isinstance(sr_context, dict) else {}
    sr_type = nearest_sr.get("type", "")
    sr_strength = nearest_sr.get("strength", 0)
    in_strong_zone = sr_context.get("in_strong_zone", False) if isinstance(sr_context, dict) else False

    if direction == "BUY" and "support" in str(sr_type).lower() and sr_strength >= 5:
        score += 1.5
        reasons.append(f"S/R=soporte_fuerte({sr_strength})")
    elif direction == "SELL" and "resist" in str(sr_type).lower() and sr_strength >= 5:
        score += 1.5
        reasons.append(f"S/R=resistencia_fuerte({sr_strength})")

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
    min_score = sym_cfg.get("min_decision_score", 5.0)
    confidence = max(1, min(10, int(score)))
    reason_str = " | ".join(reasons)

    if score >= min_score:
        log.info(f"[decision] {symbol} {direction} APROBADO score={score:.1f}/{max_score:.0f} → {reason_str}")
        return {"decision": direction, "confidence": confidence, "reason": reason_str, "score": round(score, 2)}
    else:
        log.info(f"[decision] {symbol} {direction} RECHAZADO score={score:.1f}<{min_score:.1f} → {reason_str}")
        return {"decision": "HOLD", "confidence": confidence, "reason": f"score={score:.1f}<{min_score} | {reason_str}", "score": round(score, 2)}
