#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — run_backtest.py  (FASE 8)             ║
║                                                                  ║
║   Script CLI para ejecutar backtests históricos desde terminal.  ║
║                                                                  ║
║   Uso básico:                                                    ║
║     python run_backtest.py --symbol XAUUSDm                     ║
║     python run_backtest.py --all --start 2023-01-01 --export    ║
║     python run_backtest.py --symbol EURUSDm --walk-forward      ║
║     python run_backtest.py --symbol XAUUSDm --csv-input data.csv║
║     python run_backtest.py --symbol EURUSDm --truefx            ║
║                                                                  ║
║   Benchmarks mínimos aceptables:                                 ║
║   • Win Rate        >= 45%                                       ║
║   • Profit Factor   >= 1.3                                       ║
║   • Sharpe Ratio    >= 0.5                                       ║
║   • Max Drawdown    <= 15%                                       ║
╚══════════════════════════════════════════════════════════════════╝
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from typing import List, Optional

# Añadir raíz del repo al path para imports
sys.path.insert(0, os.path.dirname(__file__))

import config as cfg
from modules.backtester import Backtester
from modules.backtest_report import (
    export_metrics_csv,
    export_trades_csv,
    print_summary,
    print_walk_forward_summary,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("run_backtest")

# ── Carpeta de resultados ────────────────────────────────────────
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "backtest_results")

# ── Constante timeframe por defecto (M1 en entero MT5) ──────────
_MT5_TF_M1 = 1   # mt5.TIMEFRAME_M1 == 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ZAR Ultimate Bot v6 — Backtester histórico (FASE 8)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Símbolo(s)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--symbol",
        metavar="SYM",
        help="Símbolo a testear (e.g. XAUUSDm, EURUSDm)",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Testear todos los símbolos configurados en config.SYMBOLS",
    )

    # Fechas
    parser.add_argument(
        "--start",
        default=cfg.BACKTEST_DEFAULT_START,
        metavar="YYYY-MM-DD",
        help=f"Fecha inicio (default: {cfg.BACKTEST_DEFAULT_START})",
    )
    parser.add_argument(
        "--end",
        default=cfg.BACKTEST_DEFAULT_END,
        metavar="YYYY-MM-DD",
        help=f"Fecha fin (default: {cfg.BACKTEST_DEFAULT_END})",
    )

    # Walk-forward
    parser.add_argument(
        "--walk-forward",
        action="store_true",
        help="Activar Walk-Forward Testing (ventana deslizante train/test)",
    )
    parser.add_argument(
        "--train-months",
        type=int,
        default=cfg.BACKTEST_WALK_FORWARD_TRAIN_MONTHS,
        metavar="N",
        help=f"Meses de entrenamiento (default: {cfg.BACKTEST_WALK_FORWARD_TRAIN_MONTHS})",
    )
    parser.add_argument(
        "--test-months",
        type=int,
        default=cfg.BACKTEST_WALK_FORWARD_TEST_MONTHS,
        metavar="N",
        help=f"Meses de test out-of-sample (default: {cfg.BACKTEST_WALK_FORWARD_TEST_MONTHS})",
    )
    parser.add_argument(
        "--step-months",
        type=int,
        default=cfg.BACKTEST_WALK_FORWARD_STEP_MONTHS,
        metavar="N",
        help=f"Paso de avance en meses (default: {cfg.BACKTEST_WALK_FORWARD_STEP_MONTHS})",
    )

    # Fuente de datos
    parser.add_argument(
        "--csv-input",
        metavar="FILE",
        help="Cargar datos desde CSV en lugar de MT5 (modo offline)",
    )
    parser.add_argument(
        "--truefx",
        action="store_true",
        help="Cargar datos de data/truefx/ (tick data TrueFX) en vez de MT5",
    )

    # Exportación
    parser.add_argument(
        "--export",
        action="store_true",
        help=f"Exportar resultados a CSV en {RESULTS_DIR}/",
    )

    # Balance inicial
    parser.add_argument(
        "--balance",
        type=float,
        default=cfg.BACKTEST_INITIAL_BALANCE,
        metavar="USD",
        help=f"Balance inicial para simulación (default: {cfg.BACKTEST_INITIAL_BALANCE})",
    )

    return parser.parse_args()


