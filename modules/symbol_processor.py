"""
ZAR v7 — Symbol Processor module
Extracted from main.py: per-symbol processing logic, decision engine integration.

Contains:
    _compute_trade_plan, _get_entry_min_rr, _should_allow_low_hurst_scalp
    _evaluate_strategy_specific_gate, _is_decision_candidate_ready
    _apply_confluence_gate_pre, _execute_decision
    _passes_direction_filter, ask_decision_engine
    process_symbol (renamed from _process_symbol)
    Helper functions: bump_decision_metric, set_last_decision_analysis,
                      get_last_candle_stamp, build_decision_cache_key
"""
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import MetaTrader5 as mt5
import config as cfg

from modules.bot_state import state
from modules.execution import (
    price_distance_to_pips, open_order,
    get_candles, get_open_positions_count_realtime, TF_CANDLES,
)
from modules.trailing_manager import (
    trail_stage, profit_candle_count, profit_candle_last_seen, init_ticket,
)
from modules.context_builder import build_context, build_lateral_context
from modules.risk_manager import (
    calc_sl_tp, is_rr_valid, get_rr, is_market_tradeable,
)
from modules.decision_engine import deterministic_decision
from modules.sr_zones import build_sr_context
from modules.neural_brain import (
    build_features, check_memory, get_memory_stats,
    derive_setup_id, derive_session_from_ind, evaluate_scorecard,
    evaluate_policy, get_adaptive_trail_params, save_trade,
)
from modules.portfolio_risk import get_effective_portfolio_risk
from modules.risk_manager import get_lot_size_kelly
from modules.telegram_notifier import (
    notify_trade_opened, notify_news_pause, notify_memory_block,
    _send as telegram_send,
)
from modules.news_engine import (
    build_shared_news_context, derive_symbol_news_context,
)
from modules.economic_calendar import calendar as eco_calendar

log = logging.getLogger(__name__)

# ── Module-level constants ───────────────────────────────────────
SYMBOL_COOLDOWN_SEC = getattr(cfg, "SYMBOL_COOLDOWN_SEC", 300)
H1_CACHE_TTL_SEC    = 300  # 5 minutes
NOTIF_COOLDOWN_SEC  = 1800


# ════════════════════════════════════════════════════════════════
#  HELPER FUNCTIONS
# ════════════════════════════════════════════════════════════════

def set_symbol_status(symbol: str, status: str) -> None:
    state.symbol_status_cache[symbol] = status


def set_last_decision_analysis(symbol: str, analysis: Optional[dict]) -> None:
    if not analysis:
        return
    with state.lock:
        state.last_decision_analysis = {
            "symbol":     symbol,
            "timestamp":  datetime.now(timezone.utc).isoformat(),
            "decision":   analysis.get("decision", "HOLD"),
            "confidence": analysis.get("confidence", 0),
            "reason":     analysis.get("reason", ""),
            "key_signals": analysis.get("key_signals", []),
            "main_risk":  analysis.get("main_risk", ""),
        }


def bump_decision_metric(metric: str) -> None:
    now      = datetime.now(timezone.utc)
    hour_key = now.strftime("%Y-%m-%d %H")
    day_key  = now.strftime("%Y-%m-%d")
    with state.lock:
        state.decision_usage_stats[metric] = int(state.decision_usage_stats.get(metric, 0)) + 1
        if metric == "api_calls_total":
            state.decision_hourly_usage[hour_key] = int(state.decision_hourly_usage.get(hour_key, 0)) + 1
            state.decision_daily_usage[day_key]   = int(state.decision_daily_usage.get(day_key, 0)) + 1
            while len(state.decision_hourly_usage) > 72:
                oldest = sorted(state.decision_hourly_usage.keys())[0]
                state.decision_hourly_usage.pop(oldest, None)
            while len(state.decision_daily_usage) > 14:
                oldest = sorted(state.decision_daily_usage.keys())[0]
                state.decision_daily_usage.pop(oldest, None)


def get_last_candle_stamp(df: pd.DataFrame) -> str:
    if df is None or len(df) == 0:
        return ""
    ts = pd.Timestamp(df["time"].iloc[-1]).to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


def build_decision_cache_key(symbol: str, direction_hint: str, candle_stamp: str, ind: dict) -> str:
    conf_total     = round(float(ind.get("confluence", {}).get("total", 0.0) or 0.0), 2)
    h1_trend       = ind.get("h1_trend", "LATERAL")
    hilbert_signal = ind.get("hilbert", {}).get("signal", "NEUTRAL")
    return "|".join([
        symbol,
        direction_hint,
        candle_stamp,
        h1_trend,
        hilbert_signal,
        f"{conf_total:.2f}",
    ])


# ════════════════════════════════════════════════════════════════
#  NOTIFICATION HELPERS (use state.xxx for cooldowns)
# ════════════════════════════════════════════════════════════════

def _notify_calendar_pause_once(symbol: str, event, sym_cfg: dict) -> None:
    from modules.economic_calendar import CalendarEvent
    if isinstance(event, CalendarEvent) and event.event_id not in eco_calendar._notified_events:
        telegram_send(eco_calendar.format_for_telegram(symbol, event))
        eco_calendar.mark_notified(event)


def _notify_news_pause_once(symbol: str, reason: str, duration_min: int) -> None:
    now_ts = time.time()
    if now_ts - state.news_pause_notified.get(symbol, 0) >= NOTIF_COOLDOWN_SEC:
        notify_news_pause(symbol, reason, duration_min)
        state.news_pause_notified[symbol] = now_ts


def _build_memory_block_fingerprint(mem_check) -> str:
    regime      = str(getattr(mem_check, "regime", "") or "").strip().upper() or "UNKNOWN"
    warning_msg = str(getattr(mem_check, "warning_msg", "") or "").strip()
    normalized  = warning_msg.lower()
    if "modo calentamiento" in normalized:
        if "caótico" in normalized or "chaotic" in normalized:
            return f"WARMUP_CHAOTIC|{regime}"
        return f"WARMUP|{regime}"
    normalized = re.sub(r"\(\s*\d+\s*/\s*\d+\s*trades?\s*\)", "(n/trades)", normalized)
    normalized = re.sub(r"\b\d+(?:\.\d+)?%?\b", "#", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" |")
    return f"{regime}|{normalized[:160]}"


