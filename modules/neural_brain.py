"""
╔══════════════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — neural_brain.py  (v2 — DEEP LEARNING)         ║
║                                                                          ║
║   ARQUITECTURA HÍBRIDA DE TRES PILARES:                                 ║
║                                                                          ║
║   PILAR 1 — MEMORIA COSENO (operativo desde trade #1)                  ║
║   Similitud coseno ponderada con 32 features. Funciona desde el        ║
║   primer trade, no necesita entrenamiento previo.                       ║
║                                                                          ║
║   PILAR 2 — RED NEURONAL MLP (se activa con 20+ trades)                ║
║   Perceptrón multicapa 32→64→32→16→1 con activaciones ReLU.            ║
║   Aprende online con mini-batch gradient descent después de cada       ║
║   trade cerrado. Pesos persistidos en SQLite. Incluye:                 ║
║   • Dropout simulado (regularización)                                  ║
║   • Momentum (Adam-lite optimizer)                                     ║
║   • Early stopping interno                                              ║
║                                                                          ║
║   PILAR 3 — ATTENTION MECHANISM (pesos de features adaptativos)        ║
║   Vector de atención de 32 dimensiones por símbolo.                    ║
║   Se actualiza con cada resultado: features que predijeron bien        ║
║   reciben más peso. Features irrelevantes se atenúan.                  ║
║                                                                          ║
║   ENSEMBLE FINAL:                                                        ║
║   score = w1×coseno + w2×mlp + w3×régimen_mercado                     ║
║   Los pesos del ensemble también se aprenden con el tiempo.            ║
║                                                                          ║
║   DETECTOR DE RÉGIMEN (Market Regime Detector):                         ║
║   Clasifica el mercado en 5 regímenes y ajusta el score:               ║
║   • TRENDING_UP / TRENDING_DOWN (Hurst>0.6, Kalman slope fuerte)      ║
║   • RANGING (Hurst≈0.5, ATR bajo)                                      ║
║   • VOLATILE (ATR alto, BB squeeze break)                              ║
║   • CHAOTIC (Hurst<0.4, sin tendencia definida)                        ║
║                                                                          ║
║   FUNCIÓN DE RECOMPENSA (Sharpe-inspired):                             ║
║   reward = profit_norm - λ×drawdown_penalty + β×duration_bonus        ║
║   No solo maximiza ganancias — penaliza el riesgo excesivo.            ║
║                                                                          ║
║   COLD START: con 0 trades → usa solo coseno + régimen.               ║
║   Con 20+ trades → activa MLP. Con 50+ → activa ensemble completo.    ║
║                                                                          ║
║   DEPENDENCIAS: numpy, sqlite3 (ambos stdlib/numpy)                   ║
║   NO requiere: torch, tensorflow, sklearn, GPU                         ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import sqlite3
import json
import html
import math
import logging
import random
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional, Dict, List, Tuple

import numpy as np
import config as cfg

log     = logging.getLogger(__name__)
DB_PATH = Path("memory/zar_memory.db")


def _normalize_message_text(text: str) -> str:
    return html.unescape((text or "")).replace("&amp;", "&")


def get_adaptive_trail_params(sym_cfg: dict, direction: str) -> dict:
    """Retorna solo los parámetros vigentes del trailing adaptativo."""
    base_be_atr_mult = float(sym_cfg.get("be_atr_mult", getattr(cfg, "BREAKEVEN_ATR_MULT", 2.0)))
    buy_buffer_mult = float(sym_cfg.get("be_buffer_mult_buy", sym_cfg.get("be_buffer_mult", 0.50)))
    sell_buffer_mult = float(sym_cfg.get("be_buffer_mult_sell", 0.35))

    if direction == "SELL":
        return {
            "be_atr_mult": round(base_be_atr_mult * 0.9, 4),
            "be_buffer_mult": sell_buffer_mult,
        }

    return {
        "be_atr_mult": base_be_atr_mult,
        "be_buffer_mult": buy_buffer_mult,
    }

# ── Umbrales de activación del ensemble ─────────────────────────
COSINE_ONLY_THRESHOLD = 50   # < 50 trades: solo coseno + régimen
MLP_ACTIVATION        = 100  # ≥ 100 trades: activar MLP (mínimo estadístico razonable)
ENSEMBLE_ACTIVATION   = 200  # ≥ 200 trades: ensemble completo

# ── Arquitectura MLP ────────────────────────────────────────────
# FASE 2: INPUT_DIM expandido de 32 → 40 (+8 features Pilar 3)
# Los pesos de modelos guardados con dim=32 serán reinicializados
# automáticamente (from_dict los detecta por shape mismatch).
INPUT_DIM   = 40
HIDDEN1     = 64
HIDDEN2     = 32
HIDDEN3     = 16
OUTPUT_DIM  = 1
LR          = 0.01       # learning rate
LR_DECAY    = 0.9995     # decay por epoch
MOMENTUM    = 0.9        # Adam-lite momentum
LAMBDA_L2   = 0.001      # regularización L2
BATCH_SIZE  = 16
MAX_EPOCHS  = 50         # epochs por sesión de entrenamiento


# ════════════════════════════════════════════════════════════════
#  FEATURES — 32 DIMENSIONES
# ════════════════════════════════════════════════════════════════

@dataclass
class TradeFeatures:
    symbol:    str
    direction: str
    timestamp: str

    # ── GRUPO 1: Tendencia (8 features) ─────────────────────────
    rsi_norm:           float   # rsi/100
    rsi_zone:           float   # -1 oversold, 0 neutral, +1 overbought
    macd_direction:     float   # +1/-1
    macd_hist_norm:     float   # histograma normalizado
    dema_cruce:         float   # +1/-1/0
    h1_trend_num:       float   # -1→+1
    kalman_trend_num:   float   # +1 alcista, -1 bajista, 0 neutral
    lr_slope_norm:      float   # pendiente regresión lineal normalizada

    # ── GRUPO 2: Momentum / Ciclos (8 features) ─────────────────
    hilbert_signal_num: float   # -1→+1
    hilbert_phase_norm: float   # fase/360 → 0→1
    fisher_norm:        float   # fisher/4 clamped -1→+1
    cycle_osc:          float   # oscilador adaptativo -1→+1
    hurst_val:          float   # 0→1
    hurst_regime_num:   float   # -1 reversión, 0 aleatorio, +1 tendencia
    stoch_norm:         float   # stoch_k/100
    momentum_norm:      float   # momentum/price, normalizado

    # ── GRUPO 3: Volatilidad / Estructura (8 features) ──────────
    atr_norm:           float   # atr/atr_norm_factor
    bb_pos_num:         float   # posición en BB -1→+1
    bb_squeeze:         float   # 1 si squeeze activo, 0 no
    ha_trend_num:       float   # +1/-1
    supertrend_num:     float   # +1/-1
    sar_trend_num:      float   # +1/-1
    cci_norm:           float   # cci/200 clamped
    williams_norm:      float   # williams%R/100 clamped

    # ── GRUPO 4: Precio/Volumen/S&R (8 features) ────────────────
    in_strong_sr:       float   # 1/0
    dist_to_sr_norm:    float   # 0→1
    vwap_side:          float   # +1 sobre VWAP, -1 bajo VWAP
    obv_trend_num:      float   # +1/-1
    cmf_norm:           float   # Chaikin Money Flow -1→+1
    mfi_norm:           float   # Money Flow Index /100
    news_sentiment:     float   # -1→+1
    news_high_impact:   float   # 0/1

    # ── GRUPO 5: Microestructura + Confluencia (8 features) — FASE 2 ──
    micro_score_norm:       float   # micro_score / 3  → -1→+1
    above_poc:              float   # +1 sobre POC, -1 bajo POC
    in_value_area:          float   # 1 dentro de VA (VAL–VAH), 0 fuera
    above_session_vwap:     float   # +1 / -1
    session_vwap_dev_norm:  float   # dev % clipeado en [-2%, +2%] → -1→+1
    fvg_bull_active:        float   # 1 si hay FVG bullish activo cercano, 0 si no
    fvg_bear_active:        float   # 1 si hay FVG bearish activo cercano, 0 si no
    confluence_total_norm:  float   # confluence_total / 3 → -1→+1

    def to_vector(self) -> np.ndarray:
        return np.array([
            # Grupo 1
            self.rsi_norm, self.rsi_zone, self.macd_direction, self.macd_hist_norm,
            self.dema_cruce, self.h1_trend_num, self.kalman_trend_num, self.lr_slope_norm,
            # Grupo 2
            self.hilbert_signal_num, self.hilbert_phase_norm, self.fisher_norm, self.cycle_osc,
            self.hurst_val, self.hurst_regime_num, self.stoch_norm, self.momentum_norm,
            # Grupo 3
            self.atr_norm, self.bb_pos_num, self.bb_squeeze, self.ha_trend_num,
            self.supertrend_num, self.sar_trend_num, self.cci_norm, self.williams_norm,
            # Grupo 4
            self.in_strong_sr, self.dist_to_sr_norm, self.vwap_side, self.obv_trend_num,
            self.cmf_norm, self.mfi_norm, self.news_sentiment, self.news_high_impact,
            # Grupo 5 — Microestructura + Confluencia (FASE 2)
            self.micro_score_norm, self.above_poc, self.in_value_area,
            self.above_session_vwap, self.session_vwap_dev_norm,
            self.fvg_bull_active, self.fvg_bear_active,
            self.confluence_total_norm,
        ], dtype=np.float32)


@dataclass
class MemoryCheck:
    should_block:    bool  = False
    confidence_adj:  float = 0.0
    warning_msg:     str   = ""
    similar_losses:  int   = 0
    similar_wins:    int   = 0
    mlp_score:       float = 0.0    # score de la red neuronal (0→1 = probabilidad pérdida)
    regime:          str   = "UNKNOWN"
    ensemble_detail: str   = ""
    warmup_mode:     bool  = False  # True cuando total_trades < MLP_ACTIVATION (cold start)


@dataclass
class ScorecardCheck:
    should_block:       bool
    win_rate:           float
    sample_size:        int
    level:              str
    key:                str
    min_win_rate:       float
    min_sample:         int
    reason:             str
    setup_id:           str
    session:            str
    regime:             str


@dataclass
class PolicyCheck:
    direction:      str
    setup_id:       str
    session:        str
    regime:         str
    sample_size:    int
    win_rate:       float
    profit_factor:  float
    avg_reward:     float
    policy_score:   float
    should_block:   bool
    reason:         str


# ════════════════════════════════════════════════════════════════
#  RED NEURONAL MLP — IMPLEMENTACIÓN NUMPY PURA
# ════════════════════════════════════════════════════════════════

class MLPBrain:
    """
    Red neuronal multicapa 32→64→32→16→1.
    Implementada en numpy puro (sin torch/tensorflow).
    Aprende online con mini-batch gradient descent + momentum.
    Incluye L2 regularización y simulación de dropout.
    """

    def __init__(self, input_dim=INPUT_DIM):
        self.input_dim = input_dim
        self.lr        = LR
        self.momentum  = MOMENTUM
        self.lambda_l2 = LAMBDA_L2
        self.trained_samples = 0
        self.epoch_count     = 0
        self.best_loss       = float("inf")

        # Inicialización He (óptima para ReLU)
        self.W1 = np.random.randn(input_dim, HIDDEN1).astype(np.float32) * np.sqrt(2 / input_dim)
        self.b1 = np.zeros((1, HIDDEN1), dtype=np.float32)
        self.W2 = np.random.randn(HIDDEN1, HIDDEN2).astype(np.float32) * np.sqrt(2 / HIDDEN1)
        self.b2 = np.zeros((1, HIDDEN2), dtype=np.float32)
        self.W3 = np.random.randn(HIDDEN2, HIDDEN3).astype(np.float32) * np.sqrt(2 / HIDDEN2)
        self.b3 = np.zeros((1, HIDDEN3), dtype=np.float32)
        self.W4 = np.random.randn(HIDDEN3, OUTPUT_DIM).astype(np.float32) * np.sqrt(2 / HIDDEN3)
        self.b4 = np.zeros((1, OUTPUT_DIM), dtype=np.float32)

        # Momentum (Adam-lite) — velocidades
        self.vW1 = np.zeros_like(self.W1); self.vb1 = np.zeros_like(self.b1)
        self.vW2 = np.zeros_like(self.W2); self.vb2 = np.zeros_like(self.b2)
        self.vW3 = np.zeros_like(self.W3); self.vb3 = np.zeros_like(self.b3)
        self.vW4 = np.zeros_like(self.W4); self.vb4 = np.zeros_like(self.b4)

    # ── Activaciones ──────────────────────────────────────────────

    @staticmethod
    def relu(x: np.ndarray) -> np.ndarray:
        return np.maximum(0, x)

    @staticmethod
    def relu_grad(x: np.ndarray) -> np.ndarray:
        return (x > 0).astype(np.float32)

    @staticmethod
    def sigmoid(x: np.ndarray) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-np.clip(x, -500, 500)))

    # ── Forward pass ──────────────────────────────────────────────

    def forward(self, X: np.ndarray, training: bool = False) -> Tuple:
        """
        Forward pass con dropout simulado durante entrenamiento.
        Dropout: 20% en capas ocultas (reduce overfitting).
        """
        Z1 = X @ self.W1 + self.b1
        A1 = self.relu(Z1)
        if training:
            # Dropout 20%
            mask1 = (np.random.rand(*A1.shape) > 0.2).astype(np.float32) / 0.8
            A1 *= mask1
        else:
            mask1 = None

        Z2 = A1 @ self.W2 + self.b2
        A2 = self.relu(Z2)
        if training:
            mask2 = (np.random.rand(*A2.shape) > 0.2).astype(np.float32) / 0.8
            A2 *= mask2
        else:
            mask2 = None

        Z3 = A2 @ self.W3 + self.b3
        A3 = self.relu(Z3)
        if training:
            mask3 = (np.random.rand(*A3.shape) > 0.2).astype(np.float32) / 0.8
            A3 *= mask3
        else:
            mask3 = None

        Z4 = A3 @ self.W4 + self.b4
        A4 = self.sigmoid(Z4)   # probabilidad de pérdida: 0=ganador, 1=perdedor

        return Z1, A1, Z2, A2, Z3, A3, Z4, A4, mask1, mask2, mask3

    def predict(self, x: np.ndarray) -> float:
        """Predice probabilidad de pérdida (0=seguro, 1=alto riesgo)."""
        X = x.reshape(1, -1)
        *_, A4, _, _, _ = self.forward(X, training=False)
        return float(A4[0, 0])

    # ── Backward pass (backpropagation) ───────────────────────────

    def train_batch(self, X: np.ndarray, y: np.ndarray) -> float:
        """
        Entrena en un mini-batch.
        X: (batch, input_dim), y: (batch, 1) — 1=loss, 0=win
        Retorna pérdida BCE del batch.
        """
        m = X.shape[0]
        Z1, A1, Z2, A2, Z3, A3, Z4, A4, mk1, mk2, mk3 = self.forward(X, training=True)

        # Aplicar masks a activaciones
        A1_d = A1 * mk1 if mk1 is not None else A1
        A2_d = A2 * mk2 if mk2 is not None else A2
        A3_d = A3 * mk3 if mk3 is not None else A3

        # Binary Cross-Entropy loss + L2 regularization
        eps   = 1e-7
        bce   = -np.mean(y * np.log(A4 + eps) + (1 - y) * np.log(1 - A4 + eps))
        l2    = (self.lambda_l2 / (2 * m)) * (
            np.sum(self.W1**2) + np.sum(self.W2**2) +
            np.sum(self.W3**2) + np.sum(self.W4**2)
        )
        loss = bce + l2

        # Backward pass
        dA4 = A4 - y
        dW4 = (A3_d.T @ dA4) / m + self.lambda_l2 * self.W4 / m
        db4 = np.mean(dA4, axis=0, keepdims=True)

        dA3 = dA4 @ self.W4.T * self.relu_grad(Z3)
        if mk3 is not None: dA3 *= mk3
        dW3 = (A2_d.T @ dA3) / m + self.lambda_l2 * self.W3 / m
        db3 = np.mean(dA3, axis=0, keepdims=True)

        dA2 = dA3 @ self.W3.T * self.relu_grad(Z2)
        if mk2 is not None: dA2 *= mk2
        dW2 = (A1_d.T @ dA2) / m + self.lambda_l2 * self.W2 / m
        db2 = np.mean(dA2, axis=0, keepdims=True)

        dA1 = dA2 @ self.W2.T * self.relu_grad(Z1)
        if mk1 is not None: dA1 *= mk1
        dW1 = (X.T @ dA1) / m + self.lambda_l2 * self.W1 / m
        db1 = np.mean(dA1, axis=0, keepdims=True)

        # Actualizar con momentum (SGD + momentum)
        self.vW4 = self.momentum * self.vW4 + (1 - self.momentum) * dW4
        self.vb4 = self.momentum * self.vb4 + (1 - self.momentum) * db4
        self.vW3 = self.momentum * self.vW3 + (1 - self.momentum) * dW3
        self.vb3 = self.momentum * self.vb3 + (1 - self.momentum) * db3
        self.vW2 = self.momentum * self.vW2 + (1 - self.momentum) * dW2
        self.vb2 = self.momentum * self.vb2 + (1 - self.momentum) * db2
        self.vW1 = self.momentum * self.vW1 + (1 - self.momentum) * dW1
        self.vb1 = self.momentum * self.vb1 + (1 - self.momentum) * db1

        self.W4 -= self.lr * self.vW4; self.b4 -= self.lr * self.vb4
        self.W3 -= self.lr * self.vW3; self.b3 -= self.lr * self.vb3
        self.W2 -= self.lr * self.vW2; self.b2 -= self.lr * self.vb2
        self.W1 -= self.lr * self.vW1; self.b1 -= self.lr * self.vb1

        self.epoch_count += 1
        self.lr *= LR_DECAY   # decay gradual del learning rate

        return float(loss)

    def fit(self, X: np.ndarray, y: np.ndarray, verbose: bool = False) -> float:
        """
        Entrena en el dataset completo por MAX_EPOCHS epochs.
        Implementa early stopping si la loss no mejora.
        """
        if len(X) < 4:
            return 0.0

        indices     = list(range(len(X)))
        final_loss  = 0.0
        no_improve  = 0
        best_loss   = float("inf")

        for epoch in range(MAX_EPOCHS):
            random.shuffle(indices)
            epoch_loss = 0.0
            batches    = 0

            for start in range(0, len(indices), BATCH_SIZE):
                batch_idx = indices[start:start + BATCH_SIZE]
                X_b = X[batch_idx]
                y_b = y[batch_idx]
                epoch_loss += self.train_batch(X_b, y_b)
                batches += 1

            avg_loss = epoch_loss / max(batches, 1)
            final_loss = avg_loss

            # Early stopping
            if avg_loss < best_loss - 0.001:
                best_loss = avg_loss
                no_improve = 0
            else:
                no_improve += 1
                if no_improve >= 8:
                    if verbose:
                        log.debug(f"[mlp] Early stop en epoch {epoch} | loss={avg_loss:.4f}")
                    break

        self.trained_samples = len(X)
        if verbose:
            log.info(f"[mlp] Entrenado {len(X)} samples | loss_final={final_loss:.4f} | "
                     f"epochs={self.epoch_count}")
        return final_loss

    # ── Serialización ──────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serializa los pesos a dict (para guardar en SQLite)."""
        return {
            "W1": self.W1.tolist(), "b1": self.b1.tolist(),
            "W2": self.W2.tolist(), "b2": self.b2.tolist(),
            "W3": self.W3.tolist(), "b3": self.b3.tolist(),
            "W4": self.W4.tolist(), "b4": self.b4.tolist(),
            "vW1": self.vW1.tolist(), "vb1": self.vb1.tolist(),
            "vW2": self.vW2.tolist(), "vb2": self.vb2.tolist(),
            "vW3": self.vW3.tolist(), "vb3": self.vb3.tolist(),
            "vW4": self.vW4.tolist(), "vb4": self.vb4.tolist(),
            "lr": self.lr, "epoch_count": self.epoch_count,
            "trained_samples": self.trained_samples,
        }

    def from_dict(self, d: dict):
        """Restaura pesos desde dict."""
        try:
            self.W1 = np.array(d["W1"], dtype=np.float32)
            self.b1 = np.array(d["b1"], dtype=np.float32)
            self.W2 = np.array(d["W2"], dtype=np.float32)
            self.b2 = np.array(d["b2"], dtype=np.float32)
            self.W3 = np.array(d["W3"], dtype=np.float32)
            self.b3 = np.array(d["b3"], dtype=np.float32)
            self.W4 = np.array(d["W4"], dtype=np.float32)
            self.b4 = np.array(d["b4"], dtype=np.float32)
            self.vW1 = np.array(d["vW1"], dtype=np.float32)
            self.vb1 = np.array(d["vb1"], dtype=np.float32)
            self.vW2 = np.array(d["vW2"], dtype=np.float32)
            self.vb2 = np.array(d["vb2"], dtype=np.float32)
            self.vW3 = np.array(d["vW3"], dtype=np.float32)
            self.vb3 = np.array(d["vb3"], dtype=np.float32)
            self.vW4 = np.array(d["vW4"], dtype=np.float32)
            self.vb4 = np.array(d["vb4"], dtype=np.float32)
            self.lr             = float(d.get("lr", LR))
            self.epoch_count    = int(d.get("epoch_count", 0))
            self.trained_samples= int(d.get("trained_samples", 0))
        except Exception as e:
            log.warning(f"[mlp] Error restaurando pesos: {e} — usando pesos frescos")


