# 🤖 ZAR ULTIMATE BOT v6

Bot de trading algorítmico profesional para MT5/Exness con inteligencia artificial,
algoritmos matemáticos avanzados y aprendizaje automático.

## ▶️ Inicio rápido (3 pasos)

```bash
# 1. Instalar dependencias
pip install -r requirements.txt

# 2. Copiar .env.example a .env y completar credenciales requeridas

# 3. Ejecutar
python main.py
```

### Verificación rápida

```bash
python -m compileall .
```

## 📁 Estructura

```
zarbot_v6/
├── config.py              ← Solo editas este (ya configurado)
├── main.py                ← Solo ejecutas este
├── requirements.txt
├── modules/
│   ├── indicators.py      ← 45+ indicadores + 7 algoritmos matemáticos
│   ├── sr_zones.py        ← Soporte/Resistencia (6 métodos)
│   ├── news_engine.py     ← Noticias gratuitas (RSS + Alpha Vantage)
│   ├── neural_brain.py    ← Memoria persistente SQLite
│   ├── risk_manager.py    ← Gestión de riesgo
│   ├── telegram_notifier.py ← Notificaciones completas
│   └── dashboard.py       ← Panel visual consola
└── memory/
    └── zar_memory.db      ← Auto-creado
```

## 🧮 Algoritmos matemáticos implementados

### 1. Transformada de Hilbert (Proyecto de grado — senos y cosenos)
El algoritmo de John Ehlers descompone el precio en componentes **In-Phase** (coseno)
y **Quadrature** (seno) para detectar **máximos y mínimos locales** con precisión matemática:

- `sine ≈ +1.0` (fase ≈ 90°) → **MÁXIMO LOCAL** → señal de venta
- `sine ≈ -1.0` (fase ≈ 270°) → **MÍNIMO LOCAL** → señal de compra  
- `lead_sine` adelantado 45° → anticipa el cambio **antes** de que ocurra
- Período dominante → cuántas velas dura el ciclo actual

### 2. Exponente de Hurst
- `H > 0.6` → mercado con tendencia persistente → seguir la tendencia
- `H ≈ 0.5` → movimiento aleatorio → ser conservador
- `H < 0.4` → reversión a la media → usar S/R agresivamente

### 3. Filtro de Kalman
Estimación óptima del precio real sin lag, basado en el modelo de Rudolf Kálmán.
Más preciso que cualquier EMA para seguimiento de precio.

### 4. Ciclos de Fourier (FFT)
Descomposición espectral para encontrar el período dominante del mercado.
Identifica los ciclos naturales del precio.

### 5. Transformada de Fisher
Convierte precios a distribución Gaussiana.
- `Fisher > +2.0` → estadísticamente sobrecomprado
- `Fisher < -2.0` → estadísticamente sobrevendido

### 6. Oscilador de Ciclo Adaptativo
Combina Hilbert + Fourier. Oscila entre -1 (suelo) y +1 (techo del ciclo).

### 7. Regresión Lineal Adaptativa
Calcula la pendiente real de la tendencia. `R²` indica la calidad estadística.

## 📡 Notificaciones Telegram

El bot notifica via Telegram en tiempo real:
- ✅ Bot iniciado (balance, algoritmos activos, memoria)
- 🟢/🔴 Operación abierta (precio, SL, TP, RR, todos los indicadores)
- 🛡 Breakeven activado
- 🎯 Take Profit cercano
- 📊 Operación cerrada (resultado, pips, aprendizaje)
- 📰 Pausa por noticias de alto impacto
- 🧠 Bloqueo por memoria neural
- 📋 Resumen diario (17:00 UTC)

## ⚙️ Requisitos

- Windows (MT5 solo corre en Windows o Wine)
- MetaTrader5 instalado y la terminal abierta
- Python 3.10+
- Cuenta Demo Exness activa

## ⚠️ Importante

Siempre empieza en **DEMO**. El bot está configurado para demo Exness.
No garantiza resultados. El trading conlleva riesgo de pérdida.