def _parse_date(date_str: str) -> datetime:
    """Convierte string 'YYYY-MM-DD' a datetime UTC."""
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _run_single(
    symbol: str,
    args: argparse.Namespace,
) -> Optional[object]:
    """
    Ejecuta el backtest para un único símbolo.

    Returns:
        BacktestMetrics si se ejecutó correctamente, None si hubo error.
    """
    sym_cfg = cfg.SYMBOLS.get(symbol)
    if sym_cfg is None:
        log.error(f"Símbolo '{symbol}' no encontrado en config.SYMBOLS")
        return None

    log.info(f"╔══ Iniciando backtest: {symbol} ══")
    log.info(f"   Período : {args.start} → {args.end}")

    bt = Backtester(initial_balance=args.balance)

    # ── Carga de datos ───────────────────────────────────────────
    if args.csv_input:
        ok = bt.load_data_csv(args.csv_input)
        if not ok:
            log.error(f"No se pudo cargar CSV: {args.csv_input}")
            return None
    elif args.truefx:
        log.info(f"   Fuente: TrueFX (data/truefx/)")
        ok = bt.load_data_truefx(symbol, args.start, args.end)
        if not ok:
            log.error(
                f"No se pudo cargar datos TrueFX para {symbol}. "
                "Verifica que existan archivos en data/truefx/ "
                f"(e.g. {symbol.rstrip('m')}-YYYY-MM.csv)"
            )
            return None
    else:
        try:
            import MetaTrader5 as mt5  # type: ignore
            tf = mt5.TIMEFRAME_M1
        except ImportError:
            log.warning("MetaTrader5 no disponible — usando TIMEFRAME_M1=1")
            tf = _MT5_TF_M1

        date_from = _parse_date(args.start)
        date_to   = _parse_date(args.end)

        ok = bt.load_data_mt5(symbol, tf, date_from, date_to)
        if not ok:
            log.error(
                f"No se pudo descargar datos de MT5 para {symbol}. "
                "Asegúrate de que MT5 esté abierto con la cuenta logueada."
            )
            return None

    # ── Backtest o Walk-Forward ──────────────────────────────────
    if args.walk_forward:
        log.info(
            f"   Modo: Walk-Forward "
            f"(train={args.train_months}m, test={args.test_months}m, step={args.step_months}m)"
        )
        wf_results = bt.run_walk_forward(
            symbol,
            sym_cfg,
            train_months=args.train_months,
            test_months=args.test_months,
            step_months=args.step_months,
        )
        print_walk_forward_summary(wf_results, symbol)

        if args.export and wf_results:
            _export_walk_forward(wf_results, symbol)

        return None   # Walk-forward no retorna métricas simples

    else:
        log.info("   Modo: Backtest completo")
        metrics, trades = bt.run(symbol, sym_cfg)
        print_summary(metrics, symbol)

        if args.export:
            _export_results(metrics, trades, symbol)

        return metrics


def _export_results(metrics, trades: list, symbol: str) -> None:
    """Exporta métricas y trades a CSV."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    trades_path  = os.path.join(RESULTS_DIR, f"{symbol}_trades_{ts}.csv")
    metrics_path = os.path.join(RESULTS_DIR, f"{symbol}_metrics_{ts}.csv")

    export_trades_csv(trades, trades_path)
    export_metrics_csv([metrics], metrics_path)


def _export_walk_forward(wf_results: list, symbol: str) -> None:
    """Exporta resultados walk-forward a CSV."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Exportar métricas IS y OOS en un solo archivo
    metrics_list = []
    for w in wf_results:
        tm = w["train_metrics"]
        om = w["test_metrics"]
        tm.symbol = f"{symbol}_IS_{w['window_start']}"
        om.symbol = f"{symbol}_OOS_{w['train_end']}"
        metrics_list.extend([tm, om])

    metrics_path = os.path.join(RESULTS_DIR, f"{symbol}_wf_metrics_{ts}.csv")
    export_metrics_csv(metrics_list, metrics_path)


def main() -> None:
    args = _parse_args()

    symbols_to_run: List[str]
    if args.all:
        symbols_to_run = list(cfg.SYMBOLS.keys())
        log.info(f"Testeando todos los {len(symbols_to_run)} símbolos configurados")
    else:
        symbols_to_run = [args.symbol]

    all_metrics = []

    for symbol in symbols_to_run:
        try:
            metrics = _run_single(symbol, args)
            if metrics is not None:
                all_metrics.append(metrics)
        except KeyboardInterrupt:
            log.info("Interrumpido por el usuario")
            break
        except Exception as e:
            log.error(f"Error en backtest de {symbol}: {e}", exc_info=True)

    # Si se corrió --all y --export, exportar tabla comparativa
    if args.all and args.export and all_metrics:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        summary_path = os.path.join(RESULTS_DIR, f"ALL_metrics_{ts}.csv")
        export_metrics_csv(all_metrics, summary_path)
        log.info(f"Resumen global exportado → {summary_path}")


if __name__ == "__main__":
    main()
