"""
ZAR v8 — Decision Engine Long-Term H1/H4
Motor de decisión simplificado para swing trading en timeframes altos.

FILOSOFÍA v8.0:
  Menos es más. 5 factores claros con pesos balanceados son mejor que
  45 indicadores que se contradicen entre sí. En H1/H4 las señales son
  más fiables y el spread importa mucho menos que en M1.

SCORING (max 7.0 puntos, umbral de entrada: 4.0):

  PILAR 1 — Confluencia técnica (max +3.0)
    Seis indicadores votan BUY/SELL: SuperTrend, Kalman, MACD,
    DEMA, RSI, Stochastic.
    ≥5/6 votos  →  +3.0  (señal fuerte)
    ≥4/6 votos  →  +2.0  (señal buena)
    ≥3/6 votos  →  +1.0  (señal débil)
    <3/6 votos  →  -1.5  (señal insuficiente)
    Bonus +1.0 si SuperTrend Y Kalman ambos confirman la dirección.

  PILAR 2 — Kalman + Hurst (max +1.5)
    Kalman confirma dirección  →  +0.75
    Hurst > 0.55 (mercado con tendencia clara)  →  +0.75
    Hurst < 0.40 (mercado aleatorio)  →  -1.0

  PILAR 3 — Zonas S/R (max +1.5)
    Soporte/resistencia fuerte cerca (strength ≥ 5)  →  +1.5
    En zona S/R fuerte  →  +0.5

  FILTROS (bloqueo duro, sin puntuar):
    - Spread > max_spread_pips → HOLD
    - RSI overbought en BUY / oversold en SELL → HOLD
    - Noticias de alto impacto activas → HOLD
    - Sesión muerta (session_q == 0) → HOLD

  PENALIZACIONES:
    - Sentimiento de noticias contrario → -1.0
    - Sesión baja calidad (soft) → +0.10 al umbral (nunca explota)
"""
import logging
from datetime import datetime, timezone
from modules.sentiment_data import get_sentiment_for_symbol

log = logging.getLogger(__name__)


