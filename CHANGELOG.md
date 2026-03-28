# CHANGELOG — ZAR ULTIMATE BOT v6

Registro de cambios del proyecto. Formato: `[Fecha Hora UTC] - [Módulo/Archivo] - Descripción - [Agente/Autor]`

---

## [2026-03-28 17:00 UTC] - FASE 7: Integración de correcciones críticas + nuevos módulos

### modules/indicators.py
- **FIX CRÍTICO — Hilbert Transform phase calculation**: Reemplazado el cálculo incorrecto
  `phase_deg = np.degrees(phase_rad) * len(closes) % 360` con el algoritmo correcto de
  acumulación de DeltaPhase de Ehlers (`dc_phase[i] = dc_phase[i-1] + 360/smo_per[i]`).
  Esto corrige las señales LOCAL_MAX y LOCAL_MIN que estaban basadas en fases incorrectas.
- **FIX — Hurst exponent stability**: Incrementado max_lag de 15-30 a 40-100 (adaptativo
  por ATR%), y el default automático usa `min(len(prices)//4, 100)` como recomienda la
  literatura académica. Reduce la varianza del estimador significativamente.
- **FIX — FFT detrending**: Reemplazada la sustracción lineal
  `prices - np.linspace(...)` con diferenciación logarítmica de primer orden
  (`np.diff(np.log(prices))`), que produce una serie más estacionaria y elimina
  tendencias cuadráticas y superiores.
- **[Agente: GitHub Copilot]**

### modules/neural_brain.py
- **FIX — Activation thresholds**: Incrementados umbrales para evitar overfitting:
  - `COSINE_ONLY_THRESHOLD`: 20 → 50
  - `MLP_ACTIVATION`: 20 → 100 (mínimo estadístico razonable para 40 features)
  - `ENSEMBLE_ACTIVATION`: 50 → 200
- **[Agente: GitHub Copilot]**

### config.py
- **FIX — Kelly min trades**: `KELLY_MIN_TRADES`: 30 → 100 para intervalo de confianza
  más estrecho en la estimación de win_rate.
- **NUEVO — `MAX_PORTFOLIO_RISK_PCT`**: 5.0% — umbral máximo de riesgo efectivo del
  portafolio considerando correlaciones entre activos.
- **[Agente: GitHub Copilot]**

### modules/portfolio_risk.py (NUEVO)
- Módulo de gestión de correlación entre activos.
- Matriz estática de correlaciones históricas entre los 12 símbolos.
- `get_effective_portfolio_risk()`: calcula riesgo efectivo del portafolio usando varianza
  ponderada por correlación y dirección de las posiciones.
- Bloquea nuevos trades cuando la exposición correlacionada supera `MAX_PORTFOLIO_RISK_PCT`.
- **[Agente: GitHub Copilot]**

### modules/sentiment_data.py (NUEVO)
- Integración de datos de sentimiento de mercado de fuentes gratuitas:
  - Crypto Fear & Greed Index (BTCUSDm) — api.alternative.me
  - CBOE VIX (US500m, NAS100m, GER40m, XAUUSDm, etc.) — cdn.cboe.com
- Cache con TTL de 15 minutos para respetar rate-limits.
- `get_sentiment_for_symbol()`: retorna datos de sentimiento relevantes + sesgo general.
- **[Agente: GitHub Copilot]**

### main.py
- Integrado `portfolio_risk.get_effective_portfolio_risk()` en `_execute_decision()`:
  bloquea órdenes si el riesgo efectivo del portafolio supera el umbral.
- Integrado `sentiment_data.get_sentiment_for_symbol()` en `build_context()`:
  Groq recibe datos de Fear & Greed y VIX como contexto adicional.
- Extendido `_update_web_status_snapshot()` con métricas de performance (win rate,
  profit factor, rolling WR, best/worst trade) y riesgo de portafolio en tiempo real.
- **[Agente: GitHub Copilot]**

### modules/web_dashboard.py
- Añadidas 2 secciones al dashboard:
  - **📊 Performance Tracking**: Win rate, profit factor, total trades, rolling WR,
    max drawdown, best/worst trade.
  - **🔗 Riesgo de Portafolio**: Posiciones activas, riesgo efectivo %,
    máximo permitido, pares correlacionados.
- **[Agente: GitHub Copilot]**

---