# ════════════════════════════════════════════════════════════════
#  ATTENTION MECHANISM — Pesos de features aprendidos
# ════════════════════════════════════════════════════════════════

class AttentionLayer:
    """
    Vector de atención de 32 dimensiones por símbolo.
    Se actualiza con cada resultado de trade:
    • Features que estaban alineadas con un WIN → peso +
    • Features que estaban alineadas con un LOSS → peso -
    • Actualización suave (EMA) para evitar oscilaciones
    """

    def __init__(self, dim: int = INPUT_DIM):
        self.dim      = dim
        self.weights  = np.ones(dim, dtype=np.float32)  # uniformes al inicio
        self.alpha    = 0.05   # tasa de aprendizaje de atención (lento y estable)
        self.updates  = 0

    def get_weights(self) -> np.ndarray:
        """Retorna pesos de atención normalizados (suman a dim)."""
        w = np.maximum(self.weights, 0.1)   # mínimo 0.1 para ningún feature a 0
        return w * (self.dim / w.sum())

    def update(self, feature_vec: np.ndarray, reward: float):
        """
        Actualiza los pesos de atención basado en el resultado.
        reward > 0 = trade ganador, reward < 0 = trade perdedor.
        Features activadas (|valor|>0.3) en un trade ganador reciben más peso.
        """
        activated = (np.abs(feature_vec) > 0.3).astype(np.float32)
        direction = np.sign(reward)

        # Si es una pérdida, reducir peso de features que estaban activos
        # Si es una ganancia, aumentar peso de features que estaban activos
        delta = direction * activated * self.alpha * abs(reward)
        self.weights = self.weights * (1 - self.alpha) + delta * self.alpha + self.weights * self.alpha
        self.weights = np.clip(self.weights, 0.05, 5.0)
        self.updates += 1

    def to_dict(self) -> dict:
        return {"weights": self.weights.tolist(), "updates": self.updates}

    def from_dict(self, d: dict):
        try:
            self.weights = np.array(d["weights"], dtype=np.float32)
            self.updates = int(d.get("updates", 0))
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════
#  MARKET REGIME DETECTOR
# ════════════════════════════════════════════════════════════════

