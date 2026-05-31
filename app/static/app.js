// Indian number formatting: use L/Cr for large numbers for readability
const fmtINR = (n) => {
  if (n == null || isNaN(n)) return "—";
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1e7) return sign + "₹" + (abs / 1e7).toFixed(2) + "Cr";
  if (abs >= 1e5) return sign + "₹" + (abs / 1e5).toFixed(2) + "L";
  return (n < 0 ? "-₹" : "₹") + abs.toLocaleString("en-IN", { maximumFractionDigits: 2 });
};
const fmtPct = (n) => (n == null || isNaN(n) ? "—" : (n * 100).toFixed(2) + "%");
const fmtQty = (n) => (n == null ? "—" : Number(n).toLocaleString("en-IN", { maximumFractionDigits: 4 }));
const cls = (n) => (n == null ? "" : n > 0 ? "pos" : n < 0 ? "neg" : "");

// Global — used in multiple places including error handlers
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

let chart = null;

// ---- FI benchmark rates editor ----
async function openFiRatesEditor() {
  const rates = await api("/api/fi/rates");
  const sav = prompt(`Savings account rate % (currently ${rates.savings_rate}%)\nSBI/HDFC/ICICI typically 2.7–3.5%:`, rates.savings_rate);
  if (sav === null) return;
  const fd  = prompt(`Standard 1-year FD rate % (currently ${rates.std_fd_rate}%)\nSBI ~6.8%, HDFC ~7%:`, rates.std_fd_rate);
  if (fd === null) return;
  const inf = prompt(`CPI inflation rate % (currently ${rates.inflation_rate}%)\nRBI target ~4%, recent avg ~4.5%:`, rates.inflation_rate);
  if (inf === null) return;
  try {
    await api("/api/fi/rates", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ savings_rate: parseFloat(sav), std_fd_rate: parseFloat(fd), inflation_rate: parseFloat(inf) }),
    });
    await Promise.all([loadFiCharts(), loadXirrAnalysis(document.getElementById("xirr-period-select").value)]);
  } catch (e) { alert("Failed: " + e.message); }
}

// ---- Fixed Income (FD / RD) ----

const FI_TYPE_LABELS = {
  FD_CUM:     "FD Cumulative",
  FD_NON_CUM: "FD Non-Cumulative",
  RD:         "Recurring Deposit",
};

let fiGrowthChart  = null;
let fiCashflowChart = null;
let fiFyChart = null;

async function loadFiCharts() {
  const section = document.getElementById("fi-charts-section");
  if (!section) return;
  section.style.display = activeSegment === "FI" ? "" : "none";
  if (activeSegment !== "FI") return;

  try {
    const d = await api("/api/fi/chart-data");

    // 1. Maturity Timeline (custom HTML bars)
    const tl = document.getElementById("fi-timeline");
    if (!d.maturity_timeline.length) {
      tl.innerHTML = `<div class="muted small">No FD/RD entries yet.</div>`;
    } else {
      // Find overall date range for scaling bars
      const allDates = d.maturity_timeline.flatMap(r => [new Date(r.start), new Date(r.end)]);
      const minD = new Date(Math.min(...allDates));
      const maxD = new Date(Math.max(...allDates));
      const span = maxD - minD || 1;

      tl.innerHTML = d.maturity_timeline.map(r => {
        const s = new Date(r.start), e = new Date(r.end), now = new Date();
        const left  = Math.max(0, (s - minD) / span * 100);
        const width = Math.min(100 - left, (e - s) / span * 100);
        const dtmText = r.days_to_maturity < 0
          ? `matured ${Math.abs(r.days_to_maturity)}d ago`
          : `${r.days_to_maturity}d`;
        return `<div class="fi-timeline-item">
          <div class="fi-timeline-label" title="${r.label}">${r.label}</div>
          <div class="fi-timeline-track">
            <div class="fi-timeline-bar ${r.status}" style="left:${left.toFixed(1)}%;width:${width.toFixed(1)}%"></div>
          </div>
          <div class="fi-timeline-value">${fmtINR(r.maturity_value)}<br><span class="muted" style="font-size:10px">${dtmText}</span></div>
        </div>`;
      }).join("");
    }

    // 1b. Growth curve (FI value vs savings vs inflation)
    const gc = d.growth_curve;
    const subtitle = document.getElementById("fi-curve-subtitle");
    if (subtitle) subtitle.textContent =
      `vs ${gc.savings_rate}% savings · ${gc.std_fd_rate}% std FD · ${gc.inflation_rate}% inflation`;

    const gcCtx = document.getElementById("fi-growth-chart").getContext("2d");
    if (fiGrowthChart) fiGrowthChart.destroy();
    fiGrowthChart = new Chart(gcCtx, {
      type: "line",
      data: {
        labels: gc.labels,
        datasets: [
          { label: "Your FI Portfolio",             data: gc.fi,       borderColor: "#58a6ff", backgroundColor: "transparent", borderWidth: 2.5, pointRadius: 0, tension: 0.1, spanGaps: true },
          { label: `Std FD (${gc.std_fd_rate}%)`,   data: gc.std_fd,   borderColor: "#3fb950", backgroundColor: "transparent", borderWidth: 1.5, borderDash: [6,3], pointRadius: 0, tension: 0.1, spanGaps: true },
          { label: `Savings (${gc.savings_rate}%)`, data: gc.savings,  borderColor: "#d29922", backgroundColor: "transparent", borderWidth: 1.5, borderDash: [4,4], pointRadius: 0, tension: 0.1, spanGaps: true },
          { label: `Inflation (${gc.inflation_rate}%)`, data: gc.inflation, borderColor: "#f85149", backgroundColor: "transparent", borderWidth: 1.5, borderDash: [2,3], pointRadius: 0, tension: 0.1, spanGaps: true },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { labels: { color: "#e6edf3" } },
          tooltip: { callbacks: { label: c => `${c.dataset.label}: ${fmtINR(c.parsed.y)}` } },
        },
        scales: {
          x: { ticks: { color: "#8b949e", maxTicksLimit: 10 }, grid: { color: "#2a3038" } },
          y: { ticks: { color: "#8b949e", callback: v => fmtINR(v) }, grid: { color: "#2a3038" } },
        },
      },
    });

    // 2. Cashflow Forecast bar chart
    const cfCtx = document.getElementById("fi-cashflow-chart").getContext("2d");
    if (fiCashflowChart) fiCashflowChart.destroy();
    fiCashflowChart = new Chart(cfCtx, {
      type: "bar",
      data: {
        labels: d.cashflow_forecast.labels,
        datasets: [{ label: "Expected inflow", data: d.cashflow_forecast.values,
          backgroundColor: "rgba(88,166,255,0.6)", borderColor: "rgba(88,166,255,1)", borderWidth: 1 }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: c => fmtINR(c.parsed.y) } } },
        scales: {
          x: { ticks: { color: "#8b949e", maxTicksLimit: 8 }, grid: { color: "#2a3038" } },
          y: { ticks: { color: "#8b949e", callback: v => fmtINR(v) }, grid: { color: "#2a3038" } },
        },
      },
    });

    // 3. FY Interest income bar chart
    const fyCtx = document.getElementById("fi-fy-chart").getContext("2d");
    if (fiFyChart) fiFyChart.destroy();
    fiFyChart = new Chart(fyCtx, {
      type: "bar",
      data: {
        labels: d.fy_interest.map(r => r.fy),
        datasets: [{ label: "Interest income", data: d.fy_interest.map(r => r.interest),
          backgroundColor: "rgba(63,185,80,0.6)", borderColor: "rgba(63,185,80,1)", borderWidth: 1 }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: c => fmtINR(c.parsed.y) + " (taxable)" } } },
        scales: {
          x: { ticks: { color: "#8b949e" }, grid: { color: "#2a3038" } },
          y: { ticks: { color: "#8b949e", callback: v => fmtINR(v) }, grid: { color: "#2a3038" } },
        },
      },
    });
  } catch (e) {
    console.error("FI charts error:", e);
  }
}