## [2026-03-26 19:10 UTC] - FASE 1/2: Trailing proporcional v6.9 + Web Dashboard asíncrono

### main.py
- Refactorizada `_manage_trailing_stop(...)` al esquema proporcional por `tp_progress`:
  - `<30%` → BE con buffer mínimo basado en `be_buffer_mult`
  - `>=30%` → bloquea `15%` de la ganancia actual
  - `>=50%` → bloquea `35%`
  - `>=70%` → bloquea `50%`
  - `>=85%` → bloquea `70%`
- El cálculo de SL ahora usa distancia real de precio (`profit_price`) y ratchet estricto:
  nunca retrocede matemáticamente y exige diferencia mínima de `0.5` puntos antes de modificar en MT5.
- Añadido diccionario `_profit_candle_count` por ticket: el trailing/BE solo activa tras `2` ciclos consecutivos en profit; si el trade vuelve a negativo, el contador se reinicia.
- Añadido `trade_mode_cache` por ticket para persistir parámetros adaptativos de trailing y asimetría BUY/SELL.
- Integrado snapshot thread-safe del estado del bot y arranque de dashboard web daemon antes del loop infinito.
- `ask_gemini(...)` ahora guarda el último análisis útil para exponerlo por API.
- **[Agente: GitHub Copilot]**

### modules/neural_brain.py
- Añadido `get_adaptive_trail_params(sym_cfg, direction)` que retorna solo:
  - `be_atr_mult`
  - `be_buffer_mult`
- SELL aplica mitigación institucional: `be_atr_mult × 0.9` y buffer más estrecho (`0.35×ATR` por defecto).
- Limpieza técnica: eliminado import innecesario de `time` y normalización de mensajes para convertir entidades HTML como `&amp;` a `&`.
- **[Agente: GitHub Copilot]**

### modules/web_dashboard.py
- Nuevo dashboard web asíncrono en Flask, servido desde thread daemon.
- Endpoints:
  - `/` → interfaz HTML/CSS/JS dark mode
  - `/api/status` → snapshot JSON del bot con balance, equity, posiciones activas y último análisis de Gemini
- Frontend con polling ligero cada `2500 ms` y badges visuales LONG/SHORT.
- **[Agente: GitHub Copilot]**

### requirements.txt
- Añadida dependencia `Flask>=3.0.0` para el dashboard web.
- **[Agente: GitHub Copilot]**

---

## [2026-03-26 21:50 UTC] - Optimización de cuota Gemini y ciclo principal

### main.py
- Añadido gate determinístico previo a Gemini con `_is_gemini_candidate_ready(...)`:
  Gemini solo se consulta cuando el candidato ya pasó filtro direccional, `R:R`, confluencia mínima, ciclo Hilbert y calidad básica de setup.
- Añadidas reglas específicas por `strategy_type` para endurecer el gate previo según familia de estrategia (ciclo, momentum, Kalman, Dragon).
- Añadido caché por símbolo/dirección/vela para no repetir la misma consulta varias veces sobre el mismo setup.
- Añadido cooldown global ante `429 RESOURCE_EXHAUSTED`: se parsea el `retryDelay` y se bloquean nuevas consultas hasta que venza, evitando que el ciclo se alargue varios minutos por reintentos inútiles.
- `ask_gemini(...)` reduce reintentos normales y deja de insistir cuando la cuota ya está agotada.
- Añadidas métricas internas de uso Gemini: llamadas reales, éxitos, cache hits, filtros por gate, cooldown skips, cuotas agotadas y fallback holds.
- `build_context(...)` ahora adjunta el plan de trade estimado (entry/SL/TP/RR) solo cuando el setup ya pasó el gate previo.
- **[Agente: GitHub Copilot]**

### modules/web_dashboard.py
- El dashboard web ahora muestra métricas de uso de Gemini y ventana de cooldown activa, para verificar visualmente el ahorro de cuota.
- **[Agente: GitHub Copilot]**

---

## [2026-03-26 22:20 UTC] - Correcciones sniper estructurales (ronda 1)

### modules/indicators.py
- `compute_all(...)` ahora puede recibir `df_entry` y usa el timeframe de entrada para calcular microestructura sniper (`POC`, `Session VWAP`, `FVG`, `micro_score`).
- Añadidos `entry_price` y `entry_candle_time` al contexto para sincronizar trailing y validaciones con la vela real de ejecución.
- **[Agente: GitHub Copilot]**

