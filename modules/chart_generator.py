"""
╔══════════════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — chart_generator.py                            ║
║                                                                          ║
║   Genera gráficos de análisis técnico y los envía por Telegram.        ║
║                                                                          ║
║   CONTENIDO DEL GRÁFICO (4 paneles):                                    ║
║   Panel 1 (60%): Velas japonesas + S/R zones + Bollinger + EMA200      ║
║                  + VWAP + Kalman filter + Entrada/SL/TP markers         ║
║   Panel 2 (15%): RSI + niveles oversold/overbought                     ║
║   Panel 3 (15%): MACD histograma + línea MACD + señal                 ║
║   Panel 4 (10%): Seno de Hilbert + lead_sine + período dominante       ║
║                                                                          ║
║   TECNOLOGÍA:                                                            ║
║   • matplotlib + mplfinance (renderizado profesional)                   ║
║   • io.BytesIO (en memoria — sin archivos temporales)                  ║
║   • Telegram sendPhoto API (hasta 10MB, soporta PNG)                   ║
║   • Estilo dark theme institucional                                     ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import io
import logging
import requests
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
import html as html_mod

log = logging.getLogger(__name__)

# ── Importaciones de matplotlib (opcionales — no crashea si no están) ──
try:
    import matplotlib
    matplotlib.use("Agg")          # backend sin ventana (headless)
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    import matplotlib.gridspec as gridspec
    from matplotlib.patches import Rectangle
    from matplotlib.lines import Line2D
    MATPLOTLIB_OK = True
except ImportError:
    MATPLOTLIB_OK = False
    log.warning("[chart] matplotlib no instalado — pip install matplotlib")

# ── mplfinance para velas (opcional) ──
try:
    import mplfinance as mpf
    MPLFINANCE_OK = True
except ImportError:
    MPLFINANCE_OK = False


# ════════════════════════════════════════════════════════════════
#  PALETA DE COLORES — DARK THEME INSTITUCIONAL
# ════════════════════════════════════════════════════════════════

COLORS = {
    "bg":          "#0d1117",   # fondo negro profundo (GitHub dark)
    "bg_panel":    "#161b22",   # fondo paneles
    "grid":        "#21262d",   # líneas de cuadrícula
    "text":        "#c9d1d9",   # texto principal
    "text_dim":    "#8b949e",   # texto secundario
    "candle_bull": "#3fb950",   # vela alcista — verde GitHub
    "candle_bear": "#f85149",   # vela bajista — rojo GitHub
    "wick":        "#8b949e",   # mechas de velas
    "sr_sup":      "#3fb950",   # zona soporte — verde
    "sr_res":      "#f85149",   # zona resistencia — rojo
    "sr_sup_fill": "#3fb95025", # relleno soporte (transparente)
    "sr_res_fill": "#f8514925", # relleno resistencia (transparente)
    "bb_upper":    "#79c0ff",   # Bollinger superior — azul
    "bb_lower":    "#79c0ff",   # Bollinger inferior — azul
    "bb_mid":      "#388bfd",   # media Bollinger
    "bb_fill":     "#79c0ff10", # relleno Bollinger
    "ema200":      "#d29922",   # EMA 200 — amarillo dorado
    "vwap":        "#bc8cff",   # VWAP — morado
    "kalman":      "#56d364",   # Kalman — verde claro
    "entry":       "#f0e68c",   # precio entrada — amarillo
    "sl":          "#ff6b6b",   # stop loss — rojo
    "tp":          "#51fa7b",   # take profit — verde brillante
    "rsi_line":    "#79c0ff",   # RSI — azul
    "rsi_ob":      "#f85149",   # RSI overbought
    "rsi_os":      "#3fb950",   # RSI oversold
    "macd_line":   "#79c0ff",   # MACD line
    "macd_signal": "#e3b341",   # MACD signal
    "macd_bull":   "#3fb950",   # histograma positivo
    "macd_bear":   "#f85149",   # histograma negativo
    "hilbert_sine":"#bc8cff",   # seno Hilbert
    "hilbert_lead":"#79c0ff",   # lead sine
    "fib_382":     "#d2942250", # Fibonacci 38.2%
    "fib_500":     "#f0e68c50", # Fibonacci 50%
    "fib_618":     "#56d36450", # Fibonacci 61.8%
    "accent":      "#1f6feb",   # acento azul
}


# ════════════════════════════════════════════════════════════════
#  GENERADOR PRINCIPAL DE GRÁFICOS
# ════════════════════════════════════════════════════════════════

def generate_trade_chart(
    symbol:    str,
    direction: str,
    df:        pd.DataFrame,     # DataFrame con OHLCV + time (M1 o M5)
    ind:       dict,             # resultado de compute_all()
    sr_ctx,                      # SRContext
    price:     float,
    sl:        float,
    tp:        float,
    rr:        float,
    reason:    str,
    sym_cfg:   dict,
    n_candles: int = 80,         # cuántas velas mostrar
) -> Optional[bytes]:
    """
    Genera un gráfico de análisis técnico completo como imagen PNG en memoria.

    Returns:
        bytes del PNG listo para enviar a Telegram, o None si hay error.
    """
    if not MATPLOTLIB_OK:
        log.warning("[chart] matplotlib no disponible — no se generará imagen")
        return None

    try:
        return _build_chart(
            symbol, direction, df, ind, sr_ctx,
            price, sl, tp, rr, reason, sym_cfg, n_candles
        )
    except Exception as e:
        log.error(f"[chart] Error generando gráfico para {symbol}: {e}", exc_info=True)
        return None


def _build_chart(
    symbol, direction, df, ind, sr_ctx,
    price, sl, tp, rr, reason, sym_cfg, n_candles
) -> bytes:
    """Construye el gráfico con 4 paneles."""

    # ── Preparar datos ──
    df_plot = df.tail(n_candles).copy()
    df_plot = df_plot.reset_index(drop=True)
    closes  = df_plot["close"].values
    highs   = df_plot["high"].values
    lows    = df_plot["low"].values
    opens   = df_plot["open"].values
    n       = len(df_plot)
    x       = np.arange(n)

    # Timestamps para eje X
    times = df_plot["time"].dt.strftime("%H:%M") if "time" in df_plot.columns else [str(i) for i in x]

    # ── Calcular indicadores sobre df_plot ──
    bb_upper, bb_mid, bb_lower = _bollinger(closes)
    ema200_vals = _ema(closes, min(200, n - 1))
    kalman_vals = _kalman_simple(closes)
    rsi_vals    = _rsi_series(closes)
    macd_line, macd_signal, macd_hist = _macd_series(closes)

    # ── Hilbert sine wave simulado con período dominante ──
    hilbert    = ind.get("hilbert", {})
    h_period   = max(6, hilbert.get("period", 20))
    h_phase_0  = hilbert.get("phase", 0)
    phases_rad = np.array([
        2 * np.pi * i / h_period + np.radians(h_phase_0 - n * 2 * np.pi / h_period)
        for i in range(n)
    ])
    sine_wave  = np.sin(phases_rad)
    lead_wave  = np.sin(phases_rad + np.pi / 4)

    # ── Layout: 4 paneles verticales ──
    fig = plt.figure(figsize=(16, 14), facecolor=COLORS["bg"])
    gs  = gridspec.GridSpec(
        4, 1,
        height_ratios=[6, 1.5, 1.5, 1],
        hspace=0.04,
        left=0.07, right=0.97,
        top=0.93, bottom=0.05,
    )

    ax_price  = fig.add_subplot(gs[0])
    ax_rsi    = fig.add_subplot(gs[1], sharex=ax_price)
    ax_macd   = fig.add_subplot(gs[2], sharex=ax_price)
    ax_hilbert= fig.add_subplot(gs[3], sharex=ax_price)

    for ax in [ax_price, ax_rsi, ax_macd, ax_hilbert]:
        ax.set_facecolor(COLORS["bg_panel"])
        ax.tick_params(colors=COLORS["text_dim"], labelsize=7)
        ax.spines["bottom"].set_color(COLORS["grid"])
        ax.spines["top"].set_color(COLORS["grid"])
        ax.spines["left"].set_color(COLORS["grid"])
        ax.spines["right"].set_color(COLORS["grid"])
        ax.grid(True, color=COLORS["grid"], linewidth=0.4, alpha=0.7)

    plt.setp(ax_price.get_xticklabels(), visible=False)
    plt.setp(ax_rsi.get_xticklabels(), visible=False)
    plt.setp(ax_macd.get_xticklabels(), visible=False)

    # ════════════════════════════════════════════
    #  PANEL 1 — PRECIO (velas + S/R + BB + EMA + VWAP + Kalman)
    # ════════════════════════════════════════════

    # Velas japonesas
    for i in x:
        o, c, h, l = opens[i], closes[i], highs[i], lows[i]
        color = COLORS["candle_bull"] if c >= o else COLORS["candle_bear"]

        # Cuerpo
        body_bot = min(o, c)
        body_top = max(o, c)
        body_h   = max(body_top - body_bot, (h - l) * 0.001)  # min height
        rect = Rectangle(
            (i - 0.35, body_bot), 0.70, body_h,
            facecolor=color, edgecolor=color, linewidth=0.3, zorder=3
        )
        ax_price.add_patch(rect)

        # Mecha
        ax_price.plot(
            [i, i], [l, h],
            color=COLORS["wick"], linewidth=0.7, zorder=2
        )

    # Bollinger Bands
    valid_bb = ~np.isnan(bb_upper)
    ax_price.plot(x[valid_bb], bb_upper[valid_bb],
                  color=COLORS["bb_upper"], linewidth=0.8, linestyle="--", alpha=0.8, label="BB")
    ax_price.plot(x[valid_bb], bb_lower[valid_bb],
                  color=COLORS["bb_lower"], linewidth=0.8, linestyle="--", alpha=0.8)
    ax_price.plot(x[valid_bb], bb_mid[valid_bb],
                  color=COLORS["bb_mid"], linewidth=0.6, linestyle=":", alpha=0.6)
    ax_price.fill_between(x[valid_bb], bb_upper[valid_bb], bb_lower[valid_bb],
                          color=COLORS["bb_fill"], alpha=0.3, zorder=1)

    # EMA 200 (solo si tenemos datos suficientes)
    valid_ema = ~np.isnan(ema200_vals)
    if valid_ema.sum() > 5:
        ax_price.plot(x[valid_ema], ema200_vals[valid_ema],
                      color=COLORS["ema200"], linewidth=1.0, label="EMA200", alpha=0.9)

    # Kalman filter
    valid_kal = ~np.isnan(kalman_vals)
    ax_price.plot(x[valid_kal], kalman_vals[valid_kal],
                  color=COLORS["kalman"], linewidth=1.2, label="Kalman", alpha=0.85, zorder=4)

    # VWAP (línea horizontal al valor actual)
    vwap_val = ind.get("vwap", 0)
    if vwap_val > 0:
        ax_price.axhline(vwap_val, color=COLORS["vwap"], linewidth=0.9,
                         linestyle="-.", label=f"VWAP {vwap_val:.4f}", alpha=0.85)

    # ── Zonas S/R ──
    price_range = (highs.max() - lows.min()) or 1.0
    tolerance_px = price_range * 0.003  # zona de 0.3% de ancho visual

    if sr_ctx:
        # Soportes
        for sup in (sr_ctx.supports or [])[:6]:
            if lows.min() * 0.98 <= sup.price <= highs.max() * 1.02:
                alpha = min(0.8, 0.3 + sup.strength / 10 * 0.5)
                lw    = 0.6 + sup.strength / 10 * 1.2
                ax_price.axhline(
                    sup.price,
                    color=COLORS["sr_sup"], linewidth=lw,
                    linestyle="--", alpha=alpha, zorder=2
                )
                ax_price.axhspan(
                    sup.price - tolerance_px, sup.price + tolerance_px,
                    color=COLORS["sr_sup"], alpha=0.08, zorder=1
                )
                ax_price.text(
                    n * 1.001, sup.price,
                    f"S {sup.price:.2f} ({sup.strength:.0f})",
                    color=COLORS["sr_sup"], fontsize=6.5, va="center",
                    fontweight="bold", alpha=0.85
                )

        # Resistencias
        for res in (sr_ctx.resistances or [])[:6]:
            if lows.min() * 0.98 <= res.price <= highs.max() * 1.02:
                alpha = min(0.8, 0.3 + res.strength / 10 * 0.5)
                lw    = 0.6 + res.strength / 10 * 1.2
                ax_price.axhline(
                    res.price,
                    color=COLORS["sr_res"], linewidth=lw,
                    linestyle="--", alpha=alpha, zorder=2
                )
                ax_price.axhspan(
                    res.price - tolerance_px, res.price + tolerance_px,
                    color=COLORS["sr_res"], alpha=0.08, zorder=1
                )
                ax_price.text(
                    n * 1.001, res.price,
                    f"R {res.price:.2f} ({res.strength:.0f})",
                    color=COLORS["sr_res"], fontsize=6.5, va="center",
                    fontweight="bold", alpha=0.85
                )

    # ── Fibonacci si disponible ──
    if sr_ctx and hasattr(sr_ctx, "fib_levels") and sr_ctx.fib_levels:
        fibs_to_show = {
            "fib_382": (COLORS["fib_382"], "38.2%"),
            "fib_500": (COLORS["fib_500"], "50.0%"),
            "fib_618": (COLORS["fib_618"], "61.8%"),
        }
        for fib_key, (fib_color, fib_label) in fibs_to_show.items():
            fib_val = sr_ctx.fib_levels.get(fib_key)
            if fib_val and lows.min() * 0.97 <= fib_val <= highs.max() * 1.03:
                ax_price.axhline(
                    fib_val, color=fib_color.replace("50", ""),
                    linewidth=0.7, linestyle=":", alpha=0.6
                )
                ax_price.text(
                    2, fib_val, f"Fib {fib_label}: {fib_val:.4f}",
                    color=COLORS["text_dim"], fontsize=6, va="bottom", alpha=0.7
                )

    # ── Entrada / SL / TP ──
    entry_x = n - 1  # última vela = punto de entrada

    # Líneas horizontales
    ax_price.axhline(price, color=COLORS["entry"], linewidth=1.5,
                     linestyle="-", zorder=6, alpha=0.9)
    ax_price.axhline(sl, color=COLORS["sl"], linewidth=1.2,
                     linestyle="--", zorder=6, alpha=0.85)
    ax_price.axhline(tp, color=COLORS["tp"], linewidth=1.2,
                     linestyle="--", zorder=6, alpha=0.85)

    # Relleno SL-Entry y Entry-TP
    ax_price.axhspan(min(price, sl), max(price, sl),
                     color=COLORS["sl"], alpha=0.07, zorder=1)
    ax_price.axhspan(min(price, tp), max(price, tp),
                     color=COLORS["tp"], alpha=0.07, zorder=1)

    # Flecha de entrada
    arrow_dir = 1 if direction == "BUY" else -1
    ax_price.annotate(
        f"  {'▲ BUY' if direction == 'BUY' else '▼ SELL'}  @{price:.4f}",
        xy=(entry_x, price),
        xytext=(entry_x - n * 0.12, price + arrow_dir * price_range * 0.04),
        fontsize=9, fontweight="bold",
        color=COLORS["candle_bull"] if direction == "BUY" else COLORS["candle_bear"],
        arrowprops=dict(
            arrowstyle="->",
            color=COLORS["entry"],
            lw=1.5,
        ),
        zorder=8,
        bbox=dict(
            boxstyle="round,pad=0.3",
            facecolor=COLORS["bg"],
            edgecolor=COLORS["entry"],
            alpha=0.85,
        )
    )

    # Etiquetas SL y TP
    ax_price.text(
        3, sl,
        f" SL: {sl:.4f}  (riesgo)",
        color=COLORS["sl"], fontsize=7.5, va="bottom",
        fontweight="bold", zorder=7
    )
    ax_price.text(
        3, tp,
        f" TP: {tp:.4f}  (R:R 1:{rr:.1f})",
        color=COLORS["tp"], fontsize=7.5, va="top",
        fontweight="bold", zorder=7
    )

    # ── Título del panel 1 ──
    strategy = sym_cfg.get("strategy_type", "")
    hurst_val = ind.get("hurst", 0)
    fisher_val = ind.get("fisher", 0)
    ax_price.set_title(
        f"{symbol} — {sym_cfg.get('name', '')} | {strategy} | "
        f"ATR={ind.get('atr', 0):.4f} | Hurst={hurst_val:.2f} | Fisher={fisher_val:.2f}",
        color=COLORS["text"], fontsize=9.5, fontweight="bold", pad=6
    )

    ax_price.set_xlim(-1, n + 8)  # espacio derecho para etiquetas S/R
    ax_price.set_ylabel("Precio", color=COLORS["text_dim"], fontsize=8)
    ax_price.tick_params(axis="y", labelcolor=COLORS["text_dim"])

    # Leyenda
    legend_elems = [
        Line2D([0], [0], color=COLORS["bb_upper"], lw=1, linestyle="--", label="BB"),
        Line2D([0], [0], color=COLORS["ema200"],   lw=1, label="EMA 200"),
        Line2D([0], [0], color=COLORS["kalman"],   lw=1.2, label="Kalman"),
        Line2D([0], [0], color=COLORS["vwap"],     lw=1, linestyle="-.", label="VWAP"),
        Line2D([0], [0], color=COLORS["entry"],    lw=1.5, label="Entrada"),
        Line2D([0], [0], color=COLORS["sl"],       lw=1.2, linestyle="--", label="SL"),
        Line2D([0], [0], color=COLORS["tp"],       lw=1.2, linestyle="--", label="TP"),
    ]
    ax_price.legend(
        handles=legend_elems,
        loc="upper left", fontsize=7,
        facecolor=COLORS["bg"], edgecolor=COLORS["grid"],
        labelcolor=COLORS["text_dim"],
        framealpha=0.8, ncol=4,
    )

    # ════════════════════════════════════════════
    #  PANEL 2 — RSI
    # ════════════════════════════════════════════

    oversold  = sym_cfg.get("rsi_oversold", 30)
    overbought= sym_cfg.get("rsi_overbought", 70)
    valid_rsi = ~np.isnan(rsi_vals)

    ax_rsi.plot(x[valid_rsi], rsi_vals[valid_rsi],
                color=COLORS["rsi_line"], linewidth=1.0, zorder=3)

    # Rellenos zonas extremas
    ax_rsi.axhline(overbought, color=COLORS["rsi_ob"], linewidth=0.7,
                   linestyle="--", alpha=0.7)
    ax_rsi.axhline(oversold, color=COLORS["rsi_os"], linewidth=0.7,
                   linestyle="--", alpha=0.7)
    ax_rsi.axhline(50, color=COLORS["text_dim"], linewidth=0.4, alpha=0.4)
    ax_rsi.axhspan(overbought, 100, color=COLORS["rsi_ob"], alpha=0.06)
    ax_rsi.axhspan(0, oversold, color=COLORS["rsi_os"], alpha=0.06)

    # Colorear RSI según zona
    if valid_rsi.sum() > 0:
        rsi_display = rsi_vals.copy()
        rsi_display[~valid_rsi] = 50
        ax_rsi.fill_between(
            x, rsi_display, 50,
            where=(rsi_display >= 50),
            color=COLORS["rsi_os"], alpha=0.15
        )
        ax_rsi.fill_between(
            x, rsi_display, 50,
            where=(rsi_display < 50),
            color=COLORS["rsi_ob"], alpha=0.15
        )

    rsi_now = rsi_vals[valid_rsi][-1] if valid_rsi.sum() > 0 else 50
    ax_rsi.set_ylabel(f"RSI({rsi_now:.1f})", color=COLORS["text_dim"], fontsize=7.5)
    ax_rsi.set_ylim(0, 100)
    ax_rsi.text(
        n * 0.98, rsi_now, f"{rsi_now:.1f}",
        color=COLORS["rsi_line"], fontsize=7, va="center", ha="right"
    )

    # ════════════════════════════════════════════
    #  PANEL 3 — MACD
    # ════════════════════════════════════════════

    valid_macd = ~np.isnan(macd_hist)
    if valid_macd.sum() > 0:
        # Histograma coloreado
        bull_mask = macd_hist >= 0
        bear_mask = macd_hist < 0
        ax_macd.bar(x[valid_macd & bull_mask], macd_hist[valid_macd & bull_mask],
                    color=COLORS["macd_bull"], alpha=0.8, width=0.8, zorder=3)
        ax_macd.bar(x[valid_macd & bear_mask], macd_hist[valid_macd & bear_mask],
                    color=COLORS["macd_bear"], alpha=0.8, width=0.8, zorder=3)

        # Líneas MACD y señal
        ax_macd.plot(x[valid_macd], macd_line[valid_macd],
                     color=COLORS["macd_line"], linewidth=0.9, zorder=4, label="MACD")
        ax_macd.plot(x[valid_macd], macd_signal[valid_macd],
                     color=COLORS["macd_signal"], linewidth=0.9, zorder=4, label="Signal")

    ax_macd.axhline(0, color=COLORS["text_dim"], linewidth=0.4, alpha=0.5)
    macd_now = ind.get("macd_hist", 0)
    ax_macd.set_ylabel(
        f"MACD({macd_now:+.4f})", color=COLORS["text_dim"], fontsize=7.5
    )
    ax_macd.legend(
        loc="upper left", fontsize=6.5,
        facecolor=COLORS["bg"], edgecolor=COLORS["grid"],
        labelcolor=COLORS["text_dim"], framealpha=0.7,
    )

    # ════════════════════════════════════════════
    #  PANEL 4 — HILBERT (senos y cosenos — TESIS)
    # ════════════════════════════════════════════

    ax_hilbert.plot(x, sine_wave,
                    color=COLORS["hilbert_sine"], linewidth=1.0,
                    label=f"Sine | fase={hilbert.get('phase', 0):.0f}°")
    ax_hilbert.plot(x, lead_wave,
                    color=COLORS["hilbert_lead"], linewidth=0.8,
                    linestyle="--", label="Lead (+45°)", alpha=0.8)

    ax_hilbert.axhline(0.85, color=COLORS["candle_bear"], linewidth=0.5,
                       linestyle=":", alpha=0.6)
    ax_hilbert.axhline(-0.85, color=COLORS["candle_bull"], linewidth=0.5,
                        linestyle=":", alpha=0.6)
    ax_hilbert.axhline(0, color=COLORS["text_dim"], linewidth=0.3, alpha=0.4)

    # Etiquetas de zonas Hilbert
    ax_hilbert.text(2, 0.87, "MAX local (vender)",
                    color=COLORS["candle_bear"], fontsize=6, alpha=0.8)
    ax_hilbert.text(2, -0.97, "MIN local (comprar)",
                    color=COLORS["candle_bull"], fontsize=6, alpha=0.8)

    ax_hilbert.set_ylim(-1.1, 1.1)
    ax_hilbert.set_ylabel(
        f"Hilbert T={hilbert.get('period', 0):.0f}v",
        color=COLORS["text_dim"], fontsize=7.5
    )
    ax_hilbert.legend(
        loc="upper right", fontsize=6,
        facecolor=COLORS["bg"], edgecolor=COLORS["grid"],
        labelcolor=COLORS["text_dim"], framealpha=0.7,
    )

    # ── Eje X compartido — etiquetas de tiempo ──
    tick_step = max(1, n // 12)
    tick_positions = list(range(0, n, tick_step))
    ax_hilbert.set_xticks(tick_positions)
    ax_hilbert.set_xticklabels(
        [times[i] if i < len(times) else "" for i in tick_positions],
        rotation=30, ha="right", fontsize=6.5, color=COLORS["text_dim"]
    )
    ax_hilbert.set_xlabel("Tiempo (UTC)", color=COLORS["text_dim"], fontsize=7.5)

    # ════════════════════════════════════════════
    #  TÍTULO PRINCIPAL DEL GRÁFICO
    # ════════════════════════════════════════════

    now_str   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    h_signal  = hilbert.get("signal", "NEUTRAL")
    hurst_reg = ind.get("hurst_regime", "?")

    main_title = (
        f"ZAR v6 — {symbol}  |  {direction}  |  R:R 1:{rr:.1f}  |  "
        f"Hurst={ind.get('hurst', 0):.2f} [{hurst_reg}]  |  "
        f"Hilbert: {h_signal}  |  {now_str}"
    )
    fig.suptitle(main_title, color=COLORS["text"], fontsize=10, fontweight="bold", y=0.97)

    # ── Info box con razón de decisión ──
    reason_short = (reason[:180] + "...") if len(reason) > 180 else reason
    fig.text(
        0.07, 0.015,
        f"📊 Decisión: {reason_short}",
        color=COLORS["text_dim"], fontsize=7,
        wrap=True, va="bottom",
        bbox=dict(
            boxstyle="round,pad=0.4",
            facecolor=COLORS["bg_panel"],
            edgecolor=COLORS["grid"],
            alpha=0.9,
        )
    )

    # ── Renderizar a bytes PNG ──
    buf = io.BytesIO()
    fig.savefig(
        buf, format="png",
        dpi=150,
        facecolor=COLORS["bg"],
        bbox_inches="tight",
    )
    buf.seek(0)
    image_bytes = buf.read()
    plt.close(fig)

    log.info(
        f"[chart] ✅ Gráfico generado para {symbol} "
        f"({len(image_bytes) / 1024:.0f} KB)"
    )
    return image_bytes


# ════════════════════════════════════════════════════════════════
#  FUNCIONES AUXILIARES DE INDICADORES (serie completa)
# ════════════════════════════════════════════════════════════════

def _ema(series: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(series), np.nan)
    if len(series) < period:
        return result
    alpha = 2 / (period + 1)
    result[period - 1] = np.mean(series[:period])
    for i in range(period, len(series)):
        result[i] = alpha * series[i] + (1 - alpha) * result[i - 1]
    return result

def _bollinger(closes: np.ndarray, period: int = 20, std: float = 2.0):
    n = len(closes)
    upper  = np.full(n, np.nan)
    mid    = np.full(n, np.nan)
    lower  = np.full(n, np.nan)
    for i in range(period - 1, n):
        window    = closes[i - period + 1:i + 1]
        m         = np.mean(window)
        s         = np.std(window)
        mid[i]    = m
        upper[i]  = m + std * s
        lower[i]  = m - std * s
    return upper, mid, lower

def _rsi_series(closes: np.ndarray, period: int = 14) -> np.ndarray:
    n      = len(closes)
    result = np.full(n, np.nan)
    if n < period + 1:
        return result
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g  = np.mean(gains[:period])
    avg_l  = np.mean(losses[:period])
    for i in range(period, n):
        avg_g = (avg_g * (period - 1) + gains[i - 1]) / period
        avg_l = (avg_l * (period - 1) + losses[i - 1]) / period
        rs    = avg_g / avg_l if avg_l != 0 else 0
        result[i] = 100 - (100 / (1 + rs))
    return result

def _macd_series(closes: np.ndarray, fast=12, slow=26, signal=9):
    n     = len(closes)
    macd  = np.full(n, np.nan)
    sig   = np.full(n, np.nan)
    hist  = np.full(n, np.nan)
    if n < slow + signal:
        return macd, sig, hist
    ef    = _ema(closes, fast)
    es    = _ema(closes, slow)
    macd  = ef - es
    valid = ~np.isnan(macd)
    sig_temp = np.full(n, np.nan)
    first_valid = np.argmax(valid)
    if first_valid + signal <= n:
        sig_temp[first_valid + signal - 1] = np.nanmean(macd[first_valid:first_valid + signal])
        alpha = 2 / (signal + 1)
        for i in range(first_valid + signal, n):
            if not np.isnan(sig_temp[i - 1]) and not np.isnan(macd[i]):
                sig_temp[i] = alpha * macd[i] + (1 - alpha) * sig_temp[i - 1]
    hist = macd - sig_temp
    return macd, sig_temp, hist

def _kalman_simple(closes: np.ndarray, R: float = 0.01, Q: float = 0.0001) -> np.ndarray:
    n      = len(closes)
    result = np.zeros(n)
    P      = 1.0
    x      = closes[0]
    for i in range(n):
        P        += Q
        K         = P / (P + R)
        x         = x + K * (closes[i] - x)
        P         = (1 - K) * P
        result[i] = x
    return result


# ════════════════════════════════════════════════════════════════
#  ENVÍO A TELEGRAM
# ════════════════════════════════════════════════════════════════

def send_chart_to_telegram(
    image_bytes: bytes,
    caption:     str,
    token:       str,
    chat_id:     str,
) -> bool:
    if not image_bytes:
        return False

    # Telegram limita captions a 1024 chars en sendPhoto
    caption = caption[:1024]

    try:
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        response = requests.post(
            url,
            data={
                "chat_id":    chat_id,
                "caption":    caption,
                "parse_mode": "HTML",   # FIX: HTML es más robusto que Markdown
            },
            files={
                "photo": ("chart.png", image_bytes, "image/png"),
            },
            timeout=30,
        )
        if response.status_code == 200:
            log.info("[chart] ✅ Imagen enviada a Telegram correctamente")
            return True
        else:
            log.warning(f"[chart] ⚠️ Telegram HTTP {response.status_code}: {response.text[:150]}")
            # Retry sin parse_mode si falla por HTML
            response2 = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption[:200]},
                files={"photo": ("chart.png", image_bytes, "image/png")},
                timeout=30,
            )
            if response2.status_code == 200:
                log.info("[chart] ✅ Imagen enviada (sin formato)")
                return True
            return False
    except Exception as e:
        log.error(f"[chart] ❌ Error enviando imagen a Telegram: {e}")
        return False


"""
FIX v6.4 — build_telegram_caption:
  • Limita reason a 150 chars ANTES de escapar (evita HTML roto por truncamiento)
  • Caption total ≤ 900 chars (Telegram permite 1024 pero dejamos margen de seguridad)
  • Evita cortar tags HTML a la mitad