class MarketRegimeDetector:
    """
    Detecta en qué régimen está el mercado y ajusta el score de riesgo.

    REGÍMENES:
    • TRENDING_UP:   Hurst>0.6 + tendencia alcista → sesgo BUY
    • TRENDING_DOWN: Hurst>0.6 + tendencia bajista → sesgo SELL
    • RANGING:       Hurst 0.45-0.55 + ATR bajo → estrategia reversión
    • VOLATILE:      ATR alto + BB squeeze break → reducir tamaño
    • CHAOTIC:       Hurst<0.4 → HOLD fuerte, mercado sin estructura
    """

    REGIMES = {
        "TRENDING_UP":   {"base_risk": 1.0, "direction_bias": "BUY"},
        "TRENDING_DOWN": {"base_risk": 1.0, "direction_bias": "SELL"},
        "RANGING":       {"base_risk": 0.8, "direction_bias": "NEUTRAL"},
        "VOLATILE":      {"base_risk": 0.6, "direction_bias": "NEUTRAL"},
        "CHAOTIC":       {"base_risk": 0.2, "direction_bias": "HOLD"},
    }

    def detect(self, features: "TradeFeatures") -> Tuple[str, float]:
        """
        Detecta el régimen del mercado a partir del vector de features.
        Retorna (nombre_régimen, score_ajuste: 0→2 donde 1=neutral)
        """
        hurst       = features.hurst_val
        hurst_regime= features.hurst_regime_num
        trend       = features.h1_trend_num
        atr         = features.atr_norm
        bb_squeeze  = features.bb_squeeze
        cycle_osc   = features.cycle_osc
        kalman      = features.kalman_trend_num

        # Reglas de detección de régimen
        if hurst < 0.30:
            # Solo es CHAOTIC con Hurst muy bajo (< 0.30)
            scalp_low_hurst_enabled = bool(getattr(cfg, "SCALPING_ALLOW_LOW_HURST", True))
            scalp_hard_floor = float(getattr(cfg, "SCALPING_HURST_HARD_FLOOR", 0.18) or 0.18)
            scalp_conf_floor = float(getattr(cfg, "CONFLUENCE_MIN_SCORE", 0.25) or 0.25) / 3.0
            if (
                scalp_low_hurst_enabled
                and hurst >= scalp_hard_floor
                and features.in_strong_sr >= 0.5
                and abs(features.confluence_total_norm) >= scalp_conf_floor
            ):
                return "RANGING", 0.78
            return "CHAOTIC", 0.40  # Score subido de 0.25 a 0.40
        elif hurst < 0.40:
            # Hurst 0.30-0.40: RANGING, no CHAOTIC. Scalping viable.
            return "RANGING", 0.70

        if atr > 0.75 and bb_squeeze > 0.5:
            return "VOLATILE", 0.60

        if hurst >= 0.60:
            if trend >= 0.5 and kalman >= 0.5:
                return "TRENDING_UP", 1.20
            elif trend <= -0.5 and kalman <= -0.5:
                return "TRENDING_DOWN", 1.20

        if 0.43 <= hurst <= 0.57 and atr < 0.40:
            return "RANGING", 0.85

        # Régimen mixto — neutro
        score = 0.7 + hurst * 0.6   # 0.7 a 1.3 según Hurst
        return "MIXED", float(np.clip(score, 0.5, 1.3))

    def regime_matches_direction(self, regime: str, direction: str) -> float:
        """
        Verifica si el régimen del mercado favorece la dirección propuesta.
        Retorna multiplicador: >1 favorece, <1 desfavorece.
        """
        bias = self.REGIMES.get(regime, {}).get("direction_bias", "NEUTRAL")
        if bias == "HOLD":
            return 0.3
        if bias == direction:
            return 1.3
        if bias == "NEUTRAL":
            return 1.0
        # Dirección opuesta al régimen
        return 0.7


# ════════════════════════════════════════════════════════════════
#  FUNCIÓN DE RECOMPENSA SHARPE-INSPIRED
# ════════════════════════════════════════════════════════════════

def compute_reward(
    profit:       float,
    sl_distance:  float,   # distancia SL en pips
    duration_min: int,
    result:       str,
    balance:      float = 10000.0,
) -> float:
    """
    Función de recompensa que va más allá del win/loss binario.

    reward = profit_norm - λ×risk_penalty + β×efficiency_bonus

    • profit_norm:       ganancia/pérdida como % del balance
    • risk_penalty:      penaliza SL demasiado grande
    • efficiency_bonus:  bonifica trades rápidos y eficientes
    • sharpe_component:  penaliza drawdown, no solo pérdidas

    Rango output: [-1, +1]
    """
    if balance <= 0:
        balance = 10000.0

    # Normalizar profit al balance
    profit_norm = np.clip(profit / balance * 100, -5.0, 5.0) / 5.0   # -1→+1

    # Penalizar SL excesivo (> 3% del balance es peligroso)
    risk_pct    = sl_distance / balance * 100 if sl_distance > 0 else 0
    risk_penalty= np.clip(risk_pct / 3.0, 0, 1) * 0.3

    # Bonus por eficiencia temporal
    # Trade ganador rápido (< 60min) = bonus
    # Trade perdedor largo (> 120min) = penalización adicional
    if result == "WIN":
        eff_bonus = 0.15 * max(0, 1 - duration_min / 60)
    elif result == "LOSS":
        eff_bonus = -0.10 * min(1, duration_min / 120)
    else:
        eff_bonus = 0.0

    reward = profit_norm - risk_penalty + eff_bonus
    return float(np.clip(reward, -1.0, 1.0))