def _get_session_quality(sym_cfg: dict) -> float:
    """Retorna el factor de calidad de la sesión actual (0.0-1.0)."""
    session_quality = sym_cfg.get("session_quality")
    if not session_quality:
        return 1.0
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
    direction: str,
    indicators: dict,
    sr_context: dict,
    news_context,
    sym_cfg: dict,
) -> dict:
    """
    Motor de decisión determinista long-term (H1/H4).

    Returns:
        {"decision": "BUY"|"SELL"|"HOLD", "confidence": int, "reason": str, "score": float}
    """
    # ═══ FILTRO DURO 1: SPREAD ═══
    spread_pips = float(indicators.get("spread_pips", 0.0) or 0.0)
    max_spread  = float(sym_cfg.get("max_spread_pips", 3.0))
    if spread_pips > 0 and spread_pips > max_spread:
        return {
            "decision":   "HOLD",
            "confidence": 1,
            "reason":     f"SPREAD={spread_pips:.1f}>{max_spread:.1f}pips",
            "score":      0.0,
        }

    # ═══ FILTRO DURO 2: RSI EXTREMOS ═══
    rsi_val    = float(indicators.get("rsi", 50.0) or 50.0)
    overbought = sym_cfg.get("rsi_overbought", 70)
    oversold   = sym_cfg.get("rsi_oversold", 30)
    if direction == "BUY" and rsi_val > overbought:
        return {"decision": "HOLD", "confidence": 2,
                "reason": f"RSI={rsi_val:.0f}>OB({overbought})", "score": 0.0}
    if direction == "SELL" and rsi_val < oversold:
        return {"decision": "HOLD", "confidence": 2,
                "reason": f"RSI={rsi_val:.0f}<OS({oversold})", "score": 0.0}

    # ═══ FILTRO DURO 3: NOTICIAS DE ALTO IMPACTO ═══
    if news_context and hasattr(news_context, "should_pause") and news_context.should_pause:
        return {"decision": "HOLD", "confidence": 1,
                "reason": "BREAKING_NEWS_PAUSE", "score": 0.0}

    score   = 0.0
    reasons = []

    # ═══ PILAR 1: CONFLUENCIA TÉCNICA (max +3.0, bonus +1.0) ═══
    trend_votes = indicators.get("trend_votes", {})
    bull_votes  = trend_votes.get("bull", 0)
    bear_votes  = trend_votes.get("bear", 0)
    vote_score  = bull_votes if direction == "BUY" else bear_votes

    if vote_score >= 5:
        score += 3.0
        reasons.append(f"votes:{vote_score}/6=FUERTE")
    elif vote_score >= 4:
        score += 2.0
        reasons.append(f"votes:{vote_score}/6=OK")
    elif vote_score >= 3:
        score += 1.0
        reasons.append(f"votes:{vote_score}/6=DÉBIL")
    else:
        score -= 1.5
        reasons.append(f"votes:{vote_score}/6=INSUF")

    # Bonus si SuperTrend Y Kalman confirman juntos
    st_val       = indicators.get("supertrend", 0)
    kalman_trend = indicators.get("kalman_trend", "")
    if direction == "BUY":
        both_confirm = (st_val == 1) and (kalman_trend == "ALCISTA")
    else:
        both_confirm = (st_val == -1) and (kalman_trend == "BAJISTA")

    if both_confirm:
        score += 1.0
        reasons.append("ST+Kalman=OK")

    # ═══ PILAR 2: KALMAN + HURST (max +1.5) ═══
    # Kalman direction
    if direction == "BUY" and kalman_trend == "ALCISTA":
        score += 0.75
        reasons.append("Kalman=ALCISTA✓")
    elif direction == "SELL" and kalman_trend == "BAJISTA":
        score += 0.75
        reasons.append("Kalman=BAJISTA✓")
    else:
        reasons.append(f"Kalman={kalman_trend}✗")

    # Hurst — régimen de mercado
    hurst_val = float(indicators.get("hurst", 0.5) or 0.5)
    if hurst_val > 0.55:
        score += 0.75
        reasons.append(f"Hurst={hurst_val:.2f}(trend✓)")
    elif hurst_val < 0.40:
        score -= 1.0
        reasons.append(f"Hurst={hurst_val:.2f}(aleatorio✗)")
    else:
        reasons.append(f"Hurst={hurst_val:.2f}(neutro)")

    # ═══ PILAR 3: ZONAS S/R (max +2.0) ═══
    if isinstance(sr_context, dict):
        supports    = sr_context.get("supports", [])
        resistances = sr_context.get("resistances", [])

        def _strength(zone) -> float:
            if isinstance(zone, dict):
                return zone.get("strength", 0)
            return getattr(zone, "strength", 0)

        nearest_sup_str = _strength(supports[0])    if supports    else 0
        nearest_res_str = _strength(resistances[0]) if resistances else 0
        in_strong_zone  = sr_context.get("in_strong_zone", False)
    else:
        nearest_sup_str = 0
        nearest_res_str = 0
        in_strong_zone  = False

    if direction == "BUY" and nearest_sup_str >= 5:
        score += 1.5
        reasons.append(f"S/R=soporte({nearest_sup_str:.0f})")
    elif direction == "SELL" and nearest_res_str >= 5:
        score += 1.5
        reasons.append(f"S/R=resistencia({nearest_res_str:.0f})")

    if in_strong_zone:
        score += 0.5
        reasons.append("strong_zone✓")

    # ═══ SENTIMIENTO DE NOTICIAS (penalización si es contrario) ═══
    if news_context and hasattr(news_context, "currency_sentiment"):
        currencies = sym_cfg.get("currencies", ["USD"])
        net_sent   = 0.0
        for i, cur in enumerate(currencies):
            s = news_context.currency_sentiment.get(cur, 0.0)
            net_sent += s if i == 0 else -s

        if direction == "BUY" and net_sent > 0.30:
            score += 0.5
            reasons.append(f"news={net_sent:+.2f}✓")
        elif direction == "BUY" and net_sent < -0.50:
            score -= 1.0
            reasons.append(f"news={net_sent:+.2f}✗")
        elif direction == "SELL" and net_sent < -0.30:
            score += 0.5
            reasons.append(f"news={net_sent:+.2f}✓")
        elif direction == "SELL" and net_sent > 0.50:
            score -= 1.0
            reasons.append(f"news={net_sent:+.2f}✗")

    # ═══ MACRO: VIX ═══
    sentiment = get_sentiment_for_symbol(symbol)
    vix       = sentiment.get("vix")
    if vix is not None and vix > 30:
        if direction == "BUY" and any(idx in symbol for idx in ["US500", "USTEC", "US30", "DE40"]):
            score -= 1.0
            reasons.append(f"VIX={vix:.0f}>30(pánico)")
        elif "XAU" in symbol and direction == "BUY":
            score += 0.5
            reasons.append(f"VIX={vix:.0f}=safe_haven✓")

    # ═══ PENALIZACIÓN HURST EXPLÍCITA (de main.py scalping mode) ═══
    hurst_penalty = float(indicators.get("hurst_penalty", 0.0) or 0.0)
    if hurst_penalty > 0:
        score -= hurst_penalty
        reasons.append(f"hurst_pen=-{hurst_penalty:.1f}")

    # ═══ DECISIÓN FINAL ═══
    min_score = float(sym_cfg.get("min_decision_score", 4.0))

    # Calidad de sesión: solo penalización leve (+0.10 al umbral máximo)
    session_q = _get_session_quality(sym_cfg)
    if session_q <= 0:
        return {"decision": "HOLD", "confidence": 1,
                "reason": "session_q=0 (sesión muerta)", "score": 0.0}
    if session_q < 1.0:
        session_penalty = round((1.0 - session_q) * 0.20, 2)   # Máximo +0.14 al umbral
        min_score += session_penalty
        reasons.append(f"session_q={session_q:.1f}(+{session_penalty:.2f})")

    confidence  = max(1, min(10, int(score)))
    reason_str  = " | ".join(reasons)
    score_round = round(score, 2)

    if score >= min_score:
        log.info(
            "[decision] %s %s APROBADO score=%.1f/7.0 → %s",
            symbol, direction, score, reason_str,
        )
        return {"decision": direction, "confidence": confidence,
                "reason": reason_str, "score": score_round}

    log.info(
        "[decision] %s %s RECHAZADO score=%.1f<%.1f → %s",
        symbol, direction, score, min_score, reason_str,
    )
    return {"decision": "HOLD", "confidence": confidence,
            "reason": f"score={score_round:.1f}<{min_score:.1f} | {reason_str}",
            "score": score_round}