def _should_silence_memory_block_notification(mem_check) -> bool:
    similar_losses = int(getattr(mem_check, "similar_losses", 0) or 0)
    if similar_losses > 0:
        return False
    regime      = str(getattr(mem_check, "regime", "") or "").strip().upper()
    warning_msg = str(getattr(mem_check, "warning_msg", "") or "").strip().lower()
    warmup_mode = bool(getattr(mem_check, "warmup_mode", False))
    return warmup_mode or regime == "CHAOTIC" or "modo calentamiento" in warning_msg


def _notify_memory_block_once(symbol: str, mem_check) -> None:
    if _should_silence_memory_block_notification(mem_check):
        return
    now_ts       = time.time()
    cooldown_sec = max(
        int(getattr(cfg, "MEMORY_BLOCK_NOTIFY_COOLDOWN_SEC", NOTIF_COOLDOWN_SEC) or NOTIF_COOLDOWN_SEC),
        NOTIF_COOLDOWN_SEC,
    )
    previous_state     = state.memory_block_notified.get(symbol, {})
    if isinstance(previous_state, dict):
        last_ts          = float(previous_state.get("ts", 0) or 0.0)
        last_fingerprint = str(previous_state.get("fingerprint", "") or "")
    else:
        last_ts          = float(previous_state or 0.0)
        last_fingerprint = ""

    current_fingerprint = _build_memory_block_fingerprint(mem_check)
    should_notify = (
        current_fingerprint != last_fingerprint
        or (now_ts - last_ts) >= cooldown_sec
    )
    if should_notify:
        notify_memory_block(symbol, "BUY/SELL", mem_check.similar_losses, mem_check.warning_msg)
        state.memory_block_notified[symbol] = {
            "ts":          now_ts,
            "fingerprint": current_fingerprint,
        }


def _notify_equity_guard_once(
    symbol: str, equity: float, equity_floor: float, min_pct: float
) -> None:
    now_ts = time.time()
    if now_ts - state.equity_guard_notified.get(symbol, 0) >= NOTIF_COOLDOWN_SEC:
        telegram_send(
            "\n".join([
                f"🛡 <b>EQUITY GUARD ACTIVADO — {symbol}</b>",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                f"📉 Equity actual: <code>${equity:,.2f}</code>",
                f"🧱 Piso requerido: <code>${equity_floor:,.2f}</code> ({min_pct:.0f}% balance)",
                "⛔ Nuevas entradas bloqueadas temporalmente para este símbolo.",
                f"⏰ <code>{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC</code>",
            ])
        )
        state.equity_guard_notified[symbol] = now_ts


# ════════════════════════════════════════════════════════════════
#  DECISION ENGINE WRAPPER
# ════════════════════════════════════════════════════════════════

def ask_decision_engine(symbol: str, sym_cfg: dict, cache_key: str = "") -> Optional[dict]:
    """Motor de decisión determinista con cache por vela/señal."""
    if cache_key and cache_key in state.decision_call_cache:
        bump_decision_metric("cache_hits_total")
        return state.decision_call_cache[cache_key]

    sym_state  = state._symbol_state.get(symbol, {})
    direction  = sym_state.get("mem_direction", "")
    indicators = sym_state.get("last_indicators", {})
    sr_context = sym_state.get("last_sr_context", {})
    news_ctx   = sym_state.get("last_news_context", None)

    if not direction:
        payload = {"decision": "HOLD", "confidence": 1, "reason": "Sin dirección"}
        if cache_key:
            state.decision_call_cache[cache_key] = payload
        return payload

    bump_decision_metric("api_calls_total")
    result = deterministic_decision(
        symbol=symbol,
        direction=direction,
        indicators=indicators,
        sr_context=sr_context,
        news_context=news_ctx,
        sym_cfg=sym_cfg,
    )
    payload = {
        "decision":   result["decision"],
        "confidence": result["confidence"],
        "reason":     result.get("reason", ""),
        "score":      result.get("score", 0.0),
        "key_signals": [],
        "main_risk":  "",
    }
    set_last_decision_analysis(symbol, payload)
    bump_decision_metric("api_success_total")
    if cache_key:
        state.decision_call_cache[cache_key] = payload
    return payload


# ════════════════════════════════════════════════════════════════
#  TRADE PLAN & GATES
# ════════════════════════════════════════════════════════════════

def _compute_trade_plan(
    symbol: str, action: str, ind: dict, sym_cfg: dict
) -> Optional[dict]:
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    price = tick.ask if action == "BUY" else tick.bid
    sl, tp = calc_sl_tp(action, price, ind["atr"], sym_cfg)
    rr = get_rr(price, sl, tp)
    return {"action": action, "price": price, "sl": sl, "tp": tp, "rr": rr}


def _get_entry_min_rr(sym_cfg: dict) -> float:
    return float(
        sym_cfg.get("min_rr", getattr(cfg, "ENTRY_MIN_RR", 1.20))
        or getattr(cfg, "ENTRY_MIN_RR", 1.20)
    )


def _should_allow_low_hurst_scalp(ind: dict, sr_ctx, sym_cfg: dict) -> tuple:
    hurst_val = float(ind.get("hurst", 0.5) or 0.5)
    min_hurst = float(sym_cfg.get("min_hurst", 0.40) or 0.40)
    if hurst_val >= min_hurst:
        return True, ""

    if not bool(getattr(cfg, "SCALPING_ALLOW_LOW_HURST", True)):
        return False, f"Hurst {hurst_val:.3f} < {min_hurst:.3f}"

    hard_floor   = float(getattr(cfg, "SCALPING_HURST_HARD_FLOOR", 0.18) or 0.18)
    soft_margin  = float(getattr(cfg, "SCALPING_HURST_SOFT_MARGIN", 0.20) or 0.20)
    soft_floor   = max(hard_floor, min_hurst - soft_margin)
    if hurst_val < soft_floor:
        return False, f"Hurst {hurst_val:.3f} < piso scalp {soft_floor:.3f}"

    enhanced_regime = str(ind.get("enhanced_regime", "UNKNOWN") or "UNKNOWN")
    zscore_signal   = str(ind.get("zscore_returns", {}).get("signal", "NEUTRAL") or "NEUTRAL")
    in_strong_zone  = bool(getattr(sr_ctx, "in_strong_zone", False))

    reasons = [f"zona gris {hurst_val:.2f}/{min_hurst:.2f}"]
    if enhanced_regime != "UNKNOWN":
        reasons.append(enhanced_regime)
    if in_strong_zone:
        reasons.append("SR fuerte")
    if zscore_signal != "NEUTRAL":
        reasons.append(f"z={zscore_signal}")
    return True, " | ".join(reasons)


