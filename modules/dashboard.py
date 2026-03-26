"""
ZAR ULTIMATE BOT v6 — dashboard.py  (v6.2)
Panel visual en consola — se actualiza cada ciclo.
Muestra TODOS los símbolos con su estado: activo / durmiendo / pausado.
"""

import os
from datetime import datetime, timezone

import config as cfg


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def _is_in_session(sym_cfg: dict) -> bool:
    hour  = datetime.now(timezone.utc).hour
    start = sym_cfg.get("session_start", 0)
    end   = sym_cfg.get("session_end", 24)
    return start <= hour < end


def _get_icon(sym: str) -> str:
    if "XAU" in sym: return "🥇"
    if "XAG" in sym: return "🥈"
    if "BTC" in sym: return "₿ "
    if "OIL" in sym: return "🛢"
    if any(x in sym for x in ["500", "NAS", "GER"]): return "📈"
    return "💱"


def _session_opens_in(sym_cfg: dict) -> str:
    now_h = datetime.now(timezone.utc).hour
    start = sym_cfg.get("session_start", 0)
    diff  = (start - now_h) % 24
    return "ahora" if diff == 0 else f"en {diff}h"


def render(
    symbols:           list,
    indicators_by_sym: dict,
    sr_by_sym:         dict,
    news_ctx,
    open_positions:    list,
    memory_stats:      dict,
    balance:           float,
    equity:            float,
    daily_pnl:         float,
    cycle:             int,
    last_action:       str = "",
    calendar_info:     str = "",
    calendar_status=None,
    status_by_sym=None,
):
    clear()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    W = 72  # ancho interior

    lines = [
        "╔" + "═" * W + "╗",
        f"║  🤖 ZAR ULTIMATE BOT v6.2   Ciclo #{cycle:<5}  {now_str}  ║",
        "╠" + "═" * W + "╣",
        f"║  💰 Balance: ${balance:>10,.2f}  │  Equity: ${equity:>10,.2f}  │  P&L hoy: ${daily_pnl:>+8.2f}  ║",
        "╠" + "═" * W + "╣",
    ]

    active_count   = 0
    sleeping_count = 0
    paused_count   = 0

    for sym in symbols:
        sym_cfg_data = cfg.SYMBOLS.get(sym, {})
        in_session   = _is_in_session(sym_cfg_data)
        ind          = indicators_by_sym.get(sym, {})
        sr           = sr_by_sym.get(sym)
        status_msg   = (status_by_sym or {}).get(sym, "")
        strategy     = sym_cfg_data.get("strategy_type", "?")[:14]
        icon         = _get_icon(sym)

        # ── FUERA DE SESIÓN ─────────────────────────────────────
        if not in_session:
            sleeping_count += 1
            start_h  = sym_cfg_data.get("session_start", 0)
            end_h    = sym_cfg_data.get("session_end", 24)
            opens_in = _session_opens_in(sym_cfg_data)
            txt = (f"{icon} {sym:<10}  😴 FUERA DE SESIÓN  "
                   f"abre {opens_in}  ({start_h:02d}:00–{end_h:02d}:00 UTC)")
            lines.append(f"║  {txt:<{W-2}}║")
            continue

        # ── EN SESIÓN SIN DATOS AÚN ─────────────────────────────
        if not ind:
            active_count += 1
            txt = f"{icon} {sym:<10}  {status_msg or '⏳ Calculando indicadores...'}"
            lines.append(f"║  {txt:<{W-2}}║")
            continue

        # ── EN SESIÓN CON DATOS ─────────────────────────────────
        active_count += 1

        price   = ind.get("price", 0)
        trend   = ind.get("h1_trend", "?")
        rsi_v   = ind.get("rsi", 0)
        atr_v   = ind.get("atr", 0)
        hilbert = ind.get("hilbert", {})
        hurst   = ind.get("hurst", 0.5)
        fisher  = ind.get("fisher", 0)
        cycle_p = ind.get("cycle_phase", "?")
        min_h   = sym_cfg_data.get("min_hurst", 0.5)

        trend_icon = {
            "ALCISTA_FUERTE": "⬆⬆", "ALCISTA": "⬆ ",
            "LATERAL":        "➡ ",
            "BAJISTA":        "⬇ ", "BAJISTA_FUERTE": "⬇⬇",
        }.get(trend, "? ")

        # S/R
        sr_txt = ""
        if sr:
            ns = f"S={sr.nearest_sup:.2f}" if getattr(sr, "nearest_sup", None) else "S=N/A"
            nr = f"R={sr.nearest_res:.2f}" if getattr(sr, "nearest_res", None) else "R=N/A"
            sr_txt = f"{ns} {nr}"
            if getattr(sr, "in_strong_zone", False):
                sr_txt += " ⚠️"

        # Pausa calendario
        cal_tag = ""
        if calendar_status and hasattr(calendar_status, "paused_currencies"):
            curs = sym_cfg_data.get("currencies", [])
            if any(c in calendar_status.paused_currencies for c in curs):
                cal_tag = " ⏸CAL"
                paused_count += 1

        hurst_tag = " ⚠️H" if hurst < min_h else ""

        l1 = (f"{icon} {sym:<10} ${price:>11.4f}  "
              f"{trend_icon} {trend:<15} ATR={atr_v:<9.4f}"
              f"[{strategy}]{cal_tag}")
        l2 = (f"   RSI={rsi_v:>5.1f}  │  Hurst={hurst:.2f}{hurst_tag}  │  "
              f"Fisher={fisher:>+6.2f}  │  Ciclo={cycle_p}")
        l3 = (f"   Hilbert: {hilbert.get('signal','NEUTRAL'):<12} "
              f"sine={hilbert.get('sine',0):>+.3f}  "
              f"fase={hilbert.get('phase',0):>6.1f}°  "
              f"T={hilbert.get('period',0):.0f}v  │ {sr_txt}")

        lines += [
            f"║  {l1:<{W-2}}║",
            f"║  {l2:<{W-2}}║",
            f"║  {l3:<{W-2}}║",
        ]
        if status_msg:
            lines.append(f"║  {('   Estado: ' + status_msg)[:W-2]:<{W-2}}║")

    # ── Resumen sesión ──────────────────────────────────────────
    lines.append("╠" + "═" * W + "╣")
    total  = len(symbols)
    resumen = (f"📊 {active_count}/{total} en sesión  │  "
               f"😴 {sleeping_count} durmiendo  │  "
               f"⏸ {paused_count} pausados por calendario")
    lines.append(f"║  {resumen:<{W-2}}║")

    # ── Calendario ──────────────────────────────────────────────
    lines.append("╠" + "═" * W + "╣")
    lines.append(f"║  {'📅 CALENDARIO ECONÓMICO EN TIEMPO REAL':<{W-2}}║")
    if calendar_info:
        for cal_line in calendar_info.split("\n")[:5]:
            lines.append(f"║  {cal_line[:W-3]:<{W-2}}║")
    else:
        lines.append(f"║  {'  Sin eventos de alto impacto próximos (2h)':<{W-2}}║")

    # ── Noticias RSS ────────────────────────────────────────────
    lines.append("╠" + "═" * W + "╣")
    if news_ctx:
        pause_str    = "🚨 BREAKING — PAUSADO" if news_ctx.should_pause else "▶ Activo"
        breaking_str = (f" │  Breaking: {news_ctx.breaking_count}"
                        if hasattr(news_ctx, "breaking_count") else "")
        l_news = (f"📰 RSS: sent={news_ctx.avg_sentiment:+.2f}  │  "
                  f"Hi-impact: {news_ctx.high_impact_count}{breaking_str}  │  {pause_str}")
        lines.append(f"║  {l_news:<{W-2}}║")
        if news_ctx.items:
            lines.append(f"║    → {news_ctx.items[0].title[:W-8]:<{W-6}}║")
    else:
        lines.append(f"║  {'📰 Noticias: no disponibles aún':<{W-2}}║")

    # ── Posiciones abiertas ─────────────────────────────────────
    lines.append("╠" + "═" * W + "╣")
    if open_positions:
        lines.append(f"║  {'📂 POSICIONES ABIERTAS (' + str(len(open_positions)) + ')':<{W-2}}║")
        for pos in open_positions[:6]:
            p_icon = "🟢" if pos.get("profit", 0) >= 0 else "🔴"
            p_dir  = "BUY" if pos.get("type", 0) == 0 else "SELL"
            p_line = (f"  {p_icon} #{pos.get('ticket',0)}  "
                      f"{pos.get('symbol',''):<10}  {p_dir}  "
                      f"Lot={pos.get('volume',0):.2f}  "
                      f"P&L=${pos.get('profit',0):>+8.2f}")
            lines.append(f"║{p_line:<{W}}║")
    else:
        lines.append(f"║  {'📂 Sin posiciones abiertas':<{W-2}}║")

    # ── Memoria neural ──────────────────────────────────────────
    lines.append("╠" + "═" * W + "╣")
    mem_line = (f"🧠 Memoria: {memory_stats.get('total',0)} trades  │  "
                f"WR={memory_stats.get('win_rate',0):.0f}%  │  "
                f"P&L=${memory_stats.get('profit',0):>+.2f}")
    lines.append(f"║  {mem_line:<{W-2}}║")

    # ── Última acción ───────────────────────────────────────────
    if last_action:
        lines.append("╠" + "═" * W + "╣")
        lines.append(f"║  ⚡ {last_action[:W-5]:<{W-4}}║")

    lines.append("╚" + "═" * W + "╝")
    print("\n".join(lines))
