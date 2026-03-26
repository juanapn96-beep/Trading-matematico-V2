# CHANGELOG — ZAR ULTIMATE BOT v6

Registro de cambios del proyecto. Formato: `[Fecha Hora UTC] - [Módulo/Archivo] - Descripción - [Agente/Autor]`

---

## [2026-03-26 16:31 UTC] - FASE 1: El Tercer Pilar (Microestructura) y Refinamiento Adaptativo

### modules/microstructure.py *(nuevo)*
- **Creado** el módulo del Tercer Pilar de la arquitectura de decisión.
- Implementada función `volume_profile()`: construye un histograma de tick_volume sobre los últimos N candles y calcula **POC** (Point of Control), **VAH** (Value Area High) y **VAL** (Value Area Low) mediante expansión dinámica del Value Area al 70 % del volumen total.
- Implementada función `session_vwap()`: calcula el **VWAP anclado** por sesión UTC (Asiática 00-08h / Europea 07-17h / Americana 13-22h), con prioridad AMERICAN > EUROPEAN > ASIAN en overlaps. Incluye VWAP de sesión anterior como referencia.
- Implementada función `detect_fair_value_gaps()`: detecta **Fair Value Gaps (FVG / Imbalances)** bullish y bearish en los últimos 50 candles. Marca cada FVG como mitigado si el precio regresó a llenarlo en velas posteriores.
- Implementada función `compute_microstructure()`: punto de entrada principal que llama a las tres funciones anteriores y computa el **Micro Score** ponderado en el rango [-3, +3] con 4 componentes (POC, Value Area, Session VWAP, FVG proximity).
- Todas las funciones son **robustas a errores** (try/except) para no interrumpir el ciclo de trading si hay datos anómalos.
- **[Agente: GitHub Copilot]**

### modules/indicators.py
- **Parámetros adaptativos a la volatilidad** (FASE 1 — Refinamiento):
  - `hilbert_transform()`: el rango de períodos mín/máx se adapta a `atr_pct`. Mercado volátil (ATR% > 1%) → `min_period=4, max_period=30`. Mercado tranquilo → valores estándar `(6, 50)`.
  - `hurst_exponent()`: `max_lag` adaptativo. ATR% > 0.8% → `max_lag=15` (ventana corta, detección rápida). ATR% < 0.4% → `max_lag=30` (ventana larga, mayor estabilidad).
  - `kalman_filter()`: parámetros `R` (ruido de observación) y `Q` (ruido de proceso) adaptativos. `R = ATR% / 100` (mayor volatilidad → filtro más suave). `Q = R × 0.01`.
- **Pilar 3 integrado**: al final de `compute_all()`, se llama a `compute_microstructure()` y el resultado se almacena en `ctx["microstructure"]`.
- **Matriz de Confluencia** (3 Pilares): se computa automáticamente `ctx["confluence"]` con `p1_score` (estadístico), `p2_score` (matemático), `p3_score` (micro), `total` (ponderado 40/30/30), `bias`, y `sniper_aligned` (True si los 3 pilares coinciden).
- Añadido `from modules.microstructure import compute_microstructure` como import de módulo.
- **[Agente: GitHub Copilot]**

### main.py
- **Sistema Prompt actualizado**: añadidas REGLA 14 (Pilar 3 Microestructura) y REGLA 15 (Confluencia de 3 Pilares) al bloque de instrucciones de Gemini. La IA ahora recibe contexto explícito sobre cómo interpretar POC, VAH/VAL, Session VWAP, FVGs y el score de confluencia.
- **`build_context()`**: añadidas dos nuevas secciones al prompt de Gemini:
  - `── PILAR 3: MICROESTRUCTURA ──` con POC, VAH, VAL, posición relativa, Session VWAP con desviación %, FVGs bullish/bearish activos, y Micro Score.
  - `── CONFLUENCIA 3 PILARES ──` con los scores individuales de P1/P2/P3, score total, bias y estado `sniper_aligned`.
- **[Agente: GitHub Copilot]**

### config.py
- Añadido bloque **`FASE 1 — TERCER PILAR: MICROESTRUCTURA`** con parámetros configurables:
  - `MICROSTRUCTURE_VP_CANDLES=100`, `MICROSTRUCTURE_VP_BINS=50` (Volume Profile)
  - `MICROSTRUCTURE_FVG_CANDLES=50`, `MICROSTRUCTURE_FVG_MAX_AGE=20` (Fair Value Gaps)
  - `CONFLUENCE_MIN_SCORE=0.3` (umbral de confluencia mínima, ajustable sin tocar la lógica)
- **[Agente: GitHub Copilot]**

---

## [2026-03-26 16:22 UTC] - FASE 0: Securización y Entorno

### config.py
- **Migración de credenciales a `.env`**: Las variables sensibles `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `GEMINI_API_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `ALPHA_VANTAGE_KEY` y `FINNHUB_KEY` ya **no se almacenan como literales** en el código fuente. Ahora se cargan dinámicamente mediante `os.environ.get()` a través de `python-dotenv`.
- Añadido `import os`, `import sys` y `from dotenv import load_dotenv` con llamada a `load_dotenv()` al inicio del módulo.
- Añadida función `_require_env()` que valida la presencia de cada credencial obligatoria en el entorno y termina el proceso con un mensaje de error claro si alguna falta, evitando fallos silenciosos.
- Versión actualizada a `v6.5 — ENV SECURIZATION`.
- **[Agente: GitHub Copilot]**

### requirements.txt
- Añadida dependencia `python-dotenv>=1.0.0` para la carga del archivo `.env`.
- **[Agente: GitHub Copilot]**

### .env.example *(nuevo)*
- Creado archivo de plantilla con las variables de entorno requeridas y valores de ejemplo/placeholder.
- Los desarrolladores deben copiar este archivo a `.env` y rellenar con sus credenciales reales.
- **[Agente: GitHub Copilot]**

### .gitignore *(nuevo)*
- Creado archivo `.gitignore` que excluye:
  - `.env` — archivo de credenciales (nunca debe subirse al repo)
  - `__pycache__/` y archivos `*.pyc`/`*.pyo` — caché de bytecode Python
  - `*.db` y `memory/*.db` — base de datos SQLite de memoria del bot
  - `*.log` — archivos de log generados en ejecución
  - Directorios de entornos virtuales (`venv/`, `.venv/`, `env/`)
  - Artefactos de build y distribución
  - Archivos de IDEs (`.idea/`, `.vscode/`)
- **[Agente: GitHub Copilot]**

### CHANGELOG.md *(nuevo)*
- Creado archivo de trazabilidad de cambios en la raíz del repositorio.
- **[Agente: GitHub Copilot]**