async function loadFiHoldings() {
  const section = document.getElementById("fi-section");
  if (!section) return;

  // Show FI holdings + charts sections only when FI tab is active
  section.style.display = activeSegment === "FI" ? "" : "none";
  const chartsSection = document.getElementById("fi-charts-section");
  if (chartsSection) chartsSection.style.display = activeSegment === "FI" ? "" : "none";
  if (activeSegment !== "FI") return;

  try {
    const [records, summary] = await Promise.all([
      api("/api/fi"),
      api("/api/fi/summary"),
    ]);

    // Summary mini-cards
    const sc = document.getElementById("fi-summary-cards");
    sc.innerHTML = [
      `<div class="card"><h3>Invested</h3><div class="value">${fmtINR(summary.total_invested)}</div></div>`,
      `<div class="card"><h3>Current Value</h3><div class="value">${fmtINR(summary.total_current)}</div></div>`,
      `<div class="card"><h3>Interest Earned</h3><div class="value ${cls(summary.total_interest_earned)}">${fmtINR(summary.total_interest_earned)}</div></div>`,
      `<div class="card"><h3>This FY Interest</h3><div class="value pos">${fmtINR(summary.total_fy_interest)}</div><div class="sub">taxable as income</div></div>`,
      summary.fi_xirr != null ? `<div class="card"><h3>FI XIRR</h3><div class="value ${cls(summary.fi_xirr)}">${fmtPct(summary.fi_xirr)}</div></div>` : "",
    ].join("");

    // TDS warning
    if (summary.tds_warnings && summary.tds_warnings.length) {
      const warn = summary.tds_warnings.map(w =>
        `<strong>${w.bank}</strong>: ₹${w.fy_interest.toFixed(0)} FY interest → est. TDS ₹${w.tds.toFixed(0)}`
      ).join("; ");
      sc.innerHTML += `<div class="card" style="border-color:rgba(210,153,34,.5);grid-column:1/-1"><h3>⚠ TDS Warning</h3><div class="sub">${warn}</div></div>`;
    }

    // Table
    const tbody = document.querySelector("#fi-table tbody");
    tbody.innerHTML = "";
    for (const r of records) {
      const badge = `<span class="fi-badge ${r.status}">${r.status}</span>`;
      const tds   = r.tds_applicable ? `<span class="tds-warn">⚠ TDS</span>` : "—";
      const typeLabel = FI_TYPE_LABELS[r.fi_type] || r.fi_type;
      const dtm   = r.days_to_maturity >= 0
        ? `${r.days_to_maturity}d left`
        : `Matured ${Math.abs(r.days_to_maturity)}d ago`;
      tbody.innerHTML += `
        <tr data-id="${r.id}">
          <td><strong>${r.bank}</strong>${r.account_no ? `<div class="muted" style="font-size:11px">${r.account_no}</div>` : ""}</td>
          <td class="muted">${typeLabel}</td>
          <td class="num">${fmtINR(r.amount)}</td>
          <td class="num">${r.interest_rate}%</td>
          <td><div style="font-size:12px">${r.start_date} → ${r.maturity_date}</div><div class="muted" style="font-size:11px">${dtm}</div></td>
          <td class="num">${fmtINR(r.current_value)}</td>
          <td class="num">${fmtINR(r.maturity_value)}</td>
          <td class="num pos">${fmtINR(r.interest_this_fy)}</td>
          <td>${tds}</td>
          <td>${badge}</td>
          <td><button class="edit-btn fi-edit-btn" data-id="${r.id}">edit</button><button class="delete-btn fi-del-btn" data-id="${r.id}">delete</button></td>
        </tr>`;
    }
    if (!tbody.innerHTML) {
      tbody.innerHTML = `<tr><td colspan="11" class="muted" style="text-align:center;padding:20px;">No FDs or RDs yet — click "+ Add FD / RD" to get started.</td></tr>`;
    }
  } catch (e) {
    console.error("FI load error:", e);
  }
}

// Type select: show/hide payout frequency and update label
document.getElementById("fi-type-select")?.addEventListener("change", (e) => {
  const isNonCum = e.target.value === "FD_NON_CUM";
  const isRD     = e.target.value === "RD";
  document.getElementById("fi-payout-row").style.display  = isNonCum ? "" : "none";
  document.getElementById("fi-initial-row").style.display = isRD     ? "" : "none";
  document.getElementById("fi-amount-label").firstChild.textContent =
    isRD ? "Monthly instalment (₹)" : "Principal (₹)";
});

const fiDialog = document.getElementById("fi-dialog");
const fiForm   = document.getElementById("fi-form");

document.getElementById("btn-fi-add")?.addEventListener("click", () => {
  fiForm.reset();
  fiForm.querySelector('input[name="id"]').value = "";
  document.getElementById("fi-form-title").textContent = "Add Fixed Deposit / RD";
  fiForm.querySelector('input[name="start_date"]').valueAsDate = new Date();
  document.getElementById("fi-type-select").dispatchEvent(new Event("change"));
  fiDialog.showModal();
});

document.getElementById("fi-cancel")?.addEventListener("click", (e) => {
  e.preventDefault();
  fiDialog.close();
});