### main.py
- `_process_symbol(...)` pasa `df_entry` a `compute_all(...)`, evitando que el Pilar 3 quede anclado a H1.
- La regla anti-SL-hunting ya no cuenta vueltas del loop: ahora `_profit_candle_count` avanza solo cuando cambia `entry_candle_time` y el trade sigue en profit.
- Añadido `_profit_candle_last_seen` para contar velas únicas en profit por ticket.
- Integrado contexto de noticias compartido globalmente: se hace un fetch agregado y luego se deriva por símbolo, evitando llamadas duplicadas por activo.
- **[Agente: GitHub Copilot]**

### modules/news_engine.py
- Añadidos `build_shared_news_context(...)` y `derive_symbol_news_context(...)` para separar la descarga global de noticias de la interpretación específica por símbolo.
- `build_news_context(...)` queda como wrapper compatible.
- **[Agente: GitHub Copilot]**

---

## [2026-03-26 22:40 UTC] - Correcciones sniper estructurales (ronda 2)

### modules/neural_brain.py
- `get_memory_stats(...)` ahora acepta filtro opcional por símbolo para métricas locales de rendimiento/aprendizaje.
- **[Agente: GitHub Copilot]**

### main.py
- Kelly position sizing ahora usa métricas por símbolo en vez de estadísticas globales del bot.
- La ruta `LATERAL` deja de preseleccionar una sola dirección antes de Gemini.
- BUY y SELL se mantienen como candidatos completos hasta el final, con contexto comparativo vía `build_lateral_context(...)`.
- `_execute_decision(...)` ya puede resolver el `mem_check` y `features` correctos según la dirección final elegida por Gemini en mercado lateral.
- **[Agente: GitHub Copilot]**

## [2026-03-26 17:58 UTC] - FASE 3: Scorecard jerárquico por activo (pre-LLM gate)

### config.py
- Añadidos parámetros configurables de FASE 3 sin hardcode:
  - `SCORECARD_LOOKBACK_TRADES`
  - `SCORECARD_MIN_SAMPLE`
  - `SCORECARD_MIN_WIN_RATE`
  - `SCORECARD_MIN_CONF_BONUS`
- Permiten ajustar lookback, muestra mínima, win-rate mínimo y endurecimiento de confianza desde configuración.
- **[Agente: GitHub Copilot]**

### modules/neural_brain.py
- **Scorecard jerárquico implementado** con `ScorecardCheck` + `evaluate_scorecard()`:
  1) `setup_id + session + regime`  
  2) `setup_id + session`  
  3) `setup_id`  
  4) `symbol` (fallback)
- El cálculo usa solo trades cerrados `WIN/LOSS` (excluye BE) y aplica bloqueo solo con muestra suficiente.
- Añadidos helpers `derive_setup_id()` y `derive_session_from_ind()` para trazabilidad consistente por activo.
- **Persistencia FASE 2 reforzada**: la tabla `trades` y sus migraciones ahora incluyen
  `setup_id`, `setup_score`, `session`, `risk_amount`, `sl`, `tp`.
- `save_trade()` actualizado para guardar el contexto enriquecido del setup en cada entrada.
- **[Agente: GitHub Copilot]**

### main.py
- Integrado **filtro pre-LLM** en ruta direccional: si el scorecard del setup es pobre, bloquea antes de consultar Gemini.
- Integrado gate equivalente en ruta lateral (BUY/SELL evaluados) y en post-Gemini como red de seguridad final.
- `build_context()` ahora adjunta el bloque `SCORECARD JERÁRQUICO` para pasar esta métrica como contexto al modelo.
- Endurecimiento dinámico de entrada: cuando el setup no está bloqueado pero su WR es marginal, sube `min_confidence` vía `SCORECARD_MIN_CONF_BONUS`.
- `save_trade()` ahora registra en memoria: `setup_id`, `setup_score`, `session`, `regime`, `risk_amount`, `sl`, `tp`.
- **[Agente: GitHub Copilot]**

---

## [2026-03-26 18:01 UTC] - FASE 4: Policy Engine (ranking WR/PF/Reward/Sample)