# ════════════════════════════════════════════════════════════════
#  BASE DE DATOS — Inicialización y persistencia
# ════════════════════════════════════════════════════════════════

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # ── Crear tablas si no existen ──
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket        INTEGER UNIQUE,
            symbol        TEXT,
            direction     TEXT,
            open_price    REAL,
            close_price   REAL,
            volume        REAL,
            profit        REAL,
            pips          REAL,
            result        TEXT,
            duration_min  INTEGER,
            opened_at     TEXT,
            closed_at     TEXT,
            features_json TEXT,
            reason_gemini TEXT,
            hilbert_signal TEXT,
            hurst_val      REAL,
            reward         REAL,
            regime         TEXT,
            setup_id       TEXT,
            setup_score    REAL,
            session        TEXT,
            risk_amount    REAL,
            sl             REAL,
            tp             REAL,
            slippage_pips  REAL
        );

        CREATE TABLE IF NOT EXISTS feature_vectors (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id    INTEGER REFERENCES trades(id),
            symbol      TEXT,
            direction   TEXT,
            result      TEXT,
            reward      REAL,
            vector_json TEXT,
            timestamp   TEXT
        );

        CREATE TABLE IF NOT EXISTS ml_models (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT UNIQUE,
            model_json  TEXT,
            attention_json TEXT,
            ensemble_weights TEXT,
            updated_at  TEXT,
            total_trained INTEGER
        );

        CREATE TABLE IF NOT EXISTS regime_history (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol    TEXT,
            regime    TEXT,
            timestamp TEXT
        );

        CREATE TABLE IF NOT EXISTS shadow_trades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol        TEXT,
            direction     TEXT,
            entry_price   REAL,
            sl            REAL,
            tp            REAL,
            volume        REAL,
            score         REAL,
            reason        TEXT,
            h1_trend      TEXT,
            htf_trend     TEXT,
            hurst         REAL,
            rsi           REAL,
            atr           REAL,
            opened_at     TEXT,
            closed_at     TEXT,
            exit_price    REAL,
            result        TEXT,
            profit_pips   REAL,
            duration_min  INTEGER
        );
    """)

    # ── MIGRACIÓN AUTOMÁTICA — agrega columnas nuevas si no existen ──
    # Esto permite actualizar el neural_brain sin borrar datos históricos.
    migrations = [
        ("trades",          "reward",          "REAL"),
        ("trades",          "regime",          "TEXT"),
        ("trades",          "setup_id",        "TEXT"),
        ("trades",          "setup_score",     "REAL"),
        ("trades",          "session",         "TEXT"),
        ("trades",          "risk_amount",     "REAL"),
        ("trades",          "sl",              "REAL"),
        ("trades",          "tp",              "REAL"),
        ("trades",          "slippage_pips",   "REAL"),
        ("feature_vectors", "reward",          "REAL"),
        ("ml_models",       "attention_json",  "TEXT"),
        ("ml_models",       "ensemble_weights","TEXT"),
        ("ml_models",       "total_trained",   "INTEGER"),
    ]
    for table, column, col_type in migrations:
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            log.info(f"[memory] ✅ Migración: {table}.{column} añadida")
        except sqlite3.OperationalError:
            pass  # La columna ya existe — normal en reinicios

    con.commit()
    con.close()
    log.info(f"[memory] DB v2 iniciada en {DB_PATH}")


# ════════════════════════════════════════════════════════════════
#  GESTOR DE MODELOS POR SÍMBOLO
# ════════════════════════════════════════════════════════════════

# Cache en memoria (evita recargar de DB en cada ciclo)
_mlp_cache:       Dict[str, MLPBrain]          = {}
_attention_cache: Dict[str, AttentionLayer]    = {}
_regime_detector  = MarketRegimeDetector()
_ensemble_weights: Dict[str, np.ndarray]       = {}   # [coseno_w, mlp_w, regime_w]


def _load_model(symbol: str) -> Tuple[MLPBrain, AttentionLayer, np.ndarray]:
    """Carga o crea modelo MLP + atención para un símbolo."""
    if symbol in _mlp_cache:
        return _mlp_cache[symbol], _attention_cache[symbol], _ensemble_weights.get(symbol, np.array([0.5, 0.3, 0.2]))

    mlp  = MLPBrain()
    att  = AttentionLayer()
    ew   = np.array([0.5, 0.3, 0.2], dtype=np.float32)

    try:
        con = sqlite3.connect(DB_PATH)
        row = con.execute(
            "SELECT model_json, attention_json, ensemble_weights FROM ml_models WHERE symbol=?",
            (symbol,)
        ).fetchone()
        con.close()

        if row:
            if row[0]: mlp.from_dict(json.loads(row[0]))
            if row[1]: att.from_dict(json.loads(row[1]))
            if row[2]: ew = np.array(json.loads(row[2]), dtype=np.float32)
            log.info(f"[mlp] Modelo cargado para {symbol} | "
                     f"trained={mlp.trained_samples} | "
                     f"att_updates={att.updates}")
    except Exception as e:
        log.warning(f"[mlp] Error cargando modelo {symbol}: {e}")

    _mlp_cache[symbol]       = mlp
    _attention_cache[symbol] = att
    _ensemble_weights[symbol] = ew
    return mlp, att, ew


def _save_model(symbol: str):
    """Persiste el modelo en SQLite."""
    mlp = _mlp_cache.get(symbol)
    att = _attention_cache.get(symbol)
    ew  = _ensemble_weights.get(symbol)
    if mlp is None:
        return

    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("""
            INSERT OR REPLACE INTO ml_models
                (symbol, model_json, attention_json, ensemble_weights, updated_at, total_trained)
            VALUES (?,?,?,?,?,?)
        """, (
            symbol,
            json.dumps(mlp.to_dict()),
            json.dumps(att.to_dict()) if att else "{}",
            json.dumps(ew.tolist()) if ew is not None else "[0.5,0.3,0.2]",
            datetime.now(timezone.utc).isoformat(),
            mlp.trained_samples,
        ))
        con.commit()
        con.close()
    except Exception as e:
        log.warning(f"[mlp] Error guardando modelo {symbol}: {e}")


# ════════════════════════════════════════════════════════════════
#  SIMILITUD COSENO PONDERADA (Pilar 1 — siempre activo)
# ════════════════════════════════════════════════════════════════

def _cosine_sim(a: np.ndarray, b: np.ndarray, weights: np.ndarray) -> float:
    """Similitud coseno ponderada entre dos vectores."""
    wa = a * weights
    wb = b * weights
    num = float(np.dot(wa, wb))
    den = float(np.linalg.norm(wa) * np.linalg.norm(wb))
    if den < 1e-9:
        return 0.0
    return max(-1.0, min(1.0, num / den))


# ════════════════════════════════════════════════════════════════
#  CONSTRUCCIÓN DE FEATURES (32 dimensiones)
# ════════════════════════════════════════════════════════════════

def _trend_to_num(trend: str) -> float:
    # FASE 2: expanded to all 7 levels added in v6.2 (was only 5 levels)
    return {
        "ALCISTA_FUERTE":  1.00,
        "ALCISTA":         0.67,
        "LATERAL_ALCISTA": 0.33,
        "LATERAL":         0.00,
        "LATERAL_BAJISTA": -0.33,
        "BAJISTA":         -0.67,
        "BAJISTA_FUERTE":  -1.00,
    }.get(trend, 0.0)

def _hilbert_to_num(signal: str) -> float:
    return {"LOCAL_MIN": 1.0, "BUY_CYCLE": 0.5, "NEUTRAL": 0.0,
            "CYCLE_CROSS": 0.0, "SELL_CYCLE": -0.5, "LOCAL_MAX": -1.0}.get(signal, 0.0)

def _hurst_regime(hurst: float) -> float:
    if hurst > 0.60:   return 1.0    # tendencia fuerte
    if hurst > 0.53:   return 0.5    # tendencia suave
    if hurst > 0.47:   return 0.0    # aleatorio
    if hurst > 0.40:   return -0.5   # reversión suave
    return -1.0                       # reversión fuerte


def derive_setup_id(ind: dict, direction: str) -> str:
    """Construye un setup_id estable con dirección + sesgo de pilares."""
    conf = ind.get("confluence", {})
    micro = ind.get("microstructure", {})
    h1 = ind.get("h1_trend", "LATERAL")
    pillar_bias = conf.get("bias", "NEUTRAL")
    micro_bias = micro.get("micro_bias", "NEUTRAL")
    return f"{direction}|{h1}|{pillar_bias}|{micro_bias}"


def derive_session_from_ind(ind: dict) -> str:
    return ind.get("microstructure", {}).get("session", "UNKNOWN")


def build_features(
    symbol: str, direction: str,
    ind: dict, sr_ctx, news_ctx,
    sym_cfg: dict,
) -> TradeFeatures:
    """Construye el vector de 32 features normalizados."""
    hilbert  = ind.get("hilbert", {})
    bb_map   = {
        "SOBRE_BANDA_SUPERIOR": 1.0, "SOBRE_SUPERIOR": 1.0, "ZONA_ALTA": 0.3,
        "ZONA_BAJA": -0.3, "BAJO_BANDA_INFERIOR": -1.0, "BAJO_INFERIOR": -1.0,
    }
    rsi_raw  = ind.get("rsi", 50)
    rsi_zone = 1.0 if rsi_raw > 70 else (-1.0 if rsi_raw < 30 else 0.0)
    hurst_v  = float(ind.get("hurst", 0.5))
    price    = ind.get("price", 1)
    mom_raw  = ind.get("momentum", 0)
    mom_norm = float(np.clip(mom_raw / max(abs(price) * 0.01, 1e-6), -1, 1))
    fisher_v = float(np.clip(ind.get("fisher", 0) / 4.0, -1, 1))
    hilbert_phase = hilbert.get("phase", 0)

    atr_factor = sym_cfg.get("atr_norm_factor", 2.0)

    return TradeFeatures(
        symbol=symbol, direction=direction,
        timestamp=datetime.now(timezone.utc).isoformat(),

        # Grupo 1 — Tendencia
        rsi_norm           = rsi_raw / 100,
        rsi_zone           = rsi_zone,
        macd_direction     = 1.0 if ind.get("macd_dir") == "ALCISTA" else -1.0,
        macd_hist_norm     = float(np.clip(ind.get("macd_hist", 0) / max(atr_factor * 0.1, 1e-6), -1, 1)),
        dema_cruce         = 1.0 if ind.get("dema_cross") == "GOLDEN_CROSS" else
                             (-1.0 if ind.get("dema_cross") == "DEATH_CROSS" else 0.0),
        h1_trend_num       = _trend_to_num(ind.get("h1_trend", "LATERAL")),
        kalman_trend_num   = 1.0 if ind.get("kalman_trend") == "ALCISTA" else
                             (-1.0 if ind.get("kalman_trend") == "BAJISTA" else 0.0),
        lr_slope_norm      = float(np.clip(ind.get("lr_slope", 0) * 1000, -1, 1)),

        # Grupo 2 — Momentum / Ciclos
        hilbert_signal_num = _hilbert_to_num(hilbert.get("signal", "NEUTRAL")),
        hilbert_phase_norm = hilbert_phase / 360.0,
        fisher_norm        = fisher_v,
        cycle_osc          = float(np.clip(ind.get("cycle_osc", 0), -1, 1)),
        hurst_val          = hurst_v,
        hurst_regime_num   = _hurst_regime(hurst_v),
        stoch_norm         = ind.get("stoch_k", 50) / 100,
        momentum_norm      = mom_norm,

        # Grupo 3 — Volatilidad / Estructura
        atr_norm           = float(np.clip(ind.get("atr", 0) / max(atr_factor, 1e-6), 0, 2)),
        bb_pos_num         = bb_map.get(ind.get("bb_pos", "ZONA_BAJA"), 0.0),
        bb_squeeze         = 1.0 if ind.get("bb_squeeze", False) else 0.0,
        ha_trend_num       = 1.0 if ind.get("ha_trend") == "ALCISTA" else -1.0,
        supertrend_num     = float(ind.get("supertrend", 0)),
        sar_trend_num      = float(ind.get("sar_trend", 0)),
        cci_norm           = float(np.clip(ind.get("cci", 0) / 200, -1, 1)),
        williams_norm      = float(np.clip(ind.get("williams", -50) / 100, -1, 1)),

        # Grupo 4 — Precio / Volumen / S&R
        in_strong_sr       = 1.0 if getattr(sr_ctx, "in_strong_zone", False) else 0.0,
        dist_to_sr_norm    = float(np.clip(min(
            getattr(sr_ctx, "dist_to_sup_pct", 999),
            getattr(sr_ctx, "dist_to_res_pct", 999),
        ) / 3.0, 0, 1)),
        vwap_side          = 1.0 if price > ind.get("vwap", price) else -1.0,
        obv_trend_num      = 1.0 if ind.get("obv_trend") == "ALCISTA" else -1.0,
        cmf_norm           = float(np.clip(ind.get("cmf", 0), -1, 1)),
        mfi_norm           = float(ind.get("mfi", 50)) / 100,
        news_sentiment     = float(getattr(news_ctx, "avg_sentiment", 0)),
        news_high_impact   = 1.0 if getattr(news_ctx, "should_pause", False) else 0.0,

        # Grupo 5 — Microestructura + Confluencia (FASE 2)
        micro_score_norm      = float(np.clip(
            ind.get("microstructure", {}).get("micro_score", 0.0) / 3.0,
            -1.0, 1.0,
        )),
        above_poc             = 1.0 if ind.get("microstructure", {}).get("above_poc", True) else -1.0,
        in_value_area         = 1.0 if ind.get("microstructure", {}).get("in_value_area", True) else 0.0,
        above_session_vwap    = 1.0 if ind.get("microstructure", {}).get("above_session_vwap", True) else -1.0,
        session_vwap_dev_norm = float(np.clip(
            ind.get("microstructure", {}).get("session_vwap_dev", 0.0) / 2.0,
            -1.0, 1.0,
        )),
        fvg_bull_active       = 1.0 if ind.get("microstructure", {}).get("fvg_bull") is not None else 0.0,
        fvg_bear_active       = 1.0 if ind.get("microstructure", {}).get("fvg_bear") is not None else 0.0,
        confluence_total_norm = float(np.clip(
            ind.get("confluence", {}).get("total", 0.0) / 3.0,
            -1.0, 1.0,
        )),
    )


# ════════════════════════════════════════════════════════════════
#  GUARDAR TRADE
# ════════════════════════════════════════════════════════════════

def save_trade(
    ticket: int, symbol: str, direction: str,
    open_price: float, volume: float,
    features: TradeFeatures, reason_gemini: str,
    hilbert_signal: str, hurst_val: float,
    setup_id: Optional[str] = None,
    setup_score: Optional[float] = None,
    session: Optional[str] = None,
    regime: Optional[str] = None,
    risk_amount: Optional[float] = None,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    slippage_pips: float = 0.0,
) -> Optional[int]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO trades
            (ticket, symbol, direction, open_price, volume,
             opened_at, features_json, reason_gemini, hilbert_signal, hurst_val,
             setup_id, setup_score, session, regime, risk_amount, sl, tp, slippage_pips)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (ticket, symbol, direction, open_price, volume,
          datetime.now(timezone.utc).isoformat(),
          json.dumps(asdict(features)), reason_gemini, hilbert_signal, hurst_val,
          setup_id, setup_score, session, regime, risk_amount, sl, tp, slippage_pips))
    trade_id = cur.lastrowid
    con.commit()
    con.close()
    return trade_id


def evaluate_scorecard(
    symbol: str,
    setup_id: str,
    session: str,
    regime: str,
) -> ScorecardCheck:
    """
    FASE 3: scorecard jerárquico por símbolo.
    Prioridad de matching:
      1) setup + session + regime
      2) setup + session
      3) setup
      4) symbol (fallback global)
    """
    lookback = int(getattr(cfg, "SCORECARD_LOOKBACK_TRADES", 300))
    min_sample = int(getattr(cfg, "SCORECARD_MIN_SAMPLE", 8))
    min_wr = float(getattr(cfg, "SCORECARD_MIN_WIN_RATE", 52.0))

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    rows = cur.execute("""
        SELECT setup_id, session, regime, result
        FROM trades
        WHERE symbol=?
          AND result IN ('WIN','LOSS')
        ORDER BY id DESC
        LIMIT ?
    """, (symbol, lookback)).fetchall()
    con.close()

    def _compute(scope_rows, level_name, key_name):
        sample = len(scope_rows)
        wins = sum(1 for r in scope_rows if r[3] == "WIN")
        wr = (wins / sample * 100.0) if sample > 0 else 0.0
        return sample, wr, level_name, key_name

    candidates = []
    rows_l1 = [r for r in rows if r[0] == setup_id and r[1] == session and r[2] == regime]
    candidates.append(_compute(rows_l1, "L1_SETUP_SESSION_REGIME", f"{setup_id}|{session}|{regime}"))
    rows_l2 = [r for r in rows if r[0] == setup_id and r[1] == session]
    candidates.append(_compute(rows_l2, "L2_SETUP_SESSION", f"{setup_id}|{session}"))
    rows_l3 = [r for r in rows if r[0] == setup_id]
    candidates.append(_compute(rows_l3, "L3_SETUP", setup_id))
    candidates.append(_compute(rows, "L4_SYMBOL", symbol))

    chosen = next((c for c in candidates if c[0] >= min_sample), candidates[-1])
    sample, win_rate, level, key = chosen

    if sample < min_sample:
        should_block = False
        reason = (
            f"Scorecard {level}: muestra insuficiente ({sample}/{min_sample}) "
            "→ fallback permisivo"
        )
    else:
        should_block = win_rate < min_wr
        reason = (
            f"Scorecard {level}: WR={win_rate:.1f}% sample={sample} "
            f"(mín WR={min_wr:.1f}%)"
        )

    return ScorecardCheck(
        should_block=should_block,
        win_rate=round(win_rate, 1),
        sample_size=sample,
        level=level,
        key=key,
        min_win_rate=min_wr,
        min_sample=min_sample,
        reason=reason,
        setup_id=setup_id,
        session=session,
        regime=regime,
    )


def evaluate_policy(
    symbol: str,
    direction: str,
    setup_id: str,
    session: str,
    regime: str,
) -> PolicyCheck:
    """
    FASE 4: ranking de candidatos por setup.
    Combina WR, PF, avg_reward y tamaño de muestra en un policy_score [0,1].
    """
    lookback = int(getattr(cfg, "POLICY_LOOKBACK_TRADES", 300))
    min_sample = int(getattr(cfg, "POLICY_MIN_SAMPLE", 10))
    w_wr = float(getattr(cfg, "POLICY_WEIGHT_WR", 0.40))
    w_pf = float(getattr(cfg, "POLICY_WEIGHT_PF", 0.25))
    w_rw = float(getattr(cfg, "POLICY_WEIGHT_REWARD", 0.20))
    w_sm = float(getattr(cfg, "POLICY_WEIGHT_SAMPLE", 0.15))
    min_score = float(getattr(cfg, "POLICY_MIN_SCORE", 0.45))

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    rows = cur.execute("""
        SELECT result, profit, reward
        FROM trades
        WHERE symbol=?
          AND direction=?
          AND setup_id=?
          AND session=?
          AND regime=?
          AND result IN ('WIN','LOSS')
        ORDER BY id DESC
        LIMIT ?
    """, (symbol, direction, setup_id, session, regime, lookback)).fetchall()
    con.close()

    sample = len(rows)
    wins = sum(1 for r in rows if r[0] == "WIN")
    losses = sample - wins
    win_rate = (wins / sample * 100.0) if sample > 0 else 0.0

    gross_win = sum(max(float(r[1] or 0.0), 0.0) for r in rows)
    gross_loss = sum(abs(min(float(r[1] or 0.0), 0.0)) for r in rows)
    profit_factor = (gross_win / gross_loss) if gross_loss > 1e-9 else (gross_win if gross_win > 0 else 0.0)
    avg_reward = (sum(float(r[2] or 0.0) for r in rows) / sample) if sample > 0 else 0.0

    wr_norm = float(np.clip(win_rate / 100.0, 0.0, 1.0))
    pf_norm = float(np.clip(profit_factor / 2.0, 0.0, 1.0))
    rw_norm = float(np.clip((avg_reward + 1.0) / 2.0, 0.0, 1.0))
    sm_norm = float(np.clip(sample / max(min_sample * 2, 1), 0.0, 1.0))

    policy_score = w_wr * wr_norm + w_pf * pf_norm + w_rw * rw_norm + w_sm * sm_norm
    should_block = sample >= min_sample and policy_score < min_score
    reason = (
        f"policy={policy_score:.3f} | WR={win_rate:.1f}% PF={profit_factor:.2f} "
        f"R={avg_reward:+.3f} n={sample}"
    )

    return PolicyCheck(
        direction=direction,
        setup_id=setup_id,
        session=session,
        regime=regime,
        sample_size=sample,
        win_rate=round(win_rate, 1),
        profit_factor=round(float(profit_factor), 3),
        avg_reward=round(float(avg_reward), 3),
        policy_score=round(float(policy_score), 3),
        should_block=should_block,
        reason=reason,
    )


# ════════════════════════════════════════════════════════════════
#  ACTUALIZAR RESULTADO + ENTRENAR (Online Learning)
# ════════════════════════════════════════════════════════════════

def update_trade_result(
    ticket: int, close_price: float, profit: float,
    pips: float, result: str, duration_min: int,
):
    """
    Actualiza el resultado del trade y dispara el entrenamiento online del MLP.
    Esta es la función más importante — aquí el bot aprende de cada trade.
    """
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    row = cur.execute(
        "SELECT id, features_json, symbol, direction, risk_amount FROM trades WHERE ticket=?",
        (ticket,)
    ).fetchone()

    if not row:
        con.close()
        return

    trade_id, feat_json, symbol, direction, risk_amount_raw = row

    # FIX v7.0: Calcular reward usando risk_amount (dólares arriesgados en el SL).
    # Antes se pasaba abs(profit/pips) = $/pip, que era incorrecto como sl_distance.
    # Con risk_amount: risk_pct = risk_amount/balance*100 → penalización correcta.
    # Fallback: usar 1.5× |profit| si risk_amount no está disponible (trades viejos).
    risk_amount_val = float(risk_amount_raw or 0.0)
    sl_dist = risk_amount_val if risk_amount_val > 0 else max(abs(profit) * 1.5, 1.0)
    reward = compute_reward(profit, sl_dist, duration_min, result)

    cur.execute("""
        UPDATE trades SET close_price=?, profit=?, pips=?,
            result=?, closed_at=?, duration_min=?, reward=?
        WHERE ticket=?
    """, (close_price, profit, pips, result,
          datetime.now(timezone.utc).isoformat(), duration_min, reward, ticket))

    # Guardar vector (tanto WIN como LOSS — aprendemos de ambos)
    if feat_json:
        cur.execute("""
            INSERT INTO feature_vectors
                (trade_id, symbol, direction, result, reward, vector_json, timestamp)
            VALUES (?,?,?,?,?,?,?)
        """, (trade_id, symbol, direction, result, reward,
              feat_json, datetime.now(timezone.utc).isoformat()))

    con.commit()
    con.close()

    # ── Disparar entrenamiento online ────────────────────────────
    _online_train(symbol)

    log.info(
        f"[memory] ✅ Trade #{ticket} {symbol} {result} | "
        f"profit={profit:+.2f} | reward={reward:+.3f} | "
        f"pips={pips:.1f} | dur={duration_min}min"
    )


def _online_train(symbol: str):
    """
    Entrenamiento online: carga todos los trades del símbolo
    y reentrena el MLP + actualiza la atención.
    Se ejecuta después de cada trade cerrado.
    """
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute("""
            SELECT vector_json, result, reward, timestamp
            FROM feature_vectors
            WHERE symbol=?
            ORDER BY timestamp ASC
        """, (symbol,)).fetchall()
        con.close()

        if len(rows) < 4:
            log.debug(f"[mlp] {symbol}: {len(rows)} trades, esperando más datos para entrenar")
            return

        # Construir dataset
        X_list, y_list, rewards = [], [], []
        for feat_json, result, reward_val, ts in rows:
            try:
                fd = json.loads(feat_json)
                # Reconstruir vector de 40 features (32 originales + 8 FASE 2)
                # Los records viejos devuelven 0.0 para las 8 features nuevas → OK
                vec = [
                    fd.get("rsi_norm", 0.5), fd.get("rsi_zone", 0),
                    fd.get("macd_direction", 0), fd.get("macd_hist_norm", 0),
                    fd.get("dema_cruce", 0), fd.get("h1_trend_num", 0),
                    fd.get("kalman_trend_num", 0), fd.get("lr_slope_norm", 0),
                    fd.get("hilbert_signal_num", 0), fd.get("hilbert_phase_norm", 0.5),
                    fd.get("fisher_norm", 0), fd.get("cycle_osc", 0),
                    fd.get("hurst_val", 0.5), fd.get("hurst_regime_num", 0),
                    fd.get("stoch_norm", 0.5), fd.get("momentum_norm", 0),
                    fd.get("atr_norm", 0.3), fd.get("bb_pos_num", 0),
                    fd.get("bb_squeeze", 0), fd.get("ha_trend_num", 0),
                    fd.get("supertrend_num", 0), fd.get("sar_trend_num", 0),
                    fd.get("cci_norm", 0), fd.get("williams_norm", -0.5),
                    fd.get("in_strong_sr", 0), fd.get("dist_to_sr_norm", 0.5),
                    fd.get("vwap_side", 0), fd.get("obv_trend_num", 0),
                    fd.get("cmf_norm", 0), fd.get("mfi_norm", 0.5),
                    fd.get("news_sentiment", 0), fd.get("news_high_impact", 0),
                    # GRUPO 5 — Microestructura + Confluencia (FASE 2)
                    fd.get("micro_score_norm",       0.0),
                    fd.get("above_poc",              0.0),
                    fd.get("in_value_area",          0.0),
                    fd.get("above_session_vwap",     0.0),
                    fd.get("session_vwap_dev_norm",  0.0),
                    fd.get("fvg_bull_active",        0.0),
                    fd.get("fvg_bear_active",        0.0),
                    fd.get("confluence_total_norm",  0.0),
                ]
                if len(vec) < INPUT_DIM:
                    vec += [0.0] * (INPUT_DIM - len(vec))
                X_list.append(vec[:INPUT_DIM])
                y_list.append(1.0 if result == "LOSS" else 0.0)
                rewards.append(float(reward_val or 0))
            except Exception:
                continue

        if len(X_list) < 4:
            return

        X = np.array(X_list, dtype=np.float32)
        y = np.array(y_list, dtype=np.float32).reshape(-1, 1)

        # Cargar modelo
        mlp, att, ew = _load_model(symbol)

        # Activar MLP solo con suficientes datos
        if len(X) >= MLP_ACTIVATION:
            loss = mlp.fit(X, y, verbose=True)
            log.info(f"[mlp] {symbol}: entrenado {len(X)} samples | loss={loss:.4f} | "
                     f"epochs={mlp.epoch_count}")

        # Actualizar atención con el último trade
        last_vec = X[-1]
        last_reward = float(rewards[-1])
        att.update(last_vec, last_reward)

        # Actualizar pesos del ensemble según performance reciente
        if len(X) >= ENSEMBLE_ACTIVATION:
            _update_ensemble_weights(symbol, X, y, mlp, att, ew)

        # Guardar modelo actualizado
        _save_model(symbol)

    except Exception as e:
        log.error(f"[mlp] Error en entrenamiento online {symbol}: {e}", exc_info=True)


def _update_ensemble_weights(
    symbol: str,
    X: np.ndarray, y: np.ndarray,
    mlp: MLPBrain, att: AttentionLayer, ew: np.ndarray
):
    """
    Ajusta los pesos del ensemble [coseno, mlp, regime] según
    cuál componente ha sido más preciso en los últimos trades.
    """
    try:
        # Evaluar precisión de cada componente en los últimos 20 trades
        n_eval = min(20, len(X))
        X_eval = X[-n_eval:]
        y_eval = y[-n_eval:].flatten()

        # MLP accuracy
        mlp_preds = np.array([mlp.predict(x) for x in X_eval])
        mlp_acc   = float(np.mean((mlp_preds > 0.5) == y_eval))

        # Coseno accuracy estimado (aproximación)
        # Si la red dice >0.7 de certeza, es más precisa
        mlp_confidence = float(np.mean(np.abs(mlp_preds - 0.5) * 2))

        # Ajuste suave de pesos del ensemble
        if mlp_acc > 0.65:
            ew[1] = min(0.5, ew[1] + 0.02)   # aumentar peso MLP
            ew[0] = max(0.3, ew[0] - 0.01)   # reducir coseno
        elif mlp_acc < 0.45:
            ew[1] = max(0.1, ew[1] - 0.02)   # reducir peso MLP
            ew[0] = min(0.7, ew[0] + 0.01)   # aumentar coseno

        # Normalizar para sumar 1
        ew = ew / ew.sum()
        _ensemble_weights[symbol] = ew
        log.debug(f"[ensemble] {symbol}: w=[{ew[0]:.2f},{ew[1]:.2f},{ew[2]:.2f}] | mlp_acc={mlp_acc:.2f}")

    except Exception as e:
        log.debug(f"[ensemble] Error actualizando pesos: {e}")


# ════════════════════════════════════════════════════════════════
#  CHECK MEMORY — Consulta al cerebro completo
# ════════════════════════════════════════════════════════════════

def check_memory(
    features: TradeFeatures, symbol: str, direction: str, sym_cfg: dict
) -> MemoryCheck:
    """
    Consulta el sistema completo de ML para evaluar una oportunidad de trade.

    Combina:
    1. Similitud coseno (siempre activo)
    2. Red neuronal MLP (se activa con ≥20 trades)
    3. Detector de régimen de mercado (siempre activo)
    4. Attention mechanism (pesos aprendidos por símbolo)

    El score final es un ensemble ponderado de los tres componentes.
    """
    block_threshold = sym_cfg.get("memory_block_threshold", 0.90)
    warn_threshold  = sym_cfg.get("memory_warn_threshold", 0.80)
    min_trades      = sym_cfg.get("memory_min_trades", 5)
    decay_days      = sym_cfg.get("memory_decay_days", 45)

    # ── Cargar modelo y atención ──────────────────────────────────
    mlp, att, ew = _load_model(symbol)
    current_vec  = features.to_vector()

    # ── Régimen de mercado ────────────────────────────────────────
    regime, regime_multiplier = _regime_detector.detect(features)
    regime_direction_mult = _regime_detector.regime_matches_direction(regime, direction)

    # ── Cargar vectores históricos ────────────────────────────────
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("""
        SELECT vector_json, result, reward, timestamp
        FROM feature_vectors
        WHERE symbol=? AND direction=?
        ORDER BY timestamp DESC
        LIMIT 200
    """, (symbol, direction)).fetchall()
    total_trades = con.execute(
        "SELECT COUNT(*) FROM feature_vectors WHERE symbol=?", (symbol,)
    ).fetchone()[0]
    con.close()

    # ── PILAR 1: Similitud coseno ponderada con atención ─────────
    attention_weights = att.get_weights()
    cosine_losses, cosine_wins = 0, 0
    cosine_penalty, cosine_warnings = 0.0, []
    now = datetime.now(timezone.utc)

    for feat_json, result, reward_val, ts_str in rows:
        try:
            fd  = json.loads(feat_json)
            past_vec = np.array([
                fd.get("rsi_norm", 0.5), fd.get("rsi_zone", 0),
                fd.get("macd_direction", 0), fd.get("macd_hist_norm", 0),
                fd.get("dema_cruce", 0), fd.get("h1_trend_num", 0),
                fd.get("kalman_trend_num", 0), fd.get("lr_slope_norm", 0),
                fd.get("hilbert_signal_num", 0), fd.get("hilbert_phase_norm", 0.5),
                fd.get("fisher_norm", 0), fd.get("cycle_osc", 0),
                fd.get("hurst_val", 0.5), fd.get("hurst_regime_num", 0),
                fd.get("stoch_norm", 0.5), fd.get("momentum_norm", 0),
                fd.get("atr_norm", 0.3), fd.get("bb_pos_num", 0),
                fd.get("bb_squeeze", 0), fd.get("ha_trend_num", 0),
                fd.get("supertrend_num", 0), fd.get("sar_trend_num", 0),
                fd.get("cci_norm", 0), fd.get("williams_norm", -0.5),
                fd.get("in_strong_sr", 0), fd.get("dist_to_sr_norm", 0.5),
                fd.get("vwap_side", 0), fd.get("obv_trend_num", 0),
                fd.get("cmf_norm", 0), fd.get("mfi_norm", 0.5),
                fd.get("news_sentiment", 0), fd.get("news_high_impact", 0),
                # GRUPO 5 — Microestructura + Confluencia (FASE 2)
                fd.get("micro_score_norm",       0.0),
                fd.get("above_poc",              0.0),
                fd.get("in_value_area",          0.0),
                fd.get("above_session_vwap",     0.0),
                fd.get("session_vwap_dev_norm",  0.0),
                fd.get("fvg_bull_active",        0.0),
                fd.get("fvg_bear_active",        0.0),
                fd.get("confluence_total_norm",  0.0),
            ], dtype=np.float32)

            # Rellenar si faltan features
            if len(past_vec) < INPUT_DIM:
                past_vec = np.pad(past_vec, (0, INPUT_DIM - len(past_vec)))

        except Exception:
            continue

        # Similitud coseno con pesos de atención
        sim = _cosine_sim(current_vec, past_vec, attention_weights)

        # Decay temporal
        try:
            past_dt  = datetime.fromisoformat(ts_str)
            days_ago = (now - past_dt.replace(tzinfo=timezone.utc)).days
            decay    = max(0.1, 1.0 - days_ago / decay_days)
        except Exception:
            decay = 1.0

        eff_sim = sim * decay

        if result == "LOSS" and eff_sim >= warn_threshold:
            cosine_losses  += 1
            penalty_raw     = (eff_sim - warn_threshold) / (1 - warn_threshold) * 2.5
            cosine_penalty += penalty_raw
            if eff_sim >= block_threshold:
                cosine_warnings.append(f"Patrón pérdida similar ({eff_sim:.2f})")
        elif result == "WIN" and eff_sim >= warn_threshold:
            cosine_wins += 1

    # ── PILAR 2: Red neuronal MLP ─────────────────────────────────
    mlp_score = 0.5   # neutro por defecto (sin sesgo)
    mlp_active = total_trades >= MLP_ACTIVATION

    if mlp_active:
        try:
            mlp_score = mlp.predict(current_vec)
        except Exception as e:
            log.debug(f"[mlp] Error prediciendo: {e}")
            mlp_score = 0.5

    # ── ENSEMBLE FINAL ────────────────────────────────────────────
    # score_final = w_coseno × coseno_score + w_mlp × mlp_score + w_regime × regime_score

    # Coseno score normalizado (0=todos wins, 1=todos losses)
    if cosine_losses + cosine_wins > 0:
        cosine_score = cosine_losses / (cosine_losses + cosine_wins)
    else:
        cosine_score = 0.3   # prior neutro-optimista

    # Régimen score (0=favorable, 1=desfavorable)
    regime_score = 1.0 - (regime_multiplier * regime_direction_mult - 0.3) / 1.0
    regime_score = float(np.clip(regime_score, 0.0, 1.0))

    # Pesos del ensemble (se adaptan con el tiempo)
    if total_trades >= ENSEMBLE_ACTIVATION:
        w_cos, w_mlp, w_reg = ew[0], ew[1], ew[2]
    elif mlp_active:
        w_cos, w_mlp, w_reg = 0.5, 0.3, 0.2
    else:
        w_cos, w_mlp, w_reg = 0.7, 0.0, 0.3

    ensemble_score = (
        w_cos * cosine_score +
        w_mlp * mlp_score +
        w_reg * regime_score
    )

    # ── Inicialización de should_block ───────────────────────────
    # IMPORTANTE: debe inicializarse ANTES de cualquier uso condicional
    # para evitar UnboundLocalError cuando warmup_mode=False.
    should_block = False

    # ── Modo Calentamiento (Cold Start) ──────────────────────────
    # Si no hay suficientes datos globales, endurecer filtros de coseno.
    # SOLO bloquear en regímenes sin estructura (CHAOTIC).
    # RANGING, MIXED y VOLATILE son regímenes válidos para operar — no bloquear.
    # FIX v6.6: Ampliado de ["TRENDING_UP","TRENDING_DOWN"] a todos los regímenes
    # excepto CHAOTIC, que ya tiene su propio bloqueo explícito debajo.
    # El umbral subió de 0.55 a 0.65 para evitar bloqueos excesivos en warmup.
    warmup_trade_count = getattr(cfg, "WARMUP_TRADE_COUNT", MLP_ACTIVATION)
    warmup_mode = total_trades < warmup_trade_count
    if warmup_mode:
        # En warmup solo bloquear si el régimen es CHAOTIC O si el ensemble es
        # muy alto (>0.65) Y el régimen es desconocido.
        # RANGING/MIXED/VOLATILE son operables — no bloquear por régimen.
        if regime == "CHAOTIC":
            should_block = True
            cosine_warnings.append(
                f"⚠️ Modo Calentamiento — Régimen CAÓTICO ({total_trades}/{warmup_trade_count} trades)"
            )
        elif regime not in ("TRENDING_UP", "TRENDING_DOWN", "RANGING", "MIXED", "VOLATILE"):
            # Régimen desconocido — ser conservador
            if ensemble_score > 0.65:
                should_block = True
                cosine_warnings.append(
                    f"⚠️ Modo Calentamiento ({total_trades}/{warmup_trade_count} trades)"
                )

    # ── Decisión final ────────────────────────────────────────────

    # Anti-parálisis: si wins > losses en patrones similares → reducir penalización
    if cosine_wins >= cosine_losses and cosine_wins > 0:
        ensemble_score *= 0.7   # reducir score de riesgo

    # Penalización total
    total_penalty = float(np.clip(cosine_penalty, 0, 2.5))
    confidence_adj = -total_penalty if total_penalty > 0.3 else 0.0

    # Ajuste positivo si el MLP dice que es seguro
    if mlp_active and mlp_score < 0.3:
        confidence_adj += 1.0   # MLP dice baja probabilidad de pérdida

    # Bloqueo basado en ensemble score + cantidad mínima de trades
    should_block = should_block or (
        ensemble_score > 0.72 and
        cosine_losses >= min_trades and
        cosine_wins < cosine_losses * 0.5
    )

    # Si el régimen es CHAOTIC, siempre bloquear
    if regime == "CHAOTIC":
        should_block = True
        cosine_warnings.append("Régimen CHAÓTICO — sin ventaja estadística")

    # Construir mensaje de detalle
    detail_parts = [
        f"Régimen: {regime} ({regime_multiplier:.2f}×)",
        f"Coseno: {cosine_score:.2f} ({cosine_wins}W/{cosine_losses}L)",
    ]
    if mlp_active:
        detail_parts.append(f"MLP: {mlp_score:.2f} ({mlp.trained_samples}sp)")
    if warmup_mode:
        detail_parts.append(f"WARMUP ({total_trades}/{warmup_trade_count})")
    detail_parts.append(f"Ensemble: {ensemble_score:.2f}")
    ensemble_detail = " | ".join(detail_parts)

    warning_msg = " | ".join(cosine_warnings) if cosine_warnings else ensemble_detail
    warning_msg = _normalize_message_text(warning_msg)
    ensemble_detail = _normalize_message_text(ensemble_detail)

    log.debug(
        f"[brain] {symbol} {direction}: "
        f"block={should_block} warmup={warmup_mode} | {ensemble_detail}"
    )

    return MemoryCheck(
        should_block   = should_block,
        confidence_adj = float(np.clip(confidence_adj, -2.5, 2.0)),
        warning_msg    = warning_msg,
        similar_losses = cosine_losses,
        similar_wins   = cosine_wins,
        mlp_score      = float(mlp_score),
        regime         = regime,
        ensemble_detail= ensemble_detail,
        warmup_mode    = warmup_mode,
    )


# ════════════════════════════════════════════════════════════════
#  ESTADÍSTICAS Y REPORTES
# ════════════════════════════════════════════════════════════════

def get_memory_stats(symbol: Optional[str] = None) -> dict:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    try:
        base_where = " WHERE symbol=?" if symbol else ""
        cond = " AND" if symbol else " WHERE"
        args = (symbol,) if symbol else ()
        total  = cur.execute(f"SELECT COUNT(*) FROM trades{base_where}", args).fetchone()[0]
        wins   = cur.execute(f"SELECT COUNT(*) FROM trades{base_where}{cond} result='WIN'", args).fetchone()[0]
        losses = cur.execute(f"SELECT COUNT(*) FROM trades{base_where}{cond} result='LOSS'", args).fetchone()[0]
        be     = cur.execute(f"SELECT COUNT(*) FROM trades{base_where}{cond} result='BE'", args).fetchone()[0]
        profit = cur.execute(f"SELECT COALESCE(SUM(profit),0) FROM trades{base_where}", args).fetchone()[0]
        avg_rw = cur.execute(
            f"SELECT COALESCE(AVG(reward),0) FROM trades{base_where}{cond} reward IS NOT NULL",
            args,
        ).fetchone()[0]

        # Avg win / avg loss / best / worst
        avg_win_row = cur.execute(
            f"SELECT COALESCE(AVG(profit),0) FROM trades{base_where}{cond} result='WIN' AND profit IS NOT NULL",
            args,
        ).fetchone()
        avg_win = float(avg_win_row[0]) if avg_win_row else 0.0

        avg_loss_row = cur.execute(
            f"SELECT COALESCE(AVG(profit),0) FROM trades{base_where}{cond} result='LOSS' AND profit IS NOT NULL",
            args,
        ).fetchone()
        avg_loss = float(avg_loss_row[0]) if avg_loss_row else 0.0

        best_row = cur.execute(
            f"SELECT COALESCE(MAX(profit),0) FROM trades{base_where}{cond} profit IS NOT NULL",
            args,
        ).fetchone()
        best_trade = float(best_row[0]) if best_row else 0.0

        worst_row = cur.execute(
            f"SELECT COALESCE(MIN(profit),0) FROM trades{base_where}{cond} profit IS NOT NULL",
            args,
        ).fetchone()
        worst_trade = float(worst_row[0]) if worst_row else 0.0

        # Max drawdown % (running peak-to-trough on cumulative profit)
        max_dd_pct = 0.0
        closed_rows = cur.execute(
            f"SELECT profit FROM trades{base_where}{cond} profit IS NOT NULL AND closed_at IS NOT NULL ORDER BY closed_at ASC",
            args,
        ).fetchall()
        if closed_rows:
            cum = 0.0
            peak = 0.0
            for (p,) in closed_rows:
                cum += float(p)
                if cum > peak:
                    peak = cum
                dd = peak - cum
                if peak > 0 and (dd / peak * 100) > max_dd_pct:
                    max_dd_pct = dd / peak * 100

        # WR = wins/(wins+losses) — BE excluido del denominador
        decidable = wins + losses
        wr = (wins / decidable * 100) if decidable > 0 else 0
        return {
            "symbol": symbol or "ALL",
            "total": total, "wins": wins, "losses": losses, "be": be,
            "profit": round(float(profit), 2),
            "win_rate": round(wr, 1),
            "avg_reward": round(float(avg_rw), 3),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "best_trade": round(best_trade, 2),
            "worst_trade": round(worst_trade, 2),
            "max_drawdown_pct": round(max_dd_pct, 2),
        }
    except Exception:
        return {"symbol": symbol or "ALL", "total": 0, "wins": 0, "losses": 0, "be": 0,
                "profit": 0.0, "win_rate": 0.0, "avg_reward": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
                "max_drawdown_pct": 0.0}
    finally:
        con.close()


def get_equity_curve_data(initial_balance: float = 10000.0, rolling_window: int = 20) -> list:
    """Retorna lista de dicts con trade_number, balance, drawdown_pct, rolling_wr, timestamp.

    Reconstruye la equity curve sumando el profit de cada trade cerrado (ordenado
    por closed_at).  Los trades sin closed_at se omiten para evitar distorsión.
    """
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    try:
        rows = cur.execute(
            "SELECT id, profit, result, closed_at "
            "FROM trades "
            "WHERE result IS NOT NULL AND closed_at IS NOT NULL "
            "ORDER BY closed_at ASC"
        ).fetchall()
    except Exception:
        return []
    finally:
        con.close()

    if not rows:
        return []

    points = []
    balance = initial_balance
    peak = initial_balance
    results_window: list = []  # últimos N resultados para rolling WR

    for i, (trade_id, profit, result, closed_at) in enumerate(rows, start=1):
        profit = float(profit or 0.0)
        balance += profit
        peak = max(peak, balance)
        dd_pct = (peak - balance) / peak * 100 if peak > 0 else 0.0

        # Only track WIN/LOSS in the window — BE trades don't count toward WR
        if result in ("WIN", "LOSS"):
            results_window.append(1 if result == "WIN" else 0)
            if len(results_window) > rolling_window:
                results_window.pop(0)
        decidable = results_window  # already filtered to WIN/LOSS only
        rolling_wr = (sum(decidable) / len(decidable) * 100) if decidable else 0.0

        points.append({
            "trade_number": i,
            "balance": round(balance, 2),
            "drawdown_pct": round(dd_pct, 2),
            "rolling_wr": round(rolling_wr, 1),
            "timestamp": closed_at,
        })

    return points


def get_distribution_data() -> dict:
    """Retorna {by_symbol: [...], by_hour: [...]} con WR y conteos.

    by_symbol: [{symbol, wins, losses, total, wr}]
    by_hour:   [{hour, wins, losses, total, wr}]
    """
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    try:
        sym_rows = cur.execute(
            "SELECT symbol, "
            "  SUM(CASE WHEN result='WIN'  THEN 1 ELSE 0 END) AS wins, "
            "  SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) AS losses, "
            "  COUNT(*) AS total "
            "FROM trades "
            "WHERE result IS NOT NULL "
            "GROUP BY symbol "
            "ORDER BY total DESC"
        ).fetchall()

        hour_rows = cur.execute(
            "SELECT CAST(strftime('%H', closed_at) AS INTEGER) AS hour, "
            "  SUM(CASE WHEN result='WIN'  THEN 1 ELSE 0 END) AS wins, "
            "  SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) AS losses, "
            "  COUNT(*) AS total "
            "FROM trades "
            "WHERE result IS NOT NULL AND closed_at IS NOT NULL "
            "GROUP BY hour "
            "ORDER BY hour ASC"
        ).fetchall()
    except Exception:
        return {"by_symbol": [], "by_hour": []}
    finally:
        con.close()

    def _wr(wins, losses):
        d = wins + losses
        return round(wins / d * 100, 1) if d > 0 else 0.0

    by_symbol = [
        {"symbol": r[0], "wins": r[1], "losses": r[2], "total": r[3],
         "wr": _wr(r[1], r[2])}
        for r in sym_rows
    ]
    by_hour = [
        {"hour": r[0], "wins": r[1], "losses": r[2], "total": r[3],
         "wr": _wr(r[1], r[2])}
        for r in hour_rows
    ]
    return {"by_symbol": by_symbol, "by_hour": by_hour}


def get_recent_trades(limit: int = 50) -> list:
    """Retorna los últimos N trades cerrados con detalles completos."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    try:
        rows = cur.execute(
            "SELECT id, ticket, symbol, direction, open_price, close_price, "
            "  profit, pips, duration_min, result, opened_at, closed_at "
            "FROM trades "
            "WHERE result IS NOT NULL "
            "ORDER BY closed_at DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    except Exception:
        return []
    finally:
        con.close()

    trades = []
    for i, r in enumerate(rows, start=1):
        trades.append({
            "num": i,
            "id": r[0],
            "ticket": r[1],
            "symbol": r[2] or "",
            "direction": r[3] or "",
            "open_price": r[4],
            "close_price": r[5],
            "profit": round(float(r[6] or 0), 2),
            "pips": round(float(r[7] or 0), 1) if r[7] is not None else None,
            "duration_min": r[8],
            "result": r[9] or "",
            "opened_at": r[10] or "",
            "closed_at": r[11] or "",
        })
    return trades


def get_pending_trades() -> list:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT ticket, symbol, direction, open_price, opened_at, slippage_pips FROM trades WHERE result IS NULL"
    ).fetchall()
    con.close()
    return [{"ticket": r[0], "symbol": r[1], "direction": r[2],
             "open_price": r[3], "opened_at": r[4], "slippage_pips": r[5]} for r in rows]


