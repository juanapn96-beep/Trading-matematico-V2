"""
exec_quality_monitor.py — Monitor activo de calidad de ejecución en tiempo real.

Mantiene un buffer rolling por símbolo con métricas de ejecución (slippage, latencia).
Detecta degradación y puede pausar la operativa si la calidad cae por debajo del umbral.
"""
import logging
import threading
import time
from collections import deque
from typing import Optional

log = logging.getLogger("zar.exec_quality")

# ── Cargar configuración con fallbacks ──────────────────────────────────────
try:
    from config import (
        EXEC_QUALITY_ENABLED,
        EXEC_QUALITY_WINDOW,
        EXEC_QUALITY_PAUSE_THRESHOLD,
        EXEC_QUALITY_MIN_SAMPLES,
        EXEC_QUALITY_NOTIFY_COOLDOWN_SEC,
        SYMBOLS,
    )
except ImportError:
    EXEC_QUALITY_ENABLED             = True
    EXEC_QUALITY_WINDOW              = 20
    EXEC_QUALITY_PAUSE_THRESHOLD     = 30.0
    EXEC_QUALITY_MIN_SAMPLES         = 5
    EXEC_QUALITY_NOTIFY_COOLDOWN_SEC = 3600
    SYMBOLS                          = {}

# ── Thread-safe state ────────────────────────────────────────────────────────
_lock: threading.Lock = threading.Lock()

# {symbol: deque[dict]}
_buffers: dict = {}

# {symbol: str}  — último status conocido para detectar transiciones
_last_status: dict = {}

# {symbol: float}  — timestamp de la última notificación de pausa
_last_pause_notify: dict = {}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_buffer(symbol: str) -> deque:
    """Devuelve (o crea) el buffer rolling del símbolo."""
    if symbol not in _buffers:
        _buffers[symbol] = deque(maxlen=int(EXEC_QUALITY_WINDOW))
    return _buffers[symbol]


def _max_allowed_avg_slippage(symbol: str) -> float:
    """
    Referencia de slippage máximo tolerable en pips para el símbolo.
    Usa max_spread_pips del config del símbolo como base; si no existe, 2.0.
    """
    sym_cfg = SYMBOLS.get(symbol, {}) if isinstance(SYMBOLS, dict) else {}
    val = sym_cfg.get("max_spread_pips", None)
    if val is None:
        # Intentar buscar en la lista de símbolos si es una lista de dicts
        if isinstance(SYMBOLS, list):
            for s in SYMBOLS:
                if isinstance(s, dict) and s.get("symbol") == symbol:
                    val = s.get("max_spread_pips", None)
                    break
    return float(val) if val else 2.0


def _compute_quality(buf: deque, symbol: str) -> dict:
    """Calcula métricas de calidad a partir del buffer."""
    if not buf:
        return {
            "avg_slippage_pips":  0.0,
            "p95_slippage_pips":  0.0,
            "max_slippage_pips":  0.0,
            "excessive_ratio":    0.0,
            "quality_score":      100.0,
            "sample_count":       0,
            "status":             "good",
        }

    slippages = [e["slippage_pips"] for e in buf]
    n         = len(slippages)
    avg       = sum(slippages) / n

    sorted_s  = sorted(slippages)
    p95_idx   = int(0.95 * n)
    p95       = sorted_s[min(p95_idx, n - 1)]
    max_slip  = sorted_s[-1]

    max_allowed = _max_allowed_avg_slippage(symbol)
    excessive   = sum(1 for s in slippages if s > max_allowed)
    exc_ratio   = excessive / n

    # Score: 100 = perfecto, 0 = terrible
    # Penalización: 50 pts por avg / max_allowed + 50 pts por ratio excesivo
    score = max(0.0, 100.0 - (avg / max(max_allowed, 0.01) * 50.0) - (exc_ratio * 50.0))

    if score >= 80:
        status = "good"
    elif score >= 60:
        status = "warning"
    elif score >= 40:
        status = "degraded"
    else:
        status = "critical"

    return {
        "avg_slippage_pips":  round(avg, 3),
        "p95_slippage_pips":  round(p95, 3),
        "max_slippage_pips":  round(max_slip, 3),
        "excessive_ratio":    round(exc_ratio, 4),
        "quality_score":      round(score, 2),
        "sample_count":       n,
        "status":             status,
    }


