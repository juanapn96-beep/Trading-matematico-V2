import logging
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template_string


HTML_TEMPLATE = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ZAR Web Dashboard</title>
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
        <h2>Último análisis Groq</h2>
        <div id="analysis" class="analysis"></div>
      </section>
    </section>

    <section class="grid">
      <section class="panel">
        <h2>Métricas Groq</h2>
        <div id="groqMetrics" class="status-list"></div>
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
        <h2>📊 Performance Tracking</h2>
        <div id="performanceMetrics" class="status-list"></div>
      </section>

      <section class="panel">
        <h2>🔗 Riesgo de Portafolio</h2>
        <div id="portfolioRisk" class="status-list"></div>
      </section>
    </section>
  </main>

  <script>
    const money = new Intl.NumberFormat('es-ES', { style: 'currency', currency: 'USD' });
    const number = new Intl.NumberFormat('es-ES', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

    function setText(id, value) {
      document.getElementById(id).textContent = value;
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
        container.innerHTML = '<div class="empty">Aún no hay análisis Groq disponible.</div>';
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

    function renderStatus(statusMap) {
      const container = document.getElementById('statusList');
      const entries = Object.entries(statusMap || {});
      if (!entries.length) {
        container.innerHTML = '<div class="empty">Sin estados de símbolo disponibles.</div>';
        return;
      }

      container.innerHTML = entries.map(([symbol, status]) => `
        <div class="status-item">
          <strong>${symbol}</strong>
          <div>${status}</div>
        </div>
      `).join('');
    }

    function renderGroqMetrics(metrics) {
      const container = document.getElementById('groqMetrics');
      if (!metrics || Object.keys(metrics).length === 0) {
        container.innerHTML = '<div class="empty">Sin métricas Groq.</div>';
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
        ['429 cuota', metrics.quota_hits_total || 0],
        ['Fallback HOLD', metrics.fallback_holds_total || 0],
      ];

      container.innerHTML = labels.map(([label, value]) => `
        <div class="status-item">
          <strong>${label}</strong>
          <div class="mono">${value}</div>
        </div>
      `).join('') + (
        metrics.cooldown_until
          ? `<div class="status-item"><strong>Cooldown hasta</strong><div>${metrics.cooldown_until}</div></div>`
          : ''
      );
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
        renderAnalysis(payload.last_groq_analysis || {});
        renderGroqMetrics(payload.groq_metrics || {});
        renderStatus(payload.symbol_status || {});
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
  </script>
</body>
</html>
"""


def create_app(status_provider):
    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template_string(HTML_TEMPLATE)

    @app.get("/api/status")
    def api_status():
        payload = status_provider() or {}
        payload.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
        payload.setdefault("active_trades", [])
        payload.setdefault("symbol_status", {})
        payload.setdefault("last_groq_analysis", {})
        return jsonify(payload)

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