### config.py
- Añadidos parámetros configurables de Policy Engine:
  - `POLICY_LOOKBACK_TRADES`
  - `POLICY_MIN_SAMPLE`
  - `POLICY_WEIGHT_WR`, `POLICY_WEIGHT_PF`, `POLICY_WEIGHT_REWARD`, `POLICY_WEIGHT_SAMPLE`
  - `POLICY_MIN_SCORE`
  - `POLICY_MIN_CONF_BONUS`
- **[Agente: GitHub Copilot]**

### modules/neural_brain.py
- Añadidos `PolicyCheck` + `evaluate_policy(...)` para calcular `policy_score` por candidato:
  - Métricas: Win Rate, Profit Factor, Avg Reward y tamaño de muestra
  - Normalización y combinación ponderada en score `[0,1]`
  - Bloqueo duro configurable cuando hay muestra suficiente y score bajo
- **[Agente: GitHub Copilot]**

### main.py
- Integrado Policy Engine en `_process_symbol(...)`:
  - Ruta lateral: evalúa BUY y SELL en paralelo, descarta candidatos bloqueados por memoria/scorecard/policy
  - Ranking final por `policy_score` (desempate con `memory confidence_adj`) y selección del mejor candidato
  - Ruta direccional: añade veto por policy antes de Gemini
- Integrado safety-net post-Gemini en `_execute_decision(...)` con `evaluate_policy(...)`.
- `build_context(...)` ahora adjunta bloque `POLICY ENGINE` para pasar score y métricas a la IA.
- Endurecimiento dinámico de `min_confidence` cuando policy score es marginal.
- **[Agente: GitHub Copilot]**

---

## [2026-03-26 18:10 UTC] - FASE 5: Equity Guard (protección de capital)

### config.py
- Añadido parámetro configurable:
  - `EQUITY_GUARD_MIN_PCT` (default `70.0`)
- Define el piso de equity (porcentaje del balance actual) bajo el cual se bloquean nuevas entradas.
- **[Agente: GitHub Copilot]**

### main.py
- Integrado gate pre-señal en `_process_symbol(...)`:
  - Si `equity < balance * (EQUITY_GUARD_MIN_PCT/100)`, el símbolo queda bloqueado para nuevas entradas.
  - Se registra warning en log, estado de símbolo y `last_action`.
  - **FASE 5.1**: notificación Telegram de Equity Guard con cooldown por símbolo para evitar spam (`_notify_equity_guard_once`).
  - No afecta gestión de posiciones ya abiertas (trailing/SL/TP siguen operando).
- **[Agente: GitHub Copilot]**

### modules/telegram_notifier.py
- `notify_bot_started()` actualizado para listar explícitamente:
  - `✅ FASE 4 — Policy Engine`
  - `✅ FASE 5 — Equity Guard`
- **[Agente: GitHub Copilot]**

---

## [2026-03-26 18:48 UTC] - FASE 6: Daily Loss Guard (protección intradía global)

### main.py
- Añadida notificación Telegram con cooldown para el guard global de pérdida diaria:
  - Nueva función `_notify_daily_loss_guard_once(...)`.
  - Mensaje incluye P&L diario actual, límite monetario diario y timestamp UTC.
- Integrado en el loop principal:
  - Cuando `is_daily_loss_ok(...)` falla, además de pausar nuevas entradas, ahora también
    notifica por Telegram (anti-spam por `NOTIF_COOLDOWN_SEC`).
  - Cuando el P&L vuelve a estar dentro del umbral, se resetea el estado de notificación
    para permitir futuras alertas.
- **[Agente: GitHub Copilot]**

### config.py
- Añadido bloque documental **FASE 6 — DAILY LOSS GUARD** para trazar el alcance:
  - Protección global intradía basada en `MAX_DAILY_LOSS`.
  - No afecta la gestión de posiciones abiertas.
- **[Agente: GitHub Copilot]**

### modules/telegram_notifier.py
- `notify_bot_started()` actualizado para listar explícitamente:
  - `✅ FASE 6 — Daily Loss Guard (pausa global por pérdida diaria)`
- **[Agente: GitHub Copilot]**

---

## [2026-03-26 17:05 UTC] - FASE 3: Smart Entry Gate + Dashboard v2 + Notifications v2