def _evaluate_strategy_specific_gate(
    action: str, ind: dict, sr_ctx, sym_cfg: dict
) -> tuple:
    strategy_type = str(sym_cfg.get("strategy_type", "HYBRID") or "HYBRID")
    rsi           = float(ind.get("rsi", 50.0) or 50.0)
    fisher        = float(ind.get("fisher", 0.0) or 0.0)
    macd_dir      = ind.get("macd_dir", "NEUTRAL")
    ha_trend      = ind.get("ha_trend", "NEUTRAL")
    kalman_trend  = ind.get("kalman_trend", "NEUTRAL")
    supertrend    = int(ind.get("supertrend", 0) or 0)
    hilbert_signal = ind.get("hilbert", {}).get("signal", "NEUTRAL")
    conf_total    = float(ind.get("confluence", {}).get("total", 0.0) or 0.0)
    in_strong_zone = bool(getattr(sr_ctx, "in_strong_zone", False))

    bullish_primaries = sum([
        macd_dir == "ALCISTA",
        ha_trend == "ALCISTA",
        kalman_trend == "ALCISTA",
        supertrend == 1,
    ])
    bearish_primaries = sum([
        macd_dir == "BAJISTA",
        ha_trend == "BAJISTA",
        kalman_trend == "BAJISTA",
        supertrend == -1,
    ])

    if strategy_type in {
        "VOLATILITY_CYCLE", "CYCLE_REVERSION",
        "GOLD_BETA_REVERSION", "CRYPTO_WAVE",
    }:
        if action == "BUY":
            if hilbert_signal == "LOCAL_MAX":
                return False, f"{strategy_type}: Hilbert en techo"
            if rsi >= float(sym_cfg.get("rsi_overbought", 70)) and not in_strong_zone:
                return False, f"{strategy_type}: RSI demasiado alto sin S/R fuerte"
            if fisher > 2.5 and not in_strong_zone:
                return False, f"{strategy_type}: Fisher extremo contrario"
        else:
            if hilbert_signal == "LOCAL_MIN":
                return False, f"{strategy_type}: Hilbert en suelo"
            if rsi <= float(sym_cfg.get("rsi_oversold", 30)) and not in_strong_zone:
                return False, f"{strategy_type}: RSI demasiado bajo sin S/R fuerte"
            if fisher < -2.5 and not in_strong_zone:
                return False, f"{strategy_type}: Fisher extremo contrario"
        return True, ""

    if strategy_type in {
        "MOMENTUM_TREND", "MOMENTUM_SURGE", "TECH_MOMENTUM",
        "FRANKFURT_BREAKOUT", "RANGE_BREAKOUT_OIL",
    }:
        primaries = bullish_primaries if action == "BUY" else bearish_primaries
        if primaries < 3:
            return False, f"{strategy_type}: primarios alineados insuficientes ({primaries}/4)"
        if abs(conf_total) < float(getattr(cfg, "CONFLUENCE_MIN_SCORE", 0.3)) * 1.2:
            return False, f"{strategy_type}: confluencia insuficiente ({conf_total:+.2f})"
        return True, ""

    if strategy_type in {"TREND_KALMAN", "RISK_CARRY"}:
        if action == "BUY" and not (kalman_trend == "ALCISTA" and supertrend == 1):
            return False, f"{strategy_type}: Kalman/SuperTrend no confirman BUY"
        if action == "SELL" and not (kalman_trend == "BAJISTA" and supertrend == -1):
            return False, f"{strategy_type}: Kalman/SuperTrend no confirman SELL"
        return True, ""

    if strategy_type == "DRAGON_EXPLOSION":
        primaries = bullish_primaries if action == "BUY" else bearish_primaries
        if primaries < 4:
            return False, f"{strategy_type}: requiere 4/4 primarios alineados"
        if abs(conf_total) < 0.8:
            return False, f"{strategy_type}: confluencia no sniper ({conf_total:+.2f})"
        return True, ""

    return True, ""


def _passes_direction_filter(action: str, ind: dict) -> tuple:
    """
    FIX 14: Bloquea trades cuando los indicadores primarios contradicen la dirección.
    """
    kalman_trend  = ind.get("kalman_trend", "NEUTRAL")
    supertrend    = ind.get("supertrend", 0)
    ha_trend      = ind.get("ha_trend", "NEUTRAL")
    macd_dir      = ind.get("macd_dir", "NEUTRAL")

    if action == "BUY":
        kalman_ok     = kalman_trend == "ALCISTA"
        supertrend_ok = supertrend == 1
        ha_ok         = ha_trend == "ALCISTA"
        macd_ok       = macd_dir == "ALCISTA"
    else:
        kalman_ok     = kalman_trend == "BAJISTA"
        supertrend_ok = supertrend == -1
        ha_ok         = ha_trend == "BAJISTA"
        macd_ok       = macd_dir == "BAJISTA"

    if not kalman_ok and not supertrend_ok:
        return False, f"Kalman+SuperTrend contradicen {action} → HOLD forzado"

    if bool(getattr(cfg, "SCALPING_ONLY", False)):
        return True, ""

    contradictions = sum([not kalman_ok, not supertrend_ok, not ha_ok, not macd_ok])
    if contradictions >= 3:
        return False, f"{contradictions}/4 indicadores primarios contradicen {action} → HOLD"

    return True, ""


