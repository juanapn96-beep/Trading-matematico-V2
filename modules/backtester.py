"""
╔══════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — backtester.py  (FASE 8)               ║
║                                                                  ║
║   Motor de backtesting histórico para validar estadísticamente  ║
║   los parámetros matemáticos del bot sin depender del LLM.      ║
║                                                                  ║
║   Fuentes de datos:                                              ║
║   1. MT5 en vivo — mt5.copy_rates_range() (broker Exness)       ║
║   2. CSV offline — columnas time,open,high,low,close,volume     ║
║                                                                  ║
║   Señales evaluadas (sin Groq/LLM):                             ║
║   • confluence["total"] > CONFLUENCE_MIN_SCORE                  ║
║   • Hurst >= sym_cfg["min_hurst"]                               ║
║   • ATR tradeable (is_market_tradeable check)                   ║
║   • h1_trend alineado con dirección (no LATERAL puro)           ║
║   • Hilbert no bloquea dirección                                ║
║   • R:R válido (is_rr_valid)                                    ║
║                                                                  ║
║   Walk-Forward: ventana train/test deslizante para detectar     ║
║   overfitting de parámetros.                                    ║
╚══════════════════════════════════════════════════════════════════╝
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import config as cfg
from modules.indicators import compute_all
from modules.risk_manager import calc_sl_tp, is_rr_valid, get_rr

log = logging.getLogger(__name__)

# ── Constantes internas ──────────────────────────────────────────
_WINDOW_BARS      = 300    # Número de barras históricas pasadas para compute_all
_PROGRESS_STEP    = 500    # Loguear progreso cada N barras
_MAX_PROFIT_FACTOR = 9.99  # Cap para Profit Factor cuando no hay pérdidas


# ══════════════════════════════════════════════════════════════════
#  DATACLASSES
# ══════════════════════════════════════════════════════════════════

@dataclass
class SimTrade:
    """Representa un trade simulado."""
    symbol:       str
    direction:    str          # "BUY" | "SELL"
    entry_time:   datetime
    entry_price:  float
    sl:           float
    tp:           float
    exit_time:    Optional[datetime] = None
    exit_price:   Optional[float]    = None
    result:       str               = "OPEN"   # WIN | LOSS | BE | OPEN
    pnl_pips:     float             = 0.0
    rr_realized:  float             = 0.0
    duration_bars: int              = 0
    entry_bar_idx: int              = 0
    # Estado trailing interno
    _trail_stage:  int              = 0
    _be_moved:     bool             = False


@dataclass
class BacktestMetrics:
    """Métricas calculadas al finalizar el backtest."""
    symbol:           str    = ""
    total_trades:     int    = 0
    wins:             int    = 0
    losses:           int    = 0
    breakevens:       int    = 0
    win_rate:         float  = 0.0   # Wins/(Wins+Losses) excluyendo BE
    profit_factor:    float  = 0.0
    sharpe_ratio:     float  = 0.0
    max_drawdown_pct: float  = 0.0
    avg_rr:           float  = 0.0
    avg_duration_bars: float = 0.0
    best_trade_pips:  float  = 0.0
    worst_trade_pips: float  = 0.0
    total_pips:       float  = 0.0
    period_start:     str    = ""
    period_end:       str    = ""


# ══════════════════════════════════════════════════════════════════
#  CLASE PRINCIPAL
# ══════════════════════════════════════════════════════════════════

class Backtester:
    """
    Motor de backtesting histórico para ZAR Ultimate Bot v6.

    Uso típico:
        bt = Backtester()
        bt.load_data_mt5("XAUUSDm", mt5.TIMEFRAME_M1, date_from, date_to)
        metrics, trades = bt.run("XAUUSDm", sym_cfg)
    """

    def __init__(self, initial_balance: float = None):
        self.initial_balance = initial_balance or cfg.BACKTEST_INITIAL_BALANCE
        self._df: Optional[pd.DataFrame] = None
        self._symbol: str = ""

    # ──────────────────────────────────────────────────────────────
    #  CARGA DE DATOS
    # ──────────────────────────────────────────────────────────────

    def load_data_mt5(
        self,
        symbol: str,
        timeframe,
        date_from: datetime,
        date_to: datetime,
    ) -> bool:
        """
        Descarga datos históricos de MT5 usando copy_rates_range().

        Args:
            symbol:    Símbolo (e.g. "XAUUSDm")
            timeframe: Constante MT5 (e.g. mt5.TIMEFRAME_M1)
            date_from: Fecha inicio (datetime con tzinfo)
            date_to:   Fecha fin   (datetime con tzinfo)

        Returns:
            True si se cargaron datos correctamente.
        """
        try:
            import MetaTrader5 as mt5  # type: ignore
        except ImportError:
            log.error("[backtester] MetaTrader5 no instalado. Usa load_data_csv() en su lugar.")
            return False

        if not mt5.initialize():
            log.error(f"[backtester] mt5.initialize() falló: {mt5.last_error()}")
            return False

        rates = mt5.copy_rates_range(symbol, timeframe, date_from, date_to)
        if rates is None or len(rates) == 0:
            log.error(f"[backtester] No se obtuvieron datos de MT5 para {symbol}")
            mt5.shutdown()
            return False

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        if "tick_volume" in df.columns and "volume" not in df.columns:
            df.rename(columns={"tick_volume": "volume"}, inplace=True)
        df = df[["time", "open", "high", "low", "close", "volume"]].copy()
        df.reset_index(drop=True, inplace=True)

        self._df = df
        self._symbol = symbol
        log.info(f"[backtester] MT5: {len(df)} barras cargadas para {symbol}")
        mt5.shutdown()
        return True

    def load_data_csv(self, filepath: str) -> bool:
        """
        Carga datos históricos desde CSV (modo offline).

        El CSV debe tener columnas: time,open,high,low,close,volume
        La columna time puede ser cualquier formato que pandas reconozca.

        Returns:
            True si se cargaron datos correctamente.
        """
        try:
            df = pd.read_csv(filepath)
            df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
            df = df.dropna(subset=["time"])
            required = {"open", "high", "low", "close", "volume"}
            missing = required - set(df.columns)
            if missing:
                log.error(f"[backtester] CSV faltan columnas: {missing}")
                return False
            df = df[["time", "open", "high", "low", "close", "volume"]].copy()
            df.reset_index(drop=True, inplace=True)
            self._df = df
            log.info(f"[backtester] CSV: {len(df)} barras cargadas desde {filepath}")
            return True
        except Exception as e:
            log.error(f"[backtester] Error cargando CSV {filepath}: {e}", exc_info=True)
            return False

    # ──────────────────────────────────────────────────────────────
    #  BACKTEST PRINCIPAL
    # ──────────────────────────────────────────────────────────────

    def run(
        self,
        symbol: str,
        sym_cfg: dict,
        df_override: Optional[pd.DataFrame] = None,
    ) -> Tuple[BacktestMetrics, List[SimTrade]]:
        """
        Ejecuta el backtest sobre los datos cargados (o df_override).

        Args:
            symbol:      Símbolo a testear
            sym_cfg:     Configuración del símbolo (de config.SYMBOLS)
            df_override: Si se provee, usa este DataFrame en lugar de self._df

        Returns:
            (BacktestMetrics, list[SimTrade])
        """
        df = df_override if df_override is not None else self._df
        if df is None or len(df) < _WINDOW_BARS + 10:
            log.warning(f"[backtester] Datos insuficientes para {symbol} ({len(df) if df is not None else 0} barras)")
            return BacktestMetrics(symbol=symbol), []

        trades: List[SimTrade] = []
        open_trades: List[SimTrade] = []
        total_bars = len(df)

        for i in range(_WINDOW_BARS, total_bars):
            bar = df.iloc[i]

            # 1. Cerrar/gestionar trades abiertos con la barra actual
            self._check_open_trades(bar, open_trades, trades)

            # 2. Solo un trade abierto a la vez por símbolo (como el bot real)
            if open_trades:
                continue

            # 3. Intentar abrir trade nuevo
            new_trade = self._simulate_bar(i, df, symbol, sym_cfg)
            if new_trade is not None:
                open_trades.append(new_trade)

            if i % _PROGRESS_STEP == 0:
                log.debug(
                    f"[backtester] {symbol}: barra {i}/{total_bars} "
                    f"— trades={len(trades)} abiertos={len(open_trades)}"
                )

        # Cerrar trades aún abiertos al final del período (forzar cierre al precio de la última barra)
        if df is not None and len(df) > 0:
            last_bar = df.iloc[-1]
            for t in open_trades:
                t.exit_time  = last_bar["time"]
                t.exit_price = float(last_bar["close"])
                t.duration_bars = (len(df) - 1) - t.entry_bar_idx
                t.result = "OPEN"   # Trade no cerrado por SL/TP — excluir de métricas principales
                t.pnl_pips = (
                    (t.exit_price - t.entry_price) if t.direction == "BUY"
                    else (t.entry_price - t.exit_price)
                )
                trades.append(t)

        metrics = self._calculate_metrics(trades, symbol, df)
        return metrics, trades

    # ──────────────────────────────────────────────────────────────
    #  WALK-FORWARD TESTING
    # ──────────────────────────────────────────────────────────────

    def run_walk_forward(
        self,
        symbol: str,
        sym_cfg: dict,
        train_months: int = None,
        test_months:  int = None,
        step_months:  int = None,
    ) -> List[Dict]:
        """
        Walk-Forward Testing: ventana train → test out-of-sample → avanzar.

        Args:
            symbol:       Símbolo a testear
            sym_cfg:      Configuración del símbolo
            train_months: Meses de entrenamiento (default: cfg.BACKTEST_WALK_FORWARD_TRAIN_MONTHS)
            test_months:  Meses de test out-of-sample (default: cfg.BACKTEST_WALK_FORWARD_TEST_MONTHS)
            step_months:  Paso de avance (default: cfg.BACKTEST_WALK_FORWARD_STEP_MONTHS)

        Returns:
            Lista de dicts con métricas por ventana (in-sample + out-of-sample)
        """
        train_m = train_months or cfg.BACKTEST_WALK_FORWARD_TRAIN_MONTHS
        test_m  = test_months  or cfg.BACKTEST_WALK_FORWARD_TEST_MONTHS
        step_m  = step_months  or cfg.BACKTEST_WALK_FORWARD_STEP_MONTHS

        df = self._df
        if df is None or len(df) == 0:
            log.error("[backtester] No hay datos cargados para walk-forward")
            return []

        df = df.copy()
        if not pd.api.types.is_datetime64_any_dtype(df["time"]):
            df["time"] = pd.to_datetime(df["time"], utc=True)

        start_ts = df["time"].iloc[0]
        end_ts   = df["time"].iloc[-1]

        results = []
        window_start = start_ts

        while True:
            train_end = window_start + pd.DateOffset(months=train_m)
            test_end  = train_end    + pd.DateOffset(months=test_m)

            if test_end > end_ts:
                break

            # Ventana in-sample (entrenamiento)
            mask_train = (df["time"] >= window_start) & (df["time"] < train_end)
            df_train   = df[mask_train].reset_index(drop=True)

            # Ventana out-of-sample (test)
            mask_test  = (df["time"] >= train_end) & (df["time"] < test_end)
            df_test    = df[mask_test].reset_index(drop=True)

            if len(df_train) < _WINDOW_BARS + 10 or len(df_test) < 10:
                window_start += pd.DateOffset(months=step_m)
                continue

            metrics_train, _ = self.run(symbol, sym_cfg, df_override=df_train)
            metrics_test,  _ = self.run(symbol, sym_cfg, df_override=df_test)

            results.append({
                "window_start":      str(window_start.date()),
                "train_end":         str(train_end.date()),
                "test_end":          str(test_end.date()),
                "train_metrics":     metrics_train,
                "test_metrics":      metrics_test,
            })

            log.info(
                f"[wf] {symbol} ventana {window_start.date()} → {test_end.date()} "
                f"| Train WR={metrics_train.win_rate:.1f}% PF={metrics_train.profit_factor:.2f} "
                f"| Test WR={metrics_test.win_rate:.1f}% PF={metrics_test.profit_factor:.2f}"
            )

            window_start += pd.DateOffset(months=step_m)

        return results

    # ──────────────────────────────────────────────────────────────
    #  SIMULACIÓN DE UNA BARRA (señal de entrada)
    # ──────────────────────────────────────────────────────────────

    def _simulate_bar(
        self,
        bar_index: int,
        df: pd.DataFrame,
        symbol: str,
        sym_cfg: dict,
    ) -> Optional[SimTrade]:
        """
        Evalúa la lógica de señal en la barra bar_index.
        Reutiliza compute_all(), calc_sl_tp(), is_rr_valid() del bot real.

        Returns:
            SimTrade si se abre un trade, None si no hay señal.
        """
        # Ventana deslizante de _WINDOW_BARS barras hasta bar_index (exclusive)
        window_start = max(0, bar_index - _WINDOW_BARS)
        df_window = df.iloc[window_start:bar_index].copy()

        if len(df_window) < 50:
            return None

        bar = df.iloc[bar_index]

        try:
            ctx = compute_all(df_window, symbol, sym_cfg)
        except Exception as e:
            log.debug(f"[backtester] compute_all error en barra {bar_index}: {e}")
            return None

        if not ctx:
            return None

        # ── Filtro 1: ATR disponible y mercado activo ────────────
        atr_val = float(ctx.get("atr", 0.0))
        price   = float(bar["close"])
        if atr_val <= 0 or price <= 0:
            return None

        # Verificar ATR mínimo (sin llamar is_market_tradeable con reloj real)
        from modules.risk_manager import ATR_MIN_PCT, DEFAULT_ATR_MIN_PCT
        atr_pct = (atr_val / price) * 100.0
        min_atr = ATR_MIN_PCT.get(symbol, DEFAULT_ATR_MIN_PCT)
        if atr_pct < min_atr:
            return None

        # ── Filtro 2: Hurst mínimo ───────────────────────────────
        hurst = float(ctx.get("hurst", 0.5))
        min_hurst = float(sym_cfg.get("min_hurst", 0.38))
        if hurst < min_hurst:
            return None

        # ── Filtro 3: Confluencia mínima ─────────────────────────
        confluence = ctx.get("confluence", {})
        conf_total = float(confluence.get("total", 0.0))
        conf_min   = float(cfg.CONFLUENCE_MIN_SCORE)
        if abs(conf_total) < conf_min:
            return None

        # ── Filtro 4: Determinar dirección desde h1_trend ────────
        h1_trend  = ctx.get("h1_trend", "LATERAL")
        direction = _trend_to_direction(h1_trend)
        if direction is None:
            return None

        # El bias de confluencia debe coincidir con la dirección
        conf_bias = confluence.get("bias", "NEUTRAL")
        if conf_bias == "NEUTRAL":
            return None
        if direction == "BUY"  and conf_bias != "BULLISH":
            return None
        if direction == "SELL" and conf_bias != "BEARISH":
            return None

        # ── Filtro 5: Hilbert no bloquea dirección ───────────────
        hilbert = ctx.get("hilbert", {})
        hil_sig = hilbert.get("signal", "NEUTRAL")
        if direction == "BUY"  and hil_sig in ("SELL_CYCLE", "LOCAL_MAX"):
            return None
        if direction == "SELL" and hil_sig in ("BUY_CYCLE", "LOCAL_MIN"):
            return None

        # ── Filtro 6: Anti-contra-tendencia (Kalman + SuperTrend) ─
        kalman_trend   = ctx.get("kalman_trend", "LATERAL")
        supertrend_val = int(ctx.get("supertrend", 0))
        if direction == "BUY":
            if kalman_trend == "BAJISTA" and supertrend_val == -1:
                return None
        else:
            if kalman_trend == "ALCISTA" and supertrend_val == 1:
                return None

        # ── Cálculo SL/TP y validación R:R ───────────────────────
        try:
            sl, tp = calc_sl_tp(direction, price, atr_val, sym_cfg)
        except Exception:
            return None

        min_rr = float(sym_cfg.get("min_rr", 1.5))
        if not is_rr_valid(price, sl, tp, min_rr):
            return None

        rr = get_rr(price, sl, tp)

        # ── Abrir trade simulado ──────────────────────────────────
        entry_time = bar["time"]
        if hasattr(entry_time, "to_pydatetime"):
            entry_time = entry_time.to_pydatetime()

        trade = SimTrade(
            symbol=symbol,
            direction=direction,
            entry_time=entry_time,
            entry_price=price,
            sl=sl,
            tp=tp,
            rr_realized=rr,
            entry_bar_idx=bar_index,
        )
        log.debug(
            f"[backtester] {symbol} {direction} @ {price:.5f} "
            f"SL={sl:.5f} TP={tp:.5f} RR={rr:.2f} conf={conf_total:.2f}"
        )
        return trade

    # ──────────────────────────────────────────────────────────────
    #  GESTIÓN DE TRADES ABIERTOS (SL/TP/TRAILING)
    # ──────────────────────────────────────────────────────────────

    def _check_open_trades(
        self,
        bar: pd.Series,
        open_trades: List[SimTrade],
        closed_trades: List[SimTrade],
    ) -> None:
        """
        Para cada trade abierto, verifica si la barra actual toca SL o TP,
        y aplica el trailing stop proporcional (5 etapas).

        Modifica open_trades en lugar (elimina los cerrados) y añade a closed_trades.
        """
        high  = float(bar["high"])
        low   = float(bar["low"])
        close = float(bar["close"])

        to_close = []

        for trade in open_trades:
            trade.duration_bars += 1

            # ── Verificar SL tocado ──────────────────────────────
            if trade.direction == "BUY":
                if low <= trade.sl:
                    self._close_trade(trade, bar, "LOSS", trade.sl)
                    to_close.append(trade)
                    continue
            else:
                if high >= trade.sl:
                    self._close_trade(trade, bar, "LOSS", trade.sl)
                    to_close.append(trade)
                    continue

            # ── Verificar TP tocado ──────────────────────────────
            if trade.direction == "BUY":
                if high >= trade.tp:
                    self._close_trade(trade, bar, "WIN", trade.tp)
                    to_close.append(trade)
                    continue
            else:
                if low <= trade.tp:
                    self._close_trade(trade, bar, "WIN", trade.tp)
                    to_close.append(trade)
                    continue

            # ── Trailing stop proporcional ───────────────────────
            self._apply_trailing(trade, close)

        for t in to_close:
            open_trades.remove(t)
            closed_trades.append(t)

    def _close_trade(
        self,
        trade: SimTrade,
        bar: pd.Series,
        result: str,
        exit_price: float,
    ) -> None:
        """Cierra un trade y calcula pnl_pips."""
        trade.result     = result
        trade.exit_price = exit_price

        exit_time = bar["time"]
        if hasattr(exit_time, "to_pydatetime"):
            exit_time = exit_time.to_pydatetime()
        trade.exit_time = exit_time

        if trade.direction == "BUY":
            trade.pnl_pips = exit_price - trade.entry_price
        else:
            trade.pnl_pips = trade.entry_price - exit_price

        # Actualizar RR realizado
        risk = abs(trade.entry_price - trade.sl)
        if risk > 0:
            trade.rr_realized = round(trade.pnl_pips / risk, 2)

        # Breakeven: cerrado muy cerca del precio de entrada
        risk_price = abs(trade.entry_price - trade.sl)
        if risk_price > 0 and abs(trade.pnl_pips) < risk_price * 0.05:
            trade.result = "BE"

    def _apply_trailing(self, trade: SimTrade, current_price: float) -> None:
        """
        Trailing stop proporcional — 5 etapas igual que _manage_trailing_stop() de main.py.

        Etapas por progreso de TP:
            <30%   → BE (no mover SL todavía)
            >=30%  → Lock 15% del profit
            >=50%  → Lock 35%
            >=70%  → Lock 50%
            >=85%  → Lock 70%
        """
        open_p = trade.entry_price
        sl     = trade.sl
        tp     = trade.tp

        if trade.direction == "BUY":
            favorable_move = current_price - open_p
        else:
            favorable_move = open_p - current_price

        if favorable_move <= 0:
            return

        # Calcular progreso de TP
        tp_total = abs(tp - open_p)
        if tp_total <= 0:
            return
        if trade.direction == "BUY":
            tp_remaining = max(tp - current_price, 0.0)
        else:
            tp_remaining = max(current_price - tp, 0.0)
        tp_progress = max(0.0, min(1.0, 1.0 - (tp_remaining / tp_total)))

        # Tabla de etapas
        stage_defs = [
            (0.85, 0.70),
            (0.70, 0.50),
            (0.50, 0.35),
            (0.30, 0.15),
        ]

        lock_pct  = 0.0
        new_stage = 0
        for min_prog, pct in stage_defs:
            if tp_progress >= min_prog:
                lock_pct  = pct
                new_stage = int(min_prog * 100)
                break

        if lock_pct <= 0:
            return

        profit_price = abs(current_price - open_p)
        locked       = profit_price * lock_pct

        if trade.direction == "BUY":
            new_sl = open_p + locked
            if sl > 0 and new_sl <= sl:
                return
        else:
            new_sl = open_p - locked
            if sl > 0 and new_sl >= sl:
                return

        trade.sl          = round(new_sl, 5)
        trade._trail_stage = new_stage

    # ──────────────────────────────────────────────────────────────
    #  CÁLCULO DE MÉTRICAS
    # ──────────────────────────────────────────────────────────────

    def _calculate_metrics(
        self,
        trades: List[SimTrade],
        symbol: str,
        df: Optional[pd.DataFrame] = None,
    ) -> BacktestMetrics:
        """
        Calcula todas las métricas estándar a partir de la lista de trades.

        Win Rate excluye BE. Profit Factor = sum(gains)/sum(losses).
        Sharpe = mean(returns)/std(returns) * sqrt(252).
        Max Drawdown = peak-to-trough máximo en % del balance acumulado.
        """
        m = BacktestMetrics(symbol=symbol)

        if df is not None and len(df) > 0:
            t0 = df["time"].iloc[0]
            t1 = df["time"].iloc[-1]
            m.period_start = str(t0.date()) if hasattr(t0, "date") else str(t0)
            m.period_end   = str(t1.date()) if hasattr(t1, "date") else str(t1)

        # Filtrar solo trades cerrados por SL/TP/BE (no OPEN)
        closed = [t for t in trades if t.result in ("WIN", "LOSS", "BE")]

        m.total_trades = len(closed)
        if m.total_trades == 0:
            return m

        m.wins       = sum(1 for t in closed if t.result == "WIN")
        m.losses     = sum(1 for t in closed if t.result == "LOSS")
        m.breakevens = sum(1 for t in closed if t.result == "BE")

        decisive = m.wins + m.losses
        if decisive > 0:
            m.win_rate = round(100.0 * m.wins / decisive, 2)

        gains  = [t.pnl_pips for t in closed if t.pnl_pips > 0]
        losses = [abs(t.pnl_pips) for t in closed if t.pnl_pips < 0]

        total_gain = sum(gains)
        total_loss = sum(losses)
        m.total_pips = round(total_gain - total_loss, 2)

        if total_loss > 0:
            m.profit_factor = round(total_gain / total_loss, 2)
        elif total_gain > 0:
            m.profit_factor = _MAX_PROFIT_FACTOR   # Sin pérdidas → PF infinito, cap

        # Sharpe Ratio
        if m.total_trades >= 2:
            returns = np.array([t.pnl_pips for t in closed])
            ret_std = float(np.std(returns, ddof=1))
            if ret_std > 0:
                m.sharpe_ratio = round(float(np.mean(returns)) / ret_std * math.sqrt(252), 2)

        # Max Drawdown (en pips acumulados relativos al balance inicial simulado)
        # Nota: pnl_pips es la diferencia de precio bruta, no el P&L monetario exacto.
        # El resultado expresa la caída porcentual del capital simulado en pips.
        risk_per_trade = cfg.RISK_PER_TRADE * self.initial_balance
        balance_curve  = [self.initial_balance]
        running        = self.initial_balance
        for t in closed:
            # Escalar pips a unidades monetarias aproximadas usando riesgo por trade
            sl_distance = abs(t.entry_price - t.sl) if t.sl != 0 else 1.0
            if sl_distance > 0:
                pnl_money = (t.pnl_pips / sl_distance) * risk_per_trade
            else:
                pnl_money = t.pnl_pips
            running += pnl_money
            balance_curve.append(running)

        peak = self.initial_balance
        max_dd = 0.0
        for b in balance_curve:
            if b > peak:
                peak = b
            dd = (peak - b) / peak * 100.0 if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
        m.max_drawdown_pct = round(max_dd, 2)

        # R:R promedio
        rr_vals = [t.rr_realized for t in closed if t.rr_realized != 0]
        if rr_vals:
            m.avg_rr = round(float(np.mean(rr_vals)), 2)

        # Duración promedio
        dur_vals = [t.duration_bars for t in closed]
        if dur_vals:
            m.avg_duration_bars = round(float(np.mean(dur_vals)), 1)

        # Mejor y peor trade
        pnl_vals = [t.pnl_pips for t in closed]
        if pnl_vals:
            m.best_trade_pips  = round(max(pnl_vals), 5)
            m.worst_trade_pips = round(min(pnl_vals), 5)

        return m


# ══════════════════════════════════════════════════════════════════
#  UTILIDADES PRIVADAS
# ══════════════════════════════════════════════════════════════════

def _trend_to_direction(h1_trend: str) -> Optional[str]:
    """
    Convierte h1_trend a "BUY" / "SELL" / None.

    Tendencias claramente alcistas → BUY.
    Tendencias claramente bajistas → SELL.
    LATERAL puro → None (no operar).
    """
    bullish = {"ALCISTA_FUERTE", "ALCISTA", "LATERAL_ALCISTA"}
    bearish = {"BAJISTA_FUERTE", "BAJISTA", "LATERAL_BAJISTA"}

    if h1_trend in bullish:
        return "BUY"
    if h1_trend in bearish:
        return "SELL"
    return None