fiForm?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(fiForm);
  const id = fd.get("id");
  const payload = {
    fi_type:          fd.get("fi_type"),
    bank:             fd.get("bank"),
    account_no:       fd.get("account_no") || null,
    amount:           parseFloat(fd.get("amount")),
    start_date:       fd.get("start_date"),
    maturity_date:    fd.get("maturity_date"),
    interest_rate:    parseFloat(fd.get("interest_rate")),
    compounding:      fd.get("compounding"),
    payout_frequency: fd.get("fi_type") === "FD_NON_CUM" ? fd.get("payout_frequency") : null,
    initial_deposit:  fd.get("fi_type") === "RD" ? parseFloat(fd.get("initial_deposit") || 0) : 0,
    is_tax_saver:     fd.get("is_tax_saver") === "on",
    notes:            fd.get("notes") || null,
  };
  const url    = id ? `/api/fi/${id}` : "/api/fi";
  const method = id ? "PATCH" : "POST";
  try {
    await api(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    fiDialog.close();
    await loadFiHoldings();
  } catch (err) {
    alert("Save failed: " + err.message);
  }
});

document.querySelector("#fi-table tbody")?.addEventListener("click", async (e) => {
  const id = e.target.dataset.id;
  if (!id) return;
  if (e.target.classList.contains("fi-del-btn")) {
    if (!confirm("Delete this FD/RD?")) return;
    try {
      await api(`/api/fi/${id}`, { method: "DELETE" });
      await loadFiHoldings();
    } catch (err) { alert("Delete failed: " + err.message); }
  } else if (e.target.classList.contains("fi-edit-btn")) {
    const records = await api("/api/fi");
    const r = records.find(x => x.id == id);
    if (!r) return;
    fiForm.reset();
    fiForm.querySelector('input[name="id"]').value = r.id;
    fiForm.querySelector('select[name="fi_type"]').value = r.fi_type;
    document.getElementById("fi-type-select").dispatchEvent(new Event("change"));
    fiForm.querySelector('input[name="bank"]').value = r.bank;
    fiForm.querySelector('input[name="account_no"]').value = r.account_no || "";
    fiForm.querySelector('input[name="amount"]').value = r.amount;
    fiForm.querySelector('input[name="start_date"]').value = r.start_date;
    fiForm.querySelector('input[name="maturity_date"]').value = r.maturity_date;
    fiForm.querySelector('input[name="interest_rate"]').value = r.interest_rate;
    fiForm.querySelector('select[name="compounding"]').value = r.compounding;
    if (r.payout_frequency) fiForm.querySelector('select[name="payout_frequency"]').value = r.payout_frequency;
    fiForm.querySelector('input[name="initial_deposit"]').value = r.initial_deposit || 0;
    fiForm.querySelector('input[name="is_tax_saver"]').checked = r.is_tax_saver;
    fiForm.querySelector('input[name="notes"]').value = r.notes || "";
    document.getElementById("fi-form-title").textContent = "Edit FD / RD";
    fiDialog.showModal();
  }
});

// ---- segment filter (All | Equities | Mutual Funds) ----
let activeSegment = "all";  // "all" | "EQ" | "MF"

function segParam() {
  return activeSegment === "all" ? "" : `?segment=${activeSegment}`;
}
function segQS(existing = "") {
  if (activeSegment === "all") return existing;
  const sep = existing.includes("?") ? "&" : "?";
  return existing + sep + `segment=${activeSegment}`;
}

document.querySelectorAll(".seg-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".seg-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    activeSegment = btn.dataset.seg;

    // Show/hide folio column in holdings table based on segment
    const folioTh = document.getElementById("th-folio");
    const symTh   = document.getElementById("th-symbol");
    if (folioTh) folioTh.style.display = activeSegment === "MF" ? "" : "none";
    if (symTh)   symTh.textContent     = activeSegment === "MF" ? "Scheme" : "Symbol";

    const isFI = activeSegment === "FI";

    // On FI tab: hide equity-specific panels (they show zeros or irrelevant data)
    const equityOnlyPanels = ["xirr", "realized", "chart", "holdings", "ca", "aliases", "fi-charts"];
    equityOnlyPanels.forEach(sec => {
      const el = document.querySelector(`[data-section="${sec}"]`);
      if (el) el.style.display = isFI ? "none" : "";
    });
    // Also hide the generic summary cards — FI section has its own mini-cards
    const summarySection = document.getElementById("summary-cards");
    if (summarySection) summarySection.style.display = isFI ? "none" : "";

    // On tab switch: skip management panels (transactions/CAs/aliases don't change)
    refreshAll(true);
  });
});

// ---- collapsible sections ----
// State stored in localStorage so preferences survive refresh.
// HTML marks open sections with ▲ button and collapsed ones with ▼.
function initCollapse() {
  document.querySelectorAll(".collapse-btn").forEach((btn) => {
    const id = btn.dataset.target;
    const body = document.getElementById("body-" + id);
    if (!body) return;

    // Restore saved state (default: whatever the HTML says)
    const saved = localStorage.getItem("collapse-" + id);
    if (saved === "collapsed" && !body.classList.contains("collapsed")) {
      body.classList.add("collapsed");
      btn.textContent = "▼";
    } else if (saved === "open" && body.classList.contains("collapsed")) {
      body.classList.remove("collapsed");
      btn.textContent = "▲";
    }

    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const isCollapsed = body.classList.toggle("collapsed");
      btn.textContent = isCollapsed ? "▼" : "▲";
      localStorage.setItem("collapse-" + id, isCollapsed ? "collapsed" : "open");
    });
  });
}
initCollapse();