def get_learning_report() -> str:
    stats = get_memory_stats()
    # Estado de los modelos por símbolo
    model_states = []
    for sym in _mlp_cache:
        mlp = _mlp_cache[sym]
        att = _attention_cache.get(sym)
        mode = ("Ensemble" if mlp.trained_samples >= ENSEMBLE_ACTIVATION else
                ("MLP+Coseno" if mlp.trained_samples >= MLP_ACTIVATION else
                 "Coseno+Régimen"))
        model_states.append(f"{sym}:{mode}({mlp.trained_samples}sp)")

    models_str = " | ".join(model_states) if model_states else "Sin modelos activos"
    be_count = stats.get("be", 0)
    return _normalize_message_text(
        f"🧠 Memoria v2: {stats['total']} trades | "
        f"✅ {stats['wins']}W ❌ {stats['losses']}L ⚡ {be_count}BE | "
        f"WR: {stats['win_rate']}% (excl. BE) | P&L: ${stats['profit']:+.2f} | "
        f"Avg Reward: {stats['avg_reward']:+.3f}\n"
        f"🤖 Modelos: {models_str}"
    )


def get_advanced_metrics() -> dict:
    """
    Advanced metrics for the dashboard: WR by session, slippage stats,
    avg trade duration, and score analysis for winners vs losers.
    """
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    result = {
        "wr_by_session": [],
        "slippage_by_symbol": [],
        "avg_duration_min": 0,
        "avg_duration_winners": 0,
        "avg_duration_losers": 0,
        "score_winners_avg": 0,
        "score_losers_avg": 0,
    }

    try:
        # WR by session (based on opened_at hour)
        rows = cur.execute("""
            SELECT
                CASE
                    WHEN CAST(strftime('%H', opened_at) AS INTEGER) BETWEEN 0 AND 6 THEN 'Asian'
                    WHEN CAST(strftime('%H', opened_at) AS INTEGER) BETWEEN 7 AND 12 THEN 'London'
                    WHEN CAST(strftime('%H', opened_at) AS INTEGER) BETWEEN 13 AND 20 THEN 'New York'
                    ELSE 'Dead Zone'
                END as session,
                COUNT(*) as total,
                SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END) as losses,
                AVG(profit) as avg_profit
            FROM trades
            WHERE result IS NOT NULL AND opened_at IS NOT NULL
            GROUP BY session
            ORDER BY
                CASE session
                    WHEN 'Asian' THEN 1
                    WHEN 'London' THEN 2
                    WHEN 'New York' THEN 3
                    ELSE 4
                END
        """).fetchall()

        for r in rows:
            session, total, wins, losses, avg_profit = r
            wr = round(wins / max(1, wins + losses) * 100, 1)
            result["wr_by_session"].append({
                "session":    session,
                "total":      total,
                "wins":       wins,
                "losses":     losses,
                "wr":         wr,
                "avg_profit": round(avg_profit or 0, 2),
            })

        # Slippage by symbol (uses slippage_pips column if available)
        try:
            rows = cur.execute("""
                SELECT symbol,
                       COUNT(*) as total,
                       AVG(slippage_pips) as avg_slip,
                       MAX(slippage_pips) as max_slip
                FROM trades
                WHERE result IS NOT NULL AND slippage_pips IS NOT NULL AND slippage_pips > 0
                GROUP BY symbol
                ORDER BY avg_slip DESC
            """).fetchall()

            for r in rows:
                symbol, total, avg_slip, max_slip = r
                result["slippage_by_symbol"].append({
                    "symbol":               symbol or "",
                    "total":                total,
                    "avg_slippage_pips":    round(avg_slip or 0, 2),
                    "max_slippage_pips":    round(max_slip or 0, 2),
                })
        except Exception:
            pass  # Column may not exist in older databases

        # Average duration
        row = cur.execute("""
            SELECT AVG(duration_min),
                   AVG(CASE WHEN result = 'WIN' THEN duration_min END),
                   AVG(CASE WHEN result = 'LOSS' THEN duration_min END)
            FROM trades
            WHERE result IS NOT NULL AND duration_min IS NOT NULL
        """).fetchone()

        if row:
            result["avg_duration_min"]      = round(row[0] or 0, 1)
            result["avg_duration_winners"]  = round(row[1] or 0, 1)
            result["avg_duration_losers"]   = round(row[2] or 0, 1)

        # Score analysis: winners vs losers (uses setup_score column if available)
        try:
            row = cur.execute("""
                SELECT AVG(CASE WHEN result = 'WIN' THEN setup_score END),
                       AVG(CASE WHEN result = 'LOSS' THEN setup_score END)
                FROM trades
                WHERE result IS NOT NULL AND setup_score IS NOT NULL
            """).fetchone()

            if row:
                result["score_winners_avg"] = round(row[0] or 0, 2)
                result["score_losers_avg"]  = round(row[1] or 0, 2)
        except Exception:
            pass  # Column may not exist in older databases

    except Exception as e:
        log.warning(f"[memory] Error getting advanced metrics: {e}")
    finally:
        con.close()

    return result


