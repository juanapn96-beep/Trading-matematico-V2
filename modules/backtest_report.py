"""
╔══════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — backtest_report.py  (FASE 8)          ║
║                                                                  ║
║   Generación de reportes de backtesting:                        ║
║   • print_summary()            — consola con formato legible    ║
║   • print_walk_forward_summary() — tabla de ventanas WF         ║
║   • export_trades_csv()        — lista de trades a CSV          ║
║   • export_metrics_csv()       — métricas a CSV                 ║
║                                                                  ║
║   Benchmarks de referencia (mínimos aceptables):                ║
║   • Win Rate   >= 45%                                            ║
║   • Profit Factor >= 1.3                                         ║
║   • Sharpe Ratio  >= 0.5                                         ║
║   • Max Drawdown  <= 15%                                         ║
╚══════════════════════════════════════════════════════════════════╝
"""

import csv
import logging
import os
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# ── Benchmarks mínimos aceptables ───────────────────────────────
BENCHMARK_WIN_RATE_MIN    = 45.0
BENCHMARK_PROFIT_FACTOR   = 1.3
BENCHMARK_SHARPE_MIN      = 0.5
BENCHMARK_MAX_DRAWDOWN    = 15.0   # % máximo aceptable


def _pass_fail(value: float, threshold: float, higher_is_better: bool = True) -> str:
    """Devuelve ✅ o ❌ según si el valor pasa el benchmark."""
    if higher_is_better:
        return "✅" if value >= threshold else "❌"
    return "✅" if value <= threshold else "❌"


# ══════════════════════════════════════════════════════════════════
#  RESUMEN EN CONSOLA
# ══════════════════════════════════════════════════════════════════

def print_summary(metrics, symbol: str) -> None:
    """
    Imprime el resumen de métricas en consola con formato legible
    y comparación con benchmarks mínimos.

    Args:
        metrics: BacktestMetrics (de modules.backtester)
        symbol:  Símbolo analizado (para el título)
    """
    sep  = "═" * 58
    sep2 = "─" * 58

    print(f"\n{sep}")
    print(f"  BACKTEST REPORT — {symbol}")
    print(f"  Período: {metrics.period_start}  →  {metrics.period_end}")
    print(sep)

    # ── Estadísticas de trades ───────────────────────────────────
    print(f"  Total Trades     : {metrics.total_trades:>8}")
    print(f"  Wins             : {metrics.wins:>8}")
    print(f"  Losses           : {metrics.losses:>8}")
    print(f"  Breakevens (BE)  : {metrics.breakevens:>8}")
    print(sep2)

    # ── Métricas principales con benchmarks ─────────────────────
    wr_flag = _pass_fail(metrics.win_rate, BENCHMARK_WIN_RATE_MIN)
    pf_flag = _pass_fail(metrics.profit_factor, BENCHMARK_PROFIT_FACTOR)
    sr_flag = _pass_fail(metrics.sharpe_ratio, BENCHMARK_SHARPE_MIN)
    dd_flag = _pass_fail(metrics.max_drawdown_pct, BENCHMARK_MAX_DRAWDOWN, higher_is_better=False)

    print(f"  Win Rate         : {metrics.win_rate:>7.2f}%   {wr_flag}  (mín ≥ {BENCHMARK_WIN_RATE_MIN:.0f}%)")
    print(f"  Profit Factor    : {metrics.profit_factor:>8.2f}   {pf_flag}  (mín ≥ {BENCHMARK_PROFIT_FACTOR:.1f})")
    print(f"  Sharpe Ratio     : {metrics.sharpe_ratio:>8.2f}   {sr_flag}  (mín ≥ {BENCHMARK_SHARPE_MIN:.1f})")
    print(f"  Max Drawdown     : {metrics.max_drawdown_pct:>7.2f}%   {dd_flag}  (máx ≤ {BENCHMARK_MAX_DRAWDOWN:.0f}%)")
    print(sep2)

    # ── Métricas adicionales ─────────────────────────────────────
    print(f"  Avg R:R          : {metrics.avg_rr:>8.2f}")
    print(f"  Avg Duration     : {metrics.avg_duration_bars:>6.1f} barras")
    print(f"  Total Pips       : {metrics.total_pips:>+9.2f}")
    print(f"  Best Trade       : {metrics.best_trade_pips:>+9.5f} pips")
    print(f"  Worst Trade      : {metrics.worst_trade_pips:>+9.5f} pips")
    print(sep)

    # ── Veredicto global ─────────────────────────────────────────
    passed = sum([
        metrics.win_rate    >= BENCHMARK_WIN_RATE_MIN,
        metrics.profit_factor >= BENCHMARK_PROFIT_FACTOR,
        metrics.sharpe_ratio  >= BENCHMARK_SHARPE_MIN,
        metrics.max_drawdown_pct <= BENCHMARK_MAX_DRAWDOWN,
    ])
    if passed == 4:
        verdict = "🏆  TODOS LOS BENCHMARKS SUPERADOS"
    elif passed >= 3:
        verdict = f"⚠️   {passed}/4 benchmarks superados"
    else:
        verdict = f"❌  {passed}/4 benchmarks superados — PARÁMETROS REQUIEREN REVISIÓN"

    print(f"\n  {verdict}")
    print(f"{sep}\n")


