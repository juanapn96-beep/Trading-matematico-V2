import logging
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template_string

try:
    from config import (
        DASHBOARD_EQUITY_INITIAL,
        DASHBOARD_ROLLING_WR_WINDOW,
        DASHBOARD_RECENT_TRADES_LIMIT,
        DASHBOARD_CHART_REFRESH_SEC,
        BACKTEST_INITIAL_BALANCE,
    )
except ImportError:
    DASHBOARD_EQUITY_INITIAL = 10000.0
    DASHBOARD_ROLLING_WR_WINDOW = 20
    DASHBOARD_RECENT_TRADES_LIMIT = 50
    DASHBOARD_CHART_REFRESH_SEC = 30
    BACKTEST_INITIAL_BALANCE = 10000.0

try:
    from modules.neural_brain import get_equity_curve_data, get_distribution_data, get_recent_trades, get_advanced_metrics
except ImportError:
    def get_equity_curve_data(*a, **kw): return []
    def get_distribution_data(): return {"by_symbol": [], "by_hour": []}
    def get_recent_trades(*a, **kw): return []
    def get_advanced_metrics(): return {}


HTML_TEMPLATE = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ZAR Web Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg: #08131f;
      --panel: rgba(14, 27, 39, 0.92);
      --panel-2: rgba(10, 20, 30, 0.88);
      --line: rgba(143, 178, 212, 0.18);
      --text: #e8f1f8;
      --muted: #8ea6bb;
      --accent: #43c6ac;
      --accent-2: #f8b84e;
      --long: #27c46b;
      --short: #e14f5f;
      --danger: #ff7a59;
      --shadow: 0 18px 60px rgba(0, 0, 0, 0.32);
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: "Segoe UI", "Trebuchet MS", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(67, 198, 172, 0.18), transparent 26%),
        radial-gradient(circle at top right, rgba(248, 184, 78, 0.16), transparent 22%),
        linear-gradient(160deg, #07111a 0%, #0b1724 42%, #050b12 100%);
    }

    .shell {
      width: min(1180px, calc(100% - 32px));
      margin: 24px auto;
      display: grid;
      gap: 18px;
    }

    .hero, .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 22px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(14px);
    }

    .hero {
      padding: 24px;
      display: grid;
      gap: 18px;
    }

    .hero-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      flex-wrap: wrap;
    }

    .title {
      font-size: clamp(28px, 4vw, 44px);
      letter-spacing: 0.04em;
      margin: 0;
      font-weight: 700;
    }

    .subtitle {
      margin: 6px 0 0;
      color: var(--muted);
    }

    .pulse {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 14px;
      border-radius: 999px;
      background: rgba(67, 198, 172, 0.12);
      color: var(--accent);
      font-weight: 600;
    }

    .pulse::before {
      content: "";
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: currentColor;
      box-shadow: 0 0 0 0 rgba(67, 198, 172, 0.7);
      animation: pulse 1.8s infinite;
    }

    .stats {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 14px;
    }

    .stat {
      padding: 16px;
      border-radius: 18px;
      background: var(--panel-2);
      border: 1px solid rgba(143, 178, 212, 0.12);
    }

    .stat-label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .stat-value {
      margin-top: 10px;
      font-size: 28px;
      font-weight: 700;
    }

    .grid {
      display: grid;
      grid-template-columns: 1.3fr 0.9fr;
      gap: 18px;
    }

    .panel {
      padding: 20px;
    }

    .panel h2 {
      margin: 0 0 16px;
      font-size: 18px;
      letter-spacing: 0.04em;
    }

    table {
      width: 100%;
      border-collapse: collapse;
    }

    th, td {
      padding: 12px 10px;
      border-bottom: 1px solid rgba(143, 178, 212, 0.1);
      text-align: left;
      font-size: 14px;
    }

    th {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 80px;
      padding: 7px 12px;
      border-radius: 999px;
      font-weight: 700;
      letter-spacing: 0.04em;
    }

    .badge.long {
      background: rgba(39, 196, 107, 0.16);
      color: #7ef0ab;
      border: 1px solid rgba(39, 196, 107, 0.34);
    }

    .badge.short {
      background: rgba(225, 79, 95, 0.16);
      color: #ff98a3;
      border: 1px solid rgba(225, 79, 95, 0.34);
    }

    .mono {
      font-variant-numeric: tabular-nums;
      font-feature-settings: "tnum";
    }

    .profit.pos { color: #7ef0ab; }
    .profit.neg { color: #ff98a3; }

    .status-list {
      display: grid;
      gap: 10px;
      max-height: 320px;
      overflow: auto;
    }

    .status-item {
      padding: 12px 14px;
      border-radius: 16px;
      background: var(--panel-2);
      border: 1px solid rgba(143, 178, 212, 0.1);
    }

    .status-item strong {
      display: block;
      margin-bottom: 6px;
    }

    .analysis {
      display: grid;
      gap: 12px;
      padding: 16px;
      border-radius: 18px;
      background: linear-gradient(145deg, rgba(67, 198, 172, 0.08), rgba(248, 184, 78, 0.08));
      border: 1px solid rgba(143, 178, 212, 0.14);
    }

    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .chip {
      padding: 7px 10px;
      border-radius: 999px;
      background: rgba(143, 178, 212, 0.12);
      color: var(--text);
      font-size: 12px;
    }

    .empty {
      color: var(--muted);
      padding: 20px 0;
    }

    @keyframes pulse {
      0% { box-shadow: 0 0 0 0 rgba(67, 198, 172, 0.7); }
      70% { box-shadow: 0 0 0 12px rgba(67, 198, 172, 0); }
      100% { box-shadow: 0 0 0 0 rgba(67, 198, 172, 0); }
    }

    @media (max-width: 920px) {
      .grid { grid-template-columns: 1fr; }
      .hero { padding: 18px; }
      .panel { padding: 16px; }
      th:nth-child(4), td:nth-child(4), th:nth-child(5), td:nth-child(5) {
        display: none;
      }
    }

    /* ── FASE 10: Advanced Charts ── */
    .section-title {
      display: flex;
      align-items: center;
      gap: 10px;
      cursor: pointer;
      user-select: none;
      padding: 4px 0 12px;
      border-bottom: 1px solid var(--line);
      margin-bottom: 16px;
    }
    .section-title::after {
      content: "▾";
      margin-left: auto;
      font-size: 18px;
      color: var(--muted);
      transition: transform 0.2s;
    }
    .section-title.collapsed::after {
      transform: rotate(-90deg);
    }
    .collapsible-body { overflow: hidden; }
    .collapsible-body.collapsed { display: none; }

    .chart-container {
      position: relative;
      width: 100%;
      min-height: 220px;
    }
    .chart-empty {
      display: flex;
      align-items: center;
      justify-content: center;
      height: 180px;
      color: var(--muted);
      font-size: 14px;
      border: 1px dashed var(--line);
      border-radius: 12px;
    }

    .chart-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 18px;
    }
    @media (max-width: 920px) {
      .chart-grid { grid-template-columns: 1fr; }
    }

    /* Recent Trades Table */
    .trades-table-wrap {
      overflow-x: auto;
      max-height: 420px;
      overflow-y: auto;
    }
    .result-win  { color: #00d4aa; font-weight: 700; }
    .result-loss { color: #ff6b6b; font-weight: 700; }
    .result-be   { color: #888; font-weight: 600; }
    .row-win  { background: rgba(0, 212, 170, 0.04); }
    .row-loss { background: rgba(255, 107, 107, 0.04); }
    .row-be   { background: transparent; }
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="hero-top">
        <div>
          <h1 class="title">ZAR Control Deck</h1>
          <p class="subtitle">Monitoreo asíncrono del bot con refresco incremental cada 2.5 segundos.</p>
        </div>
        <div class="pulse">Live polling</div>
      </div>
      <div class="stats">
        <article class="stat">
          <div class="stat-label">Balance</div>
          <div id="balance" class="stat-value mono">$0.00</div>
        </article>
        <article class="stat">
          <div class="stat-label">Equity</div>
          <div id="equity" class="stat-value mono">$0.00</div>
        </article>
        <article class="stat">
          <div class="stat-label">P&L Diario</div>
          <div id="dailyPnl" class="stat-value mono">$0.00</div>
        </article>
        <article class="stat">
          <div class="stat-label">Ciclo</div>
          <div id="cycle" class="stat-value mono">0</div>
        </article>
      </div>
    </section>

    <section class="grid">
      <section class="panel">
        <h2>Posiciones Activas</h2>
        <table>
          <thead>
            <tr>
              <th>Ticket</th>
              <th>Símbolo</th>
              <th>Dirección</th>
              <th>Apertura</th>
              <th>Actual</th>
              <th>P&L</th>
            </tr>
          </thead>
          <tbody id="positionsTable"></tbody>
        </table>
      </section>

      <section class="panel">
        <h2>Último análisis de decisión</h2>
        <div id="analysis" class="analysis"></div>
      </section>
    </section>

    <section class="grid">
      <section class="panel">
        <h2>Métricas del motor de decisión</h2>
        <div id="decisionMetrics" class="status-list"></div>
      </section>

      <section class="panel">
        <h2>Última acción</h2>
        <div id="lastAction" class="analysis"></div>
      </section>
    </section>

    <section class="grid">
      <section class="panel">
        <h2>Estado por símbolo</h2>
        <div id="statusList" class="status-list"></div>
      </section>

      <section class="panel">
        <h2>Memoria y Noticias</h2>
        <div id="memoryNews" class="status-list"></div>
      </section>
    </section>

    <section class="grid">
      <section class="panel">
        <h2>📊 Seguimiento de Rendimiento</h2>
        <div id="performanceMetrics" class="status-list"></div>
      </section>

      <section class="panel">
        <h2>🔗 Riesgo de Portafolio</h2>
        <div id="portfolioRisk" class="status-list"></div>
      </section>
    </section>

    <!-- ══ FASE 10: Advanced Performance Dashboard ══ -->

    <section class="panel">
      <div class="section-title" onclick="toggleSection(this)">📈 Equity Curve &amp; Drawdown</div>
      <div class="collapsible-body">
        <div class="chart-container"><canvas id="equityCurveChart"></canvas></div>
      </div>
    </section>

    <section class="panel">
      <div class="section-title" onclick="toggleSection(this)">📉 Rolling Win Rate (ventana {{ rolling_wr_window }} trades)</div>
      <div class="collapsible-body">
        <div class="chart-container"><canvas id="rollingWrChart"></canvas></div>
      </div>
    </section>

    <section class="panel">
      <div class="section-title" onclick="toggleSection(this)">🗂️ Distribución por Símbolo y Hora</div>
      <div class="collapsible-body">
        <div class="chart-grid">
          <div>
            <div style="font-size:13px;color:var(--muted);margin-bottom:8px;">Win Rate por símbolo</div>
            <div class="chart-container"><canvas id="symbolDistChart"></canvas></div>
          </div>
          <div>
            <div style="font-size:13px;color:var(--muted);margin-bottom:8px;">Trades por hora UTC</div>
            <div class="chart-container"><canvas id="hourDistChart"></canvas></div>
          </div>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="section-title" onclick="toggleSection(this)">🔬 Métricas Avanzadas de Scalping</div>
      <div class="collapsible-body">
        <div class="chart-grid">
          <div>
            <div style="font-size:13px;color:var(--muted);margin-bottom:8px;">Win Rate por Sesión</div>
            <div class="chart-container"><canvas id="sessionWrChart"></canvas></div>
          </div>
          <div>
            <div style="font-size:13px;color:var(--muted);margin-bottom:8px;">Slippage por Símbolo (pips)</div>
            <div class="chart-container"><canvas id="slippageChart"></canvas></div>
          </div>
        </div>
        <div class="stats" style="margin-top:18px;">
          <article class="stat">
            <div class="stat-label">Duración Media (todos)</div>
            <div id="avgDuration" class="stat-value mono">—</div>
          </article>
          <article class="stat">
            <div class="stat-label">Duración Ganadores</div>
            <div id="avgDurationWin" class="stat-value mono">—</div>
          </article>
          <article class="stat">
            <div class="stat-label">Duración Perdedores</div>
            <div id="avgDurationLoss" class="stat-value mono">—</div>
          </article>
          <article class="stat">
            <div class="stat-label">Score Ø Ganadores</div>
            <div id="scoreWinners" class="stat-value mono">—</div>
          </article>
          <article class="stat">
            <div class="stat-label">Score Ø Perdedores</div>
            <div id="scoreLosers" class="stat-value mono">—</div>
          </article>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="section-title" onclick="toggleSection(this)">📋 Trades Recientes</div>
      <div class="collapsible-body">
        <div class="trades-table-wrap">
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Símbolo</th>
                <th>Dir</th>
                <th>Entrada</th>
                <th>Salida</th>
                <th>Profit</th>
                <th>Pips</th>
                <th>Duración</th>
                <th>Resultado</th>
              </tr>
            </thead>
            <tbody id="recentTradesTable"></tbody>
          </table>
        </div>
      </div>
    </section>
  </main>

  <script>
    const money = new Intl.NumberFormat('es-ES', { style: 'currency', currency: 'USD' });
    const number = new Intl.NumberFormat('es-ES', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

    function setText(id, value) {
      document.getElementById(id).textContent = value;
    }

    // ── Collapsible sections ──
    function toggleSection(titleEl) {
      titleEl.classList.toggle('collapsed');
      titleEl.nextElementSibling.classList.toggle('collapsed');
    }

    function renderPositions(positions) {
      const tbody = document.getElementById('positionsTable');
      if (!positions.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty">Sin posiciones abiertas.</td></tr>';
        return;
      }

      tbody.innerHTML = positions.map((pos) => {
        const profitClass = pos.profit >= 0 ? 'profit pos' : 'profit neg';
        const badgeClass = pos.direction === 'LONG' ? 'badge long' : 'badge short';
        return `
          <tr>
            <td class="mono">#${pos.ticket}</td>
            <td>${pos.symbol}</td>
            <td><span class="${badgeClass}">${pos.direction}</span></td>
            <td class="mono">${number.format(pos.price_open)}</td>
            <td class="mono">${number.format(pos.price_current)}</td>
            <td class="mono ${profitClass}">${money.format(pos.profit)}</td>
          </tr>
        `;
      }).join('');
    }

    function renderAnalysis(analysis) {
      const container = document.getElementById('analysis');
      if (!analysis || !analysis.symbol) {
        container.innerHTML = '<div class="empty">Aún no hay análisis de decisión disponible.</div>';
        return;
      }

      const signals = (analysis.key_signals || []).map((item) => `<span class="chip">${item}</span>`).join('');
      container.innerHTML = `
        <div><strong>${analysis.symbol}</strong> · ${analysis.timestamp || ''}</div>
        <div><strong>Decisión:</strong> ${analysis.decision || 'HOLD'} · <strong>Confianza:</strong> ${analysis.confidence || 0}/10</div>
        <div>${analysis.reason || 'Sin razón disponible.'}</div>
        <div><strong>Riesgo principal:</strong> ${analysis.main_risk || 'N/D'}</div>
        <div class="chips">${signals || '<span class="chip">Sin señales clave</span>'}</div>
      `;
    }

    function renderStatus(statusMap, detailMap) {
      const container = document.getElementById('statusList');
      const entries = Object.entries(statusMap || {});
      if (!entries.length) {
        container.innerHTML = '<div class="empty">Sin estados de símbolo disponibles.</div>';
        return;
      }

      container.innerHTML = entries.map(([symbol, status]) => {
        const d = (detailMap || {})[symbol] || {};
        const regimeRow = d.enhanced_regime
          ? `<div><span style="color:var(--muted)">Régimen:</span> ${d.enhanced_regime} <span style="color:var(--muted)">(${d.regime_confidence || '—'})</span></div>`
          : '';
        const zscoreRow = d.z_score != null
          ? `<div><span style="color:var(--muted)">Z-Score:</span> ${parseFloat(d.z_score).toFixed(2)}</div>`
          : '';
        return `
          <div class="status-item">
            <strong>${symbol}</strong>
            <div>${status}</div>
            ${regimeRow}
            ${zscoreRow}
          </div>
        `;
      }).join('');
    }

    function renderDecisionMetrics(metrics) {
      const container = document.getElementById('decisionMetrics');
      if (!metrics || Object.keys(metrics).length === 0) {
        container.innerHTML = '<div class="empty">Sin métricas del motor.</div>';
        return;
      }

      const labels = [
        ['API calls hoy', metrics.api_calls_current_day || 0],
        ['API calls hora', metrics.api_calls_current_hour || 0],
        ['Éxitos', metrics.api_success_total || 0],
        ['Cache hits', metrics.cache_hits_total || 0],
        ['Filtrados por gate', metrics.skipped_by_gate_total || 0],
        ['Filtrados por presupuesto', metrics.skipped_by_budget_total || 0],
        ['Saltados por cooldown', metrics.cooldown_skips_total || 0],
        ['Quota hits', metrics.quota_hits_total || 0],
        ['Fallback HOLD', metrics.fallback_holds_total || 0],
      ];

      container.innerHTML = labels.map(([label, value]) => `
        <div class="status-item">
          <strong>${label}</strong>
          <div class="mono">${value}</div>
        </div>
      `).join('');
    }

    function renderLastAction(payload) {
      const container = document.getElementById('lastAction');
      container.innerHTML = `
        <div><strong>Acción:</strong> ${payload.last_action || 'Sin actividad registrada.'}</div>
        <div><strong>Actualizado:</strong> ${payload.updated_at || 'N/D'}</div>
      `;
    }

    function renderMemoryNews(payload) {
      const container = document.getElementById('memoryNews');
      const memory = payload.memory || {};
      const news = payload.news || {};

      const memoryBlock = `
        <div class="status-item">
          <strong>Memoria Neural</strong>
          <div>Total trades: <span class="mono">${memory.total || 0}</span></div>
          <div>Win rate: <span class="mono">${memory.win_rate || 0}%</span></div>
          <div>P&L acumulado: <span class="mono">${money.format(memory.profit || 0)}</span></div>
        </div>
      `;

      const newsBlock = `
        <div class="status-item">
          <strong>Contexto de Noticias</strong>
          <div>Sentimiento promedio: <span class="mono">${number.format(news.avg_sentiment || 0)}</span></div>
          <div>High impact: <span class="mono">${news.high_impact_count || 0}</span></div>
          <div>Pausa activa: <span class="mono">${news.should_pause ? 'SI' : 'NO'}</span></div>
          <div>Shared fetch: <span class="mono">${payload.shared_news_fetched_at || 'N/D'}</span></div>
        </div>
      `;

      container.innerHTML = memoryBlock + newsBlock;
    }

    function renderPerformance(perf) {
      const container = document.getElementById('performanceMetrics');
      if (!perf || Object.keys(perf).length === 0) {
        container.innerHTML = '<div class="empty">Sin datos de performance aún.</div>';
        return;
      }

      const labels = [
        ['Win Rate (real)', (perf.win_rate || 0).toFixed(1) + '%'],
        ['Profit Factor', (perf.profit_factor || 0).toFixed(2)],
        ['Total trades', perf.total_trades || 0],
        ['Wins / Losses', `${perf.wins || 0} / ${perf.losses || 0}`],
        ['Breakeven', perf.breakeven || 0],
        ['Rolling WR (20)', (perf.rolling_wr_20 || 0).toFixed(1) + '%'],
        ['Max Drawdown', (perf.max_drawdown_pct || 0).toFixed(2) + '%'],
        ['Mejor trade', '$' + (perf.best_trade || 0).toFixed(2)],
        ['Peor trade', '$' + (perf.worst_trade || 0).toFixed(2)],
      ];

      container.innerHTML = labels.map(([label, value]) => `
        <div class="status-item">
          <strong>${label}</strong>
          <div class="mono">${value}</div>
        </div>
      `).join('');
    }

    function renderPortfolioRisk(risk) {
      const container = document.getElementById('portfolioRisk');
      if (!risk || Object.keys(risk).length === 0) {
        container.innerHTML = '<div class="empty">Sin datos de portafolio.</div>';
        return;
      }

      const items = [
        ['Posiciones activas', risk.open_positions || 0],
        ['Riesgo efectivo', (risk.effective_risk_pct || 0).toFixed(1) + '%'],
        ['Máximo permitido', (risk.max_risk_pct || 5).toFixed(1) + '%'],
        ['Activos correlacionados', risk.correlated_pairs || 0],
      ];

      if (risk.positions_detail && risk.positions_detail.length) {
        risk.positions_detail.forEach(pos => {
          items.push([pos.symbol, pos.direction]);
        });
      }

      container.innerHTML = items.map(([label, value]) => `
        <div class="status-item">
          <strong>${label}</strong>
          <div class="mono">${value}</div>
        </div>
      `).join('');
    }

    async function refresh() {
      try {
        const response = await fetch('/api/status', { cache: 'no-store' });
        const payload = await response.json();
        setText('balance', money.format(payload.balance || 0));
        setText('equity', money.format(payload.equity || 0));
        setText('dailyPnl', money.format(payload.daily_pnl || 0));
        setText('cycle', String(payload.cycle || 0));
        renderPositions(payload.active_trades || []);
        renderAnalysis(payload.last_decision_analysis || {});
        renderDecisionMetrics(payload.decision_metrics || {});
        renderStatus(payload.symbol_status || {}, payload.symbol_details || {});
        renderLastAction(payload);
        renderMemoryNews(payload);
        renderPerformance(payload.performance || {});
        renderPortfolioRisk(payload.portfolio_risk || {});
      } catch (error) {
        document.getElementById('lastAction').innerHTML = `<div class="empty">Error actualizando dashboard: ${error}</div>`;
      }
    }

    refresh();
    setInterval(refresh, 2500);

    // ════════════════════════════════════════════════════════════════
    //  FASE 10 — Advanced Charts (refresh every 30 seconds)
    // ════════════════════════════════════════════════════════════════

    const CHART_COLORS = {
      equity:    '#00d4aa',
      drawdown:  'rgba(255,107,107,0.25)',
      drawdownB: '#ff6b6b',
      rollingWr: '#43c6ac',
      refLow:    'rgba(255,107,107,0.55)',
      refHigh:   'rgba(0,212,170,0.55)',
      win:       '#00d4aa',
      loss:      '#ff6b6b',
      neutral:   '#8ea6bb',
      yellow:    '#ffd93d',
      gridLine:  'rgba(143,178,212,0.12)',
      tickColor: '#8ea6bb',
    };

    const chartDefaults = {
      responsive: true,
      maintainAspectRatio: true,
      plugins: { legend: { labels: { color: '#e8f1f8', font: { size: 12 } } } },
    };

    let _equityChart = null;
    let _rollingWrChart = null;
    let _symbolDistChart = null;
    let _hourDistChart = null;

    function _chartAxes(xTitle, yTitle) {
      return {
        x: {
          grid: { color: CHART_COLORS.gridLine },
          ticks: { color: CHART_COLORS.tickColor, maxTicksLimit: 10 },
          title: xTitle ? { display: true, text: xTitle, color: CHART_COLORS.tickColor } : undefined,
        },
        y: {
          grid: { color: CHART_COLORS.gridLine },
          ticks: { color: CHART_COLORS.tickColor },
          title: yTitle ? { display: true, text: yTitle, color: CHART_COLORS.tickColor } : undefined,
        },
      };
    }

    function _showEmpty(canvasId, message) {
      const canvas = document.getElementById(canvasId);
      if (!canvas) return;
      const parent = canvas.parentElement;
      canvas.style.display = 'none';
      if (!parent.querySelector('.chart-empty')) {
        const div = document.createElement('div');
        div.className = 'chart-empty';
        div.textContent = message || 'Sin datos suficientes';
        parent.appendChild(div);
      }
    }

    function _clearEmpty(canvasId) {
      const canvas = document.getElementById(canvasId);
      if (!canvas) return;
      canvas.style.display = '';
      const parent = canvas.parentElement;
      const empty = parent.querySelector('.chart-empty');
      if (empty) empty.remove();
    }

    async function renderEquityCurve() {
      const canvasId = 'equityCurveChart';
      let data;
      try {
        const r = await fetch('/api/equity_curve', { cache: 'no-store' });
        data = await r.json();
      } catch { return; }

      if (!data || data.length < 2) {
        _showEmpty(canvasId, 'Sin datos suficientes para la equity curve (mín. 2 trades)');
        return;
      }
      _clearEmpty(canvasId);

      const labels = data.map(d => '#' + d.trade_number);
      const balances = data.map(d => d.balance);
      const drawdowns = data.map(d => -d.drawdown_pct);

      const cfg = {
        type: 'line',
        data: {
          labels,
          datasets: [
            {
              label: 'Balance ($)',
              data: balances,
              borderColor: CHART_COLORS.equity,
              backgroundColor: 'rgba(0,212,170,0.08)',
              borderWidth: 2,
              pointRadius: 0,
              fill: false,
              yAxisID: 'yBalance',
              tension: 0.3,
            },
            {
              label: 'Drawdown (%)',
              data: drawdowns,
              borderColor: CHART_COLORS.drawdownB,
              backgroundColor: CHART_COLORS.drawdown,
              borderWidth: 1,
              pointRadius: 0,
              fill: true,
              yAxisID: 'yDD',
              tension: 0.2,
            },
          ],
        },
        options: {
          ...chartDefaults,
          interaction: { mode: 'index', intersect: false },
          plugins: { ...chartDefaults.plugins, tooltip: { mode: 'index', intersect: false } },
          scales: {
            x: { grid: { color: CHART_COLORS.gridLine }, ticks: { color: CHART_COLORS.tickColor, maxTicksLimit: 12 } },
            yBalance: {
              type: 'linear', position: 'left',
              grid: { color: CHART_COLORS.gridLine },
              ticks: { color: CHART_COLORS.tickColor },
              title: { display: true, text: 'Balance ($)', color: CHART_COLORS.tickColor },
            },
            yDD: {
              type: 'linear', position: 'right',
              grid: { drawOnChartArea: false },
              ticks: { color: CHART_COLORS.drawdownB },
              title: { display: true, text: 'Drawdown (%)', color: CHART_COLORS.drawdownB },
            },
          },
        },
      };

      if (_equityChart) { _equityChart.destroy(); }
      _equityChart = new Chart(document.getElementById(canvasId), cfg);
    }

    async function renderRollingWR() {
      const canvasId = 'rollingWrChart';
      let data;
      try {
        const r = await fetch('/api/equity_curve', { cache: 'no-store' });
        data = await r.json();
      } catch { return; }

      if (!data || data.length < 2) {
        _showEmpty(canvasId, 'Sin datos suficientes para Rolling Win Rate (mín. 2 trades)');
        return;
      }
      _clearEmpty(canvasId);

      const labels = data.map(d => '#' + d.trade_number);
      const rwrValues = data.map(d => d.rolling_wr);

      const cfg = {
        type: 'line',
        data: {
          labels,
          datasets: [
            {
              label: 'Rolling WR % ({{ rolling_wr_window }} trades)',
              data: rwrValues,
              borderColor: CHART_COLORS.rollingWr,
              backgroundColor: 'rgba(67,198,172,0.1)',
              borderWidth: 2,
              pointRadius: 0,
              fill: false,
              tension: 0.3,
            },
            {
              label: 'Ref. 55% (bueno)',
              data: Array(labels.length).fill(55),
              borderColor: CHART_COLORS.refHigh,
              borderWidth: 1,
              borderDash: [6, 4],
              pointRadius: 0,
              fill: false,
            },
            {
              label: 'Ref. 45% (mínimo)',
              data: Array(labels.length).fill(45),
              borderColor: CHART_COLORS.refLow,
              borderWidth: 1,
              borderDash: [4, 4],
              pointRadius: 0,
              fill: false,
            },
          ],
        },
        options: {
          ...chartDefaults,
          interaction: { mode: 'index', intersect: false },
          scales: {
            ..._chartAxes('Trade #', 'Win Rate (%)'),
            y: {
              grid: { color: CHART_COLORS.gridLine },
              ticks: { color: CHART_COLORS.tickColor, callback: v => v + '%' },
              min: 0,
              max: 100,
            },
          },
        },
      };

      if (_rollingWrChart) { _rollingWrChart.destroy(); }
      _rollingWrChart = new Chart(document.getElementById(canvasId), cfg);
    }

    async function renderSymbolDist() {
      const canvasId = 'symbolDistChart';
      let dist;
      try {
        const r = await fetch('/api/distribution', { cache: 'no-store' });
        dist = await r.json();
      } catch { return; }

      const data = dist.by_symbol || [];
      if (!data.length) {
        _showEmpty(canvasId, 'Sin datos de distribución por símbolo');
        return;
      }
      _clearEmpty(canvasId);

      const labels = data.map(d => d.symbol);
      const wrs = data.map(d => d.wr);
      const colors = wrs.map(w => w >= 50 ? CHART_COLORS.win : CHART_COLORS.loss);

      const cfg = {
        type: 'bar',
        data: {
          labels,
          datasets: [{
            label: 'Win Rate (%)',
            data: wrs,
            backgroundColor: colors,
            borderRadius: 6,
          }],
        },
        options: {
          ...chartDefaults,
          indexAxis: 'y',
          plugins: {
            ...chartDefaults.plugins,
            legend: { display: false },
            tooltip: { callbacks: { label: ctx => ' WR: ' + ctx.raw + '%' } },
          },
          scales: {
            x: {
              grid: { color: CHART_COLORS.gridLine },
              ticks: { color: CHART_COLORS.tickColor, callback: v => v + '%' },
              min: 0, max: 100,
            },
            y: { grid: { color: CHART_COLORS.gridLine }, ticks: { color: CHART_COLORS.tickColor } },
          },
        },
      };

      if (_symbolDistChart) { _symbolDistChart.destroy(); }
      _symbolDistChart = new Chart(document.getElementById(canvasId), cfg);
    }

    async function renderHourDist() {
      const canvasId = 'hourDistChart';
      let dist;
      try {
        const r = await fetch('/api/distribution', { cache: 'no-store' });
        dist = await r.json();
      } catch { return; }

      const data = dist.by_hour || [];
      if (!data.length) {
        _showEmpty(canvasId, 'Sin datos de distribución por hora');
        return;
      }
      _clearEmpty(canvasId);

      const labels = data.map(d => d.hour + 'h');
      const totals = data.map(d => d.total);
      const colors = data.map(d => d.wr >= 50 ? CHART_COLORS.win : CHART_COLORS.loss);

      const cfg = {
        type: 'bar',
        data: {
          labels,
          datasets: [{
            label: 'Trades',
            data: totals,
            backgroundColor: colors,
            borderRadius: 4,
          }],
        },
        options: {
          ...chartDefaults,
          plugins: {
            ...chartDefaults.plugins,
            legend: { display: false },
            tooltip: {
              callbacks: {
                label: (ctx) => {
                  const d = data[ctx.dataIndex];
                  return ` ${d.total} trades · WR ${d.wr}%`;
                },
              },
            },
          },
          scales: _chartAxes('Hora UTC', 'Nº Trades'),
        },
      };

      if (_hourDistChart) { _hourDistChart.destroy(); }
      _hourDistChart = new Chart(document.getElementById(canvasId), cfg);
    }

    async function renderRecentTrades() {
      let trades;
      try {
        const r = await fetch('/api/recent_trades', { cache: 'no-store' });
        trades = await r.json();
      } catch { return; }

      const tbody = document.getElementById('recentTradesTable');
      if (!trades || !trades.length) {
        tbody.innerHTML = '<tr><td colspan="9" class="empty">Sin trades cerrados aún.</td></tr>';
        return;
      }

      tbody.innerHTML = trades.map(t => {
        const rowClass = t.result === 'WIN' ? 'row-win' : t.result === 'LOSS' ? 'row-loss' : 'row-be';
        const resClass = t.result === 'WIN' ? 'result-win' : t.result === 'LOSS' ? 'result-loss' : 'result-be';
        const profitColor = t.profit >= 0 ? 'profit pos' : 'profit neg';
        const durStr = t.duration_min != null
          ? (t.duration_min >= 60 ? Math.floor(t.duration_min / 60) + 'h ' + (t.duration_min % 60) + 'm' : t.duration_min + 'm')
          : '—';
        const pipsStr = t.pips != null ? t.pips.toFixed(1) : '—';
        const badgeClass = t.direction === 'BUY' ? 'badge long' : 'badge short';
        return `<tr class="${rowClass}">
          <td class="mono">${t.num}</td>
          <td>${t.symbol}</td>
          <td><span class="${badgeClass}">${t.direction}</span></td>
          <td class="mono">${t.open_price != null ? number.format(t.open_price) : '—'}</td>
          <td class="mono">${t.close_price != null ? number.format(t.close_price) : '—'}</td>
          <td class="mono ${profitColor}">${money.format(t.profit)}</td>
          <td class="mono">${pipsStr}</td>
          <td class="mono">${durStr}</td>
          <td class="${resClass}">${t.result}</td>
        </tr>`;
      }).join('');
    }

    async function refreshCharts() {
      await Promise.all([
        renderEquityCurve(),
        renderRollingWR(),
        renderSymbolDist(),
        renderHourDist(),
        renderRecentTrades(),
        renderAdvancedMetrics(),
      ]);
    }

    let _sessionWrChart = null;
    let _slippageChart  = null;

    async function renderAdvancedMetrics() {
      try {
        const response = await fetch('/api/advanced_metrics', { cache: 'no-store' });
        const data = await response.json();

        // ── Win Rate por Sesión ──────────────────────────────────
        const sessions   = (data.wr_by_session || []).map(s => s.session);
        const wrValues   = (data.wr_by_session || []).map(s => s.wr);
        const totalTrades = (data.wr_by_session || []).map(s => s.total);

        const ctxS = document.getElementById('sessionWrChart')?.getContext('2d');
        if (ctxS) {
          if (_sessionWrChart) _sessionWrChart.destroy();
          _sessionWrChart = new Chart(ctxS, {
            type: 'bar',
            data: {
              labels: sessions,
              datasets: [{
                label: 'Win Rate %',
                data: wrValues,
                backgroundColor: wrValues.map(v => v >= 50 ? 'rgba(74,222,128,0.7)' : 'rgba(248,113,113,0.7)'),
                borderRadius: 4,
              }],
            },
            options: {
              indexAxis: 'y',
              responsive: true,
              maintainAspectRatio: false,
              plugins: {
                legend: { display: false },
                tooltip: {
                  callbacks: {
                    label: (ctx) => {
                      const idx = ctx.dataIndex;
                      const t = data.wr_by_session[idx];
                      return ` WR: ${t.wr}% (${t.wins}W/${t.losses}L, ${t.total} trades)`;
                    }
                  }
                }
              },
              scales: {
                x: { max: 100, ticks: { color: '#94a3b8' }, grid: { color: 'rgba(255,255,255,0.05)' } },
                y: { ticks: { color: '#94a3b8' }, grid: { display: false } },
              },
            },
          });
        }

        // ── Slippage por Símbolo ─────────────────────────────────
        const slipSymbols = (data.slippage_by_symbol || []).map(s => s.symbol);
        const avgSlip     = (data.slippage_by_symbol || []).map(s => s.avg_slippage_pips);
        const maxSlip     = (data.slippage_by_symbol || []).map(s => s.max_slippage_pips);

        const ctxSl = document.getElementById('slippageChart')?.getContext('2d');
        if (ctxSl) {
          if (_slippageChart) _slippageChart.destroy();
          _slippageChart = new Chart(ctxSl, {
            type: 'bar',
            data: {
              labels: slipSymbols,
              datasets: [
                {
                  label: 'Avg Slippage',
                  data: avgSlip,
                  backgroundColor: 'rgba(251,191,36,0.7)',
                  borderRadius: 4,
                },
                {
                  label: 'Max Slippage',
                  data: maxSlip,
                  backgroundColor: 'rgba(248,113,113,0.5)',
                  borderRadius: 4,
                },
              ],
            },
            options: {
              responsive: true,
              maintainAspectRatio: false,
              plugins: { legend: { labels: { color: '#94a3b8' } } },
              scales: {
                x: { ticks: { color: '#94a3b8' }, grid: { color: 'rgba(255,255,255,0.05)' } },
                y: { ticks: { color: '#94a3b8' }, grid: { color: 'rgba(255,255,255,0.05)' } },
              },
            },
          });
        }

        // ── Duration stats ───────────────────────────────────────
        const fmt = v => v != null && v > 0 ? `${v} min` : '—';
        setText('avgDuration',    fmt(data.avg_duration_min));
        setText('avgDurationWin', fmt(data.avg_duration_winners));
        setText('avgDurationLoss',fmt(data.avg_duration_losers));

        // ── Score stats ──────────────────────────────────────────
        const fmtScore = v => v != null && v > 0 ? String(v) : '—';
        setText('scoreWinners', fmtScore(data.score_winners_avg));
        setText('scoreLosers',  fmtScore(data.score_losers_avg));

      } catch (error) {
        console.warn('renderAdvancedMetrics error:', error);
      }
    }

    // Initial chart render + periodic refresh (every 30 seconds)
    refreshCharts();
    setInterval(refreshCharts, {{ chart_refresh_ms }});
  </script>
</body>
</html>
"""


def create_app(status_provider):
    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template_string(
            HTML_TEMPLATE,
            chart_refresh_ms=int(DASHBOARD_CHART_REFRESH_SEC * 1000),
            rolling_wr_window=int(DASHBOARD_ROLLING_WR_WINDOW),
        )

    @app.get("/api/status")
    def api_status():
        payload = status_provider() or {}
        payload.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
        payload.setdefault("active_trades", [])
        payload.setdefault("symbol_status", {})
        payload.setdefault("last_decision_analysis", {})
        payload.setdefault("decision_metrics", {})
        return jsonify(payload)

    @app.get("/api/equity_curve")
    def api_equity_curve():
        data = get_equity_curve_data(
            initial_balance=DASHBOARD_EQUITY_INITIAL,
            rolling_window=DASHBOARD_ROLLING_WR_WINDOW,
        )
        return jsonify(data)

    @app.get("/api/distribution")
    def api_distribution():
        data = get_distribution_data()
        return jsonify(data)

    @app.get("/api/recent_trades")
    def api_recent_trades():
        data = get_recent_trades(limit=DASHBOARD_RECENT_TRADES_LIMIT)
        return jsonify(data)

    @app.get("/api/advanced_metrics")
    def api_advanced_metrics():
        data = get_advanced_metrics()
        return jsonify(data)

    return app


def start_web_dashboard(status_provider, host="127.0.0.1", port=8765, logger=None):
    app = create_app(status_provider)

    if logger is None:
        logger = logging.getLogger(__name__)

    def _serve():
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        logger.info(f"[web] Dashboard disponible en http://{host}:{port}")
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)

    thread = threading.Thread(target=_serve, name="zar-web-dashboard", daemon=True)
    thread.start()
    return thread