### main.py
- **Eliminación de ~140 líneas de código muerto** (`_process_symbol`):
  El cuerpo de `_process_symbol` terminaba con `return` en la línea 1221 pero el resto de
  la función (líneas 1223–1362) era un bloque duplicado idéntico nunca alcanzado.
  Eliminar este bloque muerto reduce el archivo en ~140 líneas sin cambiar ningún comportamiento.
- **`_apply_confluence_gate_pre()` (nueva función helper)**:
  Gate pre-Gemini que bloquea la consulta a la IA cuando la Confluencia 3 Pilares
  contradice fuertemente la dirección esperada.
  - Umbral: `conf_total < -(CONFLUENCE_MIN_SCORE × 2)` para BUY, viceversa para SELL
  - Con defaults: `|conf_total| > 0.6` sobre escala `-3 a +3` (sólo bloquea cuando
    el mercado es claramente opuesto — no en señales neutras)
  - Ahorra una llamada a Gemini API por ciclo cuando el gate actúa
  - Se aplica en la ruta directional (h1_trend ALCISTA/BAJISTA) antes de `build_context`
  - La ruta LATERAL no usa este gate (dirección desconocida) — usa el post-Gemini gate
- **Confluence Hard Gate post-Gemini** en `_execute_decision`:
  Safety net que veta la orden DESPUÉS de que Gemini responde.
  Captura el caso LATERAL donde la dirección no se conoce antes de consultar la IA.
  Aplica la REGLA 15 del system prompt en código Python, no solo en el prompt.
- **`kelly_active` flag** en `_execute_decision`: Se computa si el Kelly fraccionado
  está activo (sym_trades ≥ KELLY_MIN_TRADES y win_rate > 0) y se pasa a la notificación.
- **[Agente: GitHub Copilot]**

### modules/dashboard.py
- **Mapa de iconos de tendencia — 7 niveles** (FASE 3):
  Antes solo mapeaba 5 niveles (faltaban LATERAL_ALCISTA ↗ y LATERAL_BAJISTA ↘).
  Ahora todos los 7 niveles tienen icono propio:
  `ALCISTA_FUERTE=⬆⬆ / ALCISTA=⬆  / LATERAL_ALCISTA=↗  / LATERAL=➡  /
   LATERAL_BAJISTA=↘  / BAJISTA=⬇  / BAJISTA_FUERTE=⬇⬇`
- **Fila 4 de microestructura + confluencia** por símbolo (FASE 3):
  Cada símbolo ahora muestra 4 filas en vez de 3:
  ```
  l4: 🟢/🔴/⚪ M={micro_score} ▲/▼POC ▲/▼SVWAP │ Conf: P1={p1} P2={p2} P3={p3} Tot={total} ✅SNP/    
  ```
  - Micro Score con icono de color (🟢>+0.5, 🔴<-0.5, ⚪ neutro)
  - Posición relativa al POC y Session VWAP
  - Scores individuales P1/P2/P3 + total ponderado
  - Flag `✅SNP` cuando los 3 pilares están alineados (sniper_aligned)
  - Los datos provienen de `ind["microstructure"]` e `ind["confluence"]` (FASE 1)
- **[Agente: GitHub Copilot]**

### modules/telegram_notifier.py
- **`notify_trade_opened()` v2** — Enriquecida con dos nuevas secciones:
  - `🔬 Microestructura (P3)`: Micro Score + bias, sesión activa, POC con posición,
    Session VWAP con desviación %, FVG Bull/Bear activos
  - `⚡ Confluencia 3 Pilares`: scores individuales P1/P2/P3 con iconos de color,
    TOTAL con bias, flag `✅ SNIPER ALIGNED` / `⚠️ Pilares en desacuerdo`
  - Nuevo parámetro `kelly_active: bool` → muestra `<b>Kelly✓</b>` junto al volumen
    cuando el sizing dinámico Kelly está activo (indicando que el lot size fue ajustado)
- **`notify_bot_started()` v2** — Reemplaza lista de fixes v6.4 con descripción
  de arquitectura por fases (FASE 0/1/2/3 activas), listando capacidades clave de
  cada fase. Refleja el estado real del bot al arrancar.
- **[Agente: GitHub Copilot]**

---

## [2026-03-26 16:48 UTC] - FASE 2: Neural Brain v3 (Pilar 3 Integration) + Kelly Position Sizing