# ── API pública ──────────────────────────────────────────────────────────────

def record_execution(
    symbol: str,
    ticket: int,
    slippage_pips: float,
    expected_price: Optional[float] = None,
    fill_price: Optional[float] = None,
    latency_ms: Optional[float] = None,
) -> None:
    """
    Alimenta el buffer con datos de una ejecución realizada.
    Thread-safe.
    """
    if not EXEC_QUALITY_ENABLED:
        return

    entry = {
        "ticket":         ticket,
        "symbol":         symbol,
        "timestamp":      time.time(),
        "slippage_pips":  float(slippage_pips or 0.0),
        "expected_price": expected_price,
        "fill_price":     fill_price,
        "latency_ms":     latency_ms,
    }

    with _lock:
        buf     = _get_buffer(symbol)
        buf.append(entry)
        quality = _compute_quality(buf, symbol)
        new_status = quality["status"]
        old_status = _last_status.get(symbol)

        if old_status is not None and old_status != new_status:
            log.info(
                f"[exec_quality] 🔄 {symbol}: transición de calidad "
                f"{old_status} → {new_status} "
                f"(score={quality['quality_score']:.1f}, "
                f"avg_slip={quality['avg_slippage_pips']:.2f}pips)"
            )
            # Si la calidad se restaura desde degraded/critical a warning/good
            if old_status in ("degraded", "critical") and new_status in ("good", "warning"):
                log.info(
                    f"[exec_quality] ✅ {symbol}: calidad de ejecución restaurada → {new_status}"
                )

        _last_status[symbol] = new_status


def get_exec_quality(symbol: str) -> dict:
    """
    Retorna métricas de calidad de ejecución calculadas para el símbolo.
    Thread-safe.
    """
    if not EXEC_QUALITY_ENABLED:
        return {}

    with _lock:
        buf = _get_buffer(symbol)
        return _compute_quality(buf, symbol)


def should_pause_for_exec_quality(symbol: str) -> bool:
    """
    Retorna True si la calidad de ejecución está por debajo del umbral
    y hay suficientes muestras para tomar una decisión.
    Thread-safe.
    """
    if not EXEC_QUALITY_ENABLED:
        return False

    with _lock:
        buf = _get_buffer(symbol)
        if len(buf) < int(EXEC_QUALITY_MIN_SAMPLES):
            return False
        quality = _compute_quality(buf, symbol)
        should_pause = quality["quality_score"] < float(EXEC_QUALITY_PAUSE_THRESHOLD)

    if should_pause:
        log.warning(
            f"[exec_quality] 🛑 {symbol}: pausa activada — "
            f"score={quality['quality_score']:.1f} < "
            f"umbral={EXEC_QUALITY_PAUSE_THRESHOLD}"
        )

    return should_pause


def get_all_exec_quality() -> dict:
    """
    Retorna el quality dict para todos los símbolos que tengan datos.
    Thread-safe.
    """
    if not EXEC_QUALITY_ENABLED:
        return {}

    result = {}
    with _lock:
        for symbol, buf in _buffers.items():
            if buf:
                result[symbol] = _compute_quality(buf, symbol)
    return result


def reset_exec_quality(symbol: Optional[str] = None) -> None:
    """
    Limpia el buffer de un símbolo (o todos si symbol=None).
    Thread-safe.
    """
    with _lock:
        if symbol is not None:
            _buffers.pop(symbol, None)
            _last_status.pop(symbol, None)
            _last_pause_notify.pop(symbol, None)
            log.info(f"[exec_quality] 🗑 Buffer limpiado para {symbol}")
        else:
            _buffers.clear()
            _last_status.clear()
            _last_pause_notify.clear()
            log.info("[exec_quality] 🗑 Todos los buffers limpiados")


def get_last_pause_notify_ts(symbol: str) -> float:
    """Retorna el timestamp de la última notificación de pausa para el símbolo."""
    with _lock:
        return _last_pause_notify.get(symbol, 0.0)


def set_last_pause_notify_ts(symbol: str) -> None:
    """Registra el timestamp actual como última notificación de pausa."""
    with _lock:
        _last_pause_notify[symbol] = time.time()