# ══════════════════════════════════════════════════════════════════
#  RESUMEN WALK-FORWARD
# ══════════════════════════════════════════════════════════════════

def print_walk_forward_summary(wf_results: List[Dict], symbol: str) -> None:
    """
    Imprime una tabla comparativa de todas las ventanas Walk-Forward.

    Args:
        wf_results: Lista de dicts devuelta por Backtester.run_walk_forward()
        symbol:     Símbolo analizado
    """
    if not wf_results:
        print(f"\n[walk-forward] Sin resultados para {symbol}")
        return

    sep  = "═" * 90
    sep2 = "─" * 90
    hdr  = (
        f"  {'Ventana':<12}  {'Train→':<12}  {'Test→':<12}  "
        f"{'WR-IS':>7}  {'PF-IS':>6}  {'WR-OOS':>7}  {'PF-OOS':>7}  "
        f"{'DD-OOS':>7}  {'Trades':>6}"
    )

    print(f"\n{sep}")
    print(f"  WALK-FORWARD SUMMARY — {symbol}  ({len(wf_results)} ventanas)")
    print(sep)
    print(hdr)
    print(sep2)

    for w in wf_results:
        tm = w["train_metrics"]
        om = w["test_metrics"]
        row = (
            f"  {w['window_start']:<12}  {w['train_end']:<12}  {w['test_end']:<12}  "
            f"  {tm.win_rate:>5.1f}%  {tm.profit_factor:>6.2f}  "
            f"  {om.win_rate:>5.1f}%  {om.profit_factor:>7.2f}  "
            f"  {om.max_drawdown_pct:>5.1f}%  {om.total_trades:>6}"
        )
        print(row)

    print(sep2)

    # Promedios OOS
    oos_wr = [w["test_metrics"].win_rate    for w in wf_results]
    oos_pf = [w["test_metrics"].profit_factor for w in wf_results]
    oos_dd = [w["test_metrics"].max_drawdown_pct for w in wf_results]

    avg_wr = sum(oos_wr) / len(oos_wr) if oos_wr else 0
    avg_pf = sum(oos_pf) / len(oos_pf) if oos_pf else 0
    avg_dd = sum(oos_dd) / len(oos_dd) if oos_dd else 0

    print(
        f"  {'PROMEDIO OOS':<38}  "
        f"{'':>14}  {avg_wr:>5.1f}%  {avg_pf:>7.2f}  "
        f"  {avg_dd:>5.1f}%"
    )
    print(sep)

    # Advertencia de overfitting
    is_wr  = [w["train_metrics"].win_rate    for w in wf_results]
    is_pf  = [w["train_metrics"].profit_factor for w in wf_results]
    avg_is_wr = sum(is_wr) / len(is_wr) if is_wr else 0
    avg_is_pf = sum(is_pf) / len(is_pf) if is_pf else 0

    wr_decay = avg_is_wr - avg_wr
    pf_decay = avg_is_pf - avg_pf

    if wr_decay > 10 or pf_decay > 0.5:
        print(
            f"\n  ⚠️  POSIBLE OVERFITTING: WR cae {wr_decay:.1f}% OOS, "
            f"PF cae {pf_decay:.2f} OOS"
        )
    else:
        print(f"\n  ✅  Sin señales obvias de overfitting (WR decay={wr_decay:.1f}%)")
    print(f"{sep}\n")