// ---- holdings live filter ----
let holdingsFilterText = "";
document.getElementById("holdings-filter").addEventListener("input", (e) => {
  holdingsFilterText = e.target.value.trim().toUpperCase();
  renderHoldings();
});

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${path} → ${res.status}: ${await res.text()}`);
  return res.json();
}

// --- XIRR analysis section ---

function buildXirrPeriodOptions() {
  const sel = document.getElementById("xirr-period-select");
  if (!sel) return;
  const today = new Date();
  const yr = today.getMonth() >= 3 ? today.getFullYear() : today.getFullYear() - 1;
  const opts = [
    `<option value="all">All-time</option>`,
    `<option value="1y">1 Year</option>`,
    `<option value="3y">3 Years</option>`,
    `<option value="5y">5 Years</option>`,
    `<option value="current_fy">Current FY (Apr ${yr}–Mar ${yr+1})</option>`,
  ];
  for (let i = 1; i <= 4; i++) {
    const y = yr - i;
    opts.push(`<option value="fy_${y}">FY${y}–${y+1} (Apr ${y}–Mar ${y+1})</option>`);
  }
  opts.push(`<option value="custom">Custom range…</option>`);
  sel.innerHTML = opts.join("");
}
buildXirrPeriodOptions();

async function loadXirrAnalysis(period = "all", fromDate = null, toDate = null) {
  const content = document.getElementById("xirr-content");
  const label = document.getElementById("xirr-period-label");
  const warning = document.getElementById("xirr-short-period-warning");
  const customDiv = document.getElementById("xirr-custom-inputs");

  if (period === "custom" && !fromDate) {
    customDiv.style.display = "flex";
    return;
  }
  customDiv.style.display = "none";
  content.innerHTML = `<div class="muted small">Loading…</div>`;

  let url = segQS(`/api/xirr-analysis?period=${period}`);
  if (fromDate) url = segQS(`/api/xirr-analysis?from_date=${fromDate}&to_date=${toDate || ""}`);

  try {
    const r = await api(url);
    const selEl = document.getElementById("xirr-period-select");
    label.textContent = selEl.options[selEl.selectedIndex]?.text || period;

    // Warn if period is < ~90 days
    const from = r.from_date ? new Date(r.from_date) : null;
    const to = new Date(r.to_date);
    const days = from ? (to - from) / 86400000 : 9999;
    warning.style.display = (days < 90 && from) ? "block" : "none";

    const box = (label, val, sub, klass, extra = "") =>
      `<div class="xirr-box ${extra}">
        <div class="label">${label}</div>
        <div class="xirr-val ${klass}">${fmtPct(val)}</div>
        ${sub ? `<div class="sub">${sub}</div>` : ""}
      </div>`;

    let html = "";

    // Equity + MF portfolio XIRR with market benchmarks
    html += box("Portfolio XIRR",
      r.portfolio_xirr,
      r.from_date ? `${r.from_date} → ${r.to_date}` : "equities + mutual funds",
      cls(r.portfolio_xirr), "portfolio-box");

    for (const [ticker, b] of Object.entries(r.benchmarks || {})) {
      const diff = r.portfolio_xirr != null && b.xirr != null ? r.portfolio_xirr - b.xirr : null;
      const diffStr = diff != null ? `${diff >= 0 ? "+" : ""}${(diff * 100).toFixed(2)}% vs index` : null;
      html += box(b.name, b.xirr, diffStr, cls(b.xirr));
    }

    // Fixed Income XIRR with savings / inflation benchmarks — shown on All view
    if (activeSegment === "all") {
      try {
        const [fiSum, fiRates] = await Promise.all([
          api("/api/fi/summary"),
          api("/api/fi/rates"),
        ]);
        if (fiSum.fi_xirr != null) {
          const SAV  = fiRates.savings_rate   / 100;
          const FD   = fiRates.std_fd_rate    / 100;
          const INF  = fiRates.inflation_rate / 100;
          html += `<div style="grid-column:1/-1;border-top:1px solid var(--border);padding-top:8px;margin-top:4px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">
            Fixed Income — compared against fixed-rate benchmarks
            <span style="float:right;cursor:pointer;color:var(--accent)" id="fi-rates-edit-btn" onclick="openFiRatesEditor()">edit rates</span>
          </div>`;
          html += box("FI XIRR", fiSum.fi_xirr, "all FDs and RDs", cls(fiSum.fi_xirr));
          const vsSav = fiSum.fi_xirr - SAV;
          const vsFD  = fiSum.fi_xirr - FD;
          const vsInf = fiSum.fi_xirr - INF;
          html += box(`Savings (${fiRates.savings_rate}%)`, SAV,
            `${vsSav >= 0 ? "+" : ""}${(vsSav*100).toFixed(2)}% beat`, cls(vsSav));
          html += box(`Std FD (${fiRates.std_fd_rate}%)`, FD,
            `${vsFD >= 0 ? "+" : ""}${(vsFD*100).toFixed(2)}% vs FD rate`, cls(vsFD));
          html += box(`Inflation (${fiRates.inflation_rate}%)`, INF,
            `${vsInf >= 0 ? "+" : ""}${(vsInf*100).toFixed(2)}% real return`, cls(vsInf));
        }
      } catch (_) {}
    }

    content.innerHTML = html;
  } catch (e) {
    content.innerHTML = `<div class="neg small">Failed: ${escapeHtml(e.message)}</div>`;
  }
}

document.getElementById("xirr-period-select").addEventListener("change", (e) => {
  loadXirrAnalysis(e.target.value);
});

document.getElementById("xirr-apply").addEventListener("click", async () => {
  const from = document.getElementById("xirr-from").value;
  const to = document.getElementById("xirr-to").value;
  if (!from || !to) { alert("Select both from and to dates."); return; }
  await loadXirrAnalysis("custom", from, to);
  document.getElementById("xirr-period-label").textContent = `${from} → ${to}`;
});

// --- data quality banner ---
async function loadDataQuality() {
  try {
    const dq = await api("/api/data-quality");
    const banner = document.getElementById("dq-banner");
    if (dq.orphan_count === 0) { banner.style.display = "none"; return; }
    banner.style.display = "block";
    document.getElementById("dq-message").textContent = dq.message;
    const details = document.getElementById("dq-details");
    let html = `<table class="dq-table"><thead><tr>
      <th>Symbol</th><th>Sell Date</th><th>Qty</th><th>Sell Price</th><th>Proceeds</th><th>Likely cause</th>
    </tr></thead><tbody>`;
    for (const o of dq.orphans) {
      html += `<tr>
        <td><strong>${o.symbol}</strong></td>
        <td>${o.trade_date}</td>
        <td>${o.quantity}</td>
        <td>${fmtINR(o.price)}</td>
        <td>${fmtINR(o.proceeds)}</td>
        <td class="muted">${o.reason}</td>
      </tr>`;
    }
    html += `</tbody></table>
    <p class="muted small" style="margin:8px 0 0">Fix: go to <strong>Add transaction</strong> and add a Buy for each symbol above at the IPO issue price / allotment price. The P&L will update immediately.</p>`;
    details.innerHTML = html;
    document.getElementById("dq-expand").addEventListener("click", () => {
      const open = details.style.display !== "none";
      details.style.display = open ? "none" : "block";
      document.getElementById("dq-expand").textContent = open ? "Show details" : "Hide details";
    });
  } catch (e) { /* silently skip if endpoint not ready */ }
}

// --- realized P&L panel ---

// Build FY period options dynamically so labels are always correct regardless of year
function buildPeriodOptions() {
  const sel = document.getElementById("realized-period-select");
  if (!sel) return;
  const today = new Date();
  // Indian FY starts April (month index 3). If we're in Jan–Mar, current FY started last year.
  const yr = today.getMonth() >= 3 ? today.getFullYear() : today.getFullYear() - 1;

  const opts = [
    `<option value="all">All-time</option>`,
    `<option value="current_fy">FY${yr}–${yr+1} (Apr ${yr} – Mar ${yr+1})</option>`,
  ];
  for (let i = 1; i <= 4; i++) {
    const y = yr - i;
    opts.push(`<option value="fy_${y}">FY${y}–${y+1} (Apr ${y} – Mar ${y+1})</option>`);
  }
  opts.push(`<option value="current_cy">Calendar year ${today.getFullYear()}</option>`);
  opts.push(`<option value="custom">Custom range…</option>`);
  sel.innerHTML = opts.join("");  // single assignment — avoids incremental DOM re-parsing
}
buildPeriodOptions();

async function loadRealizedPnl(period = "all") {
  const content = document.getElementById("realized-pnl-content");
  const label = document.getElementById("realized-period-label");
  const customDiv = document.getElementById("custom-range-inputs");

  if (period === "custom") {
    customDiv.style.display = "flex";
    return;
  }
  customDiv.style.display = "none";
  content.innerHTML = `<div class="muted small">Loading…</div>`;

  // Map fy_YYYY values to from/to query params
  let url = segQS(`/api/realized-pnl?period=${period}`);
  if (period.startsWith("fy_")) {
    const y = parseInt(period.slice(3));
    url = `/api/realized-pnl?from_date=${y}-04-01&to_date=${y+1}-03-31`;
  }

  try {
    const r = await api(url);
    const selEl = document.getElementById("realized-period-select");
    label.textContent = selEl.options[selEl.selectedIndex]?.text || period;

    const box = (lbl, amount, sub, klass) =>
      `<div class="realized-box">
        <div class="label">${lbl}</div>
        <div class="amount ${klass}">${fmtINR(amount)}</div>
        ${sub ? `<div class="sub">${sub}</div>` : ""}
      </div>`;

    content.innerHTML =
      box("Total Realized", r.total_realized, r.from_date ? `${r.from_date} → ${r.to_date}` : null, cls(r.total_realized)) +
      box("STCG (held < 12 mo)", r.stcg, "Taxed at 20%", cls(r.stcg)) +
      box("LTCG (held ≥ 12 mo)", r.ltcg, "₹1.25L exempt · 12.5% above", cls(r.ltcg));
  } catch (e) {
    content.innerHTML = `<div class="neg small">Failed to load: ${escapeHtml(e.message)}</div>`;
  }
}

document.getElementById("realized-period-select").addEventListener("change", (e) => {
  loadRealizedPnl(e.target.value);
});

document.getElementById("custom-apply").addEventListener("click", async () => {
  const from = document.getElementById("custom-from").value;
  const to   = document.getElementById("custom-to").value;
  if (!from || !to) { alert("Select both from and to dates."); return; }
  const content = document.getElementById("realized-pnl-content");
  const label   = document.getElementById("realized-period-label");
  content.innerHTML = `<div class="muted small">Loading…</div>`;
  try {
    const r = await api(`/api/realized-pnl?from_date=${from}&to_date=${to}`);
    label.textContent = `${from} → ${to}`;
    const box = (lbl, amount, sub, klass) =>
      `<div class="realized-box"><div class="label">${lbl}</div><div class="amount ${klass}">${fmtINR(amount)}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;
    content.innerHTML =
      box("Total Realized", r.total_realized, null, cls(r.total_realized)) +
      box("STCG (held < 12 mo)", r.stcg, "Taxed at 20%", cls(r.stcg)) +
      box("LTCG (held ≥ 12 mo)", r.ltcg, "₹1.25L exempt · 12.5% above", cls(r.ltcg));
  } catch (e) {
    content.innerHTML = `<div class="neg small">Failed to load: ${escapeHtml(e.message)}</div>`;
  }
});

