"""
╔══════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — telegram_notifier.py (v6.4)            ║
║                                                                  ║
║   FIXES v6.4:                                                   ║
║   ✅ notify_eod_analysis() — análisis EOD 21:00 UTC             ║
║   ✅ Caption seguro: trunca sin romper tags HTML                 ║
║   ✅ Todas las funciones usan HTML parse_mode                   ║
╚══════════════════════════════════════════════════════════════════╝
"""

import logging
import requests
from datetime import datetime, timezone
from typing import Optional, List

import config as cfg

log = logging.getLogger(__name__)

import html as html_mod

def _esc(text: str) -> str:
    """Escapa texto para HTML de Telegram."""
    return html_mod.escape(str(text))


def _score_icon(score: float) -> str:
    """Emoji de color para un score numérico (🟢 positivo / 🔴 negativo / ⚪ neutro)."""
    if score > 0.3:
        return "🟢"
    if score < -0.3:
        return "🔴"
    return "⚪"


def _send(text: str, parse_mode: str = "HTML"):
    """
    Envía mensaje a Telegram en HTML.
    Trunca de forma segura sin romper tags HTML.
    """
    MAX_LEN = 4000
    if len(text) > MAX_LEN:
        # Truncar en el último carácter seguro (sin estar dentro de un tag)
        truncated = text[:MAX_LEN - 20]
        # Asegurarse de no cortar a mitad de un tag HTML
        last_open = truncated.rfind("<")
        last_close = truncated.rfind(">")
        if last_open > last_close:
            truncated = truncated[:last_open]
        text = truncated + "\n…[truncado]"

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{cfg.TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id":    cfg.TELEGRAM_CHAT_ID,
                "text":       text,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        if r.status_code != 200:
            log.warning(f"[telegram] HTTP {r.status_code}: {r.text[:100]}")
            # Retry sin formato HTML
            requests.post(
                f"https://api.telegram.org/bot{cfg.TELEGRAM_TOKEN}/sendMessage",
                json={
                    "chat_id": cfg.TELEGRAM_CHAT_ID,
                    "text":    html_mod.unescape(text)[:3900],
                },
                timeout=10,
            )
    except Exception as e:
        log.warning(f"[telegram] Error al enviar: {e}")


def _icon(symbol: str) -> str:
    if "XAU" in symbol or "GOLD" in symbol:
        return "🥇"
    if "500" in symbol or "SP" in symbol:
        return "📈"
    if "NAS" in symbol:
        return "💻"
    if "OIL" in symbol or "WTI" in symbol:
        return "🛢️"
    if "BTC" in symbol:
        return "₿"
    if "XAG" in symbol or "SILVER" in symbol:
        return "🥈"
    if "GER" in symbol or "DAX" in symbol:
        return "🇩🇪"
    return "💱"


# ════════════════════════════════════════════════════════════════
#  BOT INICIADO
# ════════════════════════════════════════════════════════════════

def notify_bot_started(balance: float, equity: float, memory_stats: dict, symbols: list):
    lines = [
        "🤖 <b>ZAR ULTIMATE BOT v6 — INICIADO</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"💰 Balance: <code>${balance:,.2f}</code>",
        f"📊 Equity:  <code>${equity:,.2f}</code>",
        "",
        "📚 <b>Memoria neural:</b>",
        f"  Trades registrados: <code>{memory_stats.get('total', 0)}</code>",
        f"  Win Rate histórico:  <code>{memory_stats.get('win_rate', 0)}%</code>",
        f"  P&amp;L acumulado:       <code>${memory_stats.get('profit', 0):+.2f}</code>",
        "",
        "🏗 <b>Arquitectura activa:</b>",
        "  ✅ FASE 0 — Securización (.env, _require_env)",
        "  ✅ FASE 1 — Microestructura (Volume Profile, Session VWAP, FVG)",
        "             Parámetros adaptativos (Hilbert/Hurst/Kalman vs ATR%)",
        "             Confluencia 3 Pilares ponderada 40/30/30",
        "  ✅ FASE 2 — Neural Brain v3 (40 features, Kelly Position Sizing)",
        "             MLP + Cosine Memory + Attention (ensemble automático)",
        "  ✅ FASE 3 — Confluence Hard Gate (pre+post Gemini)",
        "             Dashboard v2 (fila microestructura) + Notifs. ricas",
        "",
        "🧮 <b>Algoritmos:</b>",
        "  ✅ Transformada de Hilbert | Hurst | Kalman",
        "  ✅ Fourier | Fisher | Ciclo Adaptativo",
        "  ✅ 45+ indicadores | S/R multi-TF (6 métodos)",
        "  ✅ MLP neural + memoria coseno + attention",
        "  ✅ Volume Profile (POC/VAH/VAL) | Session VWAP | FVGs",
        "  ✅ Confluence Matrix (3 Pilares) | Kelly Position Sizing",
        "",
        "📡 <b>Activos monitoreados:</b>",
        "  " + " | ".join(f"<code>{s}</code>" for s in symbols),
        "",
        f"⏰ <code>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC</code>",
    ]
    _send("\n".join(lines))


# ════════════════════════════════════════════════════════════════
#  OPERACIÓN ABIERTA
# ════════════════════════════════════════════════════════════════

def notify_trade_opened(
    symbol: str, direction: str, price: float, sl: float, tp: float,
    volume: float, atr: float, rr: float, reason: str,
    ind: dict, hilbert: dict, hurst: float, fisher: float,
    memory_warn: str = "",
    kelly_active: bool = False,
):
    """
    FASE 3: Enriquecido con sección de Microestructura + Confluencia + Kelly.
    """
    icon = _icon(symbol)
    dir_icon = "🟢 BUY" if direction == "BUY" else "🔴 SELL"
    pips_sl  = abs(price - sl)
    pips_tp  = abs(tp - price)
    h_signal  = hilbert.get("signal", "NEUTRAL")
    h_phase   = hilbert.get("phase", 0)
    h_sine    = hilbert.get("sine", 0)
    h_period  = hilbert.get("period", 0)
    hurst_regime = (
        "📈 TENDENCIA" if hurst > 0.6 else
        "🎲 ALEATORIO" if hurst > 0.45 else
        "↩️ REVERSIÓN"
    )
    fisher_tag = " ⚠️OB" if fisher > 2 else (" ⚠️OS" if fisher < -2 else "")
    # FIX 10: Limitar reason a 200 chars ANTES de escapar
    reason_safe = _esc(reason[:200])

    # ── Microestructura + Confluencia (FASE 1/3) ─────────────────
    micro = ind.get("microstructure", {})
    conf  = ind.get("confluence", {})

    micro_score = micro.get("micro_score", 0.0)
    micro_bias  = micro.get("micro_bias", "NEUTRAL")
    poc         = micro.get("poc", 0.0)
    poc_pos     = "SOBRE POC" if micro.get("above_poc", True) else "BAJO POC"
    svwap       = micro.get("session_vwap", 0.0)
    svwap_dev   = micro.get("session_vwap_dev", 0.0)
    svwap_pos   = "sobre SVWAP" if micro.get("above_session_vwap", True) else "bajo SVWAP"
    fvg_bull    = micro.get("fvg_bull")
    fvg_bear    = micro.get("fvg_bear")
    session_name = micro.get("session", "?")

    conf_p1   = conf.get("p1_score", 0.0)
    conf_p2   = conf.get("p2_score", 0.0)
    conf_p3   = conf.get("p3_score", 0.0)
    conf_tot  = conf.get("total", 0.0)
    conf_bias = conf.get("bias", "NEUTRAL")
    sniper    = conf.get("sniper_aligned", False)

    # Score icon helpers are at module level: _score_icon()

    lines = [
        f"{icon} <b>OPERACIÓN ABIERTA — {_esc(symbol)}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🎯 Dirección: <b>{dir_icon}</b>",
        f"💵 Entrada: <code>{price}</code>",
        f"🛑 SL: <code>{sl}</code>  ({pips_sl:.4f} pts)",
        f"✅ TP: <code>{tp}</code>  ({pips_tp:.4f} pts)",
        f"⚖️ R:R: <code>1:{rr:.2f}</code>",
        f"📦 Vol: <code>{volume}</code>{'  <b>Kelly✓</b>' if kelly_active else ''}  |  ATR: <code>{atr:.4f}</code>",
        "",
        "🧮 <b>Algoritmos:</b>",
        f"  🌀 Hilbert: <code>{_esc(h_signal)}</code> | sine={h_sine:.3f} | fase={h_phase:.0f}° | T={h_period:.0f}v",
        f"  📐 Hurst: <code>{hurst:.3f}</code> {hurst_regime}",
        f"  🐟 Fisher: <code>{fisher:.3f}</code>{fisher_tag}",
        f"  🔄 Ciclo: <code>{_esc(str(ind.get('cycle_phase','?')))}</code> | osc={ind.get('cycle_osc',0):.3f}",
        f"  📊 Kalman: <code>{_esc(str(ind.get('kalman_trend','?')))}</code> | slope={ind.get('kalman_slope',0):.4f}",
        "",
        "📊 <b>Indicadores:</b>",
        f"  RSI: <code>{ind.get('rsi',0):.1f}</code>  |  MACD: <code>{_esc(str(ind.get('macd_dir','?')))}</code>  |  Hist: <code>{ind.get('macd_hist',0):.4f}</code>",
        f"  Stoch K/D: <code>{ind.get('stoch_k',0):.1f}/{ind.get('stoch_d',0):.1f}</code>  |  BB: <code>{_esc(str(ind.get('bb_pos','?')))}</code>",
        f"  HA: <code>{_esc(str(ind.get('ha_trend','?')))}</code> ({ind.get('ha_streak',0)}v)  |  H1: <code>{_esc(str(ind.get('h1_trend','?')))}</code>",
        f"  SuperTrend: <code>{'ALCISTA' if ind.get('supertrend',0)==1 else 'BAJISTA'}</code>  |  VWAP: <code>{ind.get('vwap',0):.4f}</code>",
        "",
        "🔬 <b>Microestructura (P3):</b>",
        f"  Score: <code>{micro_score:+.1f}</code> → {_esc(micro_bias)}  │  Sesión: <code>{_esc(session_name)}</code>",
        f"  POC: <code>{poc:.4f}</code> — precio {_esc(poc_pos)}",
        f"  S-VWAP: <code>{svwap:.4f}</code> — precio {_esc(svwap_pos)} ({svwap_dev:+.2f}%)",
        (f"  FVG Bull: <code>{fvg_bull['low']:.4f}–{fvg_bull['high']:.4f}</code> (hace {fvg_bull['age']}v)"
         if fvg_bull else "  FVG Bull: ninguno activo"),
        (f"  FVG Bear: <code>{fvg_bear['low']:.4f}–{fvg_bear['high']:.4f}</code> (hace {fvg_bear['age']}v)"
         if fvg_bear else "  FVG Bear: ninguno activo"),
        "",
        "⚡ <b>Confluencia 3 Pilares:</b>",
        f"  P1 (Estad.): {_score_icon(conf_p1)} <code>{conf_p1:>+.2f}</code>  │  "
        f"P2 (Matem.): {_score_icon(conf_p2)} <code>{conf_p2:>+.2f}</code>  │  "
        f"P3 (Micro): {_score_icon(conf_p3)} <code>{conf_p3:>+.2f}</code>",
        f"  TOTAL: {_score_icon(conf_tot)} <code>{conf_tot:>+.2f}</code> → {_esc(conf_bias)}  "
        + ("│  ✅ <b>SNIPER ALIGNED</b>" if sniper else "│  ⚠️ Pilares en desacuerdo"),
        "",
        f"💡 <i>{reason_safe}</i>",
    ]
    if memory_warn:
        lines += ["", f"⚠️ <b>Memoria:</b> <i>{_esc(memory_warn[:120])}</i>"]
    lines += ["", f"⏰ <code>{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC</code>"]
    _send("\n".join(lines))


# ════════════════════════════════════════════════════════════════
#  BREAKEVEN
# ════════════════════════════════════════════════════════════════

def notify_breakeven(symbol: str, ticket: int, new_sl: float, profit_pts: float):
    icon = _icon(symbol)
    _send("\n".join([
        f"{icon} <b>BREAKEVEN ACTIVADO — {_esc(symbol)}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🎫 Ticket:       <code>#{ticket}</code>",
        f"🛡 Nuevo SL:     <code>{new_sl}</code> (en breakeven)",
        f"📊 Movimiento:   <code>{profit_pts:.0f} pts</code>",
        "✅ Trade protegido — riesgo = 0",
        f"⏰ <code>{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC</code>",
    ]))


def notify_near_tp(symbol: str, ticket: int, price: float, tp: float, profit: float):
    icon = _icon(symbol)
    _send("\n".join([
        f"{icon} <b>TAKE PROFIT CERCANO — {_esc(symbol)}</b> 🎯",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🎫 Ticket:   <code>#{ticket}</code>",
        f"💵 Precio:   <code>{price}</code>",
        f"✅ TP en:    <code>{tp}</code>",
        f"💰 Profit:   <code>${profit:+.2f}</code> (flotante)",
        "📡 Monitoreando cierre...",
        f"⏰ <code>{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC</code>",
    ]))


# ════════════════════════════════════════════════════════════════
#  OPERACIÓN CERRADA
# ════════════════════════════════════════════════════════════════

def notify_trade_closed(
    symbol: str, ticket: int, direction: str,
    open_price: float, close_price: float,
    profit: float, pips: float, duration_min: int,
    result: str,
    hilbert_signal: str = "",
    memory_learned: bool = False,
):
    icon     = _icon(symbol)
    if result == "WIN":
        res_icon = "✅ WIN"
    elif result == "LOSS":
        res_icon = "❌ LOSS"
    elif result == "BE":
        res_icon = "⚡ BREAKEVEN"
    else:
        res_icon = "🔵 DESCONOCIDO"

    dur_str  = f"{duration_min // 60}h {duration_min % 60}min" if duration_min >= 60 else f"{duration_min}min"
    profit_icon = "📈" if profit > 0 else ("📉" if profit < 0 else "➡️")

    lines = [
        f"{icon} <b>OPERACIÓN CERRADA — {_esc(symbol)}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🎫 Ticket:    <code>#{ticket}</code>",
        f"📊 Resultado: <b>{res_icon}</b>",
        f"{profit_icon} Profit:    <code>${profit:+.2f}</code>",
        f"📏 Pips:      <code>{pips:+.1f}</code>",
        f"⏱ Duración:  <code>{dur_str}</code>",
        f"💵 <code>{open_price}</code> → <code>{close_price}</code>",
        f"🎯 Dirección: <code>{direction}</code>",
    ]
    if hilbert_signal:
        lines.append(f"🌀 Hilbert al abrir: <code>{_esc(hilbert_signal)}</code>")
    if result == "LOSS" and memory_learned:
        lines += ["", "🧠 <b>Aprendizaje:</b> patrón registrado en memoria.", "El bot evitará condiciones similares."]
    elif result == "WIN":
        lines += ["", "🎉 ¡Buen trade! Patrón ganador registrado."]
    elif result == "BE":
        lines += ["", "⚡ Cerrado en breakeven — capital protegido."]
    lines += ["", f"⏰ <code>{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC</code>"]
    _send("\n".join(lines))


# ════════════════════════════════════════════════════════════════
#  PAUSAS
# ════════════════════════════════════════════════════════════════

def notify_news_pause(symbol: str, reason: str, duration_min: int):
    icon = _icon(symbol)
    _send("\n".join([
        f"{icon} <b>PAUSA POR NOTICIAS — {_esc(symbol)}</b> 📰",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"⚠️ {_esc(reason[:200])}",
        f"⏸ Trading pausado <code>{duration_min}</code> min",
        f"⏰ <code>{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC</code>",
    ]))


def notify_memory_block(symbol: str, direction: str, similar_losses: int, warning: str):
    icon = _icon(symbol)
    _send("\n".join([
        f"{icon} <b>BLOQUEO POR MEMORIA — {_esc(symbol)}</b> 🧠",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🚫 {_esc(direction)} bloqueado",
        f"📊 Patrones perdedores similares: <code>{similar_losses}</code>",
        f"⚠️ {_esc(warning[:200])}",
        "El bot aprendió a evitar esta configuración.",
        f"⏰ <code>{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC</code>",
    ]))


# ════════════════════════════════════════════════════════════════
#  RESÚMENES — INTERMEDIO Y EOD
# ════════════════════════════════════════════════════════════════

def notify_daily_summary(
    balance: float, equity: float, daily_profit: float,
    trades_today: int, wins: int, losses: int,
    memory_stats: dict,
):
    wr  = (wins / trades_today * 100) if trades_today > 0 else 0
    icon = "🟢" if daily_profit > 0 else ("🔴" if daily_profit < 0 else "⚪")
    _send("\n".join([
        f"📊 <b>RESUMEN PARCIAL 17:00 UTC — ZAR v6</b> {icon}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"💰 Balance:     <code>${balance:,.2f}</code>",
        f"📊 Equity:      <code>${equity:,.2f}</code>",
        f"📈 P&amp;L hoy:     <code>${daily_profit:+.2f}</code>",
        "",
        f"🎯 Trades hoy:  <code>{trades_today}</code>",
        f"✅ Wins:         <code>{wins}</code>",
        f"❌ Losses:       <code>{losses}</code>",
        f"📊 Win Rate:     <code>{wr:.1f}%</code>",
        "",
        "🧠 <b>Memoria acumulada:</b>",
        f"  Total: <code>{memory_stats.get('total', 0)}</code>  |  WR: <code>{memory_stats.get('win_rate', 0)}%</code>  |  P&amp;L: <code>${memory_stats.get('profit', 0):+.2f}</code>",
        "",
        "ℹ️ Mercado aún abierto — resumen EOD completo a las 21:00 UTC",
        f"⏰ <code>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC</code>",
    ]))


def notify_eod_analysis(
    balance: float, equity: float, daily_profit: float,
    trades_today: int, wins: int, losses: int, be_count: int,
    win_rate: float, growth_pct: float,
    loss_reasons: list, be_reason: str,
    memory_stats: dict,
    daily_trades: list,
):
    """
    FIX 5: Análisis EOD completo a las 21:00 UTC.
    Incluye diagnóstico de causas de pérdidas y lecciones del día.
    """
    icon  = "🟢" if daily_profit > 0 else ("🔴" if daily_profit < 0 else "⚪")
    trend = "▲ POSITIVO" if daily_profit > 0 else ("▼ NEGATIVO" if daily_profit < 0 else "► NEUTRO")

    # Resumen por símbolo
    sym_summary: dict = {}
    for t in daily_trades:
        s = t.get("symbol", "?")
        if s not in sym_summary:
            sym_summary[s] = {"W": 0, "L": 0, "BE": 0, "pnl": 0.0}
        r = t.get("result", "?")
        sym_summary[s]["pnl"] += t.get("profit", 0)
        if r == "WIN":   sym_summary[s]["W"] += 1
        elif r == "LOSS": sym_summary[s]["L"] += 1
        elif r == "BE":   sym_summary[s]["BE"] += 1

    lines = [
        f"📋 <b>ANÁLISIS EOD — ZAR v6</b> {icon} {trend}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"💰 Balance:      <code>${balance:,.2f}</code>",
        f"📊 Equity:       <code>${equity:,.2f}</code>",
        f"📈 P&amp;L día:      <code>${daily_profit:+.2f}</code>  ({growth_pct:+.2f}%)",
        "",
        f"🎯 Trades total: <code>{trades_today}</code>",
        f"✅ Wins:         <code>{wins}</code>",
        f"❌ Losses:       <code>{losses}</code>",
        f"⚡ Breakeven:    <code>{be_count}</code>",
        f"📊 Win Rate:     <code>{win_rate:.1f}%</code>",
        "",
    ]

    if sym_summary:
        lines.append("📍 <b>Resultado por activo:</b>")
        for sym, data in sorted(sym_summary.items(), key=lambda x: x[1]["pnl"], reverse=True):
            pnl_icon = "🟢" if data["pnl"] > 0 else ("🔴" if data["pnl"] < 0 else "⚪")
            lines.append(
                f"  {pnl_icon} <code>{sym}</code>: "
                f"{data['W']}W/{data['L']}L/{data['BE']}BE — "
                f"<code>${data['pnl']:+.2f}</code>"
            )
        lines.append("")

    # Diagnóstico de fallos
    if loss_reasons:
        lines.append("🔍 <b>Diagnóstico de pérdidas:</b>")
        for reason in loss_reasons[:4]:
            lines.append(f"  • {_esc(reason)}")
        lines.append("")

    if be_reason:
        lines.append(f"⚡ <b>Breakeven:</b> {_esc(be_reason)}")
        lines.append("")

    # Evaluación general
    if win_rate >= 60:
        eval_msg = "🏆 Excelente sesión — estrategia funcionando bien."
    elif win_rate >= 50:
        eval_msg = "✅ Sesión positiva — ajustes menores posibles."
    elif win_rate >= 40:
        eval_msg = "⚠️ Sesión mixta — revisar filtros de entrada."
    else:
        eval_msg = "🔴 Sesión difícil — condiciones del mercado adversas."

    lines += [
        f"💬 {eval_msg}",
        "",
        "🧠 <b>Memoria acumulada:</b>",
        f"  Total: <code>{memory_stats.get('total', 0)}</code>  |  WR: <code>{memory_stats.get('win_rate', 0)}%</code>  |  P&amp;L: <code>${memory_stats.get('profit', 0):+.2f}</code>",
        "",
        "🌙 Mercado cerrado — el bot sigue activo 24/5.",
        f"⏰ <code>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC</code>",
    ]
    _send("\n".join(lines))


# ════════════════════════════════════════════════════════════════
#  ERRORES
# ════════════════════════════════════════════════════════════════

def notify_error(error_msg: str):
    _send("\n".join([
        "🚨 <b>ERROR CRÍTICO — ZAR v6</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"<code>{_esc(error_msg[:400])}</code>",
        f"⏰ <code>{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC</code>",
    ]))