# ══════════════════════════════════════════════════════════════════
#  EXPORTACIÓN A CSV
# ══════════════════════════════════════════════════════════════════

def export_trades_csv(trades: list, filepath: str) -> bool:
    """
    Exporta la lista de trades simulados a CSV.

    Columnas: symbol, direction, entry_time, entry_price, sl, tp,
              exit_time, exit_price, result, pnl_pips, rr_realized,
              duration_bars

    Args:
        trades:   Lista de SimTrade
        filepath: Ruta de destino (se crea el directorio si no existe)

    Returns:
        True si se exportó correctamente.
    """
    try:
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        fieldnames = [
            "symbol", "direction", "entry_time", "entry_price",
            "sl", "tp", "exit_time", "exit_price",
            "result", "pnl_pips", "rr_realized", "duration_bars",
        ]
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for t in trades:
                writer.writerow({
                    "symbol":        t.symbol,
                    "direction":     t.direction,
                    "entry_time":    str(t.entry_time),
                    "entry_price":   round(t.entry_price, 5),
                    "sl":            round(t.sl, 5),
                    "tp":            round(t.tp, 5),
                    "exit_time":     str(t.exit_time) if t.exit_time else "",
                    "exit_price":    round(t.exit_price, 5) if t.exit_price else "",
                    "result":        t.result,
                    "pnl_pips":      round(t.pnl_pips, 5),
                    "rr_realized":   t.rr_realized,
                    "duration_bars": t.duration_bars,
                })
        log.info(f"[report] Trades exportados → {filepath} ({len(trades)} registros)")
        return True
    except Exception as e:
        log.error(f"[report] Error exportando trades CSV: {e}", exc_info=True)
        return False


def export_metrics_csv(metrics_list: list, filepath: str) -> bool:
    """
    Exporta una lista de BacktestMetrics a CSV.
    Útil para comparar múltiples símbolos o ventanas walk-forward.

    Args:
        metrics_list: Lista de BacktestMetrics
        filepath:     Ruta de destino

    Returns:
        True si se exportó correctamente.
    """
    try:
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        fieldnames = [
            "symbol", "period_start", "period_end",
            "total_trades", "wins", "losses", "breakevens",
            "win_rate", "profit_factor", "sharpe_ratio",
            "max_drawdown_pct", "avg_rr", "avg_duration_bars",
            "total_pips", "best_trade_pips", "worst_trade_pips",
        ]
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for m in metrics_list:
                writer.writerow({
                    "symbol":           m.symbol,
                    "period_start":     m.period_start,
                    "period_end":       m.period_end,
                    "total_trades":     m.total_trades,
                    "wins":             m.wins,
                    "losses":           m.losses,
                    "breakevens":       m.breakevens,
                    "win_rate":         m.win_rate,
                    "profit_factor":    m.profit_factor,
                    "sharpe_ratio":     m.sharpe_ratio,
                    "max_drawdown_pct": m.max_drawdown_pct,
                    "avg_rr":           m.avg_rr,
                    "avg_duration_bars": m.avg_duration_bars,
                    "total_pips":       m.total_pips,
                    "best_trade_pips":  m.best_trade_pips,
                    "worst_trade_pips": m.worst_trade_pips,
                })
        log.info(f"[report] Métricas exportadas → {filepath} ({len(metrics_list)} símbolos)")
        return True
    except Exception as e:
        log.error(f"[report] Error exportando métricas CSV: {e}", exc_info=True)
        return False