async function loadSummary() {
  const s = await api(`/api/summary${segParam()}`);
  document.getElementById("as-of").textContent = "As of " + s.as_of;

  const cards = document.getElementById("summary-cards");
  cards.innerHTML = "";

  const card = (title, value, sub, klass = "") =>
    `<div class="card"><h3>${title}</h3><div class="value ${klass}">${value}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;

  cards.innerHTML += card("Invested", fmtINR(s.invested), "active holdings cost basis");
  cards.innerHTML += card("Current Value", fmtINR(s.current_value), "active holdings at market");
  // Unrealized P&L — directly comparable to Zerodha's portfolio widget
  cards.innerHTML += card(
    "Unrealized P&L",
    fmtINR(s.unrealized_pnl),
    fmtPct(s.unrealized_pnl != null && s.invested ? s.unrealized_pnl / s.invested : null) + " · active only",
    cls(s.unrealized_pnl)
  );
  // Realized P&L — from all sold positions (shown separately in Zerodha's P&L report)
  cards.innerHTML += card(
    "Realized P&L",
    fmtINR(s.realized_pnl),
    "sold positions",
    cls(s.realized_pnl)
  );
  // Total = unrealized + realized
  cards.innerHTML += card(
    "Total P&L",
    fmtINR(s.total_pnl),
    fmtPct(s.pct_return) + " · all-time",
    cls(s.total_pnl)
  );
  cards.innerHTML += card(
    "Portfolio XIRR",
    fmtPct(s.portfolio_xirr),
    "annualized, incl. dividends",
    cls(s.portfolio_xirr)
  );
  for (const [ticker, b] of Object.entries(s.benchmarks || {})) {
    cards.innerHTML += card(b.name + " XIRR", fmtPct(b.xirr), ticker, cls(b.xirr));
  }
}

// --- sortable holdings ---
let holdingsData = [];
let holdingsSort = { col: "current_value", dir: "desc" };

function sortHoldings(col) {
  if (holdingsSort.col === col) {
    holdingsSort.dir = holdingsSort.dir === "asc" ? "desc" : "asc";
  } else {
    holdingsSort.col = col;
    holdingsSort.dir = col === "symbol" ? "asc" : "desc";
  }
  renderHoldings();
}

function renderHoldings() {
  const col = holdingsSort.col;
  const dir = holdingsSort.dir;

  // Update header icons
  document.querySelectorAll("#holdings-table th.sortable").forEach((th) => {
    th.classList.remove("asc", "desc");
    if (th.dataset.col === col) th.classList.add(dir);
  });

  const sorted = [...holdingsData].sort((a, b) => {
    let va = col === "pnl" ? (a.unrealized_pnl ?? 0) + (a.realized_pnl ?? 0)
           : col === "symbol" ? (a.symbol || "")
           : (a[col] ?? (dir === "asc" ? Infinity : -Infinity));
    let vb = col === "pnl" ? (b.unrealized_pnl ?? 0) + (b.realized_pnl ?? 0)
           : col === "symbol" ? (b.symbol || "")
           : (b[col] ?? (dir === "asc" ? Infinity : -Infinity));
    if (typeof va === "string") return dir === "asc" ? va.localeCompare(vb) : vb.localeCompare(va);
    return dir === "asc" ? va - vb : vb - va;
  });

  const hideClosed = document.getElementById("hide-closed").checked;
  const tbody = document.querySelector("#holdings-table tbody");
  tbody.innerHTML = "";
  for (const r of sorted) {
    if (hideClosed && (r.quantity || 0) <= 0) continue;
    if (!hideClosed && (r.quantity || 0) <= 0 && (r.realized_pnl || 0) === 0) continue;
    if (holdingsFilterText && !r.symbol.toUpperCase().includes(holdingsFilterText)) continue;
    const pnl = (r.unrealized_pnl ?? 0) + (r.realized_pnl ?? 0);
    const nameCell = activeSegment === "MF"
      ? `<td><div style="font-size:12px;max-width:220px;white-space:normal;line-height:1.4">${r.display_name || r.symbol}</div></td>`
      : `<td>${r.display_name || r.symbol}</td>`;
    const folioCell = activeSegment === "MF"
      ? `<td class="muted" style="font-size:11px">${r.folio || "—"}</td>` : "";

    tbody.innerHTML += `
      <tr>
        ${nameCell}
        ${folioCell}
        <td>${r.segment}</td>
        <td class="num">${fmtQty(r.quantity)}</td>
        <td class="num">${fmtINR(r.avg_cost)}</td>
        <td class="num">${fmtINR(r.current_price)}</td>
        <td class="num">${fmtINR(r.invested)}</td>
        <td class="num">${fmtINR(r.current_value)}</td>
        <td class="num ${cls(pnl)}">${fmtINR(pnl)}</td>
        <td class="num ${cls(r.pct_return)}">${fmtPct(r.pct_return)}</td>
        <td class="num ${cls(r.xirr)}">${fmtPct(r.xirr)}</td>
      </tr>`;
  }
  if (!tbody.innerHTML) {
    tbody.innerHTML = `<tr><td colspan="10" class="muted" style="text-align:center;padding:20px;">No holdings yet — import your tradebook to get started.</td></tr>`;
  }
}

// wire sort clicks
document.querySelectorAll("#holdings-table th.sortable").forEach((th) => {
  th.addEventListener("click", () => sortHoldings(th.dataset.col));
});

// hide/show closed positions — no server call needed, data already in holdingsData
document.getElementById("hide-closed").addEventListener("change", renderHoldings);

async function loadHoldings() {
  holdingsData = await api(`/api/holdings${segParam()}`);
  renderHoldings();
}

async function loadTransactions() {
  const rows = await api("/api/transactions");
  const tbody = document.querySelector("#txn-table tbody");
  tbody.innerHTML = "";
  for (const t of rows) {
    tbody.innerHTML += `
      <tr data-id="${t.id}">
        <td>${t.trade_date}</td>
        <td>${t.symbol}</td>
        <td>${t.segment}</td>
        <td class="${t.trade_type === 'buy' ? 'pos' : 'neg'}">${t.trade_type}</td>
        <td class="num">${fmtQty(t.quantity)}</td>
        <td class="num">${fmtINR(t.price)}</td>
        <td class="num">${fmtINR(t.fees)}</td>
        <td>${t.source}</td>
        <td><button class="edit-btn" data-id="${t.id}">edit</button><button class="delete-btn" data-id="${t.id}">delete</button></td>
      </tr>`;
  }
  if (!tbody.innerHTML) {
    tbody.innerHTML = `<tr><td colspan="9" class="muted" style="text-align:center;padding:20px;">No transactions yet.</td></tr>`;
  }
}

async function loadEquityCurve() {
  // Show a subtle loading state on the chart container
  const container = document.querySelector(".chart-container");
  if (container) container.style.opacity = "0.4";
  const data = await api(segQS("/api/equity-curve")).finally(() => {
    if (container) container.style.opacity = "1";
  });
  if (data.base_date) {
    document.getElementById("curve-subtitle").textContent =
      `All series rebased to 100 on ${data.base_date} (your first transaction). ` +
      `A value of 200 means 2× what that investment would be worth today.`;
  }
  const ctx = document.getElementById("equity-chart").getContext("2d");
  if (chart) chart.destroy();

  const colors = ["#58a6ff", "#3fb950", "#d29922", "#f778ba", "#a371f7", "#f85149"];
  const datasets = Object.entries(data.series || {}).map(([name, vals], i) => ({
    label: name,
    data: vals,
    borderColor: name === "Portfolio" ? "#e6edf3" : colors[i % colors.length],
    backgroundColor: "transparent",
    borderWidth: name === "Portfolio" ? 2 : 1.5,
    pointRadius: 0,
    spanGaps: true,
    tension: 0.1,
  }));

  chart = new Chart(ctx, {
    type: "line",
    data: { labels: data.dates, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { color: "#e6edf3" } },
        tooltip: {
          callbacks: {
            label: (c) => `${c.dataset.label}: ${c.parsed.y == null ? "—" : c.parsed.y.toFixed(2)}`,
          },
        },
      },
      scales: {
        x: { ticks: { color: "#8b949e", maxTicksLimit: 10 }, grid: { color: "#2a3038" } },
        y: { ticks: { color: "#8b949e" }, grid: { color: "#2a3038" } },
      },
    },
  });
}

// --- CAS PDF import (Mutual Funds) ---

document.getElementById("import-cas-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  const result = document.getElementById("import-result");
  result.innerHTML = "Importing CAS PDF…";
  try {
    const r = await api("/api/import-cas", { method: "POST", body: fd });
    let msg = `CAS import: ${r.inserted} new transactions, ${r.skipped_duplicates} duplicates, ${r.schemes_found} schemes.`;
    if (r.errors && r.errors.length) msg += ` ${r.errors.length} error(s).`;
    result.innerHTML = msg;
    e.target.value = "";
    await refreshAll();
  } catch (err) {
    result.innerHTML = `<span class="neg">CAS import failed: ${escapeHtml(err.message)}</span>`;
  }
});

// --- Tradebook import (Equities) ---

document.getElementById("import-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  const result = document.getElementById("import-result");
  result.innerHTML = "Importing…";
  try {
    const r = await api("/api/import", { method: "POST", body: fd });
    const summary = `Imported ${r.inserted} new, skipped ${r.skipped_duplicates} duplicates${r.errors?.length ? `, ${r.errors.length} errors` : ""}.`;
    let html = `<div>${summary}</div>`;
    if (r.errors && r.errors.length) {
      const shown = r.errors.slice(0, 20);
      html += `<details open style="margin-top:6px;"><summary class="neg" style="cursor:pointer;">Show errors</summary><ul style="margin:6px 0 0 20px;color:var(--neg);">${shown.map(e => `<li>${escapeHtml(e)}</li>`).join("")}${r.errors.length > shown.length ? `<li>… and ${r.errors.length - shown.length} more</li>` : ""}</ul></details>`;
    }
    result.innerHTML = html;
    e.target.value = "";
    await refreshAll();
  } catch (err) {
    result.innerHTML = `<span class="neg">Import failed: ${escapeHtml(err.message)}</span>`;
  }
});

// --- transaction CRUD ---

const dialog = document.getElementById("txn-dialog");
const form = document.getElementById("txn-form");

document.getElementById("btn-add").addEventListener("click", () => {
  form.reset();
  form.querySelector('input[name="id"]').value = "";
  document.getElementById("txn-form-title").textContent = "Add transaction";
  form.querySelector('input[name="trade_date"]').valueAsDate = new Date();
  dialog.showModal();
});

document.getElementById("txn-cancel").addEventListener("click", (e) => {
  e.preventDefault();
  dialog.close();
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(form);
  const id = fd.get("id");
  const payload = {
    trade_date: fd.get("trade_date"),
    symbol: fd.get("symbol"),
    isin: fd.get("isin") || null,
    exchange: fd.get("exchange") || null,
    segment: fd.get("segment"),
    trade_type: fd.get("trade_type"),
    quantity: parseFloat(fd.get("quantity")),
    price: parseFloat(fd.get("price")),
    fees: parseFloat(fd.get("fees") || 0),
    notes: fd.get("notes") || null,
  };
  const url = id ? `/api/transactions/${id}` : "/api/transactions";
  const method = id ? "PATCH" : "POST";
  try {
    await api(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    dialog.close();
    await refreshAll();
  } catch (err) {
    alert("Save failed: " + err.message);
  }
});

document.querySelector("#txn-table tbody").addEventListener("click", async (e) => {
  const id = e.target.dataset.id;
  if (!id) return;
  if (e.target.classList.contains("delete-btn")) {
    if (!confirm("Delete this transaction?")) return;
    try {
      await api(`/api/transactions/${id}`, { method: "DELETE" });
      await refreshAll();
    } catch (err) {
      alert("Delete failed: " + err.message);
    }
  } else if (e.target.classList.contains("edit-btn")) {
    const rows = await api("/api/transactions");
    const t = rows.find((r) => r.id == id);
    if (!t) return;
    form.reset();
    form.querySelector('input[name="id"]').value = t.id;
    form.querySelector('input[name="trade_date"]').value = t.trade_date;
    form.querySelector('input[name="symbol"]').value = t.symbol;
    form.querySelector('input[name="isin"]').value = t.isin || "";
    form.querySelector('input[name="exchange"]').value = t.exchange || "";
    form.querySelector('select[name="segment"]').value = t.segment;
    form.querySelector('select[name="trade_type"]').value = t.trade_type;
    form.querySelector('input[name="quantity"]').value = t.quantity;
    form.querySelector('input[name="price"]').value = t.price;
    form.querySelector('input[name="fees"]').value = t.fees;
    form.querySelector('input[name="notes"]').value = t.notes || "";
    document.getElementById("txn-form-title").textContent = "Edit transaction";
    dialog.showModal();
  }
});

document.getElementById("btn-refresh").addEventListener("click", async () => {
  const btn = document.getElementById("btn-refresh");
  btn.disabled = true;
  btn.textContent = "Refreshing…";
  try {
    await api("/api/refresh-prices", { method: "POST" });
    await refreshAll();
  } finally {
    btn.disabled = false;
    btn.textContent = "Refresh prices";
  }
});

// --- corporate actions ---

function fmtCA(ca) {
  if (ca.action_type === "dividend") return `₹${ca.amount_per_share?.toFixed(2)} / share`;
  if (ca.ratio) {
    const r = ca.ratio;
    // Express as "N:1" (e.g. ratio=1.5 → "1.5:1", ratio=2.0 → "2:1")
    const n = Number.isInteger(r) ? r : r.toFixed(2);
    return `${n}:1`;
  }
  return "—";
}

async function loadCorporateActions() {
  const rows = await api("/api/corporate-actions");
  const tbody = document.querySelector("#ca-table tbody");
  tbody.innerHTML = "";
  for (const ca of rows) {
    tbody.innerHTML += `
      <tr data-id="${ca.id}">
        <td><strong>${ca.symbol}</strong></td>
        <td>${ca.action_type}</td>
        <td>${ca.ex_date}</td>
        <td class="num">${fmtCA(ca)}</td>
        <td class="muted">${ca.notes || ""}</td>
        <td class="muted">${ca.source}</td>
        <td>
          <button class="edit-btn ca-edit-btn" data-id="${ca.id}">edit</button>
          <button class="delete-btn ca-delete-btn" data-id="${ca.id}">delete</button>
        </td>
      </tr>`;
  }
  if (!tbody.innerHTML) {
    tbody.innerHTML = `<tr><td colspan="7" class="muted" style="text-align:center;padding:16px;">No corporate actions yet — click "Sync from yfinance" to auto-fetch splits &amp; dividends.</td></tr>`;
  }
}

// sync button
document.getElementById("btn-ca-fetch").addEventListener("click", async () => {
  const btn = document.getElementById("btn-ca-fetch");
  const result = document.getElementById("ca-sync-result");
  btn.disabled = true;
  btn.textContent = "Syncing…";
  result.textContent = "";
  try {
    const r = await api("/api/corporate-actions/auto-fetch", { method: "POST" });
    const total = Object.values(r.by_symbol || {}).reduce((s, v) => s + v.splits + v.dividends, 0);
    result.textContent = `Synced ${total} records across ${r.symbols_processed} symbols.`;
    await Promise.all([loadCorporateActions(), loadHoldings(), loadSummary()]);
  } catch (err) {
    result.textContent = "Sync failed: " + err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Sync from yfinance";
  }
});

// show/hide ratio vs amount fields based on type
const caTypeSelect = document.getElementById("ca-type-select");
caTypeSelect.addEventListener("change", () => {
  const isDiv = caTypeSelect.value === "dividend";
  document.getElementById("ca-ratio-row").style.display = isDiv ? "none" : "";
  document.getElementById("ca-div-row").style.display = isDiv ? "" : "none";
});

const caDialog = document.getElementById("ca-dialog");
const caForm = document.getElementById("ca-form");

document.getElementById("btn-ca-add").addEventListener("click", () => {
  caForm.reset();
  caForm.querySelector('input[name="id"]').value = "";
  document.getElementById("ca-form-title").textContent = "Add corporate action";
  caTypeSelect.dispatchEvent(new Event("change"));
  caDialog.showModal();
});

document.getElementById("ca-cancel").addEventListener("click", (e) => {
  e.preventDefault();
  caDialog.close();
});

caForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(caForm);
  const id = fd.get("id");
  const isDiv = fd.get("action_type") === "dividend";
  const payload = {
    symbol: fd.get("symbol"),
    isin: fd.get("isin") || null,
    action_type: fd.get("action_type"),
    ex_date: fd.get("ex_date"),
    ratio: !isDiv && fd.get("ratio") ? parseFloat(fd.get("ratio")) : null,
    amount_per_share: isDiv && fd.get("amount_per_share") ? parseFloat(fd.get("amount_per_share")) : null,
    notes: fd.get("notes") || null,
  };
  const url = id ? `/api/corporate-actions/${id}` : "/api/corporate-actions";
  const method = id ? "PATCH" : "POST";
  try {
    await api(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    caDialog.close();
    await Promise.all([loadCorporateActions(), loadHoldings(), loadSummary()]);
  } catch (err) {
    alert("Save failed: " + err.message);
  }
});

document.querySelector("#ca-table tbody").addEventListener("click", async (e) => {
  const id = e.target.dataset.id;
  if (!id) return;
  if (e.target.classList.contains("ca-delete-btn")) {
    if (!confirm("Delete this corporate action?")) return;
    try {
      await api(`/api/corporate-actions/${id}`, { method: "DELETE" });
      await Promise.all([loadCorporateActions(), loadHoldings(), loadSummary()]);
    } catch (err) {
      alert("Delete failed: " + err.message);
    }
  } else if (e.target.classList.contains("ca-edit-btn")) {
    const rows = await api("/api/corporate-actions");
    const ca = rows.find((r) => r.id == id);
    if (!ca) return;
    caForm.reset();
    caForm.querySelector('input[name="id"]').value = ca.id;
    caForm.querySelector('input[name="symbol"]').value = ca.symbol;
    caForm.querySelector('input[name="isin"]').value = ca.isin || "";
    caForm.querySelector('select[name="action_type"]').value = ca.action_type;
    caTypeSelect.dispatchEvent(new Event("change"));
    caForm.querySelector('input[name="ex_date"]').value = ca.ex_date;
    if (ca.ratio) caForm.querySelector('input[name="ratio"]').value = ca.ratio;
    if (ca.amount_per_share) caForm.querySelector('input[name="amount_per_share"]').value = ca.amount_per_share;
    caForm.querySelector('input[name="notes"]').value = ca.notes || "";
    document.getElementById("ca-form-title").textContent = "Edit corporate action";
    caDialog.showModal();
  }
});

// --- symbol aliases ---
async function loadSymbolAliases() {
  const rows = await api("/api/symbol-aliases");
  const tbody = document.querySelector("#alias-table tbody");
  tbody.innerHTML = "";
  for (const r of rows) {
    const canDelete = r.source === "user";
    tbody.innerHTML += `
      <tr>
        <td><strong>${r.old_symbol}</strong></td>
        <td>${r.new_symbol}</td>
        <td class="muted">${r.source}</td>
        <td>${canDelete ? `<button class="delete-btn alias-del-btn" data-old="${r.old_symbol}">delete</button>` : ""}</td>
      </tr>`;
  }
  if (!tbody.innerHTML) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted" style="text-align:center;padding:12px;">No custom renames yet.</td></tr>`;
  }
}