def _is_decision_candidate_ready(
    symbol: str, action: str, ind: dict, sr_ctx, sym_cfg: dict
) -> tuple:
    plan = _compute_trade_plan(symbol, action, ind, sym_cfg)
    if plan is None:
        return False, "⚠️ Tick no disponible", None

    if bool(getattr(cfg, "SCALPING_ONLY", False)):
        scalp_tp_mult = float(getattr(cfg, "SCALPING_TP_MULT", 0.98) or 0.98)
        scalp_tp_mult = max(0.2, min(1.0, scalp_tp_mult))
        price = plan["price"]
        if action == "BUY":
            plan["tp"] = round(price + ((plan["tp"] - price) * scalp_tp_mult), 5)
        else:
            plan["tp"] = round(price - ((price - plan["tp"]) * scalp_tp_mult), 5)
        plan["rr"] = get_rr(price, plan["sl"], plan["tp"])

    min_rr = _get_entry_min_rr(sym_cfg)
    if not is_rr_valid(plan["price"], plan["sl"], plan["tp"], min_rr=min_rr):
        return False, f"⚖️ R:R inválido ({plan['rr']:.2f})", plan

    passes_filter, filter_reason = _passes_direction_filter(action, ind)
    if not passes_filter:
        return False, f"🚫 {filter_reason}", plan

    conf            = ind.get("confluence", {})
    conf_total      = float(conf.get("total", 0.0) or 0.0)
    conf_min        = float(getattr(cfg, "CONFLUENCE_MIN_SCORE", 0.3))
    sniper_aligned  = bool(conf.get("sniper_aligned", False))
    in_strong_zone  = bool(getattr(sr_ctx, "in_strong_zone", False))
    trend           = ind.get("h1_trend", "LATERAL")
    votes           = ind.get("trend_votes", {"bull": 0, "bear": 0})
    vote_edge       = (
        (votes.get("bull", 0) - votes.get("bear", 0)) if action == "BUY"
        else (votes.get("bear", 0) - votes.get("bull", 0))
    )
    hilbert_signal  = ind.get("hilbert", {}).get("signal", "NEUTRAL")

    if action == "BUY":
        conf_ok  = conf_total >= conf_min
        cycle_ok = hilbert_signal != "LOCAL_MAX"
        trend_ok = ("ALCISTA" in trend) or vote_edge >= 2
    else:
        conf_ok  = conf_total <= -conf_min
        cycle_ok = hilbert_signal != "LOCAL_MIN"
        trend_ok = ("BAJISTA" in trend) or vote_edge >= 2

    if not conf_ok:
        return False, (
            f"🔒 Confluencia insuficiente {action}: "
            f"conf={conf_total:+.2f} (min={'+'if action=='BUY' else '-'}{conf_min:.2f})"
        ), plan

    support_ok    = sniper_aligned or in_strong_zone or abs(conf_total) >= (conf_min * 1.8)
    quality_score = sum([cycle_ok, trend_ok, support_ok])
    min_quality   = max(2, int(getattr(cfg, "DECISION_MIN_ENTRY_QUALITY", 3) or 3))

    if quality_score < min_quality:
        return False, (
            f"🧪 Setup débil {action}: q={quality_score}/{min_quality} "
            f"conf={conf_total:+.2f} votes={vote_edge:+d}"
        ), plan

    strategy_ok, strategy_reason = _evaluate_strategy_specific_gate(action, ind, sr_ctx, sym_cfg)
    if not strategy_ok:
        return False, f"🧭 {strategy_reason}", plan

    strong_entry_required = bool(getattr(cfg, "DECISION_ENTRY_STRONG_ONLY", True))
    premium_conf_mult     = float(getattr(cfg, "DECISION_ENTRY_CONF_MULT", 2.0) or 2.0)
    premium_conf_ok       = abs(conf_total) >= (conf_min * premium_conf_mult)
    hilbert_extreme_ok    = (
        (action == "BUY"  and hilbert_signal == "LOCAL_MIN")
        or (action == "SELL" and hilbert_signal == "LOCAL_MAX")
    )
    if strong_entry_required and not (
        sniper_aligned or in_strong_zone or premium_conf_ok or hilbert_extreme_ok
    ):
        return False, (
            f"⏱️ Entrada aún no es inminente {action}: "
            f"sin zona fuerte ni confluencia premium ({conf_total:+.2f})"
        ), plan

    return True, "", plan


def _apply_confluence_gate_pre(symbol: str, ind: dict, direction: str) -> bool:
    """
    FASE 3 — Confluence Hard Gate (pre-decisión).
    Retorna True si se debe bloquear.
    """
    conf_total  = ind.get("confluence", {}).get("total", 0.0)
    hard_thresh = (
        getattr(cfg, "CONFLUENCE_MIN_SCORE", 0.3)
        * getattr(cfg, "CONFLUENCE_HARD_GATE_MULT", 2)
    )

    if direction == "BUY" and conf_total < -hard_thresh:
        msg = f"⚡ Conf veta BUY ({conf_total:+.2f}) — pilares bajistas"
        set_symbol_status(symbol, msg[:48])
        state.last_action = f"{msg} {symbol}"
        log.info(f"[{symbol}] {msg} — decisión no procesada")
        return True

    if direction == "SELL" and conf_total > hard_thresh:
        msg = f"⚡ Conf veta SELL ({conf_total:+.2f}) — pilares alcistas"
        set_symbol_status(symbol, msg[:48])
        state.last_action = f"{msg} {symbol}"
        log.info(f"[{symbol}] {msg} — decisión no procesada")
        return True

    return False


# ════════════════════════════════════════════════════════════════
#  EXECUTE DECISION
# ════════════════════════════════════════════════════════════════