### modules/neural_brain.py
- **`INPUT_DIM` expandido de 32 → 40** (+8 features del Pilar 3 de Microestructura + Confluencia).
  Los pesos de modelos guardados con dim=32 son detectados por shape mismatch en `from_dict()` y
  reinicializan automáticamente con pesos He — sin pérdida de datos históricos en SQLite.
- **Nuevo GRUPO 5 — Microestructura + Confluencia (8 features)** en `TradeFeatures`:
  | Feature | Descripción | Normalización |
  |---|---|---|
  | `micro_score_norm` | Micro Score del Pilar 3 | `micro_score / 3` → [-1, +1] |
  | `above_poc` | Posición respecto al POC de Volume Profile | +1 / -1 |
  | `in_value_area` | Precio dentro del Value Area (VAL–VAH) | 1 / 0 |
  | `above_session_vwap` | Posición respecto al Session VWAP | +1 / -1 |
  | `session_vwap_dev_norm` | Desviación % del Session VWAP | clipeado en [-2%, +2%] → [-1, +1] |
  | `fvg_bull_active` | FVG Bullish activo cercano | 1 / 0 |
  | `fvg_bear_active` | FVG Bearish activo cercano | 1 / 0 |
  | `confluence_total_norm` | Score de Confluencia de 3 Pilares | `conf_total / 3` → [-1, +1] |
- **`to_vector()`**: actualizado para incluir los 8 features nuevos → vector de 40 dimensiones.
- **`_trend_to_num()` corregido**: antes solo mapeaba 5 niveles (ALCISTA_FUERTE/ALCISTA/LATERAL/BAJISTA/BAJISTA_FUERTE). Ahora mapea correctamente los 7 niveles introducidos en v6.2:
  ```
  ALCISTA_FUERTE=+1.00 / ALCISTA=+0.67 / LATERAL_ALCISTA=+0.33 / LATERAL=0.00
  LATERAL_BAJISTA=-0.33 / BAJISTA=-0.67 / BAJISTA_FUERTE=-1.00
  ```
  Los valores anteriores (±0.5) causaban que LATERAL_ALCISTA y LATERAL_BAJISTA quedaran en 0.0 (neutral).
- **`build_features()`**: nuevo bloque Grupo 5 que lee `ind["microstructure"]` y `ind["confluence"]` para poblar las 8 features nuevas con valores correctamente normalizados.
- **Vector reconstruction en `_online_train()` y `check_memory()`**: actualizados para reconstruir el vector de 40 elementos desde JSON almacenado. Records viejos (sin las 8 keys nuevas) devuelven defaults seguros (0.0) — retrocompatibilidad total sin migración de DB.
- **[Agente: GitHub Copilot]**

### modules/risk_manager.py
- **Añadida `get_lot_size_kelly()`** — nueva función de sizing con Criterio de Kelly Fraccionado:
  - Fórmula: `f* = (b×p - q) / b` donde p=win_rate, q=1-p, b=RR ratio
  - Se aplica Kelly Fraccionado: `f_actual = KELLY_FRACTION × f*`
  - **Salvaguardas**: Kelly negativo (edge insuficiente) → vuelve a sizing estándar; techo absoluto de riesgo = `RISK_PER_TRADE × 3`; lote mínimo 0.01
  - `n_trades < KELLY_MIN_TRADES` → fallback automático a `get_lot_size()` estándar
- **Añadido `import numpy as np`** (requerido por `np.clip` en Kelly).
- **[Agente: GitHub Copilot]**

### config.py
- Añadido bloque **`FASE 2 — NEURAL BRAIN v3 + KELLY POSITION SIZING`** con:
  - `KELLY_FRACTION = 0.25` (quarter-Kelly = conservador, resiste errores de estimación en p y b)
  - `KELLY_MIN_TRADES = 30` (mínimo de trades históricos por símbolo para activar Kelly)
  - Comentarios detallados explicando el impacto de cada parámetro
- **[Agente: GitHub Copilot]**

### main.py
- **`_execute_decision()`**: importado `get_lot_size_kelly` y reemplazado el `get_lot_size()` fijo por la nueva función de Kelly. Usa `get_memory_stats()` (ya disponible) para extraer win_rate y total de trades, y pasa el RR actual como `avg_rr`.
- **Importación**: `get_lot_size_kelly` añadido al import de `modules.risk_manager`.
- **[Agente: GitHub Copilot]**

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