document.getElementById("btn-alias-add").addEventListener("click", async () => {
  const old = prompt("Old NSE symbol (as it appears in your transactions, e.g. GOLDETFADD):");
  if (!old) return;
  const nw = prompt(`New current NSE symbol for ${old.toUpperCase()} (e.g. GOLDADD):`);
  if (!nw) return;
  try {
    await api("/api/symbol-aliases", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ old_symbol: old, new_symbol: nw }),
    });
    await Promise.all([loadSymbolAliases(), loadHoldings(), loadSummary()]);
  } catch (err) {
    alert("Failed: " + err.message);
  }
});

document.querySelector("#alias-table tbody").addEventListener("click", async (e) => {
  if (!e.target.classList.contains("alias-del-btn")) return;
  const old = e.target.dataset.old;
  if (!confirm(`Remove alias ${old}?`)) return;
  try {
    await api(`/api/symbol-aliases/${old}`, { method: "DELETE" });
    await loadSymbolAliases();
  } catch (err) {
    alert("Failed: " + err.message);
  }
});

async function refreshAll(tabSwitch = false) {
  const realizedPeriod = document.getElementById("realized-period-select").value;
  const xirrPeriod    = document.getElementById("xirr-period-select").value;

  // Phase 1 — fast (DB-only, no price fetching): render immediately
  const phase1 = [
    loadSummary(),
    loadHoldings(),
    loadFiHoldings(),    // pure math — no API calls, always fast
    loadFiCharts(),      // pure math — maturity timeline + cashflow forecast + FY interest
    loadRealizedPnl(realizedPeriod),
    loadDataQuality(),
  ];
  // Management panels don't change on tab switch — skip them for speed
  if (!tabSwitch) {
    phase1.push(loadTransactions(), loadCorporateActions(), loadSymbolAliases());
  }
  await Promise.all(phase1);

  // Phase 2 — slow (equity curve + full XIRR): fire and forget so the page is
  // already usable. A loading indicator is shown while they compute.
  Promise.all([
    loadEquityCurve(),
    loadXirrAnalysis(xirrPeriod),
  ]).catch(console.error);
}

// Keyboard shortcuts
document.addEventListener("keydown", (e) => {
  // Ignore if typing in an input / dialog is open
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT") return;
  if (document.querySelector("dialog[open]")) return;
  if (e.key === "n" || e.key === "N") {
    // N → open Add Transaction (most common action)
    document.getElementById("btn-add").click();
  } else if (e.key === "r" || e.key === "R") {
    // R → Refresh prices
    document.getElementById("btn-refresh").click();
  } else if (e.key === "/" ) {
    // / → focus holdings filter
    e.preventDefault();
    const f = document.getElementById("holdings-filter");
    f.focus();
    // expand holdings section if collapsed
    const body = document.getElementById("body-holdings");
    if (body && body.classList.contains("collapsed")) {
      document.querySelector('[data-target="holdings"]').click();
    }
  }
});

refreshAll().catch((err) => {
  console.error(err);
  alert("Failed to load: " + err.message);
});