def _execute_decision(
    symbol, sym_cfg, decision, ind, sr_ctx, news_ctx,
    mem_check, features, balance, df_entry,
    candidate_payloads=None,
) -> None:
    """Valida la decisión del motor determinista y aplica filtros de seguridad."""
    if decision is None:
        log.warning(f"[{symbol}] motor de decisión sin respuesta")
        set_symbol_status(symbol, "🤖 motor sin respuesta")
        return

    action     = decision.get("decision", "HOLD")
    confidence = int(decision.get("confidence", 0))
    reason     = decision.get("reason", "")

    if action in ("BUY", "SELL") and candidate_payloads is not None:
        payload = candidate_payloads.get(action)
        if payload is None or not payload.get("candidate_ok", False):
            msg = f"🧪 Candidato {action} no pasó gates internos"
            set_symbol_status(symbol, msg[:48])
            state.last_action = f"{msg} {symbol}"
            log.info(f"[{symbol}] {msg} — decisión forzada a HOLD")
            return

    if candidate_payloads and action in candidate_payloads:
        payload   = candidate_payloads[action]
        mem_check = payload["mem_check"]
        features  = payload["features"]

    log.info(f"[{symbol}] 🤖 {action} conf={confidence} | {reason[:80]}")

    if action in ("BUY", "SELL"):
        setup_id   = derive_setup_id(ind, action)
        session_id = derive_session_from_ind(ind)
        scorecard  = evaluate_scorecard(
            symbol=symbol, setup_id=setup_id,
            session=session_id, regime=mem_check.regime,
        )
        policy = evaluate_policy(
            symbol=symbol, direction=action,
            setup_id=setup_id, session=session_id, regime=mem_check.regime,
        )
        if scorecard.should_block:
            msg = (
                f"🧮 Scorecard vetó {action} "
                f"(WR={scorecard.win_rate:.1f}% n={scorecard.sample_size})"
            )
            state.last_action = f"{msg} {symbol}"
            set_symbol_status(symbol, msg[:48])
            log.info(f"[{symbol}] {msg} | {scorecard.reason}")
            return
        if policy.should_block:
            msg = (
                f"📉 Policy vetó {action} "
                f"(score={policy.policy_score:.3f} n={policy.sample_size})"
            )
            state.last_action = f"{msg} {symbol}"
            set_symbol_status(symbol, msg[:48])
            log.info(f"[{symbol}] {msg} | {policy.reason}")
            return
    else:
        scorecard = None
        policy    = None

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
        state.last_action = f"HOLD {symbol} conf={confidence} (min={min_conf})"
        set_symbol_status(symbol, f"🤖 {action} conf={confidence}/{min_conf}")
        return

    passes_filter, filter_reason = _passes_direction_filter(action, ind)
    if not passes_filter:
        log.info(f"[{symbol}] 🚫 Anti-contratendencia: {filter_reason}")
        state.last_action = f"🚫 {filter_reason[:60]}"
        set_symbol_status(symbol, f"🚫 {filter_reason[:48]}")
        return

    conf_total  = ind.get("confluence", {}).get("total", 0.0)
    conf_thresh = (
        getattr(cfg, "CONFLUENCE_MIN_SCORE", 0.3)
        * getattr(cfg, "CONFLUENCE_HARD_GATE_MULT", 2)
    )
    if action == "BUY" and conf_total < -conf_thresh:
        msg = f"⚡ Conf veta BUY post-decisión ({conf_total:+.2f})"
        log.info(f"[{symbol}] {msg}")
        state.last_action = f"{msg} {symbol}"
        set_symbol_status(symbol, msg[:48])
        return
    if action == "SELL" and conf_total > conf_thresh:
        msg = f"⚡ Conf veta SELL post-decisión ({conf_total:+.2f})"
        log.info(f"[{symbol}] {msg}")
        state.last_action = f"{msg} {symbol}"
        set_symbol_status(symbol, msg[:48])
        return

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        set_symbol_status(symbol, "⚠️ Tick no disponible")
        return
    price     = tick.ask if action == "BUY" else tick.bid
    min_hurst = float(sym_cfg.get("min_hurst", 0.40) or 0.40)
    hurst_val = float(ind.get("hurst", 0.5) or 0.5)
    is_scalp_trade = bool(getattr(cfg, "SCALPING_ONLY", False)) or bool(hurst_val < min_hurst)

    if is_scalp_trade:
        atr_entry    = ind.get("atr_entry", ind["atr"])
        scalp_sl_mult = float(getattr(cfg, "SCALPING_SL_ATR_MULT", 3.0))
        scalp_tp_mult = float(getattr(cfg, "SCALPING_TP_ATR_MULT", 6.0))
        if action == "BUY":
            sl = round(price - scalp_sl_mult * atr_entry, 5)
            tp = round(price + scalp_tp_mult * atr_entry, 5)
        else:
            sl = round(price + scalp_sl_mult * atr_entry, 5)
            tp = round(price - scalp_tp_mult * atr_entry, 5)
    else:
        sl, tp = calc_sl_tp(action, price, ind["atr"], sym_cfg)

    rr     = get_rr(price, sl, tp)
    min_rr = _get_entry_min_rr(sym_cfg)
    if not is_rr_valid(price, sl, tp, min_rr=min_rr):
        state.last_action = f"R:R inválido {symbol} ({rr:.2f})"
        set_symbol_status(symbol, f"⚖️ R:R inválido ({rr:.2f})")
        return

    all_positions = mt5.positions_get()
    if all_positions is None:
        all_positions = []
    portfolio_risk_pct, risk_blocked, risk_reason = get_effective_portfolio_risk(
        open_positions=all_positions,
        new_symbol=symbol,
        new_direction=action,
    )
    if risk_blocked:
        msg = f"🔗 Riesgo portafolio {portfolio_risk_pct:.1f}%"
        state.last_action = f"{msg} {symbol}"
        set_symbol_status(symbol, msg[:48])
        log.info(f"[{symbol}] {risk_reason}")
        return

    sym_info = mt5.symbol_info(symbol)
    point    = sym_info.point if sym_info else 0.00001
    sl_pips  = abs(price - sl) / point

    mem_stats      = get_memory_stats(symbol)
    sym_trades     = mem_stats.get("total", 0)
    win_rate_frac  = mem_stats.get("win_rate", 0.0) / 100.0
    kelly_min_trades = getattr(cfg, "KELLY_MIN_TRADES", 30)
    kelly_active   = sym_trades >= kelly_min_trades and win_rate_frac > 0
    vol = get_lot_size_kelly(
        balance=balance,
        sl_pips=sl_pips,
        symbol=symbol,
        win_rate=win_rate_frac,
        avg_rr=rr,
        n_trades=sym_trades,
    )

    if getattr(mem_check, "warmup_mode", False):
        warmup_factor = float(getattr(cfg, "WARMUP_LOT_FACTOR", 0.5))
        original_vol  = vol
        vol = max(0.01, round(vol * warmup_factor, 2))
        log.info(
            f"[warmup] {symbol}: lote reducido {original_vol} → {vol} "
            f"(factor={warmup_factor}, trades={sym_trades})"
        )

    min_profit_usd = float(getattr(cfg, "MIN_EXPECTED_PROFIT_USD", 5.0))
    if min_profit_usd > 0 and sym_info is not None:
        _tick_val = float(getattr(sym_info, "trade_tick_value", 0) or 0)
        _tick_sz  = float(getattr(sym_info, "trade_tick_size",  0) or 0)
        if _tick_val > 0 and _tick_sz > 0:
            _tp_diff         = abs(tp - price)
            _expected_profit = (_tp_diff / _tick_sz) * _tick_val * vol
            if _expected_profit < min_profit_usd:
                msg = (
                    f"💵 Profit esperado ${_expected_profit:.2f} "
                    f"< ${min_profit_usd:.0f} mín (lote={vol})"
                )
                state.last_action = f"{msg} {symbol}"
                set_symbol_status(symbol, f"💵 Profit ${_expected_profit:.2f}<${min_profit_usd:.0f}")
                log.info(f"[{symbol}] {msg} — HOLD (aumentar balance o lote mínimo)")
                return

    hilbert  = ind.get("hilbert", {})
    h_signal = hilbert.get("signal", "NEUTRAL")
    if action == "BUY"  and h_signal == "LOCAL_MAX":
        state.last_action = f"🌀 Hilbert bloqueó BUY {symbol}"
        set_symbol_status(symbol, "🌀 Hilbert bloqueó BUY")
        return
    if action == "SELL" and h_signal == "LOCAL_MIN":
        state.last_action = f"🌀 Hilbert bloqueó SELL {symbol}"
        set_symbol_status(symbol, "🌀 Hilbert bloqueó SELL")
        return

    ticket, executed_price = open_order(symbol, action, sl, tp, vol)
    if ticket is None:
        state.last_action = f"❌ Orden fallida {symbol}"
        set_symbol_status(symbol, "❌ Orden fallida")
        return

    slippage_price = abs(executed_price - price)
    slippage_pips  = price_distance_to_pips(symbol, slippage_price)
    if slippage_pips > 0.1:
        log.info(
            f"[slippage] #{ticket} {symbol} {action}: "
            f"req={price:.5f} exec={executed_price:.5f} slip={slippage_pips:.1f}pips"
        )

    price = executed_price
    state.last_trade_time[symbol] = time.time()

    # Initialize trailing state for this ticket
    init_ticket(ticket)
    trade_mode = get_adaptive_trail_params(sym_cfg, action)
    trade_mode["scalp_mode"] = is_scalp_trade
    state.trade_mode_cache[ticket] = trade_mode

    direction_features = build_features(symbol, action, ind, sr_ctx, news_ctx, sym_cfg)
    setup_id   = derive_setup_id(ind, action)
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
        slippage_pips=slippage_pips,
    )
    state.tickets_en_memoria.add(ticket)

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

    state.last_action = f"✅ {action} {symbol} #{ticket} conf={confidence}"
    set_symbol_status(symbol, f"✅ {action} #{ticket} conf={confidence}")
    log.info(f"[{symbol}] ✅ #{ticket} {action} p={price} sl={sl} tp={tp}")