# ════════════════════════════════════════════════════════════════
#  MEJORA 15 — SHADOW TRADES (Paper Trading)
# ════════════════════════════════════════════════════════════════

def save_shadow_trade(
    symbol: str, direction: str, entry_price: float,
    sl: float, tp: float, volume: float, score: float,
    reason: str, h1_trend: str, htf_trend: str,
    hurst: float, rsi: float, atr: float,
) -> int:
    """Inserta un nuevo shadow trade. Retorna el id asignado."""
    from datetime import datetime, timezone
    opened_at = datetime.now(timezone.utc).isoformat()
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.execute(
            """INSERT INTO shadow_trades
               (symbol, direction, entry_price, sl, tp, volume, score,
                reason, h1_trend, htf_trend, hurst, rsi, atr, opened_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, direction, entry_price, sl, tp, volume, score,
             reason, h1_trend, htf_trend, hurst, rsi, atr, opened_at),
        )
        shadow_id = cur.lastrowid
        con.commit()
        con.close()
        return shadow_id
    except Exception as e:
        log.error(f"[shadow] Error guardando shadow trade: {e}")
        return -1


def close_shadow_trade(
    shadow_id: int, exit_price: float, result: str,
    profit_pips: float, duration_min: int,
) -> None:
    """Actualiza un shadow trade al cerrarse (SL/TP alcanzado)."""
    from datetime import datetime, timezone
    closed_at = datetime.now(timezone.utc).isoformat()
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute(
            """UPDATE shadow_trades
               SET closed_at=?, exit_price=?, result=?, profit_pips=?, duration_min=?
               WHERE id=?""",
            (closed_at, exit_price, result, profit_pips, duration_min, shadow_id),
        )
        con.commit()
        con.close()
    except Exception as e:
        log.error(f"[shadow] Error cerrando shadow trade #{shadow_id}: {e}")


def get_shadow_stats() -> dict:
    """Retorna estadísticas agregadas de los shadow trades cerrados."""
    result = {
        "total": 0, "wins": 0, "losses": 0,
        "win_rate": 0.0, "avg_profit_pips": 0.0,
    }
    try:
        con = sqlite3.connect(DB_PATH)
        row = con.execute(
            """SELECT COUNT(*),
                      SUM(CASE WHEN result='WIN'  THEN 1 ELSE 0 END),
                      SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END),
                      AVG(profit_pips)
               FROM shadow_trades
               WHERE result IS NOT NULL"""
        ).fetchone()
        con.close()
        if row and row[0]:
            total = int(row[0])
            wins  = int(row[1] or 0)
            losses = int(row[2] or 0)
            result["total"]           = total
            result["wins"]            = wins
            result["losses"]          = losses
            result["win_rate"]        = round((wins / total) * 100, 1) if total else 0.0
            result["avg_profit_pips"] = round(row[3] or 0.0, 2)
    except Exception as e:
        log.warning(f"[shadow] Error obteniendo estadísticas: {e}")
    return result


def get_recent_shadow_trades(limit: int = 50) -> list:
    """Retorna los últimos N shadow trades para el dashboard."""
    rows = []
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.execute(
            """SELECT id, symbol, direction, entry_price, sl, tp, volume,
                      score, h1_trend, htf_trend, hurst, rsi, atr,
                      opened_at, closed_at, exit_price, result, profit_pips, duration_min
               FROM shadow_trades
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        con.close()
    except Exception as e:
        log.warning(f"[shadow] Error obteniendo shadow trades: {e}")
    return rows
