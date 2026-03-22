INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>K8s Diagnosis</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f3f1ea;
      --panel: rgba(255,255,255,0.9);
      --border: #cfc6b8;
      --text: #1c1b18;
      --muted: #6b665d;
      --accent: #17624f;
      --warn: #9a6700;
      --crit: #a12622;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top right, rgba(23,98,79,0.12), transparent 28%),
        linear-gradient(180deg, #f7f4ee, #ece6db);
      min-height: 100vh;
    }
    .shell {
      max-width: 1280px;
      margin: 0 auto;
      padding: 24px;
    }
    .hero {
      display: flex;
      justify-content: space-between;
      align-items: end;
      gap: 16px;
      margin-bottom: 20px;
    }
    .hero h1 {
      margin: 0;
      font-family: "IBM Plex Serif", Georgia, serif;
      font-size: 42px;
      line-height: 1;
    }
    .hero p {
      margin: 8px 0 0;
      color: var(--muted);
    }
    .actions {
      display: flex;
      gap: 10px;
      align-items: center;
    }
    button {
      border: 1px solid var(--border);
      background: white;
      color: var(--text);
      padding: 10px 14px;
      border-radius: 999px;
      cursor: pointer;
      font: inherit;
    }
    button:hover { border-color: var(--accent); }
    .grid {
      display: grid;
      grid-template-columns: 360px 1fr;
      gap: 20px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 20px;
      box-shadow: 0 12px 30px rgba(49, 44, 34, 0.08);
      backdrop-filter: blur(10px);
    }
    .filters {
      padding: 18px;
      display: grid;
      gap: 12px;
      margin-bottom: 16px;
    }
    .filters label {
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 13px;
    }
    .filters input, .filters select {
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px 12px;
      font: inherit;
      background: white;
    }
    .list {
      max-height: calc(100vh - 250px);
      overflow: auto;
      padding: 0 8px 8px;
    }
    .item {
      padding: 14px;
      border: 1px solid transparent;
      border-radius: 16px;
      margin: 8px;
      background: rgba(255,255,255,0.55);
      cursor: pointer;
    }
    .item.active {
      border-color: var(--accent);
      background: rgba(23,98,79,0.08);
    }
    .item h3 {
      margin: 0 0 8px;
      font-size: 15px;
    }
    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      font-size: 12px;
      color: var(--muted);
    }
    .badge {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 999px;
      background: #ece7de;
      color: var(--muted);
      font-size: 12px;
    }
    .badge.warning { background: rgba(154,103,0,0.12); color: var(--warn); }
    .badge.critical { background: rgba(161,38,34,0.12); color: var(--crit); }
    .badge.info { background: rgba(23,98,79,0.12); color: var(--accent); }
    .detail {
      padding: 24px;
      min-height: 640px;
    }
    .detail h2 {
      margin: 0 0 8px;
      font-family: "IBM Plex Serif", Georgia, serif;
      font-size: 28px;
    }
    .detail .summary {
      margin: 0 0 20px;
      font-size: 17px;
    }
    .section {
      margin-top: 22px;
    }
    .section h3 {
      margin: 0 0 8px;
      font-size: 14px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }
    .section ul {
      margin: 0;
      padding-left: 18px;
      line-height: 1.5;
    }
    .empty {
      color: var(--muted);
      padding: 28px;
      text-align: center;
    }
    @media (max-width: 960px) {
      .grid { grid-template-columns: 1fr; }
      .list { max-height: none; }
      .hero { align-items: start; flex-direction: column; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <div class="hero">
      <div>
        <h1>K8s Diagnosis</h1>
        <p>DiagnosisReport list and detail view sourced directly from the cluster.</p>
      </div>
      <div class="actions">
        <span id="last-refresh" class="badge">Not loaded</span>
        <button id="refresh-btn">Refresh</button>
      </div>
    </div>
    <div class="grid">
      <div>
        <div class="panel filters">
          <label>Namespace
            <select id="filter-namespace">
              <option value="">All</option>
            </select>
          </label>
          <label>Severity
            <select id="filter-severity">
              <option value="">All</option>
              <option value="critical">critical</option>
              <option value="warning">warning</option>
              <option value="info">info</option>
            </select>
          </label>
          <label>Symptom
            <input id="filter-symptom" type="text" placeholder="CrashLoopBackOff">
          </label>
        </div>
        <div class="panel list" id="report-list"></div>
      </div>
      <div class="panel detail" id="report-detail">
        <div class="empty">No diagnosis selected.</div>
      </div>
    </div>
  </div>
  <script>
    const state = {
      reports: [],
      selected: null,
      filters: { namespace: "", severity: "", symptom: "" },
      timer: null,
    };

    const listEl = document.getElementById("report-list");
    const detailEl = document.getElementById("report-detail");
    const refreshEl = document.getElementById("last-refresh");

    function severityClass(value) {
      return ["critical", "warning", "info"].includes(value) ? value : "";
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    async function loadReports() {
      const response = await fetch("/api/reports");
      if (!response.ok) throw new Error("Failed to load reports");
      const payload = await response.json();
      state.reports = payload.items || [];
      refreshEl.textContent = "Refreshed " + new Date().toLocaleTimeString();
      populateNamespaces();
      renderList();
      if (state.selected) {
        const match = state.reports.find((item) => item.name === state.selected);
        if (match) {
          loadDetail(match.name);
        }
      }
    }

    function filteredReports() {
      return state.reports.filter((item) => {
        if (state.filters.namespace && item.namespace !== state.filters.namespace) return false;
        if (state.filters.severity && item.severity !== state.filters.severity) return false;
        if (state.filters.symptom && !item.symptom.includes(state.filters.symptom)) return false;
        return true;
      });
    }

    function populateNamespaces() {
      const select = document.getElementById("filter-namespace");
      const namespaces = [...new Set(state.reports.map((item) => item.namespace).filter(Boolean))].sort();
      const current = state.filters.namespace;
      select.innerHTML = '<option value="">All</option>' + namespaces.map((ns) =>
        `<option value="${escapeHtml(ns)}">${escapeHtml(ns)}</option>`
      ).join("");
      select.value = current;
    }

    function renderList() {
      const reports = filteredReports();
      if (!reports.length) {
        listEl.innerHTML = '<div class="empty">No matching DiagnosisReport objects.</div>';
        return;
      }
      listEl.innerHTML = reports.map((item) => `
        <div class="item ${item.name === state.selected ? "active" : ""}" data-name="${escapeHtml(item.name)}">
          <div class="meta">
            <span class="badge ${severityClass(item.severity)}">${escapeHtml(item.severity)}</span>
            <span>${escapeHtml(item.namespace)}</span>
            <span>${escapeHtml(item.symptom)}</span>
          </div>
          <h3>${escapeHtml(item.workload.kind)}/${escapeHtml(item.workload.name)}</h3>
          <div>${escapeHtml(item.summary)}</div>
          <div class="meta" style="margin-top: 8px;">
            <span>trigger ${escapeHtml(item.triggerAt || "unknown")}</span>
            <span>${escapeHtml(item.lastAnalyzedAt || "unknown")}</span>
          </div>
        </div>
      `).join("");
      [...listEl.querySelectorAll(".item")].forEach((node) => {
        node.addEventListener("click", () => loadDetail(node.dataset.name));
      });
      if (!state.selected && reports.length) {
        loadDetail(reports[0].name);
      }
    }

    async function loadDetail(name) {
      state.selected = name;
      renderList();
      const response = await fetch("/api/reports/" + encodeURIComponent(name));
      if (!response.ok) {
        detailEl.innerHTML = '<div class="empty">Failed to load detail.</div>';
        return;
      }
      const item = await response.json();
      detailEl.innerHTML = `
        <h2>${escapeHtml(item.workload.kind)}/${escapeHtml(item.workload.name)}</h2>
        <p class="summary">${escapeHtml(item.summary)}</p>
        <div class="meta">
          <span class="badge ${severityClass(item.severity)}">${escapeHtml(item.severity)}</span>
          <span>${escapeHtml(item.namespace)}</span>
          <span>${escapeHtml(item.symptom)}</span>
          <span>confidence ${escapeHtml(item.confidence)}</span>
          <span>cluster ${escapeHtml(item.cluster || "unknown")}</span>
        </div>
        <div class="section">
          <h3>Probable Causes</h3>
          <ul>${item.probableCauses.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>
        </div>
        <div class="section">
          <h3>Evidence</h3>
          <ul>${item.evidence.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>
        </div>
        <div class="section">
          <h3>Fix Suggestions</h3>
          <ul>${item.recommendations.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>
        </div>
        <div class="section">
          <h3>Metadata</h3>
          <ul>
            <li>Trigger at: ${escapeHtml(item.triggerAt || "unknown")}</li>
            <li>Last analyzed: ${escapeHtml(item.lastAnalyzedAt || "unknown")}</li>
            <li>Trigger source: ${escapeHtml(item.source || "unknown")}</li>
            <li>Observed for: ${escapeHtml(item.observedFor || "unknown")} seconds</li>
            <li>Cluster: ${escapeHtml(item.cluster || "unknown")}</li>
            <li>Analysis version: ${escapeHtml(item.analysisVersion || "unknown")}</li>
            <li>Model: ${escapeHtml(item.modelInfo?.name || "unknown")}</li>
            <li>Fallback: ${escapeHtml(String(item.modelInfo?.fallback ?? false))}</li>
            <li>Event reason: ${escapeHtml(item.rawSignal?.reason || "n/a")}</li>
            <li>Event message: ${escapeHtml(item.rawSignal?.message || "n/a")}</li>
            <li>Event time: ${escapeHtml(item.rawSignal?.timestamp || "n/a")}</li>
          </ul>
        </div>
      `;
    }

    function bindFilters() {
      document.getElementById("filter-namespace").addEventListener("change", (event) => {
        state.filters.namespace = event.target.value.trim();
        renderList();
      });
      document.getElementById("filter-severity").addEventListener("change", (event) => {
        state.filters.severity = event.target.value.trim();
        renderList();
      });
      document.getElementById("filter-symptom").addEventListener("input", (event) => {
        state.filters.symptom = event.target.value.trim();
        renderList();
      });
      document.getElementById("refresh-btn").addEventListener("click", () => loadReports().catch(showError));
    }

    function showError(error) {
      detailEl.innerHTML = `<div class="empty">${escapeHtml(error.message || error)}</div>`;
    }

    bindFilters();
    loadReports().catch(showError);
    state.timer = setInterval(() => loadReports().catch(showError), 15000);
  </script>
</body>
</html>
"""