# ════════════════════════════════════════════════════════════════
#  MAIN PER-SYMBOL PROCESSING
# ════════════════════════════════════════════════════════════════

def process_symbol(
    symbol: str, sym_cfg: dict,
    open_positions: list, balance: float, equity: float,
) -> None:
    """
    Procesa un símbolo en el ciclo principal: calcula indicadores, evalúa
    señales y decide si abrir un trade.
    """
    from modules.indicators import compute_all

    equity_guard_min_pct = float(getattr(cfg, "EQUITY_GUARD_MIN_PCT", 70.0))
    equity_floor = balance * (equity_guard_min_pct / 100.0) if balance and balance > 0 else 0.0
    if equity_floor > 0 and equity < equity_floor:
        msg = (
            f"🛡 Equity guard: {equity:,.2f} < {equity_floor:,.2f} "
            f"({equity_guard_min_pct:.0f}% bal)"
        )
        set_symbol_status(symbol, msg[:48])
        state.last_action = f"{msg} {symbol}"
        log.warning(f"[{symbol}] {msg} — nuevas entradas bloqueadas")
        _notify_equity_guard_once(symbol, equity, equity_floor, equity_guard_min_pct)
        return
    else:
        state.equity_guard_notified.pop(symbol, None)

    tradeable_basic, motivo_basic = is_market_tradeable(symbol, sym_cfg)
    if not tradeable_basic:
        set_symbol_status(symbol, motivo_basic)
        return

    live_sym_positions = mt5.positions_get(symbol=symbol)
    if live_sym_positions is None:
        live_sym_positions = []
    bot_sym_positions = [p for p in live_sym_positions if p.magic == cfg.MAGIC_NUMBER]
    has_bot_position  = len(bot_sym_positions) > 0
    max_per_symbol    = getattr(cfg, "MAX_OPEN_PER_SYMBOL", 1)

    now_ts            = time.time()
    last_open         = state.last_trade_time.get(symbol, 0)
    cooldown_active   = (now_ts - last_open) < SYMBOL_COOLDOWN_SEC
    cooldown_remaining = int(max(0, SYMBOL_COOLDOWN_SEC - (now_ts - last_open)))
    total_open        = get_open_positions_count_realtime()

    df_entry = get_candles(symbol, cfg.TF_ENTRY, TF_CANDLES.get(cfg.TF_ENTRY, 300))
    df_h1    = get_candles(symbol, cfg.TF_TREND, TF_CANDLES.get(cfg.TF_TREND, 150))
    if df_entry is None or df_h1 is None or len(df_entry) < 60:
        set_symbol_status(symbol, "⚠️ Datos insuficientes de MT5")
        return

    _now_h1    = time.time()
    _cached_h1 = state._h1_cache.get(symbol)
    if _cached_h1 is not None and (_now_h1 - _cached_h1.get("timestamp", 0)) < H1_CACHE_TTL_SEC:
        ind = _cached_h1["indicators"]
        ind["price"] = round(float(df_h1["close"].iloc[-1]), 4)
        if df_entry is not None and len(df_entry) > 0:
            ind["entry_price"] = round(float(df_entry["close"].iloc[-1]), 4)
            if "time" in df_entry.columns:
                ind["entry_candle_time"] = pd.Timestamp(df_entry["time"].iloc[-1]).isoformat()
        log.debug(f"[{symbol}] H1 cache hit — indicadores reutilizados")
    else:
        ind = compute_all(df_h1, symbol, sym_cfg, df_entry=df_entry)
        state._h1_cache[symbol] = {"indicators": ind, "timestamp": _now_h1}

    state.ind_cache[symbol] = ind
    state.symbol_detail_cache[symbol] = {
        "enhanced_regime":   ind.get("enhanced_regime", "UNKNOWN"),
        "regime_confidence": ind.get("regime_confidence", "LOW"),
        "z_score":           ind.get("zscore_returns", {}).get("z_score", None),
    }

    _tick_spread = mt5.symbol_info_tick(symbol)
    if _tick_spread is not None:
        _spread_price = _tick_spread.ask - _tick_spread.bid
        ind["spread_pips"] = price_distance_to_pips(symbol, _spread_price)
    else:
        ind["spread_pips"] = 0.0

    if has_bot_position:
        set_symbol_status(symbol, f"📂 Posiciones abiertas ({len(bot_sym_positions)}/{max_per_symbol})")

    if cooldown_active:
        set_symbol_status(symbol, f"⏳ Cooldown activo — {cooldown_remaining}s")
        return

    if len(bot_sym_positions) >= max_per_symbol:
        set_symbol_status(symbol, f"📂 Límite por símbolo alcanzado ({len(bot_sym_positions)}/{max_per_symbol})")
        return

    if total_open >= cfg.MAX_OPEN_TRADES:
        set_symbol_status(symbol, f"⛔ Límite global alcanzado ({total_open}/{cfg.MAX_OPEN_TRADES})")
        return

    atr_now   = ind.get("atr", 0)
    price_now = ind.get("price", 0)
    tradeable_atr, motivo_atr = is_market_tradeable(symbol, sym_cfg, atr_now, price_now)
    if not tradeable_atr:
        log.info(f"[{symbol}] {motivo_atr}")
        state.last_action = f"📉 {motivo_atr[:55]}"
        set_symbol_status(symbol, motivo_atr)
        return

    dfs_by_tf = {}
    for tf in sym_cfg.get("sr_timeframes", ["H1"]):
        df_tf = get_candles(symbol, tf, TF_CANDLES.get(tf, 150))
        if df_tf is not None:
            dfs_by_tf[tf] = df_tf
    sr_ctx = build_sr_context(dfs_by_tf, ind["price"], symbol, sym_cfg)
    state.sr_cache[symbol] = sr_ctx

    pause_cal, cal_reason, cal_event, already_cal_notified = \
        eco_calendar.should_pause_symbol(symbol, sym_cfg)
    if pause_cal:
        state.last_action = f"📅 Cal: {cal_reason[:55]}"
        set_symbol_status(symbol, f"📅 {cal_reason[:48]}")
        if not already_cal_notified and cal_event is not None:
            _notify_calendar_pause_once(symbol, cal_event, sym_cfg)
        log.info(f"[{symbol}] ⏸ Cal: {cal_reason[:60]}")
        return

    cal_events_nearby = eco_calendar.get_events_for_symbol(symbol, sym_cfg, minutes_ahead=120)

    now_ts_news = time.time()
    if (
        state.shared_news_cache is None
        or (now_ts_news - state.shared_news_last_update) > cfg.NEWS_REFRESH_MIN * 60
    ):
        merged_topics = ",".join(sorted({
            topic.strip()
            for cfg_item in cfg.SYMBOLS.values()
            for topic in str(cfg_item.get("news_topics", "economy_monetary,economy_macro")).split(",")
            if topic.strip()
        }))
        state.shared_news_cache = build_shared_news_context(merged_topics)
        state.shared_news_last_update = now_ts_news

    if symbol not in state.news_cache or (now_ts_news - state.news_last_update.get(symbol, 0)) > cfg.NEWS_REFRESH_MIN * 60:
        news_ctx = derive_symbol_news_context(symbol, sym_cfg, state.shared_news_cache)
        state.news_cache[symbol] = news_ctx
        state.news_last_update[symbol] = now_ts_news
    else:
        news_ctx = state.news_cache[symbol]

    if news_ctx.should_pause:
        state.last_action = f"📰 News pausa {symbol}"
        set_symbol_status(symbol, f"📰 {news_ctx.pause_reason[:48]}")
        _notify_news_pause_once(symbol, news_ctx.pause_reason, cfg.NEWS_PAUSE_MINUTES_BEFORE)
        return
    else:
        state.news_pause_notified.pop(symbol, None)

    # Store context for deterministic decision engine
    if symbol not in state._symbol_state:
        state._symbol_state[symbol] = {}
    state._symbol_state[symbol]["last_sr_context"] = (
        sr_ctx.__dict__ if hasattr(sr_ctx, "__dict__") else sr_ctx
    )
    state._symbol_state[symbol]["last_news_context"] = news_ctx

    hurst_val = ind.get("hurst", 0.5)
    min_hurst = sym_cfg.get("min_hurst", 0.40)
    hurst_allowed, hurst_reason = _should_allow_low_hurst_scalp(ind, sr_ctx, sym_cfg)
    if not hurst_allowed:
        if getattr(cfg, "SCALPING_ONLY", False):
            hurst_penalty = round((min_hurst - hurst_val) * 5, 2)
            log.info(f"[{symbol}] Hurst {hurst_val:.3f} < {min_hurst:.3f} — penalty={hurst_penalty:.1f} (scalping mode)")
            set_symbol_status(symbol, f"📉 Hurst scalp penalizado {hurst_val:.2f}")
            ind["hurst_penalty"] = hurst_penalty
            state._symbol_state[symbol]["hurst_penalty"] = hurst_penalty
        else:
            log.info(f"[{symbol}] Hurst {hurst_val:.3f} < {min_hurst:.3f} — HOLD")
            state.last_action = f"📊 Hurst bajo {symbol} ({hurst_val:.2f}<{min_hurst:.2f})"
            set_symbol_status(symbol, f"📊 Hurst bajo {hurst_val:.2f}<{min_hurst:.2f}")
            return
    else:
        ind["hurst_penalty"] = 0.0
        state._symbol_state[symbol]["hurst_penalty"] = 0.0

    state._symbol_state[symbol]["last_indicators"] = ind
    if hurst_val < min_hurst:
        log.info(f"[{symbol}] Hurst {hurst_val:.3f} < {min_hurst:.3f} pero se permite scalp — {hurst_reason}")
        set_symbol_status(symbol, f"📉 Hurst scalp OK {hurst_val:.2f}")

    h1_trend     = ind.get("h1_trend", "LATERAL")
    candle_stamp = get_last_candle_stamp(df_entry)

    if h1_trend in ("BAJISTA_FUERTE", "BAJISTA"):
        mem_direction = "SELL"
        state._symbol_state[symbol]["mem_direction"] = mem_direction
    elif h1_trend in ("ALCISTA_FUERTE", "ALCISTA"):
        mem_direction = "BUY"
        state._symbol_state[symbol]["mem_direction"] = mem_direction
    else:
        # LATERAL → evaluar ambas direcciones
        feat_buy  = build_features(symbol, "BUY",  ind, sr_ctx, news_ctx, sym_cfg)
        feat_sell = build_features(symbol, "SELL", ind, sr_ctx, news_ctx, sym_cfg)
        mem_buy   = check_memory(feat_buy,  symbol, "BUY",  sym_cfg)
        mem_sell  = check_memory(feat_sell, symbol, "SELL", sym_cfg)
        setup_buy  = derive_setup_id(ind, "BUY")
        setup_sell = derive_setup_id(ind, "SELL")
        session_id = derive_session_from_ind(ind)
        score_buy  = evaluate_scorecard(symbol, setup_buy,  session_id, mem_buy.regime)
        score_sell = evaluate_scorecard(symbol, setup_sell, session_id, mem_sell.regime)
        policy_buy  = evaluate_policy(symbol, "BUY",  setup_buy,  session_id, mem_buy.regime)
        policy_sell = evaluate_policy(symbol, "SELL", setup_sell, session_id, mem_sell.regime)

        if mem_buy.should_block and mem_sell.should_block:
            state.last_action = f"🧠 Memoria bloqueó {symbol}"
            set_symbol_status(symbol, "🧠 Memoria bloqueó BUY y SELL")
            _notify_memory_block_once(symbol, mem_buy)
            return
        if score_buy.should_block and score_sell.should_block:
            msg = "🧮 Scorecard bloqueó BUY y SELL (historial pobre)"
            state.last_action = f"{msg} {symbol}"
            set_symbol_status(symbol, msg[:48])
            log.info(f"[{symbol}] {msg} | BUY={score_buy.reason} | SELL={score_sell.reason}")
            return

        candidates = [
            ("BUY",  feat_buy,  mem_buy,  score_buy,  policy_buy),
            ("SELL", feat_sell, mem_sell, score_sell, policy_sell),
        ]
        viable = [
            c for c in candidates
            if not c[2].should_block and not c[3].should_block and not c[4].should_block
        ]
        if not viable:
            msg = "🧮 Policy/Scorecard bloqueó candidatos BUY y SELL"
            set_symbol_status(symbol, msg[:48])
            state.last_action = f"{msg} {symbol}"
            log.info(
                f"[{symbol}] {msg} | "
                f"BUY={policy_buy.reason} | SELL={policy_sell.reason}"
            )
            return

        candidate_payloads = {}
        ready_actions      = []
        for action, features, mem_check, scorecard, policy in viable:
            candidate_ok, candidate_reason, trade_plan = _is_decision_candidate_ready(
                symbol, action, ind, sr_ctx, sym_cfg
            )
            candidate_payloads[action] = {
                "features":         features,
                "mem_check":        mem_check,
                "scorecard":        scorecard,
                "policy":           policy,
                "trade_plan":       trade_plan,
                "candidate_ok":     candidate_ok,
                "candidate_reason": candidate_reason,
            }
            if candidate_ok:
                ready_actions.append(action)

        if not ready_actions:
            bump_decision_metric("skipped_by_gate_total")
            reasons = " | ".join(
                f"{action}:{payload['candidate_reason']}"
                for action, payload in candidate_payloads.items()
            )
            set_symbol_status(symbol, reasons[:48])
            state.last_action = f"🧪 Lateral sin candidato listo {symbol}"
            log.info(f"[{symbol}] Lateral sin candidato listo — {reasons}")
            return

        cache_key = (
            build_decision_cache_key(symbol, "LATERAL", candle_stamp, ind)
            + "|" + ",".join(sorted(ready_actions))
        )
        state._symbol_state[symbol]["mem_direction"] = ready_actions[0]
        decision      = ask_decision_engine(symbol, sym_cfg, cache_key=cache_key)
        base_payload  = candidate_payloads[ready_actions[0]]
        _execute_decision(
            symbol, sym_cfg, decision, ind, sr_ctx, news_ctx,
            base_payload["mem_check"], base_payload["features"], balance, df_entry,
            candidate_payloads=candidate_payloads,
        )
        return

    # ── Directional path (non-LATERAL) ──────────────────────────
    features  = build_features(symbol, mem_direction, ind, sr_ctx, news_ctx, sym_cfg)
    mem_check = check_memory(features, symbol, mem_direction, sym_cfg)
    setup_id  = derive_setup_id(ind, mem_direction)
    session_id = derive_session_from_ind(ind)
    scorecard = evaluate_scorecard(
        symbol=symbol, setup_id=setup_id,
        session=session_id, regime=mem_check.regime,
    )
    policy = evaluate_policy(
        symbol=symbol, direction=mem_direction,
        setup_id=setup_id, session=session_id, regime=mem_check.regime,
    )

    if mem_check.should_block:
        state.last_action = f"🧠 Memoria bloqueó {symbol}"
        set_symbol_status(symbol, f"🧠 {mem_check.warning_msg[:48]}")
        _notify_memory_block_once(symbol, mem_check)
        return
    if scorecard.should_block:
        msg = (
            f"🧮 Scorecard vetó {mem_direction} "
            f"(WR={scorecard.win_rate:.1f}% n={scorecard.sample_size})"
        )
        set_symbol_status(symbol, msg[:48])
        state.last_action = f"{msg} {symbol}"
        log.info(f"[{symbol}] {msg} | {scorecard.reason}")
        return
    if policy.should_block:
        msg = (
            f"📉 Policy vetó {mem_direction} "
            f"(score={policy.policy_score:.3f} n={policy.sample_size})"
        )
        set_symbol_status(symbol, msg[:48])
        state.last_action = f"{msg} {symbol}"
        log.info(f"[{symbol}] {msg} | {policy.reason}")
        return

    if _apply_confluence_gate_pre(symbol, ind, mem_direction):
        return

    candidate_ok, candidate_reason, trade_plan = _is_decision_candidate_ready(
        symbol, mem_direction, ind, sr_ctx, sym_cfg
    )
    if not candidate_ok:
        bump_decision_metric("skipped_by_gate_total")
        set_symbol_status(symbol, candidate_reason[:48])
        state.last_action = f"{candidate_reason} {symbol}"
        log.info(f"[{symbol}] {candidate_reason} — decisión no procesada")
        return

    cache_key = build_decision_cache_key(symbol, mem_direction, candle_stamp, ind)
    decision = ask_decision_engine(symbol, sym_cfg, cache_key=cache_key)
    _execute_decision(
        symbol, sym_cfg, decision, ind, sr_ctx, news_ctx,
        mem_check, features, balance, df_entry,
    )