"""

import html as html_mod

def _safe_caption(text: str, max_len: int = 900) -> str:
    """
    FIX 10: Trunca un caption HTML de forma segura.
    Si el texto es muy largo, lo trunca en el último '>' antes del límite,
    evitando cortar a mitad de un tag HTML y romper el parser de Telegram.
    """
    if len(text) <= max_len:
        return text
    truncated = text[:max_len - 15]
    # Buscar el último cierre de tag antes del límite
    last_close = truncated.rfind(">")
    last_open  = truncated.rfind("<")
    if last_open > last_close:
        # Estamos dentro de un tag incompleto → cortar antes del tag
        truncated = truncated[:last_open]
    return truncated.rstrip() + "\n…[truncado]"


def build_telegram_caption(
    symbol: str, direction: str,
    price: float, sl: float, tp: float,
    rr: float, volume: float,
    ind: dict, hilbert: dict,
    hurst: float, fisher: float,
    reason: str, sym_cfg: dict,
) -> str:
    """
    FIX 10: Caption HTML seguro para Telegram.
    - Reason limitado a 150 chars antes de escapar
    - Total limitado a 900 chars con truncamiento seguro
    """
    def e(t): return html_mod.escape(str(t))

    dir_icon  = "🟢 BUY" if direction == "BUY" else "🔴 SELL"
    h_signal  = hilbert.get("signal", "NEUTRAL")
    h_sine    = hilbert.get("sine", 0)
    hurst_reg = ind.get("hurst_regime", "?")
    strategy  = sym_cfg.get("strategy_type", "")
    hurst_icon = (
        "📈 TENDENCIA" if hurst > 0.6 else
        "🎲 ALEATORIO" if hurst > 0.45 else
        "↩️ REVERSIÓN"
    )
    fisher_tag = " ⚠️OB" if fisher > 2 else (" ⚠️OS" if fisher < -2 else "")

    # FIX: limitar reason a 150 chars ANTES de escapar
    reason_short = e(reason[:150])

    lines = [
        f"📊 <b>{e(symbol)}</b> — {e(sym_cfg.get('name',''))}",
        f"📋 <code>{e(strategy)}</code>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🎯 <b>{dir_icon}</b>  @<code>{price:.5f}</code>",
        f"🛑 SL: <code>{sl:.5f}</code>",
        f"✅ TP: <code>{tp:.5f}</code>",
        f"⚖️ R:R: <code>1:{rr:.2f}</code>  |  Vol: <code>{volume}</code>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🌀 Hilbert: <code>{e(h_signal)}</code> | sine=<code>{h_sine:.3f}</code>",
        f"📐 Hurst: <code>{hurst:.3f}</code> {hurst_icon}",
        f"🐟 Fisher: <code>{fisher:.3f}</code>{fisher_tag}",
        f"📊 RSI: <code>{ind.get('rsi',0):.1f}</code> | MACD: <code>{e(str(ind.get('macd_dir','?')))}</code>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"💡 <i>{reason_short}</i>",
    ]
    full = "\n".join(lines)
    # FIX: truncar de forma segura
    return _safe_caption(full, max_len=900)