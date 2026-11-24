(() => {
  const state = {
    books: [],
    scenarios: [],
    selectedBook: "next27_flagship",
    selectedScenarioId: "fed_up_50_oil_spike",
    rateBps: 50,
    oilPct: 25,
    selectedTicker: "UAL",
    selectedEdgeId: null,
    analysis: null,
    formulaGrid: null,
    watcher: null,
    evalReport: null,
    ledger: { approvals: [], refusals: [] },
    metadata: null,
    signoffPolicy: { dual_control: true, require_live_eval: false },
    lastLiveReport: null,
    selectedCaseId: null,
  };

  const fmtMoney = (v) => {
    const n = Number(v);
    const sign = n > 0 ? "+" : n < 0 ? "-" : "";
    return `${sign}$${Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}M`;
  };

  const fmtEps = (v) => {
    const n = Number(v);
    const sign = n > 0 ? "+" : n < 0 ? "-" : "";
    return `${sign}$${Math.abs(n).toFixed(4)}`;
  };

  const valClass = (v) => {
    const n = Number(v);
    if (n > 0.0001) return "val-pos";
    if (n < -0.0001) return "val-neg";
    return "";
  };

  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

  const STATUS_LABEL = {
    income_statement: "Annual income sensitivity",
    fair_value_only: "Fair-value sensitivity only",
    not_convertible: "Quantified, not convertible",
    not_quantified: "No amount disclosed",
    incorporated_by_reference: "Incorporated by reference",
    none_found: "No passage located",
  };

  const blank = (value) => (value === null || value === undefined || value === "" ? "—" : esc(value));

  // Display value of a long decimal input cell (the exact value stays in the cell's tooltip and the workbook).
  const shortNumber = (value) =>
    value === null || value === undefined || value === "" ? "—" : esc(String(Number(Number(value).toFixed(4))));

  // Notes the engine attached to one factor ("Rate" or "Oil"), without the prefix.
  const factorNotes = (holding, prefix) =>
    [...(holding.coverage_notes || []), ...(holding.basis_notes || [])]
      .filter((note) => note.startsWith(`${prefix}:`))
      .map((note) => note.slice(prefix.length + 1).trim());

  function factorBlock(title, prov, notes) {
    const status = STATUS_LABEL[prov.disclosure_status] || prov.disclosure_status || "";
    const badgeClass = prov.disclosure_status === "income_statement" ? "badge-ok" : "badge-neutral";
    const quote = prov.verbatim_quote
      ? `<div class="quote-box">“${esc(prov.verbatim_quote)}”</div>`
      : `<div class="quote-box quote-empty">No passage is bundled for this factor.</div>`;
    const supporting = prov.supporting_passages || [];
    const support = supporting.length
      ? `<details class="support-list"><summary>${supporting.length} supporting passage${supporting.length === 1 ? "" : "s"}</summary><ul>` +
        supporting.map((s) => `<li><span class="support-section">${esc(s.section)}</span>${esc(s.text)}</li>`).join("") +
        `</ul></details>`
      : "";
    const noteHtml = notes.map((note) => `<div class="basis-note">${esc(note)}</div>`).join("");
    return `
      <div class="factor-head"><strong>${esc(title)}</strong><span class="badge ${badgeClass}">${esc(status)}</span></div>
      <div class="factor-section">${esc(prov.filing_section)}</div>
      ${quote}${support}${noteHtml}`;
  }

  function switchTab(tabId) {
    document.querySelectorAll(".tab-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.tab === tabId);
    });
    document.querySelectorAll(".tab-panel").forEach((p) => {
      p.classList.toggle("active", p.id === `panel-${tabId}`);
    });
  }

  function updateExportLinks() {
    const qs = new URLSearchParams({
      book_id: state.selectedBook,
      rate_bps: String(state.rateBps),
      oil_pct: String(state.oilPct),
    });
    if (state.selectedScenarioId) qs.set("scenario_id", state.selectedScenarioId);
    const href = `/api/export.xlsx?${qs.toString()}`;
    document.getElementById("download-xlsx-top").href = href;
    document.getElementById("download-xlsx-sheet").href = href;
  }

  function renderKpis() {
    if (!state.analysis) return;
    const sc = state.analysis.scenario;
    const tot = state.analysis.portfolio_totals;
    const g = state.analysis.contagion_graph.summary;
    const gov = state.analysis.governance;

    const rSign = Number(sc.rate_bps) >= 0 ? "+" : "";
    const oSign = Number(sc.oil_pct) >= 0 ? "+" : "";
    document.getElementById("kpi-shock").textContent = `${rSign}${sc.rate_bps} bp / ${oSign}${sc.oil_pct}%`;
    document.getElementById("kpi-odds").textContent = `${sc.label}`;

    const preEl = document.getElementById("kpi-pretax");
    preEl.textContent = fmtMoney(tot.aggregate_pre_tax_impact_m);
    preEl.className = `kpi-value ${valClass(tot.aggregate_pre_tax_impact_m)}`;
    // The total adds only the holdings whose filing states a figure that converts to earnings.
    const withFigure = state.analysis.holdings.filter(
      (h) => h.disclosed_basis.rate_status === "income_statement" || h.disclosed_basis.oil_status === "income_statement",
    ).length;
    document.getElementById("kpi-holdings-count").textContent = withFigure === tot.holdings_count
      ? `Sum of all ${tot.holdings_count} holdings`
      : `Sum of ${withFigure} of ${tot.holdings_count} holdings; ${tot.holdings_count - withFigure} state no figure`;

    const aftEl = document.getElementById("kpi-aftertax");
    aftEl.textContent = fmtMoney(tot.aggregate_after_tax_impact_m);
    aftEl.className = `kpi-value ${valClass(tot.aggregate_after_tax_impact_m)}`;
    const statutory = state.analysis.holdings.filter((h) => h.scaled_impact.tax_rate_basis === "us_federal_statutory_rate" && (h.conversion_notes || []).length);
    document.getElementById("kpi-aftertax-foot").textContent = statutory.length
      ? `XBRL effective tax rate; ${statutory.map((h) => h.ticker).join(", ")} at the 21% US statutory rate (see its note)`
      : "At each issuer's XBRL effective tax rate";

    document.getElementById("kpi-edges").textContent = `${g.total_edges} links`;
    document.getElementById("kpi-hops").textContent =
      `Issuers: ${g.hop1_count} shocked · ${g.hop2_count} 2nd-order · ${g.hop3_count} 3rd-order`;

    const warnTotal = tot.extrapolation_warning_count + tot.non_comparable_warning_count;
    document.getElementById("kpi-warnings").textContent = `${tot.extrapolation_warning_count} >2x · ${tot.non_comparable_warning_count} no figure`;
    document.getElementById("packet-hash-chip").textContent = `Packet: ${gov.confirm_code_6}`;
    updateExportLinks();
  }

  function renderScenarioWheel() {
    const svg = document.getElementById("scenario-wheel-svg");
    const rateAngle = (Number(state.rateBps) / 300) * 135;
    const oilAngle = (Number(state.oilPct) / 40) * 135;
    const rSign = Number(state.rateBps) >= 0 ? "+" : "";
    const oSign = Number(state.oilPct) >= 0 ? "+" : "";

    svg.innerHTML = `
      <circle cx="125" cy="125" r="106" fill="none" stroke="#e2e0d8" stroke-width="18" />
      <circle cx="125" cy="125" r="106" fill="none" stroke="#1a56db" stroke-width="18"
        stroke-dasharray="165 510" transform="rotate(${rateAngle - 90} 125 125)" />
      <circle cx="125" cy="125" r="76" fill="none" stroke="#e2e0d8" stroke-width="16" />
      <circle cx="125" cy="125" r="76" fill="none" stroke="#b45309" stroke-width="16"
        stroke-dasharray="120 400" transform="rotate(${oilAngle - 90} 125 125)" />
      <circle cx="125" cy="125" r="52" fill="#0f172a" />
      <text x="125" y="119" text-anchor="middle" fill="#ffffff" font-family="JetBrains Mono, monospace" font-size="13" font-weight="700">${rSign}${state.rateBps} bp</text>
      <text x="125" y="138" text-anchor="middle" fill="#fbbf24" font-family="JetBrains Mono, monospace" font-size="12" font-weight="600">Oil ${oSign}${state.oilPct}%</text>
    `;
  }

  function renderScenarioPresets() {
    const box = document.getElementById("scenario-presets");
    box.innerHTML = "";
    state.scenarios.forEach((sc) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `preset-btn ${state.selectedScenarioId === sc.id ? "active" : ""}`;
      const odds = sc.market_odds_pct
        ? `<span class="badge badge-neutral">${esc(sc.market_odds_pct)}% odds</span>`
        : "";
      btn.innerHTML = `<span>${esc(sc.label)}</span>${odds}`;
      btn.addEventListener("click", () => {
        state.selectedScenarioId = sc.id;
        state.rateBps = Number(sc.rate_bps);
        state.oilPct = Number(sc.oil_pct);
        syncSlidersFromState();
        refreshAnalysis();
      });
      box.appendChild(btn);
    });
  }

  function renderHoldingsTable() {
    const tbody = document.querySelector("#holdings-table tbody");
    tbody.innerHTML = "";
    if (!state.analysis) return;

    state.analysis.holdings.forEach((h) => {
      const tr = document.createElement("tr");
      tr.className = `clickable-row ${state.selectedTicker === h.ticker ? "selected" : ""}`;
      const imp = h.scaled_impact;
      let badgeHtml = `<span class="badge badge-ok">WITHIN 2x</span>`;
      if (h.extrapolation_warning) {
        badgeHtml = `<span class="badge badge-warn">&gt;2x EXTRAPOLATION</span>`;
      } else if (h.non_comparable_warning) {
        badgeHtml = `<span class="badge badge-neutral">BASIS NOTE</span>`;
      } else if ((h.conversion_notes || []).length) {
        // A short label: this column is narrow. The note itself is in the tooltip and on Tab 2.
        const noteLabel = imp.tax_rate_basis === "us_federal_statutory_rate" ? "TAX NOTE" : "EPS NOTE";
        badgeHtml = `<span class="badge badge-neutral" title="${esc(h.conversion_notes.join(" "))}">${noteLabel}</span>`;
      }
      // No percentage is reported when fiscal-year EPS is zero or negative: it would not be meaningful.
      const epsPctCell = imp.eps_pct_of_fy == null
        ? `<td class="num-right" title="Not meaningful: reported diluted EPS is zero or negative">n.m.</td>`
        : `<td class="num-right ${valClass(imp.eps_pct_of_fy)}">${Number(imp.eps_pct_of_fy) > 0 ? "+" : ""}${imp.eps_pct_of_fy}%</td>`;

      tr.innerHTML = `
        <td class="tabular"><strong>${esc(h.ticker)}</strong></td>
        <td>${esc(h.name)}</td>
        <td>${esc(h.sector)}</td>
        <td class="num-right ${valClass(imp.rate_pre_tax_m)}">${fmtMoney(imp.rate_pre_tax_m)}</td>
        <td class="num-right ${valClass(imp.oil_pre_tax_m)}">${fmtMoney(imp.oil_pre_tax_m)}</td>
        <td class="num-right ${valClass(imp.total_pre_tax_m)}">${fmtMoney(imp.total_pre_tax_m)}</td>
        <td class="num-right ${valClass(imp.after_tax_m)}">${fmtMoney(imp.after_tax_m)}</td>
        <td class="num-right ${valClass(imp.eps_delta)}">${fmtEps(imp.eps_delta)}</td>
        ${epsPctCell}
        <td>${badgeHtml}</td>
      `;
      tr.addEventListener("click", () => {
        state.selectedTicker = h.ticker;
        state.selectedEdgeId = null;
        renderHoldingsTable();
        renderContagionGraph();
        renderCitationInspector();
      });
      tbody.appendChild(tr);
    });
  }

  function renderContagionGraph() {
    const svg = document.getElementById("contagion-svg");
    if (!state.analysis) return;
    const graph = state.analysis.contagion_graph;
    document.getElementById("gql-drawer").textContent = graph.bigquery_gql;

    const cols = {
      1: graph.nodes.filter((n) => n.hop_order === 1),
      2: graph.nodes.filter((n) => n.hop_order === 2),
      3: graph.nodes.filter((n) => n.hop_order === 3),
      0: graph.nodes.filter((n) => n.hop_order === 0),
    };

    const colX = { 1: 115, 2: 380, 3: 645, 0: 115 };
    const hopColor = { 1: "#b91c1c", 2: "#b45309", 3: "#1a56db", 0: "#64748b" };
    const pos = {};

    // Place Hop 1 + Hop 0 on left, Hop 2 center, Hop 3 right
    const leftNodes = [...cols[1], ...cols[0]];
    leftNodes.forEach((n, idx) => {
      const step = 360 / Math.max(leftNodes.length, 1);
      pos[n.ticker] = { x: colX[1], y: 50 + idx * step + step / 2 };
    });
    cols[2].forEach((n, idx) => {
      const step = 360 / Math.max(cols[2].length, 1);
      pos[n.ticker] = { x: colX[2], y: 50 + idx * step + step / 2 };
    });
    cols[3].forEach((n, idx) => {
      const step = 360 / Math.max(cols[3].length, 1);
      pos[n.ticker] = { x: colX[3], y: 50 + idx * step + step / 2 };
    });

    let html = `
      <text x="115" y="24" text-anchor="middle" fill="#475569" font-size="11" font-weight="700">TIER 1 · DIRECT BOOK HOLDINGS</text>
      <text x="380" y="24" text-anchor="middle" fill="#475569" font-size="11" font-weight="700">TIER 2 · CUSTOMER / SUPPLIER RIPPLE</text>
      <text x="645" y="24" text-anchor="middle" fill="#475569" font-size="11" font-weight="700">TIER 3 · UPSTREAM CONTAGION</text>
    `;

    graph.edges.forEach((e) => {
      const p1 = pos[e.src];
      const p2 = pos[e.dst];
      if (!p1 || !p2) return;
      const active =
        state.selectedEdgeId === e.edge_id ||
        state.selectedTicker === e.src ||
        state.selectedTicker === e.dst;
      const stroke = active ? "#1a56db" : "#cbd5e1";
      const width = active ? 2.6 : 1.5;
      const midX = (p1.x + p2.x) / 2;
      const midY = (p1.y + p2.y) / 2 - 6;
      html += `
        <g class="graph-edge" data-edge="${e.edge_id}" style="cursor:pointer;">
          <line x1="${p1.x}" y1="${p1.y}" x2="${p2.x}" y2="${p2.y}" stroke="${stroke}" stroke-width="${width}" />
          <rect x="${midX - 28}" y="${midY - 9}" width="56" height="15" rx="3" fill="#ffffff" stroke="${stroke}" stroke-width="0.8" />
          <text x="${midX}" y="${midY + 2}" text-anchor="middle" fill="#334155" font-family="JetBrains Mono, monospace" font-size="9.5">${e.revenue_share_pct != null ? `${esc(e.revenue_share_pct)}% rev` : "link"}</text>
        </g>
      `;
    });

    graph.nodes.forEach((n) => {
      const p = pos[n.ticker];
      if (!p) return;
      const isSelected = state.selectedTicker === n.ticker;
      const fill = hopColor[n.hop_order] || "#475569";
      html += `
        <g class="graph-node" data-ticker="${n.ticker}" style="cursor:pointer;">
          <rect x="${p.x - 46}" y="${p.y - 16}" width="92" height="32" rx="6"
            fill="${isSelected ? "#0f172a" : "#ffffff"}"
            stroke="${fill}" stroke-width="${isSelected ? 3 : 2}" />
          <circle cx="${p.x - 34}" cy="${p.y}" r="4.5" fill="${fill}" />
          <text x="${p.x - 23}" y="${p.y - 2}" fill="${isSelected ? "#ffffff" : "#0f172a"}" font-family="JetBrains Mono, monospace" font-size="11.5" font-weight="700">${n.ticker}</text>
          <text x="${p.x - 23}" y="${p.y + 10}" fill="${isSelected ? "#cbd5e1" : "#64748b"}" font-family="JetBrains Mono, monospace" font-size="9.5">${fmtMoney(n.total_pre_tax_m)}</text>
        </g>
      `;
    });

    svg.innerHTML = html;

    svg.querySelectorAll(".graph-node").forEach((el) => {
      el.addEventListener("click", () => {
        state.selectedTicker = el.dataset.ticker;
        state.selectedEdgeId = null;
        renderHoldingsTable();
        renderContagionGraph();
        renderCitationInspector();
      });
    });

    svg.querySelectorAll(".graph-edge").forEach((el) => {
      el.addEventListener("click", () => {
        state.selectedEdgeId = el.dataset.edge;
        renderContagionGraph();
        renderCitationInspector();
      });
    });
  }

  function renderCitationInspector() {
    const container = document.getElementById("inspector-body");
    if (!state.analysis) return;

    if (state.selectedEdgeId) {
      const edge = state.analysis.contagion_graph.edges.find((e) => e.edge_id === state.selectedEdgeId);
      if (edge) {
        document.getElementById("inspector-title").textContent = `Supply-Chain Edge · ${edge.src} → ${edge.dst}`;
        document.getElementById("inspector-doc-meta").textContent = `${edge.provenance.doc_id} · Hop ${edge.hop_order}`;
        const share =
          edge.revenue_share_pct != null ? `${esc(edge.revenue_share_pct)}% disclosed share` : "no share disclosed";
        container.innerHTML = `
          <div style="margin-bottom:10px;">
            <span class="badge badge-warn">Hop ${edge.hop_order} Contagion</span>
            <span class="badge badge-neutral">${esc(edge.relationship)} (${share})</span>
          </div>
          ${edge.share_note ? `<div class="basis-note">${esc(edge.share_note)}</div>` : ""}
          <div class="factor-section">${esc(edge.provenance.filing_section)}</div>
          <div class="quote-box">“${esc(edge.provenance.verbatim_quote)}”</div>
          <div class="hash-banner">Excerpt SHA-256: ${esc(edge.provenance.sha256)}</div>
        `;
        return;
      }
    }

    let holding = state.analysis.holdings.find((h) => h.ticker === state.selectedTicker);
    if (!holding && state.analysis.holdings.length > 0) {
      holding = state.analysis.holdings[0];
      state.selectedTicker = holding.ticker;
    }
    if (!holding) return;

    const pRate = holding.provenance[0];
    const pOil = holding.provenance[1];
    const imp = holding.scaled_impact;
    const xbrl = holding.xbrl;

    document.getElementById("inspector-title").textContent = `${holding.ticker} · ${holding.name}`;
    document.getElementById("inspector-doc-meta").textContent = `${pRate.doc_id} · CIK ${holding.cik}`;
    const xbrlDebt = xbrl.long_term_debt_m != null ? `$${Number(xbrl.long_term_debt_m).toLocaleString("en-US")}M` : "—";

    const flags = [...holding.extrapolation_flags, ...holding.non_comparable_flags, ...(holding.conversion_notes || [])]
      .map((f) => `<div class="decision-banner refused" style="margin:6px 0; padding:7px 10px;">⚠ ${esc(f)}</div>`)
      .join("");

    container.innerHTML = `
      ${flags}
      <div style="display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-bottom:12px; background:var(--bg-subtle); padding:10px; border-radius:6px; border:1px solid var(--border-hairline);">
        <div><div class="kpi-label">XBRL Tax Rate</div><div class="tabular"><strong>${(Number(xbrl.effective_tax_rate) * 100).toFixed(1)}%</strong>${imp.tax_rate_basis === "us_federal_statutory_rate" ? ` <span style="font-size:11px;">not used · ${(Number(imp.tax_rate_applied) * 100).toFixed(1)}% statutory applied</span>` : ""}</div></div>
        <div><div class="kpi-label">Diluted Shares</div><div class="tabular"><strong>${xbrl.diluted_shares_m}M</strong></div></div>
        <div><div class="kpi-label">Δ Diluted EPS</div><div class="tabular ${valClass(imp.eps_delta)}"><strong>${fmtEps(imp.eps_delta)}/sh</strong></div></div>
        <div><div class="kpi-label">XBRL Revenue</div><div class="tabular"><strong>$${Number(xbrl.revenue_m).toLocaleString("en-US")}M</strong></div></div>
        <div><div class="kpi-label">FY Diluted EPS</div><div class="tabular"><strong>$${Number(xbrl.diluted_eps).toFixed(2)}</strong></div></div>
        <div><div class="kpi-label">Long-Term Debt</div><div class="tabular"><strong>${xbrlDebt}</strong></div></div>
      </div>

      ${factorBlock("1. Interest rate sensitivity", pRate, factorNotes(holding, "Rate"))}
      ${factorBlock("2. Oil / fuel sensitivity", pOil, factorNotes(holding, "Oil"))}

      <div class="hash-banner">Excerpt SHA-256: ${esc(pRate.sha256)}</div>
    `;
  }

  const guardrailClass = (status) =>
    status.includes("EXTRAPOLATION") ? "badge-warn" : status.includes("NON-COMPARABLE") ? "badge-neutral" : "badge-ok";

  function renderFormulaSheetAndWatcher() {
    if (!state.formulaGrid || !state.watcher) return;
    const tbody = document.querySelector("#formula-sheet-table tbody");
    tbody.innerHTML = "";

    state.formulaGrid.rows.forEach((r, rowIdx) => {
      const er = r.excel_row;
      const inp = r.inputs;
      const frm = r.formulas;
      const ev = r.evaluated;

      const oFxEscaped = `=${frm[`O${er}`]}`.replace(/"/g, "&quot;");
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="tabular">${er}</td>
        <td class="tabular"><strong>${esc(r.ticker)}</strong></td>
        <td class="sheet-input-cell">${blank(inp[`C${er}`])}</td>
        <td class="sheet-input-cell">${blank(inp[`D${er}`])}</td>
        <td class="sheet-input-cell" title="${esc(inp[`E${er}`] ?? "no annual income sensitivity disclosed")}">${shortNumber(inp[`E${er}`])}</td>
        <td class="sheet-input-cell">${blank(inp[`F${er}`])}</td>
        <td class="sheet-formula-cell" data-ref="G${er}" data-fx="${esc(`=${frm[`G${er}`]}`)}">${ev.rate_scaled_m}</td>
        <td class="sheet-formula-cell" data-ref="H${er}" data-fx="${esc(`=${frm[`H${er}`]}`)}">${ev.oil_scaled_m}</td>
        <td class="sheet-formula-cell" data-ref="I${er}" data-fx="=${frm[`I${er}`]}">${ev.total_pre_tax_m}</td>
        <td class="sheet-input-cell"${r.tax_rate_basis === "us_federal_statutory_rate" ? ' title="US federal statutory rate: the XBRL effective rate of this filing is not usable as a marginal rate (see the holding\'s note)"' : ""}>${inp[`J${er}`]}${r.tax_rate_basis === "us_federal_statutory_rate" ? " (statutory)" : ""}</td>
        <td class="sheet-formula-cell" data-ref="K${er}" data-fx="=${frm[`K${er}`]}">${ev.after_tax_m}</td>
        <td class="sheet-input-cell">${inp[`L${er}`]}</td>
        <td class="sheet-formula-cell" data-ref="M${er}" data-fx="=${frm[`M${er}`]}">${ev.eps_delta}</td>
        <td class="sheet-formula-cell" style="text-align:left;" data-ref="O${er}" data-fx="${oFxEscaped}"><span class="badge ${guardrailClass(ev.guardrail_status)}">${esc(ev.guardrail_status)}</span></td>
        <td class="sheet-input-cell" style="text-align:left;">${blank(inp[`P${er}`])}</td>
      `;
      tbody.appendChild(tr);

      if (rowIdx === 0) {
        document.getElementById("active-cell-ref").textContent = `G${er} (${r.ticker})`;
        document.getElementById("active-cell-formula").textContent = `=${frm[`G${er}`]}`;
      }
    });

    tbody.querySelectorAll(".sheet-formula-cell").forEach((cell) => {
      cell.addEventListener("click", () => {
        tbody.querySelectorAll(".sheet-formula-cell").forEach((c) => c.classList.remove("active-cell"));
        cell.classList.add("active-cell");
        document.getElementById("active-cell-ref").textContent = cell.dataset.ref;
        document.getElementById("active-cell-formula").textContent = cell.dataset.fx;
      });
    });

    const wBody = document.querySelector("#watcher-table tbody");
    wBody.innerHTML = "";
    state.watcher.rows.forEach((row) => {
      const cs = row.credit_scores;
      const rubricCell = cs
        ? `<span class="badge ${cs.overall_stance.includes("Watch") ? "badge-warn" : "badge-ok"}">${esc(cs.overall_stance)}</span>` +
          `<br/><span class="tabular">${esc(cs.composite_score)} (${esc(cs.leverage_trajectory)}/${esc(cs.liquidity_headroom)}/${esc(cs.margin_resilience)}/${esc(cs.covenant_risk)})</span>`
        : `<span class="badge badge-neutral">not scored</span>`;
      const evidence = row.evidence_quote
        ? `“${esc(row.evidence_quote)}”`
        : `<span class="watch-source">${esc(row.value_source)}</span>`;
      const basis = row.basis === "filing_passage" ? "quoted passage" : row.basis === "xbrl" ? "XBRL" : esc(row.basis);
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="tabular"><strong>${esc(row.ticker)}</strong><br/><span style="color:#64748b;font-size:11px;">${esc(row.current_doc_id)}</span></td>
        <td><strong>${esc(row.metric)}</strong><br/><span class="badge badge-neutral">${basis}</span></td>
        <td class="tabular">${esc(row.prior_value)}<br/><span class="watch-period">${esc(row.prior_period_end)}</span></td>
        <td class="tabular"><strong>${esc(row.current_value)}</strong><br/><span class="watch-period">${esc(row.current_period_end)}</span></td>
        <td>${esc(row.change_summary)}</td>
        <td>${rubricCell}</td>
        <td style="max-width:320px; font-size:11.5px; color:#334155;">${evidence}</td>
      `;
      wBody.appendChild(tr);
    });
  }

  // Gates fed by the offline replay and, in a live run, by checks on the agent's answers as well.
  const MIXED_GATES = ["citation_exactness_pct", "advice_guardrail_pct"];

  const VERDICT_CLASS = {
    release_eligible: "approved",
    deterministic_only: "neutral",
    incomplete: "refused",
    blocked: "refused",
  };

  function statusBadge(status) {
    if (status === "pass") return `<span class="badge badge-ok">PASS</span>`;
    if (status === "fail") return `<span class="badge badge-neg">FAIL</span>`;
    return `<span class="badge badge-neutral">NOT RUN</span>`;
  }

  function tierCell(tiers, name) {
    const t = tiers[name];
    if (!t) return `<span class="tier-none">none</span>`;
    const ran = t.pass + t.fail;
    if (ran === 0) return `<span class="badge badge-neutral">not run</span>`;
    const cls = t.fail > 0 ? "badge-neg" : "badge-ok";
    const pending = t.not_run > 0 ? ` (+${t.not_run} not run)` : "";
    return `<span class="badge ${cls}">${t.pass}/${ran}${pending}</span>`;
  }

  function renderDataNotice() {
    if (!state.metadata) return;
    document.getElementById("posture-chip").textContent = `Posture: ${state.metadata.source_posture}`;
    document.getElementById("inspector-posture-badge").textContent = state.metadata.source_posture;
    const notice = document.getElementById("data-notice");
    if (state.metadata.data_notice) {
      notice.textContent = state.metadata.data_notice;
      notice.hidden = false;
    } else {
      notice.hidden = true;
    }
  }

  // The last live run saved to disk for this dataset. Reference only: sign-off is bound to
  // the run made in this server session, which is the scorecard above.
  function renderLastLive() {
    const box = document.getElementById("eval-last-live");
    const live = state.lastLiveReport;
    if (!live || (state.evalReport && state.evalReport.run_id === live.run_id)) {
      box.hidden = true;
      return;
    }
    const s = live.summary;
    const failedGates = live.gates.filter((g) => g.status === "fail").map((g) => g.gate);
    box.className = `decision-banner ${VERDICT_CLASS[live.verdict] || "refused"}`;
    box.innerHTML =
      `<strong>Last saved live agent run: ${esc(live.verdict.replace(/_/g, " ").toUpperCase())}</strong> · ` +
      `run ${esc(live.run_id)}, ${esc(live.started_at_utc)}, agent ${esc(live.agent_model)}` +
      (live.reviewer_model ? `, reviewer ${esc(live.reviewer_model)}` : "") +
      `, judge ${esc(live.judge_model)}<br/>` +
      `<span class="tabular">Checks run ${s.checks_run}/${s.checks_total} · passed ${s.checks_passed} · failed ${s.checks_failed}` +
      (failedGates.length ? ` · failed gates: ${esc(failedGates.join(", "))}` : "") +
      `</span><br/>For reference. It ran on this dataset and may predate later changes to the agent or the code. ` +
      `Sign-off uses the run made in this session, shown above.`;
    box.hidden = false;
  }

  function renderEval() {
    renderLastLive();
    const r = state.evalReport;
    if (!r) return;
    const s = r.summary;
    const isLive = r.mode === "live";
    const modeText = isLive
      ? `Live agent run. Agent model ${r.agent_model}` +
        (r.reviewer_model ? `, second-model reviewer ${r.reviewer_model}` : "") +
        `, judge model ${r.judge_model}.`
      : "Offline deterministic replay. No model was run, so the agent itself is not evaluated.";
    document.getElementById("eval-run-meta").textContent =
      `${modeText} Run ${r.run_id}, ${r.started_at_utc}, ${r.elapsed_ms} ms.`;

    const det = r.gates.find((g) => g.gate === "deterministic_pass_pct");
    const agentEvaluated = r.gates.some((g) => g.live_only && g.status !== "not_evaluated");
    const chip = document.getElementById("eval-gate-chip");
    chip.textContent = `Eval: deterministic ${det.passed}/${det.ran} · agent ${agentEvaluated ? "evaluated" : "not evaluated"}`;
    chip.className = `status-chip ${r.verdict === "release_eligible" ? "pass" : ""}`;

    const banner = document.getElementById("eval-verdict-banner");
    banner.className = `decision-banner ${VERDICT_CLASS[r.verdict] || "refused"}`;
    banner.innerHTML =
      `<strong>${esc(r.verdict.replace(/_/g, " ").toUpperCase())}</strong> · ${esc(r.verdict_label)}<br/>` +
      `<span class="tabular">Checks run ${s.checks_run}/${s.checks_total} · passed ${s.checks_passed} · ` +
      `failed ${s.checks_failed} · not run ${s.checks_not_run}</span>`;

    const errorBox = document.getElementById("eval-error");
    if (r.live_error) {
      errorBox.textContent = r.live_error;
      errorBox.hidden = false;
    } else {
      errorBox.hidden = true;
    }

    const gateBody = document.querySelector("#eval-gates-table tbody");
    gateBody.innerHTML = r.gates
      .map((g) => {
        const measured = g.value === null ? "not measured" : `${g.value.toFixed(1)}%`;
        const pending = g.selected > g.ran ? ` of ${g.selected}` : "";
        return `<tr>
          <td>${esc(g.label)}<br/><span class="gate-name tabular">${esc(g.gate)}</span></td>
          <td>${g.live_only ? "Live agent run" : isLive && MIXED_GATES.includes(g.gate) ? "Offline replay + agent answers" : "Offline replay"}</td>
          <td class="num-right">${g.passed}/${g.ran}${pending}</td>
          <td class="num-right">${measured}</td>
          <td class="num-right"><input class="gate-input" type="number" min="0" max="100" step="1" data-gate="${esc(g.gate)}" value="${g.threshold}" aria-label="Threshold for ${esc(g.label)}" /></td>
          <td>${statusBadge(g.status === "not_evaluated" ? "not_run" : g.status)}</td>
        </tr>`;
      })
      .join("");

    if (!state.selectedCaseId || !r.cases.some((c) => c.id === state.selectedCaseId)) {
      const firstFailed = r.cases.find((c) => c.status === "fail");
      state.selectedCaseId = (firstFailed || r.cases[0]).id;
    }
    const caseBody = document.querySelector("#eval-cases-table tbody");
    caseBody.innerHTML = r.cases
      .map(
        (c) => `<tr class="clickable-row ${c.id === state.selectedCaseId ? "selected" : ""}" data-case="${esc(c.id)}">
          <td class="tabular"><strong>${esc(c.id)}</strong><br/><span class="gate-name">${esc(c.category)}</span></td>
          <td class="case-prompt-cell">${esc(c.prompt)}</td>
          <td>${tierCell(c.tiers, "deterministic")}</td>
          <td>${tierCell(c.tiers, "trajectory")}</td>
          <td>${tierCell(c.tiers, "response")}</td>
          <td>${tierCell(c.tiers, "rubric")}</td>
        </tr>`
      )
      .join("");
    caseBody.querySelectorAll("tr").forEach((row) => {
      row.addEventListener("click", () => {
        state.selectedCaseId = row.dataset.case;
        renderEval();
        // The case list is several screens long and the detail sits below it, so take the
        // reader to the case they chose. Only a click does this; loading the page does not.
        const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        document
          .getElementById("eval-case-detail")
          .scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
      });
    });
    renderCaseDetail();
  }

  function renderCaseDetail() {
    const r = state.evalReport;
    const c = r.cases.find((item) => item.id === state.selectedCaseId);
    const box = document.getElementById("eval-case-detail");
    if (!c) {
      box.innerHTML = "";
      return;
    }
    let traceHtml = "";
    if (c.trace) {
      const calls = c.trace.tool_calls.length
        ? c.trace.tool_calls.map((call) => `${esc(call.name)}(${esc(JSON.stringify(call.args))})`).join("<br/>")
        : "none";
      traceHtml = `
        <div class="case-block"><span class="kpi-label">Tool calls</span><div class="tabular case-mono">${calls}</div></div>
        <div class="case-block"><span class="kpi-label">Agent response</span><div class="case-response">${esc(c.trace.error || c.trace.final_text || "(empty)")}</div></div>`;
    }
    const rows = c.checks
      .map(
        (check) => `<tr>
          <td class="tabular">${esc(check.tier)}${check.mandatory ? "" : " (advisory)"}</td>
          <td>${esc(check.label)}</td>
          <td>${statusBadge(check.status)}</td>
          <td class="check-detail">${esc(check.detail)}</td>
        </tr>`
      )
      .join("");
    box.innerHTML = `
      <div class="case-block"><span class="kpi-label">Prompt · ${esc(c.id)}</span><div class="case-prompt">${esc(c.prompt)}</div></div>
      ${traceHtml}
      <div class="table-scroll">
        <table>
          <thead><tr><th>Tier</th><th>Check</th><th>Status</th><th>Detail</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  function renderSignoffPanel() {
    const policy = state.signoffPolicy;
    document.getElementById("signoff-policy-badge").textContent =
      `dual control ${policy.dual_control ? "on" : "off"} · live eval ${policy.require_live_eval ? "required" : "not required"}`;

    if (state.analysis) {
      const gov = state.analysis.governance;
      document.getElementById("signoff-hash-box").innerHTML =
        `<div><strong>Packet SHA-256:</strong> ${esc(gov.packet_sha256)}</div>` +
        `<div class="hash-line"><strong>Confirmation phrase:</strong> <span class="phrase">${esc(gov.approval_phrase)}</span></div>`;
      // The phrase is "APPROVE" and the first six characters of the packet hash, not a name.
      document.getElementById("confirm-phrase-label").textContent = `Type this phrase exactly: ${gov.approval_phrase}`;
      document.getElementById("confirm-phrase").placeholder = gov.approval_phrase;
    }

    const r = state.evalReport;
    const evidence = document.getElementById("signoff-eval-evidence");
    if (!r) {
      evidence.textContent = "No evaluation run attached. Sign-off will be refused.";
      return;
    }
    let consequence = "Sign-off will be refused.";
    if (r.verdict === "release_eligible") {
      consequence = "A release sign-off can be recorded.";
    } else if (r.verdict === "deterministic_only") {
      consequence = policy.require_live_eval
        ? "Sign-off will be refused: this server requires a live evaluation."
        : "Practice sign-off only: the agent was not evaluated in this run.";
    }
    evidence.innerHTML =
      `<div><strong>Evaluation evidence:</strong> run ${esc(r.run_id)} (${esc(r.mode)})</div>` +
      `<div class="hash-line"><strong>Verdict:</strong> ${esc(r.verdict)}. ${esc(consequence)}</div>`;
  }

  function renderLedgerEntries() {
    const box = document.getElementById("ledger-log-list");
    const items = [
      ...state.ledger.approvals.map((a) => ({ ...a, _kind: "APPROVED" })),
      ...state.ledger.refusals.map((r) => ({ ...r, _kind: "REFUSED" })),
    ].sort((a, b) => (a.timestamp_utc < b.timestamp_utc ? 1 : -1));

    if (items.length === 0) {
      box.textContent = "No sign-off decisions recorded yet.";
      return;
    }

    box.innerHTML = items
      .map((item) => {
        if (item._kind === "APPROVED") {
          return `<div class="ledger-row">
            <span class="ledger-ok">[APPROVED · ${esc(item.signoff_kind || "unspecified")}]</span> ${esc(item.timestamp_utc)} · signer=${esc(item.signer_email)} · code=${esc(item.confirm_code_6)}<br/>
            eval ${esc(item.eval_run_id || "none")} (${esc(item.eval_verdict || "none")}) · chain ${esc(String(item.prev_record_hash).slice(0, 10))}… → <strong>${esc(String(item.record_hash).slice(0, 14))}…</strong>
          </div>`;
        }
        return `<div class="ledger-row">
          <span class="ledger-no">[REFUSED]</span> ${esc(item.timestamp_utc)} · signer=${esc(item.signer_email)}<br/>
          ${esc(item.refusal_reason)}
        </div>`;
      })
      .join("");
  }

  function renderEvalAndSignoff() {
    renderEval();
    renderSignoffPanel();
    renderLedgerEntries();
  }

  function syncSlidersFromState() {
    document.getElementById("rate-slider").value = String(state.rateBps);
    document.getElementById("oil-slider").value = String(state.oilPct);
    document.getElementById("rate-val-label").textContent = `${state.rateBps >= 0 ? "+" : ""}${state.rateBps} bp`;
    document.getElementById("oil-val-label").textContent = `${state.oilPct >= 0 ? "+" : ""}${state.oilPct}%`;
  }

  // A slider drag sends one request per step. Only the answer to the latest request is shown,
  // so a slower, earlier answer cannot replace a newer one and disagree with the slider label.
  let analysisRequestSeq = 0;

  async function refreshAnalysis() {
    const seq = ++analysisRequestSeq;
    const resp = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        book_id: state.selectedBook,
        scenario_id: state.selectedScenarioId,
        rate_bps: String(state.rateBps),
        oil_pct: String(state.oilPct),
      }),
    });
    const data = await resp.json();
    if (seq !== analysisRequestSeq) return;
    state.analysis = data.analysis;
    state.formulaGrid = data.formula_grid;
    state.watcher = data.watcher;
    renderAll();
  }

  // ---- Live Google Sheet: an optional fifth tab, shown only when the server has a Sheet configured.

  const signed = (value) => `${Number(value) >= 0 ? "+" : ""}${value}`;

  function sheetBanner(kind, html) {
    document.getElementById("gsheet-feedback").innerHTML = `<div class="decision-banner ${kind}">${html}</div>`;
  }

  function reconciliationHtml(rec, against) {
    const parts = [`${rec.rows_matching} of ${rec.rows_checked} rows match ${against}`];
    if (rec.edited_input_count) {
      parts.push(`${rec.edited_input_count} input cell${rec.edited_input_count === 1 ? " no longer equals" : "s no longer equal"} the filing data`);
    }
    if (rec.mismatch_count) {
      parts.push(`${rec.mismatch_count} computed cell${rec.mismatch_count === 1 ? " differs" : "s differ"} from the engine`);
    }
    const items = (rec.edited_inputs || [])
      .map((e) => `<li>${esc(e.ticker)} ${esc(e.cell)}: the Sheet has ${esc(e.sheet_value)}, the filing data has ${esc(e.filing_value)}</li>`)
      .concat(
        (rec.mismatches || []).map(
          (m) => `<li>${esc(m.ticker)} ${esc(m.cell)}: the Sheet has ${esc(m.sheet_value)}, the engine has ${esc(m.engine_value)}</li>`
        )
      );
    return `${esc(parts.join("; "))}.${items.length ? `<ul class="gsheet-findings">${items.join("")}</ul>` : ""}`;
  }

  function showSheetFrame(url) {
    const frame = document.getElementById("gsheet-frame");
    if (url && frame.dataset.shown !== url) {
      frame.dataset.shown = url;
      frame.src = url;
    }
  }

  async function initSheetPanel() {
    let status;
    try {
      status = await (await fetch("/api/sheet")).json();
    } catch (err) {
      return;
    }
    if (!status.configured) return;
    document.getElementById("tab-btn-gsheet").hidden = false;
    const meta = document.getElementById("gsheet-meta");
    if (!status.reachable) {
      meta.textContent = status.message;
      document.getElementById("gsheet-write-btn").disabled = true;
      document.getElementById("gsheet-read-btn").disabled = true;
      document.getElementById("gsheet-reported-btn").disabled = true;
      return;
    }
    meta.textContent = `Sheet: ${status.title}. ` +
      (status.has_model ? "It holds the model from an earlier write." : "The model has not been written to it yet.");
    document.getElementById("gsheet-open-link").href = status.sheet_url;
    showSheetFrame(status.model_tab_url);
  }

  async function writeScenarioToSheet() {
    const button = document.getElementById("gsheet-write-btn");
    button.disabled = true;
    sheetBanner("neutral", "Writing the model into the Sheet, one row at a time.");
    try {
      const resp = await fetch("/api/sheet/write", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          book_id: state.selectedBook,
          scenario_id: state.selectedScenarioId,
          rate_bps: String(state.rateBps),
          oil_pct: String(state.oilPct),
        }),
      });
      const out = await resp.json();
      if (!resp.ok) {
        sheetBanner("refused", esc(out.error || "The Sheet could not be written."));
        return;
      }
      const rec = out.reconciliation;
      sheetBanner(
        rec.rows_matching === rec.rows_checked ? "approved" : "refused",
        `Wrote ${esc(out.holdings)} holdings and ${esc(out.formula_cells)} live formulas for rates ${esc(signed(out.scenario.rate_bps))} bp, ` +
          `oil ${esc(signed(out.scenario.oil_pct))}%. Google Sheets computed the formulas: ` +
          reconciliationHtml(rec, "the Python engine")
      );
      document.getElementById("gsheet-open-link").href = out.sheet_url;
      showSheetFrame(out.model_tab_url);
    } catch (err) {
      sheetBanner("refused", `The Sheet could not be written: ${esc(err)}`);
    } finally {
      button.disabled = false;
    }
  }

  function reportedCheckHtml(rec) {
    const head = `${rec.cells_matching} of ${rec.cells_checked} cells match the figures computed in Python`;
    const items = (rec.mismatches || []).map(
      (m) => `<li>${esc(m.cell)}: the Sheet has ${esc(m.sheet_value)}, Python has ${esc(m.python_value)}</li>`
    );
    return `${esc(head)}.${items.length ? `<ul class="gsheet-findings">${items.join("")}</ul>` : ""}`;
  }

  async function writeReportedToSheet() {
    const button = document.getElementById("gsheet-reported-btn");
    const tickers = document.getElementById("gsheet-tickers").value.split(/[\s,;]+/).filter(Boolean);
    const post = (body) =>
      fetch("/api/sheet/reported", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    button.disabled = true;
    sheetBanner("neutral", "Writing the reported figures into the Sheet, one row at a time.");
    try {
      // Make sure the tabs exist and show the one the rows arrive on, before the first row is sent.
      const prepared = await post({ prepare: true });
      const tabs = await prepared.json();
      if (!prepared.ok) {
        sheetBanner("refused", esc(tabs.error || "The Sheet could not be prepared."));
        return;
      }
      const frame = document.getElementById("gsheet-frame");
      if (tabs.reported_tab_url && frame.dataset.shown !== tabs.reported_tab_url) {
        showSheetFrame(tabs.reported_tab_url);
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
      const resp = await post({ tickers });
      const out = await resp.json();
      if (!resp.ok) {
        sheetBanner("refused", esc(out.error || "The reported figures could not be written."));
        return;
      }
      const rec = out.reconciliation;
      const missing = (out.not_available || []).map((c) => `${c.ticker}: ${c.message}`);
      sheetBanner(
        rec.mismatch_count === 0 ? "approved" : "refused",
        `Wrote the reported figures of ${esc(out.companies.join(", "))}: ${esc(out.company_year_rows)} company-year rows and ` +
          `${esc(out.formula_cells)} live formulas. Google Sheets computed the formulas: ` +
          reportedCheckHtml(rec) +
          (missing.length ? ` Not written: ${esc(missing.join(" "))}` : "")
      );
      document.getElementById("gsheet-open-link").href = out.sheet_url;
      showSheetFrame(out.comparison_tab_url);
    } catch (err) {
      sheetBanner("refused", `The reported figures could not be written: ${esc(err)}`);
    } finally {
      button.disabled = false;
    }
  }

  async function readScenarioFromSheet() {
    const button = document.getElementById("gsheet-read-btn");
    button.disabled = true;
    try {
      const resp = await fetch("/api/sheet/read", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      const out = await resp.json();
      if (!resp.ok) {
        sheetBanner("refused", esc(out.error || "The Sheet could not be read."));
        return;
      }
      // An answer to an earlier slider request must not replace what was read from the Sheet.
      analysisRequestSeq += 1;
      const sheet = out.sheet;
      state.selectedBook = sheet.book_id;
      state.selectedScenarioId = null;
      state.rateBps = Number(sheet.scenario.rate_bps);
      state.oilPct = Number(sheet.scenario.oil_pct);
      state.analysis = out.analysis;
      state.formulaGrid = out.formula_grid;
      state.watcher = out.watcher;
      const bookSelect = document.getElementById("book-select");
      if (bookSelect) bookSelect.value = state.selectedBook;
      syncSlidersFromState();
      renderAll();
      const rec = sheet.reconciliation;
      sheetBanner(
        rec.rows_matching === rec.rows_checked ? "approved" : "refused",
        `Read from the Sheet: rates ${esc(signed(sheet.scenario.rate_bps))} bp, oil ${esc(signed(sheet.scenario.oil_pct))}%. ` +
          `This page now shows that scenario, computed in Python. ` +
          reconciliationHtml(rec, "the filing data and the engine")
      );
    } catch (err) {
      sheetBanner("refused", `The Sheet could not be read: ${esc(err)}`);
    } finally {
      button.disabled = false;
    }
  }

  function renderAll() {
    renderDataNotice();
    renderKpis();
    renderScenarioWheel();
    renderScenarioPresets();
    renderHoldingsTable();
    renderContagionGraph();
    renderCitationInspector();
    renderFormulaSheetAndWatcher();
    renderEvalAndSignoff();
  }

  async function init() {
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    });

    const resp = await fetch("/api/bootstrap");
    const boot = await resp.json();
    state.books = boot.books;
    state.scenarios = boot.scenarios;
    state.analysis = boot.analysis;
    state.formulaGrid = boot.formula_grid;
    state.watcher = boot.watcher;
    state.evalReport = boot.eval_report;
    state.ledger = boot.ledger;
    state.metadata = boot.metadata;
    state.signoffPolicy = boot.signoff_policy;
    state.lastLiveReport = boot.last_live_report;

    const bookSelect = document.getElementById("book-select");
    state.books.forEach((b) => {
      const opt = document.createElement("option");
      opt.value = b.id;
      opt.textContent = b.label;
      bookSelect.appendChild(opt);
    });
    bookSelect.value = state.selectedBook;
    bookSelect.addEventListener("change", () => {
      state.selectedBook = bookSelect.value;
      state.selectedEdgeId = null;
      refreshAnalysis();
    });

    const rateSlider = document.getElementById("rate-slider");
    const oilSlider = document.getElementById("oil-slider");
    const onDial = () => {
      state.selectedScenarioId = null;
      state.rateBps = Number(rateSlider.value);
      state.oilPct = Number(oilSlider.value);
      syncSlidersFromState();
      refreshAnalysis();
    };
    rateSlider.addEventListener("input", onDial);
    oilSlider.addEventListener("input", onDial);

    document.getElementById("spin-wheel-btn").addEventListener("click", () => {
      const idx = (state.scenarios.findIndex((s) => s.id === state.selectedScenarioId) + 1) % state.scenarios.length;
      const next = state.scenarios[idx];
      state.selectedScenarioId = next.id;
      state.rateBps = Number(next.rate_bps);
      state.oilPct = Number(next.oil_pct);
      syncSlidersFromState();
      refreshAnalysis();
    });

    document.getElementById("toggle-gql-btn").addEventListener("click", (ev) => {
      const drawer = document.getElementById("gql-drawer");
      const hidden = drawer.style.display === "none";
      drawer.style.display = hidden ? "block" : "none";
      ev.currentTarget.textContent = hidden ? "Hide BigQuery GQL" : "Show BigQuery GQL";
    });

    const showEvalError = (message) => {
      const box = document.getElementById("eval-error");
      box.textContent = message;
      box.hidden = false;
    };

    const sleepMs = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

    const runEval = async (mode) => {
      document.getElementById("eval-error").hidden = true;
      const gates = {};
      document.querySelectorAll(".gate-input").forEach((input) => {
        gates[input.dataset.gate] = Number(input.value);
      });
      const offlineBtn = document.getElementById("run-eval-offline-btn");
      const liveBtn = document.getElementById("run-eval-live-btn");
      offlineBtn.disabled = true;
      liveBtn.disabled = true;
      const active = mode === "live" ? liveBtn : offlineBtn;
      const idleLabel = active.textContent;
      active.textContent = mode === "live" ? "Starting live eval…" : "Running…";
      try {
        if (mode !== "live") {
          const evalResp = await fetch("/api/eval", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode, gates }),
          });
          const out = await evalResp.json();
          if (!evalResp.ok) {
            showEvalError(out.error || "The evaluation request failed.");
            return;
          }
          state.evalReport = out;
          renderEvalAndSignoff();
          return;
        }

        const startResp = await fetch("/api/eval/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode, gates }),
        });
        const started = await startResp.json();
        if (!startResp.ok) {
          showEvalError(started.error || "Could not start live evaluation.");
          return;
        }

        let consecutivePollErrors = 0;
        while (true) {
          await sleepMs(2000);
          let statusOut;
          try {
            const pollResp = await fetch(`/api/eval/status?job_id=${encodeURIComponent(started.job_id || "")}`, {
              cache: "no-store",
            });
            statusOut = await pollResp.json();
            if (!pollResp.ok) {
              showEvalError(statusOut.error || "Evaluation status check failed.");
              return;
            }
            consecutivePollErrors = 0;
          } catch (pollErr) {
            consecutivePollErrors += 1;
            if (consecutivePollErrors >= 6) {
              throw pollErr;
            }
            continue;
          }

          const done = Number(statusOut.completed_cases || 0);
          const total = Number(statusOut.total_cases || 0);
          if (statusOut.running) {
            if (statusOut.phase === "grading") {
              active.textContent = `Grading rubric (${total}/${total})…`;
            } else if (total > 0) {
              active.textContent = `Running agent (${done}/${total})…`;
            } else {
              active.textContent = "Running agent…";
            }
            continue;
          }

          if (statusOut.error) {
            showEvalError(statusOut.error);
            return;
          }
          if (statusOut.report) {
            state.evalReport = statusOut.report;
            if (statusOut.report.mode === "live") state.lastLiveReport = statusOut.report;
            renderEvalAndSignoff();
          }
          return;
        }
      } catch (err) {
        showEvalError(`The evaluation request failed: ${err}`);
      } finally {
        active.textContent = idleLabel;
        offlineBtn.disabled = false;
        liveBtn.disabled = false;
      }
    };

    document.getElementById("run-eval-offline-btn").addEventListener("click", () => runEval("offline"));
    document.getElementById("run-eval-live-btn").addEventListener("click", () => runEval("live"));

    const submitSignoff = async (overrideSigner) => {
      const payload = {
        book_id: state.selectedBook,
        scenario_id: state.selectedScenarioId,
        rate_bps: String(state.rateBps),
        oil_pct: String(state.oilPct),
        confirmation_text: document.getElementById("confirm-phrase").value,
        maker_email: document.getElementById("maker-email").value,
        signer_email: overrideSigner || document.getElementById("signer-email").value,
        reason_code: document.getElementById("reason-code").value,
        notes: document.getElementById("reviewer-notes").value,
      };
      const fb = document.getElementById("signoff-feedback");
      try {
        const res = await fetch("/api/signoff", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const out = await res.json();
        if (!res.ok) {
          fb.innerHTML = `<div class="decision-banner refused">${esc(out.error || "The sign-off request failed.")}</div>`;
          return;
        }
        state.ledger = out.ledger;
        if (out.decision.status === "approved") {
          fb.innerHTML = `<div class="decision-banner approved">Recorded as a ${esc(out.decision.signoff_kind)} sign-off ` +
            `(record_hash ${esc(out.decision.record_hash.slice(0, 16))}…).</div>`;
        } else {
          fb.innerHTML = `<div class="decision-banner refused">Refused: ${esc(out.decision.refusal_reason)}</div>`;
        }
        renderLedgerEntries();
      } catch (err) {
        fb.innerHTML = `<div class="decision-banner refused">The sign-off request failed: ${esc(err)}</div>`;
      }
    };

    document.getElementById("submit-signoff-btn").addEventListener("click", () => submitSignoff());
    document.getElementById("test-self-approve-btn").addEventListener("click", () =>
      submitSignoff(document.getElementById("maker-email").value)
    );

    document.getElementById("gsheet-write-btn").addEventListener("click", writeScenarioToSheet);
    document.getElementById("gsheet-read-btn").addEventListener("click", readScenarioFromSheet);
    document.getElementById("gsheet-reported-btn").addEventListener("click", writeReportedToSheet);

    syncSlidersFromState();
    renderAll();
    initSheetPanel();

    const urlParams = new URLSearchParams(window.location.search);
    const initialTab = urlParams.get("tab");
    if (initialTab && ["portfolio", "graph", "sheet", "eval", "gsheet"].includes(initialTab)) {
      switchTab(initialTab);
    }
    if (urlParams.get("gql") === "1") {
      const drawer = document.getElementById("gql-drawer");
      drawer.style.display = "block";
      document.getElementById("toggle-gql-btn").textContent = "Hide BigQuery GQL";
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
