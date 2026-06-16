// Privacy mode: when on, monetary amounts and quantities render as dots so the
// dashboard can be shown/screenshotted without revealing wealth. Percentages
// stay visible (the performance story). Persisted in localStorage; charts are
// blurred via CSS (a canvas can't show dots).
let privacyMode = localStorage.getItem("privacy-mode") === "on";
const MASK = "••••";
// When true, chart (re)creation is skipped — used during a privacy toggle so we
// re-render text without destroying/recreating charts (recreation can trigger a
// Chart.js resize loop that hangs the page). Existing charts stay, blurred by CSS.
let chartsPaused = false;
// When true, loaders reuse already-fetched data instead of re-hitting the network
// (used by the privacy toggle so re-rendering is instant).
let _useCache = false;

// Indian number formatting: use L/Cr for large numbers; full value below ₹1L.
const fmtINR = (n) => {
  if (n == null || isNaN(n)) return "—";
  if (privacyMode) return "₹" + MASK;
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1e7) return sign + "₹" + (abs / 1e7).toFixed(2) + "Cr";
  if (abs >= 1e5) return sign + "₹" + (abs / 1e5).toFixed(2) + "L";
  return (n < 0 ? "-₹" : "₹") + abs.toLocaleString("en-IN", { maximumFractionDigits: 2 });
};
// Compact ₹ that also shortens thousands to K (₹70.4K). Used ONLY in the Net Worth
// summary (headline + by-account breakdown) for a clean glance; detailed segment
// sections keep full values via fmtINR.
const fmtINRshort = (n) => {
  if (n == null || isNaN(n)) return "—";
  if (privacyMode) return "₹" + MASK;
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1e7) return sign + "₹" + (abs / 1e7).toFixed(2) + "Cr";
  if (abs >= 1e5) return sign + "₹" + (abs / 1e5).toFixed(2) + "L";
  if (abs >= 1e3) return sign + "₹" + (abs / 1e3).toFixed(1) + "K";
  return (n < 0 ? "-₹" : "₹") + abs.toLocaleString("en-IN", { maximumFractionDigits: 2 });
};
// USD formatter for the Global tab (amounts bypass fmtINR). Masks in privacy mode.
const fmtUSD = (n, dp = 2) => {
  if (n == null || isNaN(n)) return "—";
  if (privacyMode) return "$" + MASK;
  return "$" + Number(n).toFixed(dp);
};
const fmtPct = (n) => (n == null || isNaN(n) ? "—" : (n * 100).toFixed(2) + "%");
const fmtQty = (n) => (n == null ? "—" : privacyMode ? MASK : Number(n).toLocaleString("en-IN", { maximumFractionDigits: 4 }));
const cls = (n) => (n == null ? "" : n > 0 ? "pos" : n < 0 ? "neg" : "");

// Amount that stays compact (₹4.32L) but reveals the exact value (₹4,32,123.45)
// on hover (desktop, native title) or tap (mobile, toggles the text). Only wraps
// when fmtINR actually abbreviates (≥ ₹1L); smaller amounts are already exact.
// Returns plain fmtINR in privacy mode so the exact value is never exposed.
function amt(n) {
  if (n == null || isNaN(n) || privacyMode) return fmtINR(n);
  const short = fmtINR(n);
  const exact = (n < 0 ? "-₹" : "₹") + Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 2 });
  if (short === exact) return short;
  return `<span class="amt" title="${exact}" data-short="${short}" data-full="${exact}">${short}</span>`;
}
// Tap an .amt to toggle exact ↔ compact (for phones, which have no hover).
document.addEventListener("click", (e) => {
  const el = e.target.closest && e.target.closest(".amt");
  if (!el || !el.dataset.full) return;
  const showingFull = el.dataset.showing === "full";
  el.textContent = showingFull ? el.dataset.short : el.dataset.full;
  el.dataset.showing = showingFull ? "short" : "full";
});

// Global — used in multiple places including error handlers
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

let chart = null;

// ---- Net Worth ----
let nwDonutChart = null;
let nwProjectionChart = null;
let nwData = null;            // cached networth API response
let nwView = "account";       // "account" | "asset_class"

const CAT_LABELS = {
  equity: "Equity", debt: "Debt", gold: "Gold",
  hybrid: "Hybrid", cash: "Cash/Liquid", silver: "Silver",
  epf: "EPF", other: "Other",
};

const ACCT_COLORS = { equity: "#58a6ff", mf: "#3fb950", fi: "#d29922", epf: "#a371f7", bonds: "#f0c14b" };
const ACCT_LABELS = { equity: "Equities", mf: "Mutual Funds", fi: "Fixed Income", epf: "EPF", bonds: "Bonds/SGB" };

function renderNwDonut(nw) {
  const c = nw.current;
  const ctx = document.getElementById("nw-donut").getContext("2d");
  if (!chartsPaused && nwDonutChart) nwDonutChart.destroy();

  let labels, values, colors;
  if (nwView === "asset_class") {
    const bd = nw.by_asset_class?.breakdown || {};
    labels = Object.keys(bd).map(k => CAT_LABELS[k] || k);
    values = Object.values(bd);
    colors = Object.keys(bd).map(k => nw.by_asset_class?.colors?.[k] || "#444c56");
  } else {
    labels = Object.keys(ACCT_LABELS).filter(k => c[k] > 0).map(k => ACCT_LABELS[k]);
    values = Object.keys(ACCT_LABELS).filter(k => c[k] > 0).map(k => c[k]);
    colors = Object.keys(ACCT_LABELS).filter(k => c[k] > 0).map(k => ACCT_COLORS[k]);
  }

  nwDonutChart = chartsPaused ? nwDonutChart : new Chart(ctx, {
    type: "doughnut",
    data: {
      labels, datasets: [{ data: values, backgroundColor: colors, borderWidth: 0 }],
    },
    options: {
      responsive: false, cutout: "72%",
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => `${ctx.label}: ${fmtINR(ctx.parsed)}` } },
      },
    },
  });

  // Allocation strip
  const strip = document.getElementById("nw-alloc-strip");
  const total = c.total || 1;
  if (nwView === "asset_class") {
    const bd = nw.by_asset_class?.breakdown || {};
    const pc = nw.by_asset_class?.pct || {};
    const cl = nw.by_asset_class?.colors || {};
    strip.innerHTML = Object.entries(bd).map(([k, v]) =>
      `<div class="nw-alloc-item">
        <div class="nw-alloc-label">${CAT_LABELS[k] || k}</div>
        <div class="nw-alloc-val" style="color:${cl[k] || '#e6edf3'}">${fmtINRshort(v)}</div>
        <div class="nw-alloc-bar" style="background:${cl[k] || '#444'};width:${Math.max(pc[k]||0,2)}px"></div>
        <div class="muted small">${pc[k] || 0}%</div>
      </div>`
    ).join("");
  } else {
    strip.innerHTML = Object.entries(ACCT_LABELS)
      .filter(([k]) => c[k] > 0)
      .map(([k, label]) => {
        const pct = Math.round(c[k] / total * 100);
        return `<div class="nw-alloc-item">
          <div class="nw-alloc-label">${label}</div>
          <div class="nw-alloc-val" style="color:${ACCT_COLORS[k]}">${fmtINRshort(c[k])}</div>
          <div class="nw-alloc-bar" style="background:${ACCT_COLORS[k]};width:${Math.max(pct,2)}px"></div>
          <div class="muted small">${pct}%</div>
        </div>`;
      }).join("");
  }
}

// View toggle buttons
document.querySelectorAll(".nw-view-btn").forEach(btn => {
  btn.addEventListener("click", async () => {
    if (btn.dataset.view === "edit") { openCategoryEditor(); return; }
    document.querySelectorAll(".nw-view-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    nwView = btn.dataset.view;
    if (nwData) renderNwDonut(nwData);
  });
});

// Category editor
async function openCategoryEditor() {
  const dialog = document.getElementById("cat-dialog");
  const wrap   = document.getElementById("cat-table-wrap");
  wrap.innerHTML = `<div class="muted small">Loading…</div>`;
  dialog.showModal();
  const cats = await api("/api/instrument-categories");
  const VALID_CATS = ["equity","debt","gold","hybrid","cash","silver","epf","other"];
  wrap.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:13px">
    <thead><tr>
      <th style="text-align:left;padding:6px;border-bottom:1px solid #2a3038">Symbol</th>
      <th style="text-align:left;padding:6px;border-bottom:1px solid #2a3038">Name</th>
      <th style="text-align:left;padding:6px;border-bottom:1px solid #2a3038">Category</th>
    </tr></thead>
    <tbody>
    ${cats.map(c => `<tr data-isin="${c.isin}">
      <td style="padding:5px 6px"><strong>${c.symbol}</strong></td>
      <td style="padding:5px 6px;color:#8b949e;font-size:12px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${c.name}</td>
      <td style="padding:5px 6px">
        <select class="cat-select" data-isin="${c.isin}" style="background:#0d1117;color:#e6edf3;border:1px solid #2a3038;padding:3px 6px;border-radius:4px;font-size:12px">
          ${VALID_CATS.map(v => `<option value="${v}" ${c.category===v?'selected':''}>${CAT_LABELS[v]||v}</option>`).join('')}
        </select>
        ${c.category !== c.auto_detected ? `<span class="muted small" style="margin-left:4px">auto: ${c.auto_detected}</span>` : ''}
      </td>
    </tr>`).join('')}
    </tbody>
  </table>`;

  // Save on change
  wrap.querySelectorAll(".cat-select").forEach(sel => {
    sel.addEventListener("change", async () => {
      await api(`/api/instrument-categories/${sel.dataset.isin}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ category: sel.value }),
      });
      if (nwData) { nwData = await api("/api/networth"); renderNwDonut(nwData); }
    });
  });
}

document.getElementById("cat-close")?.addEventListener("click", () =>
  document.getElementById("cat-dialog").close());

document.getElementById("cat-auto-btn")?.addEventListener("click", async () => {
  await api("/api/instrument-categories/auto-detect", { method: "POST" });
  const wrap = document.getElementById("cat-table-wrap");
  wrap.innerHTML = `<div class="muted small">Re-running auto-detection…</div>`;
  const cats = await api("/api/instrument-categories");
  document.getElementById("cat-dialog").close();
  if (nwData) { nwData = await api("/api/networth"); renderNwDonut(nwData); }
  openCategoryEditor();
});

const NW_CACHE_KEY = "nw_cache_v1";
const NW_CACHE_TTL = 5 * 60 * 1000;   // 5 minutes in ms

function _nwCacheGet() {
  try {
    const raw = sessionStorage.getItem(NW_CACHE_KEY);
    if (!raw) return null;
    const { ts, data } = JSON.parse(raw);
    if (Date.now() - ts < NW_CACHE_TTL) return data;
  } catch (_) {}
  return null;
}

function _nwCacheSet(data) {
  try { sessionStorage.setItem(NW_CACHE_KEY, JSON.stringify({ ts: Date.now(), data })); }
  catch (_) {}
}

async function loadNetWorth() {
  const banner    = document.getElementById("nw-banner");
  const chartSec  = document.getElementById("nw-chart-section");
  const isAll     = activeSegment === "all";
  if (banner)   banner.style.display   = isAll ? "" : "none";
  if (chartSec) chartSec.style.display = isAll ? "" : "none";
  if (!isAll) return;

  // Stale-while-revalidate: show cached data immediately, fetch fresh in background
  const stale = _nwCacheGet();
  if (stale) {
    _renderNetWorth(stale);   // instant — no network
  }

  try {
    const nw = await api("/api/networth");
    _nwCacheSet(nw);
    _renderNetWorth(nw);
  } catch (e) {
    console.error("Net worth load error:", e);
  }
}

function _renderNetWorth(nw) {
    const c = nw.current;

    // Headline total
    document.getElementById("nw-total").textContent = fmtINRshort(c.total);

    // Net Worth XIRR — overall Personal Rate of Return across all asset classes
    const xirrEl = document.getElementById("nw-xirr");
    if (xirrEl) {
      if (nw.nw_xirr != null) {
        const xpct = (nw.nw_xirr * 100).toFixed(2);
        xirrEl.innerHTML =
          `<span class="${nw.nw_xirr >= 0 ? 'pos' : 'neg'}">${xpct}% p.a.</span>` +
          ` <span class="muted small">overall XIRR (all assets)</span>`;
      } else {
        xirrEl.textContent = "";
      }
    }
    // Combined daily gain (EQ + MF) since the previous close
    const dayEl = document.getElementById("nw-day");
    if (dayEl) {
      const dc = c.day_change;
      if (dc != null && dc !== 0) {
        const pos = dc >= 0;
        const pctStr = c.day_change_pct != null ? ` (${pos ? "+" : ""}${(c.day_change_pct * 100).toFixed(2)}%)` : "";
        dayEl.innerHTML = `<span class="muted">Today</span> <span class="${pos ? 'pos' : 'neg'}">${pos ? "+" : ""}${fmtINRshort(dc)}${pctStr}</span>`;
      } else {
        dayEl.textContent = "";
      }
    }
    document.getElementById("nw-asof").textContent  = `As of ${nw.as_of}`;

    nwData = nw;
    renderNwDonut(nw);

    // Assumptions note
    const asmEl = document.getElementById("nw-assumptions");
    if (asmEl && nw.assumptions?.equity_growth_rate) {
      asmEl.textContent = `projected at ${nw.assumptions.equity_growth_rate}% p.a. (all-time portfolio XIRR) · EPF at ${nw.assumptions.epf_rate}% · dots = your actual records`;
    }

    // Projection chart with milestones
    const projCtx = document.getElementById("nw-projection-chart").getContext("2d");
    if (!chartsPaused && nwProjectionChart) nwProjectionChart.destroy();

    const histLabels = nw.history.map(h => h.month);
    const histVals   = nw.history.map(h => h.total);
    const projLabels = nw.projection.map(p => p.month);
    const projVals   = nw.projection.map(p => p.total);
    const allLabels  = [...histLabels, ...projLabels];
    const histFull   = [...histVals, ...new Array(projLabels.length).fill(null)];
    const projFull   = [...new Array(histLabels.length).fill(null), ...projVals];

    // Build milestone annotation lines
    const annotations = {};
    nw.milestones.forEach((m, idx) => {
      const monthIdx = allLabels.indexOf(m.month);
      if (monthIdx < 0) return;
      annotations[`ms${idx}`] = {
        type: "line",
        yMin: m.amount, yMax: m.amount,
        borderColor: m.is_future ? "rgba(163,113,247,0.5)" : "rgba(63,185,80,0.6)",
        borderWidth: 1, borderDash: [4, 4],
        label: {
          display: true, content: m.label,
          position: "start",
          color: m.is_future ? "#a371f7" : "#3fb950",
          font: { size: 10 }, backgroundColor: "transparent",
        },
      };
    });

    // Register annotation plugin if available
    if (window.ChartAnnotation) Chart.register(window.ChartAnnotation);

    // Overlay actual milestone snapshots — match by year-month since snapshot
    // dates (e.g. 2024-07-08) won't exactly match chart labels (2024-07-01)
    const snapshotData = (nw.snapshots || []).map(s => {
      const snapYM = s.date.slice(0, 7);   // "YYYY-MM"
      const idx = allLabels.findIndex(lbl => lbl.startsWith(snapYM));
      return { x: s.date, y: s.amount, label: s.label, idx };
    }).filter(s => s.idx >= 0);

    // Build sparse array aligned to allLabels for snapshot dots
    const snapshotY = allLabels.map((lbl, i) => {
      const found = snapshotData.find(s => s.idx === i);
      return found ? found.y : null;
    });

    nwProjectionChart = chartsPaused ? nwProjectionChart : new Chart(projCtx, {
      type: "line",
      data: {
        labels: allLabels,
        datasets: [
          { label: "Historical (computed)", data: histFull,
            borderColor: "#58a6ff", backgroundColor: "rgba(88,166,255,0.1)",
            borderWidth: 2, fill: true, pointRadius: 0, tension: 0.3, spanGaps: false },
          { label: "Projected", data: projFull,
            borderColor: "#a371f7", backgroundColor: "transparent",
            borderWidth: 1.5, borderDash: [5,4], pointRadius: 0, tension: 0.3, spanGaps: false },
          { label: "Actual (your records)", data: snapshotY,
            borderColor: "transparent", backgroundColor: "#f0c14b",
            pointBackgroundColor: "#f0c14b", pointBorderColor: "#f0c14b",
            pointRadius: 6, pointHoverRadius: 8,
            borderWidth: 0, fill: false, spanGaps: false, showLine: false },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { labels: { color: "#e6edf3" } },
          tooltip: { callbacks: { label: c => `${c.dataset.label}: ${fmtINR(c.parsed.y)}` } },
          annotation: Object.keys(annotations).length ? { annotations } : undefined,
        },
        scales: {
          x: { ticks: { color: "#8b949e", maxTicksLimit: 10 }, grid: { color: "#2a3038" } },
          y: { ticks: { color: "#8b949e", callback: v => fmtINR(v) }, grid: { color: "#2a3038" } },
        },
      },
    });
}

// ---- EPF section ----
let epfMonthlyChart  = null;
let epfInterestChart = null;
let epfBalanceChart  = null;

async function loadEpfSection() {
  const section = document.getElementById("epf-section");
  if (!section) return;
  section.style.display = activeSegment === "EPF" ? "" : "none";
  if (activeSegment !== "EPF") return;

  try {
    const s = await api("/api/epf/summary");

    // Summary cards
    const cards = document.getElementById("epf-summary-cards");
    const card = (title, val, sub) =>
      `<div class="card"><h3>${title}</h3><div class="value pos">${fmtINR(val)}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;
    cards.innerHTML =
      card("Total Balance",      s.estimated_balance,          `${s.months_imported} months imported`) +
      card("Employee Contributions", s.total_employee_contributions, "your share") +
      card("Employer Contributions", s.total_employer_contributions, "company's share") +
      card("Interest Credited",  s.total_interest_credited,    "EPF Board") +
      card("EPS Pension",        s.total_pension_contributions, "pension fund") +
      card("This FY",            s.fy_employee_contribution + s.fy_employer_contribution, "contributions this year");

    // Monthly contributions chart
    const months = s.monthly_contributions;
    const mcCtx = document.getElementById("epf-monthly-chart").getContext("2d");
    if (!chartsPaused && epfMonthlyChart) epfMonthlyChart.destroy();
    epfMonthlyChart = chartsPaused ? epfMonthlyChart : new Chart(mcCtx, {
      type: "bar",
      data: {
        labels: months.map(m => m.month.slice(0, 7)),
        datasets: [
          { label: "Employee", data: months.map(m => m.employee), backgroundColor: "rgba(88,166,255,0.7)" },
          { label: "Employer", data: months.map(m => m.employer), backgroundColor: "rgba(63,185,80,0.7)" },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { color: "#e6edf3" } } },
        scales: {
          x: { stacked: true, ticks: { color: "#8b949e", maxTicksLimit: 12 }, grid: { color: "#2a3038" } },
          y: { stacked: true, ticks: { color: "#8b949e", callback: v => fmtINR(v) }, grid: { color: "#2a3038" } },
        },
      },
    });

    // Balance growth line chart
    let balRunning = 0;
    const balData = months.map(m => { balRunning += m.employee + m.employer; return balRunning; });
    const bgCtx = document.getElementById("epf-balance-chart").getContext("2d");
    if (!chartsPaused && epfBalanceChart) epfBalanceChart.destroy();
    epfBalanceChart = chartsPaused ? epfBalanceChart : new Chart(bgCtx, {
      type: "line",
      data: {
        labels: months.map(m => m.month.slice(0, 7)),
        datasets: [{ label: "Balance", data: balData, borderColor: "#a371f7",
          backgroundColor: "rgba(163,113,247,0.1)", fill: true, borderWidth: 2, pointRadius: 0, tension: 0.3 }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: "#8b949e", maxTicksLimit: 8 }, grid: { color: "#2a3038" } },
          y: { ticks: { color: "#8b949e", callback: v => fmtINR(v) }, grid: { color: "#2a3038" } },
        },
      },
    });

    // Contribution ledger table
    const allEntries = await api("/api/epf");
    const tbody = document.querySelector("#epf-table tbody");
    const countEl = document.getElementById("epf-entry-count");
    if (countEl) countEl.textContent = `${allEntries.length} entries`;

    tbody.innerHTML = "";
    let running = 0;
    for (const e of allEntries) {
      running += (e.employee_share + e.employer_share) - (e.employee_withdrawal + e.employer_withdrawal);
      const typeLabel = e.entry_type === "interest" ? "Interest" : e.entry_type === "withdrawal" ? "Withdrawal" : "Contribution";
      const cls_type  = e.entry_type === "interest" ? "pos" : e.entry_type === "withdrawal" ? "neg" : "";
      tbody.innerHTML += `
        <tr>
          <td>${e.month.slice(0, 7)}</td>
          <td class="${cls_type}">${typeLabel}</td>
          <td class="num">${e.employee_share > 0 ? fmtINR(e.employee_share) : "—"}</td>
          <td class="num">${e.employer_share > 0 ? fmtINR(e.employer_share) : "—"}</td>
          <td class="num">${e.pension_contrib > 0 ? fmtINR(e.pension_contrib) : "—"}</td>
          <td class="num pos">${fmtINR(running)}</td>
        </tr>`;
    }
    if (!allEntries.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="muted" style="text-align:center;padding:20px">
        No EPF data yet — click <strong>Import EPF Passbook</strong> in the Transactions section to import your passbook.</td></tr>`;
    }

    if (!months.length && !allEntries.length) {
      cards.innerHTML = `<div class="muted" style="padding:12px">No EPF data yet — import your EPFO passbook PDF to get started.</div>`;
    }

  } catch (e) {
    console.error("EPF section load error:", e);
  }
}

// ---- Bonds ----

async function loadBondsSection() {
  const sec = document.getElementById("bonds-section");
  if (!sec) return;
  sec.style.display = activeSegment === "BONDS" ? "" : "none";
  if (activeSegment !== "BONDS") return;

  const content = document.getElementById("bonds-content");
  content.innerHTML = `<div class="muted small">Loading…</div>`;

  try {
    const holdings = await api("/api/bonds");
    if (!holdings.length) {
      content.innerHTML = `<div class="muted" style="padding:16px">
        No bond details registered yet.<br>
        <strong>Note:</strong> Your transactions (buys/sells) are in the equity tradebook.
        Click <strong>+ Add bond details</strong> to register the bond metadata (issue price, coupon, maturity date, tax treatment).
        SGBDEC31III is pre-seeded — just click the button to confirm.
      </div>`;
      return;
    }

    let html = "";
    for (const b of holdings) {
      const dtm = b.days_to_maturity;
      const dtmLabel = dtm < 0 ? `Matured ${Math.abs(dtm)}d ago`
                      : dtm < 365 ? `${dtm}d to maturity`
                      : `${(dtm/365).toFixed(1)}y to maturity`;
      const exemptBadge = b.capital_gains_exempt_at_maturity
        ? `<span class="fi-badge active" style="background:rgba(63,185,80,.15);color:var(--pos)">Capital gains EXEMPT at maturity</span>`
        : `<span class="fi-badge matured">STCG/LTCG applicable</span>`;

      html += `
      <div class="card" style="margin-bottom:14px">
        <div class="row-between" style="margin-bottom:10px">
          <div>
            <strong style="font-size:16px">${b.symbol}</strong>
            <span class="muted" style="font-size:12px;margin-left:8px">${b.full_name || ''}</span>
            <span class="fi-badge active" style="margin-left:8px">${b.bond_type}</span>
          </div>
          <div class="actions">
            ${exemptBadge}
            <button class="edit-btn bond-edit-btn" data-id="${b.id}">edit</button>
          </div>
        </div>
        <div class="cards" style="margin-bottom:12px">
          <div class="card"><h3>Units Held</h3><div class="value">${b.units || '—'}</div></div>
          <div class="card"><h3>Issue Price</h3><div class="value">${fmtINR(b.issue_price)}</div></div>
          <div class="card"><h3>Current Price</h3><div class="value">${b.current_price ? fmtINR(b.current_price) : '—'}</div><div class="sub muted">${b.price_source || ''}</div></div>
          <div class="card"><h3>Current Value</h3><div class="value ${cls(b.unrealized_pnl)}">${b.current_value ? fmtINR(b.current_value) : '—'}</div></div>
          <div class="card"><h3>P&L</h3><div class="value ${cls(b.unrealized_pnl)}">${b.unrealized_pnl != null ? fmtINR(b.unrealized_pnl) : '—'}</div><div class="sub">${b.pct_return != null ? b.pct_return.toFixed(2)+'%' : ''}</div></div>
          <div class="card"><h3>Maturity</h3><div class="value">${b.maturity_date}</div><div class="sub muted">${dtmLabel}</div></div>
        </div>

        <!-- Interest income -->
        ${b.coupon_rate > 0 ? `
        <div style="border-top:1px solid var(--border);padding-top:12px;margin-bottom:10px">
          <strong style="font-size:13px">Interest Income</strong>
          <span class="muted small" style="margin-left:6px">${b.coupon_rate}% p.a. on issue price · ${b.coupon_frequency}</span>
          <div class="cards" style="margin-top:8px">
            <div class="card"><h3>Total Earned</h3><div class="value pos">${fmtINR(b.total_interest_earned)}</div><div class="sub">taxable at slab rate</div></div>
            <div class="card"><h3>This FY</h3><div class="value pos">${fmtINR(b.fy_interest)}</div></div>
            <div class="card"><h3>Next Coupon</h3><div class="value">${b.next_coupon_date || '—'}</div><div class="sub">${b.next_coupon_amount ? fmtINR(b.next_coupon_amount) : ''}</div></div>
          </div>
          <details style="margin-top:8px">
            <summary style="cursor:pointer;color:var(--muted);font-size:12px">Full coupon schedule (${b.coupon_schedule?.length || 0} payments)</summary>
            <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px">
              ${(b.coupon_schedule || []).map(c =>
                `<span style="background:${c.received ? 'rgba(63,185,80,.12)' : 'rgba(139,148,158,.1)'};color:${c.received ? 'var(--pos)' : 'var(--muted)'};padding:3px 8px;border-radius:4px;font-size:11px">
                  ${c.date}: ${fmtINR(c.amount)} ${c.received ? '✓' : ''}
                </span>`
              ).join('')}
            </div>
          </details>
        </div>` : ''}

        <!-- Tax note -->
        <div class="muted small" style="border-top:1px solid var(--border);padding-top:10px;line-height:1.6">
          <strong>Tax:</strong> ${b.tax_note}
        </div>
      </div>`;
    }
    content.innerHTML = html;
  } catch (e) {
    content.innerHTML = `<div class="neg small">Error: ${escapeHtml(e.message)}</div>`;
    console.error(e);
  }
}

// Bond modal
const bondDialog = document.getElementById("bond-dialog");
const bondForm   = document.getElementById("bond-form");
document.getElementById("btn-bond-add")?.addEventListener("click", () => {
  bondForm.reset();
  document.getElementById("bond-form-title").textContent = "Add Bond Details";
  bondDialog.showModal();
});
document.getElementById("bond-cancel")?.addEventListener("click", (e) => {
  e.preventDefault(); bondDialog.close();
});
bondForm?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(bondForm);
  const purchasePriceRaw = fd.get("purchase_price");
  const payload = {
    symbol:           fd.get("symbol").toUpperCase().trim(),
    bond_type:        fd.get("bond_type"),
    full_name:        fd.get("full_name") || null,
    isin:             fd.get("isin") || null,
    issue_price:      parseFloat(fd.get("issue_price")),
    issue_date:       fd.get("issue_date"),
    maturity_date:    fd.get("maturity_date"),
    coupon_rate:      parseFloat(fd.get("coupon_rate") || 0),
    coupon_frequency: fd.get("coupon_frequency"),
    quantity:         parseFloat(fd.get("quantity") || 0),
    purchase_price:   purchasePriceRaw ? parseFloat(purchasePriceRaw) : null,
    purchase_date:    fd.get("purchase_date") || null,
    price_override:   fd.get("price_override") ? parseFloat(fd.get("price_override")) : null,
    capital_gains_exempt_at_maturity: fd.get("capital_gains_exempt_at_maturity") === "on",
    notes:            fd.get("notes") || null,
  };
  try {
    await api("/api/bonds/details", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    bondDialog.close();
    await loadBondsSection();
  } catch (err) { alert("Save failed: " + err.message); }
});

// Edit existing bond — delegate from bonds-content div
document.getElementById("bonds-content")?.addEventListener("click", async (e) => {
  if (!e.target.classList.contains("bond-edit-btn")) return;
  const id = e.target.dataset.id;
  const details = await api("/api/bonds/details");
  const b = details.find(d => String(d.id) === String(id));
  if (!b) return;

  bondForm.reset();
  document.getElementById("bond-form-title").textContent = "Edit Bond Details";
  bondForm.querySelector('[name="symbol"]').value        = b.symbol;
  bondForm.querySelector('[name="bond_type"]').value     = b.bond_type;
  bondForm.querySelector('[name="full_name"]').value     = b.full_name || "";
  bondForm.querySelector('[name="isin"]').value          = b.isin || "";
  bondForm.querySelector('[name="issue_price"]').value   = b.issue_price;
  bondForm.querySelector('[name="issue_date"]').value    = b.issue_date;
  bondForm.querySelector('[name="maturity_date"]').value = b.maturity_date;
  bondForm.querySelector('[name="coupon_rate"]').value   = b.coupon_rate;
  bondForm.querySelector('[name="coupon_frequency"]').value = b.coupon_frequency;
  bondForm.querySelector('[name="quantity"]').value      = b.quantity || 0;
  bondForm.querySelector('[name="purchase_price"]').value = b.purchase_price || "";
  bondForm.querySelector('[name="purchase_date"]').value  = b.purchase_date || "";
  bondForm.querySelector('[name="price_override"]').value = b.price_override || "";
  bondForm.querySelector('[name="capital_gains_exempt_at_maturity"]').checked =
    b.capital_gains_exempt_at_maturity;
  bondForm.querySelector('[name="notes"]').value = b.notes || "";

  bondDialog.showModal();
});

// ---- Global Equities ----

async function loadGlobalSection() {
  const sec = document.getElementById("global-section");
  if (!sec) return;
  sec.style.display = activeSegment === "GLOBAL" ? "" : "none";
  if (activeSegment !== "GLOBAL") return;

  try {
    const [summary, txns] = await Promise.all([
      api("/api/global-equity/summary"),
      api("/api/global-equity"),
    ]);

    // USD/INR label
    const fx = document.getElementById("global-usdinr");
    if (fx && summary.usdinr) fx.textContent = `USD/INR: ${summary.usdinr}`;

    // Summary mini-cards
    const sc = document.getElementById("global-summary-cards");
    const card = (title, val, sub) =>
      `<div class="card"><h3>${title}</h3><div class="value">${val}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;
    sc.innerHTML =
      card("Invested (USD)",  fmtUSD(summary.invested_usd||0), `₹${fmtINR(summary.invested_inr||0).replace('₹','')}`) +
      card("Current (USD)",   fmtUSD(summary.total_usd||0),    `₹${fmtINR(summary.total_inr||0).replace('₹','')}`) +
      card("P&L (USD)", fmtUSD((summary.total_usd||0)-(summary.invested_usd||0)), null);

    // Holdings table
    const htbody = document.querySelector("#global-holdings-table tbody");
    htbody.innerHTML = "";
    for (const h of summary.holdings || []) {
      const badge = h.status === "closed"
        ? `<span class="fi-badge matured">Closed</span>`
        : `<span class="fi-badge active">Active</span>`;
      htbody.innerHTML += `<tr>
        <td><strong>${h.symbol}</strong><br><span class="muted" style="font-size:11px">${h.name||''}</span></td>
        <td class="num">${h.quantity ? fmtQty(h.quantity) : "—"}</td>
        <td class="num">${h.avg_cost_usd ? fmtUSD(h.avg_cost_usd, 4) : "—"}</td>
        <td class="num">${h.current_price_usd ? fmtUSD(h.current_price_usd) : "—"}</td>
        <td class="num">${h.invested_usd ? fmtUSD(h.invested_usd) : "—"}</td>
        <td class="num">${h.current_value_usd != null ? fmtUSD(h.current_value_usd) : "—"}</td>
        <td class="num ${(h.unrealized_pnl_usd||0) >= 0 ? 'pos' : 'neg'}">${h.unrealized_pnl_usd != null ? fmtUSD(h.unrealized_pnl_usd) : h.realized_pnl_usd ? fmtUSD(h.realized_pnl_usd) : "—"}</td>
        <td class="num ${(h.unrealized_pnl_inr||0) >= 0 ? 'pos' : 'neg'}">${h.unrealized_pnl_inr != null ? fmtINR(h.unrealized_pnl_inr) : "—"}</td>
        <td class="num ${(h.pct_return||0) >= 0 ? 'pos' : 'neg'}">${h.pct_return != null ? h.pct_return.toFixed(2)+'%' : "—"}</td>
        <td>${badge}</td>
      </tr>`;
    }
    if (!summary.holdings?.length) {
      htbody.innerHTML = `<tr><td colspan="10" class="muted" style="text-align:center;padding:20px">No global equity transactions yet — import your INDMoney XLS or add manually.</td></tr>`;
    }

    // Transaction history
    const ttbody = document.querySelector("#global-txn-table tbody");
    ttbody.innerHTML = "";
    for (const t of txns) {
      ttbody.innerHTML += `<tr>
        <td>${t.trade_date}</td>
        <td><strong>${t.symbol}</strong></td>
        <td class="${t.trade_type==='buy'?'pos':'neg'}">${t.trade_type}</td>
        <td class="num">${privacyMode ? MASK : t.quantity.toFixed(6)}</td>
        <td class="num">${privacyMode ? MASK : "$" + t.price_usd.toFixed(4)}</td>
        <td class="num">${privacyMode ? MASK : "$" + t.amount_usd.toFixed(4)}</td>
        <td class="num">${privacyMode ? MASK : t.fees_usd.toFixed(2)}</td>
        <td class="num">${t.exchange_rate ? t.exchange_rate.toFixed(2) : '—'}</td>
        <td class="num">${t.amount_inr ? fmtINR(t.amount_inr) : '—'}</td>
        <td><button class="delete-btn global-del-btn" data-id="${t.id}">delete</button></td>
      </tr>`;
    }
  } catch (e) {
    console.error("Global equity load error:", e);
  }
}

let globalCurveChart = null;

async function loadGlobalAnalytics() {
  if (activeSegment !== "GLOBAL") return;
  try {
    // Load all three in parallel — these involve yfinance calls so may be slow
    const [xirrData, curveData, taxData] = await Promise.all([
      api("/api/global-equity/xirr"),
      api("/api/global-equity/equity-curve"),
      api("/api/global-equity/tax"),
    ]);

    // ---- XIRR section ----
    const xirrSec = document.getElementById("global-xirr-section");
    xirrSec.style.display = "";
    const box = (label, val, sub, klass) =>
      `<div class="xirr-box ${klass}">
        <div class="label">${label}</div>
        <div class="xirr-val ${cls(val)}">${val != null ? (val*100).toFixed(2)+'%' : '—'}</div>
        ${sub ? `<div class="sub">${sub}</div>` : ""}
      </div>`;
    let xirrHtml = box("Portfolio XIRR (USD)", xirrData.portfolio_xirr, "global equities", "portfolio-box");
    for (const [ticker, b] of Object.entries(xirrData.benchmarks || {})) {
      const diff = xirrData.portfolio_xirr != null && b.xirr != null
        ? xirrData.portfolio_xirr - b.xirr : null;
      const diffStr = diff != null ? `${diff >= 0 ? "+" : ""}${(diff*100).toFixed(2)}% vs index` : null;
      xirrHtml += box(b.name, b.xirr, diffStr, "");
    }
    document.getElementById("global-xirr-cards").innerHTML = xirrHtml;

    // ---- Equity curve ----
    const curveSec = document.getElementById("global-curve-section");
    curveSec.style.display = "";
    const sub = document.getElementById("global-curve-subtitle");
    if (sub && curveData.base_date)
      sub.textContent = `All series rebased to 100 on ${curveData.base_date} · USD`;

    const ctx = document.getElementById("global-curve-chart").getContext("2d");
    if (!chartsPaused && globalCurveChart) globalCurveChart.destroy();
    const COLORS = { "Portfolio": "#e6edf3", "S&P 500": "#58a6ff", "NASDAQ 100": "#3fb950" };
    const datasets = Object.entries(curveData.series || {}).map(([name, vals]) => ({
      label: name,
      data: vals,
      borderColor: COLORS[name] || "#a371f7",
      backgroundColor: "transparent",
      borderWidth: name === "Portfolio" ? 2 : 1.5,
      pointRadius: 0, spanGaps: true, tension: 0.1,
    }));
    globalCurveChart = chartsPaused ? globalCurveChart : new Chart(ctx, {
      type: "line",
      data: { labels: curveData.dates, datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { labels: { color: "#e6edf3" } },
          tooltip: { callbacks: { label: c => `${c.dataset.label}: ${c.parsed.y != null ? c.parsed.y.toFixed(2) : "—"}` } },
        },
        scales: {
          x: { ticks: { color: "#8b949e", maxTicksLimit: 10 }, grid: { color: "#2a3038" } },
          y: { ticks: { color: "#8b949e" }, grid: { color: "#2a3038" } },
        },
      },
    });

    // ---- Tax section ----
    const taxSec = document.getElementById("global-tax-section");
    taxSec.style.display = "";

    const taxCard = (title, val, sub, klass = "") =>
      `<div class="card"><h3>${title}</h3><div class="value ${klass}">${fmtINR(val)}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;

    const stcg30 = taxData.tax_estimate?.stcg_at_30_pct || 0;
    const stcg20 = taxData.tax_estimate?.stcg_at_20_pct || 0;
    const ltcgTax = taxData.tax_estimate?.ltcg_at_12_5 || 0;

    document.getElementById("global-tax-summary").innerHTML =
      taxCard("Total Realized (INR)", taxData.total_realized_inr, null, cls(taxData.total_realized_inr)) +
      taxCard("STCG (< 24 months)", taxData.stcg_inr, "at your slab rate", cls(taxData.stcg_inr)) +
      taxCard("Est. Tax @ 30%",     stcg30, "STCG at 30% slab", "neg") +
      taxCard("Est. Tax @ 20%",     stcg20, "STCG at 20% slab", "neg") +
      taxCard("LTCG (≥ 24 months)", taxData.ltcg_inr, "at 12.5%, no exemption", cls(taxData.ltcg_inr)) +
      (ltcgTax > 0 ? taxCard("LTCG Tax @ 12.5%", ltcgTax, "no ₹1.25L exemption for foreign equity", "neg") : "");

    document.getElementById("global-tax-note").innerHTML =
      taxData.tax_estimate?.note || "";

    // Tax breakdown table
    const tbody = document.querySelector("#global-tax-table tbody");
    tbody.innerHTML = "";
    for (const r of (taxData.breakdown || [])) {
      const typeClass = r.type === "LTCG" ? "pos" : "neg";
      tbody.innerHTML += `<tr>
        <td><strong>${r.symbol}</strong></td>
        <td>${r.buy_date}</td><td>${r.sell_date}</td>
        <td class="num">${r.held_months}mo</td>
        <td class="${typeClass}">${r.type}</td>
        <td class="num ${cls(r.pnl_usd)}">${fmtUSD(r.pnl_usd, 4)}</td>
        <td class="num">${fmtINR(r.cost_inr)}</td>
        <td class="num">${fmtINR(r.proceeds_inr)}</td>
        <td class="num ${cls(r.pnl_inr)}">${fmtINR(r.pnl_inr)}</td>
      </tr>`;
    }
    if (!taxData.breakdown?.length)
      tbody.innerHTML = `<tr><td colspan="9" class="muted" style="text-align:center;padding:16px">No realized transactions yet.</td></tr>`;

  } catch (e) {
    console.error("Global analytics error:", e);
  }
}

// Import handler
document.getElementById("import-global-input")?.addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const statusEl = document.getElementById("global-import-status");
  const fillEl   = document.getElementById("global-progress-fill");
  const msgEl    = document.getElementById("global-import-msg");
  statusEl.style.display = "";
  fillEl.style.width = "30%";
  msgEl.innerHTML = `<span class="muted">Importing…</span>`;
  setTimeout(() => { fillEl.style.transition = "width 1.5s"; fillEl.style.width = "70%"; }, 100);
  const fd = new FormData(); fd.append("file", file);
  try {
    const r = await api("/api/import-global", { method: "POST", body: fd });
    fillEl.style.width = "100%";
    if (r.inserted > 0) {
      msgEl.innerHTML = `<span class="pos">✓ Imported ${r.inserted} transactions (${r.skipped_duplicates} duplicates, ${r.total_found} found).</span>`;
      await loadGlobalSection();
    } else if (r.total_found === 0) {
      msgEl.innerHTML = `<span class="neg">⚠ No transactions found. Check that this is the INDMoney Global Equity Full Report.</span>`;
    } else {
      msgEl.innerHTML = `<span class="muted">${r.inserted} new, ${r.skipped_duplicates} already imported.</span>`;
    }
    if (r.errors?.length) msgEl.innerHTML += `<br><span class="neg">${r.errors.slice(0,2).join("; ")}</span>`;
  } catch (err) {
    fillEl.style.background = "var(--neg)"; fillEl.style.width = "100%";
    msgEl.innerHTML = `<span class="neg">✗ ${escapeHtml(err.message)}</span>`;
  }
  e.target.value = "";
});

// Delete transaction
document.querySelector("#global-txn-table tbody")?.addEventListener("click", async (e) => {
  if (!e.target.classList.contains("global-del-btn")) return;
  if (!confirm("Delete this transaction?")) return;
  await api(`/api/global-equity/${e.target.dataset.id}`, { method: "DELETE" });
  await loadGlobalSection();
});

// Manual add modal
const globalDialog = document.getElementById("global-dialog");
const globalForm   = document.getElementById("global-form");

document.getElementById("btn-global-add")?.addEventListener("click", () => {
  globalForm.reset();
  globalForm.querySelector('input[name="trade_date"]').valueAsDate = new Date();
  globalDialog.showModal();
});
document.getElementById("global-cancel")?.addEventListener("click", (e) => {
  e.preventDefault(); globalDialog.close();
});
globalForm?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(globalForm);
  const payload = {
    symbol: fd.get("symbol").toUpperCase().trim(),
    stock_name: fd.get("stock_name") || null,
    trade_date: fd.get("trade_date"),
    trade_type: fd.get("trade_type"),
    quantity: parseFloat(fd.get("quantity")),
    price_usd: parseFloat(fd.get("price_usd")),
    fees_usd: parseFloat(fd.get("fees_usd") || 0),
    exchange_rate: fd.get("exchange_rate") ? parseFloat(fd.get("exchange_rate")) : null,
    notes: fd.get("notes") || null,
  };
  try {
    await api("/api/global-equity", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    globalDialog.close();
    await loadGlobalSection();
  } catch (err) { alert("Save failed: " + err.message); }
});

// ---- SIP Schedules ----
let sipPreviewScheduleId = null;

async function loadSipSchedules() {
  const sec = document.getElementById("sip-section");
  if (!sec) return;
  // Show only on MF tab
  sec.style.display = activeSegment === "MF" ? "" : "none";
  if (activeSegment !== "MF") return;

  try {
    const schedules = await api("/api/sip-schedules");
    const tbody = document.querySelector("#sip-table tbody");
    tbody.innerHTML = "";
    for (const s of schedules) {
      const status = s.is_active
        ? `<span class="fi-badge active">Active</span>`
        : `<span class="fi-badge matured">Paused</span>`;
      tbody.innerHTML += `<tr>
        <td><strong>${s.scheme_name || s.isin}</strong><br>
          <span class="muted" style="font-size:11px">${s.isin}${s.folio ? ' · folio ' + s.folio : ''}</span></td>
        <td class="num">${fmtINR(s.amount)}/mo</td>
        <td class="num">${s.sip_day}th</td>
        <td>${s.start_date}</td>
        <td>${status}</td>
        <td class="muted small">${s.last_synced_date || 'Never'}</td>
        <td>
          <button class="edit-btn sip-preview-btn" data-id="${s.id}">Preview</button>
          <button class="delete-btn sip-del-btn" data-id="${s.id}">delete</button>
        </td>
      </tr>`;
    }
    if (!schedules.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="muted" style="text-align:center;padding:16px">
        No SIP schedules yet. Add one to auto-import future SIPs using official AMFI NAV.</td></tr>`;
    }
  } catch (e) { console.error("SIP load error:", e); }
}

document.querySelector("#sip-table tbody")?.addEventListener("click", async (e) => {
  const id = e.target.dataset.id;
  if (!id) return;
  if (e.target.classList.contains("sip-del-btn")) {
    if (!confirm("Delete this SIP schedule? (Existing imported transactions are unaffected.)")) return;
    await api(`/api/sip-schedules/${id}`, { method: "DELETE" });
    await loadSipSchedules();
  } else if (e.target.classList.contains("sip-preview-btn")) {
    await showSipPreview(parseInt(id));
  }
});

async function showSipPreview(schedId) {
  const panel = document.getElementById("sip-preview-panel");
  const title = document.getElementById("sip-preview-title");
  const summary = document.getElementById("sip-preview-summary");
  const tbody = document.querySelector("#sip-preview-table tbody");
  panel.style.display = "";
  title.textContent = "Loading preview…";
  tbody.innerHTML = "";
  summary.textContent = "";
  sipPreviewScheduleId = schedId;

  try {
    const p = await api(`/api/sip-schedules/${schedId}/preview`);
    title.textContent = `Preview: ${p.scheme_name}`;
    summary.innerHTML =
      `<span class="pos">${p.new} new transactions</span> · ` +
      `<span class="muted">${p.skipped} already imported · ${p.no_nav} no NAV</span>`;

    const STATUS_COLORS = { new: "pos", skipped: "muted", no_nav: "neg" };
    for (const item of p.items) {
      tbody.innerHTML += `<tr>
        <td>${item.sip_date}</td>
        <td>${item.allotment_date || '—'}</td>
        <td class="num">${item.nav != null ? item.nav.toFixed(4) : '—'}</td>
        <td class="num">${item.units != null ? item.units.toFixed(6) : '—'}</td>
        <td class="num">${fmtINR(item.amount)}</td>
        <td class="${STATUS_COLORS[item.status] || ''}">${item.status}</td>
        <td class="muted small">${item.note || ''}</td>
      </tr>`;
    }
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (e) {
    title.textContent = "Preview failed";
    summary.innerHTML = `<span class="neg">${escapeHtml(e.message)}</span>`;
  }
}

document.getElementById("btn-sip-cancel-preview")?.addEventListener("click", () => {
  document.getElementById("sip-preview-panel").style.display = "none";
  sipPreviewScheduleId = null;
});

document.getElementById("btn-sip-confirm")?.addEventListener("click", async () => {
  if (!sipPreviewScheduleId) return;
  const btn = document.getElementById("btn-sip-confirm");
  btn.disabled = true; btn.textContent = "Importing…";
  try {
    const r = await api(`/api/sip-schedules/${sipPreviewScheduleId}/confirm`, { method: "POST" });
    document.getElementById("sip-sync-result").innerHTML =
      `<span class="pos">✓ Imported ${r.inserted} new transactions, ${r.skipped} already existed.</span>`;
    document.getElementById("sip-preview-panel").style.display = "none";
    sipPreviewScheduleId = null;
    await Promise.all([loadSipSchedules(), loadHoldings(), loadSummary()]);
  } catch (e) {
    document.getElementById("sip-sync-result").innerHTML =
      `<span class="neg">✗ ${escapeHtml(e.message)}</span>`;
  } finally { btn.disabled = false; btn.textContent = "Confirm & Import"; }
});

document.getElementById("btn-sip-sync-all")?.addEventListener("click", async () => {
  const btn = document.getElementById("btn-sip-sync-all");
  const resultEl = document.getElementById("sip-sync-result");
  btn.disabled = true; btn.textContent = "Syncing…";
  resultEl.innerHTML = `<span class="muted">Fetching NAVs from AMFI…</span>`;
  try {
    const r = await api("/api/sip-schedules/sync-all", { method: "POST" });
    resultEl.innerHTML =
      `<span class="pos">✓ ${r.total_inserted} new transactions imported across ${r.schedules_processed} schedules.</span>`;
    await Promise.all([loadSipSchedules(), loadHoldings(), loadSummary()]);
  } catch (e) {
    resultEl.innerHTML = `<span class="neg">✗ ${escapeHtml(e.message)}</span>`;
  } finally { btn.disabled = false; btn.textContent = "Sync All SIPs"; }
});

// SIP Add modal
const sipDialog = document.getElementById("sip-dialog");
const sipForm   = document.getElementById("sip-form");
document.getElementById("btn-sip-add")?.addEventListener("click", () => {
  sipForm.reset(); sipDialog.showModal();
});
document.getElementById("sip-cancel")?.addEventListener("click", (e) => {
  e.preventDefault(); sipDialog.close();
});
sipForm?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(sipForm);
  const payload = {
    isin:         fd.get("isin").toUpperCase().trim(),
    scheme_name:  fd.get("scheme_name") || null,
    folio:        fd.get("folio") || null,
    amount:       parseFloat(fd.get("amount")),
    sip_day:      parseInt(fd.get("sip_day")),
    start_date:   fd.get("start_date") || null,
    end_date:     fd.get("end_date") || null,
    notes:        fd.get("notes") || null,
  };
  try {
    await api("/api/sip-schedules", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    sipDialog.close();
    await loadSipSchedules();
  } catch (err) { alert("Save failed: " + err.message); }
});

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
    if (!chartsPaused && fiGrowthChart) fiGrowthChart.destroy();
    fiGrowthChart = chartsPaused ? fiGrowthChart : new Chart(gcCtx, {
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
    if (!chartsPaused && fiCashflowChart) fiCashflowChart.destroy();
    fiCashflowChart = chartsPaused ? fiCashflowChart : new Chart(cfCtx, {
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
    if (!chartsPaused && fiFyChart) fiFyChart.destroy();
    fiFyChart = chartsPaused ? fiFyChart : new Chart(fyCtx, {
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
        `<strong>${w.bank}</strong>: ${fmtINR(w.fy_interest)} FY interest → est. TDS ${fmtINR(w.tds)}`
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

    const isFI     = activeSegment === "FI";
    const isEPF    = activeSegment === "EPF";
    const isGlobal = activeSegment === "GLOBAL";
    const isBonds  = activeSegment === "BONDS";
    const isXray   = activeSegment === "XRAY";

    // X-Ray tab: show only the allocation section, hide everything else
    const allocSection = document.querySelector('[data-section="allocation"]');
    if (allocSection) allocSection.style.display = isXray ? "" : "none";

    if (isXray) {
      // Hide all regular panels
      ["xirr","realized","chart","holdings","ca","aliases","txns","nw-chart"].forEach(sec => {
        const el = document.querySelector(`[data-section="${sec}"]`);
        if (el) el.style.display = "none";
      });
      const summarySection = document.getElementById("summary-cards");
      if (summarySection) summarySection.style.display = "none";
      ["global-section","bonds-section","fi-section","fi-charts-section","epf-section"].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.display = "none";
      });
      loadAllocation();
      return;
    }

    // Show/hide folio column in holdings table based on segment
    const folioTh = document.getElementById("th-folio");
    const symTh   = document.getElementById("th-symbol");
    if (folioTh) folioTh.style.display = activeSegment === "MF" ? "" : "none";
    if (symTh)   symTh.textContent     = activeSegment === "MF" ? "Scheme" : "Symbol";

    // Section visibility for special tabs
    const globalSec = document.getElementById("global-section");
    if (globalSec) globalSec.style.display = isGlobal ? "" : "none";
    const bondsSec = document.getElementById("bonds-section");
    if (bondsSec) bondsSec.style.display = isBonds ? "" : "none";

    // Equity/MF panels — hidden on FI, EPF, Global, and Bonds tabs
    const hideOnSpecial = isFI || isEPF || isGlobal || isBonds;
    const equityOnlyPanels = ["xirr", "realized", "chart", "holdings", "ca", "aliases", "txns", "nw-chart"];
    equityOnlyPanels.forEach(sec => {
      const el = document.querySelector(`[data-section="${sec}"]`);
      if (el) el.style.display = hideOnSpecial ? "none" : "";
    });

    // FI-specific panels — only visible on FI tab
    const fiSection      = document.getElementById("fi-section");
    const fiChartSection = document.getElementById("fi-charts-section");
    if (fiSection)      fiSection.style.display      = isFI ? "" : "none";
    if (fiChartSection) fiChartSection.style.display = isFI ? "" : "none";

    // EPF section — only on EPF tab
    const epfSec = document.getElementById("epf-section");
    if (epfSec) epfSec.style.display = isEPF ? "" : "none";

    // Generic summary cards hidden on FI, EPF, Global, and Bonds tabs
    const summarySection = document.getElementById("summary-cards");
    if (summarySection) summarySection.style.display = hideOnSpecial ? "none" : "";

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

    // Equity + MF portfolio XIRR — headline box with active/closed split footnote
    {
      // Label reflects the active tab — the XIRR is already segment-scoped via segQS()
      const segLabel = activeSegment === "EQ" ? "equities"
                     : activeSegment === "MF" ? "mutual funds"
                     : "equities + mutual funds";
      const subText = r.from_date ? `${r.from_date} → ${r.to_date}` : segLabel;
      // Active vs closed split: only available on all-time view (no from_date)
      let splitHtml = "";
      if (!r.from_date && (r.active_xirr != null || r.closed_xirr != null)) {
        const activePart  = r.active_xirr  != null
          ? `Active: <span class="split-val ${cls(r.active_xirr)}">${fmtPct(r.active_xirr)}</span>`  : "";
        const closedPart  = r.closed_xirr  != null
          ? `Exited: <span class="split-val ${cls(r.closed_xirr)}">${fmtPct(r.closed_xirr)}</span>` : "";
        const sep = activePart && closedPart ? " &nbsp;·&nbsp; " : "";
        splitHtml = `<div class="xirr-split">${activePart}${sep}${closedPart}</div>`;
      }
      html += `<div class="xirr-box portfolio-box">
        <div class="label">Portfolio XIRR</div>
        <div class="xirr-val ${cls(r.portfolio_xirr)}">${fmtPct(r.portfolio_xirr)}</div>
        <div class="sub">${subText}</div>
        ${splitHtml}
      </div>`;
    }

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

// --- Portfolio X-Ray ---

const _CAP_COLOR    = { large:"#38bdf8", mid:"#818cf8", small:"#34d399", unclassified:"#64748b" };
const _SECT_PALETTE = ["#38bdf8","#818cf8","#fb923c","#f472b6","#a78bfa","#34d399","#4ade80","#facc15","#f87171","#2dd4bf","#c084fc","#6ee7b7"];
const _allocCharts  = {};

function _destroyAllocCharts() {
  for (const k of Object.keys(_allocCharts)) {
    try { if (!chartsPaused) _allocCharts[k]?.destroy(); } catch (_) {}
    delete _allocCharts[k];
  }
}

// Small donut used only for sector overview
function _makeDonut(canvasId, items, colors, labelFn, onClickFn) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !items.length) return;
  const chart = chartsPaused ? _allocCharts[canvasId] : new Chart(canvas, {
    type: "doughnut",
    data: {
      labels: items.map(labelFn),
      datasets: [{ data: items.map(i => i.value), backgroundColor: colors,
        borderWidth: 2, borderColor: "#0d1b2e", hoverOffset: 6 }]
    },
    options: {
      cutout: "65%",
      animation: { duration: 500 },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "#1e293b", borderColor: "#334155", borderWidth: 1,
          callbacks: { label: ctx => `  ${labelFn(items[ctx.dataIndex])}: ${items[ctx.dataIndex].pct}%` }
        }
      },
      onClick: (_, els) => { if (els.length) onClickFn(els[0].index); }
    }
  });
  _allocCharts[canvasId] = chart;
  if (typeof applyChartBlur === "function") applyChartBlur();  // blur wrapper if privacy on
}

// Holdings table rows (shared between cap drill and sector/mf details)
function _holdingRows(holdings) {
  return holdings.map(h => {
    // MF holdings have ISINs as symbols (INF... / IN0...).
    // Show the full scheme name as the primary bold cell; ISIN as secondary muted.
    // For equities, keep symbol as primary and display_name as secondary.
    const isMF = /^IN[F0]/i.test(h.symbol);
    const hasSchemeName = h.display_name && h.display_name !== h.symbol;
    const primary   = (isMF && hasSchemeName) ? escapeHtml(h.display_name) : h.symbol;
    const secondary = (isMF && hasSchemeName) ? `<span style="font-size:10px;color:var(--muted)">${h.symbol}</span>`
                    : (hasSchemeName ? escapeHtml(h.display_name) : "—");
    return `
    <tr>
      <td class="sym" style="${isMF ? "font-weight:500;font-size:12px;white-space:normal;max-width:220px;line-height:1.4" : ""}">${primary}</td>
      <td class="xray-nm">${secondary}</td>
      <td class="num">${fmtINR(h.value)}</td>
      <td class="num">${h.pct}%</td>
      <td class="num ${cls(h.pct_return)}">${fmtPct(h.pct_return)}</td>
      <td class="num ${cls(h.xirr)}">${fmtPct(h.xirr)}</td>
    </tr>`;
  }).join("");
}

function _holdingsTable(holdings) {
  const isMFBatch = holdings.length > 0 && /^IN[F0]/i.test(holdings[0]?.symbol || "");
  return `<div style="overflow-x:auto">
    <table class="xray-holdings-tbl">
      <thead><tr><th>${isMFBatch ? "Scheme" : "Symbol"}</th><th>${isMFBatch ? "ISIN" : "Name"}</th><th class="num">Value</th><th class="num">Weight</th><th class="num">Return</th><th class="num">XIRR</th></tr></thead>
      <tbody>${_holdingRows(holdings)}</tbody>
    </table></div>`;
}

// Market cap: show holdings for the active pill category
function _showCapDrill(items, colors, idx) {
  const drill = document.getElementById("xray-cap-drill");
  if (!drill || !items[idx]) return;
  const item = items[idx], color = colors[idx];
  drill.innerHTML = `
    <div class="xray-drill-header">
      <span class="xray-drill-dot" style="background:${color}"></span>
      <span>${item.label}</span>
      <span class="muted small">${fmtINR(item.value)} · ${item.holdings.length} stock${item.holdings.length !== 1 ? "s" : ""}</span>
    </div>
    ${_holdingsTable(item.holdings)}`;
}

// Sector / MF: toggle inline detail panel under the clicked row
function _toggleRowDetail(items, colors, detailPrefix, rowAttr, idx, labelFn) {
  const detailEl = document.getElementById(`${detailPrefix}-${idx}`);
  if (!detailEl) return;
  const isOpen = detailEl.style.display !== "none";

  // collapse all
  document.querySelectorAll(`[id^="${detailPrefix}-"]`).forEach(el => { el.style.display = "none"; });
  document.querySelectorAll(`.xray-sector-row[${rowAttr}]`).forEach(r => r.classList.remove("active"));

  if (!isOpen) {
    detailEl.style.display = "block";
    document.querySelector(`.xray-sector-row[${rowAttr}="${idx}"]`)?.classList.add("active");
    const item = items[idx], color = colors[idx], label = labelFn(item);
    detailEl.innerHTML = `
      <div class="xray-detail-header">
        <span class="xray-detail-dot" style="background:${color}"></span>
        <span class="xray-detail-title">${label}</span>
        <span class="xray-detail-meta">${fmtINR(item.value)} · ${item.pct}%</span>
      </div>
      ${_holdingsTable(item.holdings)}`;
  }
}

// ---- X-Ray insights: data-driven observations + rotating curated tips ----
// Educational, not advice. Data-driven cards recompute from the live allocation
// each load; curated tips (with sources) are rotated for variety.

const CURATED_TIPS = [
  { text: "Large / Mid / Small cap = the top 100 / next 150 / next 250 listed companies by market cap.", source: "SEBI / AMFI categorisation" },
  { text: "Diversification can lower risk without lowering expected return — often called the only 'free lunch' in investing.", source: "Modern Portfolio Theory, Harry Markowitz (1952)" },
  { text: "Beyond ~20–30 stocks, extra holdings add little diversification — Peter Lynch called over-diversifying 'diworsification'.", source: "Peter Lynch, One Up on Wall Street" },
  { text: "Time in the market usually beats timing the market — steady investing tends to win over guessing tops and bottoms.", source: "John Bogle / Bogleheads" },
  { text: "Small caps can outrun large caps in bull runs but fall harder in downturns — higher risk, higher potential reward.", source: "SEBI risk classification" },
  { text: "Costs compound too: lower expense ratios and fewer trades leave more of the return with you.", source: "John Bogle, Common Sense on Mutual Funds" },
  { text: "A stock's cap category and sector aren't fixed — they shift as the company and market move, so review periodically.", source: "general" },
  { text: "Rupee-cost averaging (SIPs) spreads your entry price over time and reduces the risk of buying everything at a peak.", source: "general" },
  { text: "Past performance doesn't guarantee future results — a strong recent return isn't a promise.", source: "SEBI-mandated disclaimer" },
  { text: "Rebalancing to target weights trims winners and tops up laggards — a disciplined 'buy low, sell high'.", source: "Bogleheads" },
];

// % of equity held in the top N holdings (holdings carry pct of the equity total).
function _topHoldingsConcentration(eq, n = 3) {
  const all = (eq.by_market_cap || []).flatMap(g => g.holdings || []);
  all.sort((a, b) => (b.value || 0) - (a.value || 0));
  const topN = all.slice(0, n).reduce((s, h) => s + (h.pct || 0), 0);
  return { topN: Math.round(topN), top1: all.length ? Math.round(all[0].pct || 0) : 0 };
}

// Observations about THIS portfolio (recomputed every load). Returns up to 4.
function _xrayInsights(eq, mf) {
  const out = [];
  if (eq.unclassified_count > 0) {
    const total = eq.by_market_cap.reduce((s, r) => s + r.holdings.length, 0) || 1;
    if (eq.unclassified_count / total >= 0.3)
      out.push({ kind: "warn", text: `${eq.unclassified_count} stocks have no market-cap data — click <em>Refresh market data</em> to classify them.`, source: "" });
  }
  const topSector = (eq.by_sector || [])[0];
  if (topSector && topSector.pct > 30)
    out.push({ kind: "warn", text: `Concentrated in <strong>${topSector.sector}</strong> at ${topSector.pct}%. Spreading across sectors reduces company- and sector-specific risk.`, source: "Modern Portfolio Theory, Harry Markowitz (1952)" });
  const conc = _topHoldingsConcentration(eq, 3);
  if (conc.top1 > 15 || conc.topN > 40)
    out.push({ kind: "warn", text: `Your top 3 holdings are <strong>${conc.topN}%</strong> of equity — concentrated single-stock risk if any one stumbles.`, source: "general diversification principle" });
  const small = (eq.by_market_cap || []).find(r => r.category === "small");
  if (small && small.pct > 30)
    out.push({ kind: "warn", text: `Small caps are <strong>${small.pct}%</strong> of equity — historically higher volatility than large caps.`, source: "SEBI risk classification; NIFTY Smallcap 250 vs NIFTY 100" });
  const large = (eq.by_market_cap || []).find(r => r.category === "large");
  if (large && large.pct > 60)
    out.push({ kind: "info", text: `Large-cap heavy at <strong>${large.pct}%</strong> — typically steadier but slower-growing. Large cap = the top 100 companies by market cap.`, source: "SEBI / AMFI categorisation" });
  const topCat = (mf.by_category || [])[0];
  if (topCat && topCat.pct > 50)
    out.push({ kind: "info", text: `Mutual funds are tilted to <strong>${topCat.label || topCat.category}</strong> at ${topCat.pct}% — check it matches your intended style mix.`, source: "" });
  if (out.length === 0 && large && large.pct > 0 && (eq.by_sector || []).length >= 3)
    out.push({ kind: "good", text: `Nicely spread across ${eq.by_sector.length} sectors with a <strong>${large.pct}%</strong> large-cap core.`, source: "" });
  return out.slice(0, 4);
}

// Build the Insights panel: data-driven cards (static) + a curated-tip block that
// auto-rotates on a timer while the X-Ray is open.
let _insightsTimer = null;
const _INSIGHT_ICONS = { warn: "⚠️", info: "📊", good: "✅", tip: "💡" };

function _insightCard(i, isTip) {
  return `<div class="xray-insight-card ${isTip ? "tip" : i.kind}">
       <span class="xray-insight-icon">${isTip ? "💡" : (_INSIGHT_ICONS[i.kind] || "💡")}</span>
       <span>${i.text}${i.source ? `<span class="xray-insight-src">— ${i.source}</span>` : ""}</span>
     </div>`;
}
function _renderTipsHTML() {
  return [...CURATED_TIPS].sort(() => Math.random() - 0.5).slice(0, 2).map(t => _insightCard(t, true)).join("");
}
function _renderInsightsPanel(eq, mf) {
  const cards = _xrayInsights(eq, mf).map(i => _insightCard(i, false)).join("");
  const tips = _renderTipsHTML();
  if (!cards && !tips) return "";
  return `<div class="xray-insights">
      <div class="xray-block-title" style="margin-bottom:10px">Insights</div>
      ${cards}
      <div id="xray-tips" class="xray-tips">${tips}</div>
      <div class="xray-insight-disclaimer">ℹ️ General educational information, not financial advice.</div>
    </div>`;
}
// Refresh just the curated tips every 12s while on the X-Ray tab; self-stops on leave.
function _rotateInsightTips() {
  const el = document.getElementById("xray-tips");
  if (!el || activeSegment !== "XRAY") {
    if (_insightsTimer) { clearInterval(_insightsTimer); _insightsTimer = null; }
    return;
  }
  el.style.opacity = "0";
  setTimeout(() => { el.innerHTML = _renderTipsHTML(); el.style.opacity = "1"; }, 250);
}
function _startInsightRotation() {
  if (_insightsTimer) clearInterval(_insightsTimer);
  _insightsTimer = setInterval(_rotateInsightTips, 12000);
}

async function loadAllocation() {
  const content = document.getElementById("allocation-content");
  if (!content) return;
  _destroyAllocCharts();
  content.innerHTML = `<div class="muted small">Loading…</div>`;

  try {
    const data = await api("/api/allocation");
    const eq = data.equity, mf = data.mf;

    if (!eq.total_value && !mf.total_value) {
      content.innerHTML = `<div class="muted small">No holdings found. Import transactions first.</div>`;
      return;
    }

    const capColors  = eq.by_market_cap.map(r => _CAP_COLOR[r.category] || "#64748b");
    const sectColors = eq.by_sector.map((_, i) => _SECT_PALETTE[i % _SECT_PALETTE.length]);
    const mfColors   = mf.by_category.map((_, i) => _SECT_PALETTE[i % _SECT_PALETTE.length]);

    let html = _renderInsightsPanel(eq, mf);

    // ── Market Cap block ─────────────────────────────────────────────────
    if (eq.total_value > 0 && eq.by_market_cap.length > 0) {
      const propSegs = eq.by_market_cap.map((r, i) =>
        `<div class="xray-prop-seg" style="width:${r.pct}%;background:${capColors[i]}" title="${r.label}: ${r.pct}%"></div>`
      ).join("");

      const pills = eq.by_market_cap.map((r, i) =>
        `<button class="xray-pill${i === 0 ? " active" : ""}" data-cap-idx="${i}"
          style="${i === 0 ? `background:${capColors[i]}26;border-color:${capColors[i]}` : ""}">
          <span class="xray-pill-dot" style="background:${capColors[i]}"></span>
          <span class="xray-pill-label">${r.label}</span>
          <span class="xray-pill-pct">${r.pct}%</span>
        </button>`
      ).join("");

      html += `
        <div class="xray-block">
          <div class="xray-block-header">
            <span class="xray-block-title">Market Cap Allocation</span>
            <span class="xray-block-meta">${eq.by_market_cap.reduce((s,r)=>s+r.holdings.length,0)} stocks
              ${eq.unclassified_count > 0 ? `· <span class="xray-hint">${eq.unclassified_count} unclassified</span>` : ""}
            </span>
          </div>
          <div class="xray-prop-bar">${propSegs}</div>
          <div class="xray-pills" id="xray-cap-pills">${pills}</div>
          <div class="xray-cap-drill" id="xray-cap-drill"></div>
        </div>`;
    }

    // ── Sector block ──────────────────────────────────────────────────────
    if (eq.total_value > 0 && eq.by_sector.length > 0) {
      const sectorRows = eq.by_sector.map((r, i) => `
        <div class="xray-sector-row" data-sect-idx="${i}">
          <span class="xray-sector-dot" style="background:${sectColors[i]}"></span>
          <span class="xray-sector-name">${r.sector}</span>
          <span class="xray-sector-pct">${r.pct}%</span>
          <span class="xray-sector-val">${fmtINR(r.value)}</span>
          <span class="xray-sector-arrow">›</span>
        </div>
        <div class="xray-row-detail" id="xray-sect-detail-${i}" style="display:none"></div>`
      ).join("");

      html += `
        <div class="xray-block">
          <div class="xray-block-header">
            <span class="xray-block-title">Sector Allocation</span>
            <span class="xray-block-meta">${eq.by_sector.length} sectors</span>
          </div>
          <div class="xray-sector-layout">
            <div class="xray-sector-donut">
              <canvas id="alloc-sector-chart" width="110" height="110"></canvas>
            </div>
            <div class="xray-sector-rows" id="xray-sector-rows">${sectorRows}</div>
          </div>
        </div>`;
    }

    // ── MF block ──────────────────────────────────────────────────────────
    if (mf.total_value > 0 && mf.by_category.length > 0) {
      // Top 4 headline categories shown individually; everything else collapsed into "Others"
      const TOP_MF = ["Large Cap", "Mid Cap", "Large & Mid Cap", "Flexi Cap", "Small Cap"];
      const topCats   = mf.by_category.filter(r => TOP_MF.includes(r.category));
      const otherCats = mf.by_category.filter(r => !TOP_MF.includes(r.category));

      // Top-category rows
      let mfRows = topCats.map(r => {
        const i = mf.by_category.indexOf(r);
        return `
        <div class="xray-sector-row" data-mf-idx="${i}">
          <span class="xray-sector-dot" style="background:${mfColors[i]}"></span>
          <span class="xray-sector-name">${r.category}</span>
          <span class="xray-sector-pct">${r.pct}%</span>
          <span class="xray-sector-val">${fmtINR(r.value)}</span>
          <span class="xray-sector-arrow">›</span>
        </div>
        <div class="xray-row-detail" id="xray-mf-detail-${i}" style="display:none"></div>`;
      }).join("");

      // "Others" row + expandable sub-categories
      if (otherCats.length > 0) {
        const othersVal = otherCats.reduce((s, r) => s + r.value, 0);
        const othersPct = Math.round(otherCats.reduce((s, r) => s + r.pct, 0) * 100) / 100;
        const subRows = otherCats.map((r, si) => {
          const i = mf.by_category.indexOf(r);
          return `
          <div class="xray-sector-row xray-subcat-row" data-subcat-idx="${si}" data-mf-idx="${i}">
            <span class="xray-sector-dot" style="background:${mfColors[i]}"></span>
            <span class="xray-sector-name">${r.category}</span>
            <span class="xray-sector-pct">${r.pct}%</span>
            <span class="xray-sector-val">${fmtINR(r.value)}</span>
            <span class="xray-sector-arrow">›</span>
          </div>
          <div class="xray-row-detail" id="xray-subcat-detail-${si}" style="display:none"></div>`;
        }).join("");
        mfRows += `
        <div class="xray-sector-row" data-mf-others="1">
          <span class="xray-sector-dot" style="background:#64748b"></span>
          <span class="xray-sector-name">Others <span style="font-size:11px;color:var(--muted);font-weight:400">${otherCats.length} categories</span></span>
          <span class="xray-sector-pct">${othersPct}%</span>
          <span class="xray-sector-val">${fmtINR(othersVal)}</span>
          <span class="xray-sector-arrow">›</span>
        </div>
        <div class="xray-row-detail" id="xray-mf-others-detail" style="display:none">
          <div style="padding:4px 0 4px 14px">${subRows}</div>
        </div>`;
      }

      html += `
        <div class="xray-block">
          <div class="xray-block-header">
            <span class="xray-block-title">Mutual Fund Categories</span>
            <span class="xray-block-meta">${mf.by_category.reduce((s,r)=>s+r.holdings.length,0)} funds
              <span class="xray-hint">· SEBI category from scheme name</span>
            </span>
          </div>
          <div class="xray-sector-rows" id="xray-mf-rows">${mfRows}</div>
        </div>`;
    }

    content.innerHTML = html;
    _startInsightRotation();   // begin cycling the curated tips

    // ── Wire interactions ────────────────────────────────────────────────

    // Market cap pills → swap drill table
    if (eq.by_market_cap.length) {
      _showCapDrill(eq.by_market_cap, capColors, 0); // show Large Cap by default
      document.getElementById("xray-cap-pills")?.querySelectorAll(".xray-pill").forEach(pill => {
        pill.addEventListener("click", () => {
          const idx = +pill.dataset.capIdx;
          document.querySelectorAll(".xray-pill[data-cap-idx]").forEach((p, i) => {
            const on = i === idx;
            p.classList.toggle("active", on);
            p.style.background = on ? `${capColors[i]}26` : "";
            p.style.borderColor = on ? capColors[i] : "";
          });
          _showCapDrill(eq.by_market_cap, capColors, idx);
        });
      });
    }

    // Sector rows → inline expand
    if (eq.by_sector.length) {
      _makeDonut("alloc-sector-chart", eq.by_sector, sectColors, r => r.sector,
        idx => _toggleRowDetail(eq.by_sector, sectColors, "xray-sect-detail", "data-sect-idx", idx, r => r.sector));
      document.getElementById("xray-sector-rows")?.querySelectorAll(".xray-sector-row[data-sect-idx]").forEach(row =>
        row.addEventListener("click", () => _toggleRowDetail(
          eq.by_sector, sectColors, "xray-sect-detail", "data-sect-idx", +row.dataset.sectIdx, r => r.sector))
      );
    }

    // MF rows → three-level expand
    if (mf.by_category.length) {
      const mfRowsEl = document.getElementById("xray-mf-rows");

      // Top-category rows (direct expand to holdings)
      mfRowsEl?.querySelectorAll(".xray-sector-row[data-mf-idx]:not(.xray-subcat-row)").forEach(row =>
        row.addEventListener("click", () => _toggleRowDetail(
          mf.by_category, mfColors, "xray-mf-detail", "data-mf-idx", +row.dataset.mfIdx, r => r.category))
      );

      // "Others" row → expand/collapse sub-category list
      const othersRow    = mfRowsEl?.querySelector(".xray-sector-row[data-mf-others]");
      const othersDetail = document.getElementById("xray-mf-others-detail");
      if (othersRow && othersDetail) {
        othersRow.addEventListener("click", () => {
          const open = othersDetail.style.display !== "none";
          othersDetail.style.display = open ? "none" : "block";
          othersRow.classList.toggle("active", !open);
        });
      }

      // Sub-category rows (inside Others) → expand to individual funds
      mfRowsEl?.querySelectorAll(".xray-subcat-row[data-subcat-idx]").forEach(row => {
        row.addEventListener("click", e => {
          e.stopPropagation(); // don't bubble to "Others" toggle
          const si     = +row.dataset.subcatIdx;
          const mfIdx  = +row.dataset.mfIdx;
          const detail = document.getElementById(`xray-subcat-detail-${si}`);
          const isOpen = detail?.style.display !== "none";

          // Close other sub-details
          document.querySelectorAll('[id^="xray-subcat-detail-"]').forEach(el => { el.style.display = "none"; });
          document.querySelectorAll(".xray-subcat-row").forEach(r => r.classList.remove("active"));

          if (!isOpen && detail) {
            detail.style.display = "block";
            row.classList.add("active");
            const r = mf.by_category[mfIdx];
            detail.innerHTML = `
              <div class="xray-detail-header">
                <span class="xray-detail-dot" style="background:${mfColors[mfIdx]}"></span>
                <span class="xray-detail-title">${r.category}</span>
                <span class="xray-detail-meta">${fmtINR(r.value)} · ${r.pct}%</span>
              </div>
              ${_holdingsTable(r.holdings)}`;
          }
        });
      });
    }

  } catch (e) {
    content.innerHTML = `<div class="neg small">Failed: ${escapeHtml(e.message)}</div>`;
  }
}

document.getElementById("refresh-market-meta-btn").addEventListener("click", async () => {
  const btn = document.getElementById("refresh-market-meta-btn");
  btn.disabled = true;
  btn.textContent = "Fetching…";
  try {
    const r = await api("/api/refresh-market-meta", { method: "POST" });
    btn.textContent = `Done (${r.updated}/${r.total})`;
    await loadAllocation();
    setTimeout(() => { btn.textContent = "Refresh market data"; btn.disabled = false; }, 3000);
  } catch (e) {
    btn.textContent = "Failed — retry";
    btn.disabled = false;
  }
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

  // On MF/All, flag that switches are booked as redeem+re-buy (tax-correct), which
  // raises Invested vs apps that carry the old cost forward. Separate ⓘ icon so the
  // value's click/hover-to-reveal-exact behaviour is untouched.
  const investedInfo = (activeSegment === "MF" || activeSegment === "all")
    ? ` <span class="info-i" title="Fund switches are treated as redeem + re-buy (the Indian tax treatment): the switch-out gain is realised into Realized P&L and the new fund's cost basis becomes its switch-in value. Apps that carry the old cost forward (e.g. INDMoney) may show a lower Invested and higher unrealised return — the total return is the same.">&#9432;</span>`
    : "";
  cards.innerHTML += card("Invested" + investedInfo, amt(s.invested), "active holdings cost basis");
  cards.innerHTML += card("Current Value", amt(s.current_value), "active holdings at market");
  // Day's gain — change since the previous close (needs a price refresh to be current)
  cards.innerHTML += card(
    "Day's Gain",
    amt(s.day_change),
    (s.day_change_pct != null ? fmtPct(s.day_change_pct) + " · " : "") + "since prev close",
    cls(s.day_change)
  );
  // Unrealized P&L — directly comparable to Zerodha's portfolio widget
  cards.innerHTML += card(
    "Unrealized P&L",
    amt(s.unrealized_pnl),
    fmtPct(s.unrealized_pnl != null && s.invested ? s.unrealized_pnl / s.invested : null) + " · active only",
    cls(s.unrealized_pnl)
  );
  // Realized P&L — from all sold positions (shown separately in Zerodha's P&L report)
  cards.innerHTML += card(
    "Realized P&L",
    amt(s.realized_pnl),
    "sold positions",
    cls(s.realized_pnl)
  );
  // Total = unrealized + realized
  cards.innerHTML += card(
    "Total P&L",
    amt(s.total_pnl),
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
    // For active positions show UNREALIZED P&L only.
    // Realized gains (e.g. from a fund switch) live in the Realized P&L section.
    // Combining them here shows "+₹13k P&L" beside "Invested ₹1k / Current ₹909" — confusing.
    // For closed positions (qty=0) show realized P&L — that's all that remains.
    const isActive = (r.quantity || 0) > 0;
    const pnl = isActive ? (r.unrealized_pnl ?? 0) : (r.realized_pnl ?? 0);
    const hasRealized = isActive && Math.abs(r.realized_pnl || 0) > 1;
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
        <td class="num">${amt(r.avg_cost)}</td>
        <td class="num">${amt(r.current_price)}</td>
        <td class="num">${amt(r.invested)}</td>
        <td class="num">${amt(r.current_value)}</td>
        <td class="num ${cls(r.day_change)}">${r.day_change != null ? `${amt(r.day_change)} <span class="muted" style="font-size:10px">${fmtPct(r.day_change_pct)}</span>` : "—"}</td>
        <td class="num ${cls(pnl)}">${amt(pnl)}${hasRealized ? `<div class="muted" style="font-size:10px" title="Realized gain from past switch/sale">${fmtINR(r.realized_pnl)} realized</div>` : ''}</td>
        <td class="num ${cls(r.pct_return)}">${fmtPct(r.pct_return)}</td>
        <td class="num ${cls(r.xirr)}">${fmtPct(r.xirr)}</td>
      </tr>`;
  }
  if (!tbody.innerHTML) {
    tbody.innerHTML = `<tr><td colspan="11" class="muted" style="text-align:center;padding:20px;">No holdings yet — import your tradebook to get started.</td></tr>`;
  }
}

// wire sort clicks
document.querySelectorAll("#holdings-table th.sortable").forEach((th) => {
  th.addEventListener("click", () => sortHoldings(th.dataset.col));
});

// hide/show closed positions — no server call needed, data already in holdingsData
document.getElementById("hide-closed").addEventListener("change", renderHoldings);

async function loadHoldings() {
  holdingsData = _useCache && holdingsData.length ? holdingsData : await api(`/api/holdings${segParam()}`);
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
  if (!chartsPaused && chart) chart.destroy();

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

  chart = chartsPaused ? chart : new Chart(ctx, {
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

// --- EPF Passbook import (shared handler used by both the Transactions button and EPF tab button) ---
async function runEpfImport(file, statusEl, progressFill, msgEl) {
  // Show progress bar indeterminate animation
  if (statusEl)     statusEl.style.display = "";
  if (progressFill) { progressFill.style.width = "30%"; progressFill.style.transition = "none"; }
  if (msgEl)        msgEl.innerHTML = `<span class="muted">Reading PDF…</span>`;

  // Animate to 70% while waiting
  setTimeout(() => { if (progressFill) { progressFill.style.transition = "width 1.5s"; progressFill.style.width = "70%"; } }, 100);

  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await api("/api/import-epf", { method: "POST", body: fd });
    if (progressFill) progressFill.style.width = "100%";

    if (r.inserted > 0) {
      if (msgEl) msgEl.innerHTML =
        `<span class="pos">✓ Imported ${r.inserted} entries (${r.skipped_duplicates} duplicates skipped, ${r.entries_found} found in PDF).</span>`;
      await loadEpfSection();
    } else if (r.entries_found === 0) {
      if (msgEl) msgEl.innerHTML =
        `<span class="neg">⚠ No entries found in PDF. The passbook format may differ — ` +
        `<a href="#" id="epf-debug-link" style="color:var(--accent)">click here to see extracted text</a> for diagnosis.</span>`;
      // Wire debug link
      setTimeout(() => {
        const link = document.getElementById("epf-debug-link");
        if (link) link.addEventListener("click", async (ev) => {
          ev.preventDefault();
          const fd2 = new FormData(); fd2.append("file", file);
          try {
            const dbg = await api("/api/epf/debug-pdf", { method: "POST", body: fd2 });
            const pre = document.createElement("pre");
            pre.style.cssText = "font-size:11px;max-height:300px;overflow:auto;background:#0d1117;padding:8px;border-radius:4px;margin-top:8px;white-space:pre-wrap";
            pre.textContent = dbg.text_preview;
            link.parentElement.appendChild(pre);
          } catch (e2) { alert("Debug failed: " + e2.message); }
        });
      }, 100);
    } else {
      if (msgEl) msgEl.innerHTML =
        `<span class="muted">${r.inserted} new, ${r.skipped_duplicates} already imported (${r.entries_found} total found).</span>`;
    }
    if (r.errors?.length && msgEl) {
      msgEl.innerHTML += `<br><span class="neg">Errors: ${r.errors.slice(0, 3).map(escapeHtml).join("; ")}</span>`;
    }
  } catch (err) {
    if (progressFill) progressFill.style.width = "100%";
    if (progressFill) progressFill.style.background = "var(--neg)";
    if (msgEl) msgEl.innerHTML = `<span class="neg">✗ Import failed: ${escapeHtml(err.message)}</span>`;
  }
}

// EPF import from Transactions section (existing button)
document.getElementById("import-epf-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const result = document.getElementById("import-result");
  result.innerHTML = "";
  await runEpfImport(file, null, null, result);
  e.target.value = "";
});

// EPF import from EPF tab (new button in EPF section header)
document.getElementById("epf-tab-import")?.addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const statusEl = document.getElementById("epf-import-status");
  const fillEl   = document.getElementById("epf-progress-fill");
  const msgEl    = document.getElementById("epf-import-msg");
  fillEl.style.background = "var(--accent)";  // reset color
  await runEpfImport(file, statusEl, fillEl, msgEl);
  e.target.value = "";
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

// Instruments currently held (qty > 0) — used to suggest + restrict sell symbols.
// Fetched without a segment filter so the picker is complete regardless of active tab.
let sellableHoldings = [];

async function refreshSymbolPicker() {
  try {
    sellableHoldings = (await api("/api/holdings")).filter((h) => (h.quantity || 0) > 0);
  } catch {
    sellableHoldings = [];
  }
  const dl = document.getElementById("txn-symbol-list");
  if (!dl) return;
  dl.innerHTML = sellableHoldings
    .map((h) => {
      const name = h.display_name && h.display_name !== h.symbol ? `${h.display_name} · ` : "";
      return `<option value="${h.symbol}">${name}${h.segment} · ${fmtQty(h.quantity)} held</option>`;
    })
    .join("");
}

// When the typed/picked symbol matches a held instrument, auto-fill ISIN + segment
// so a sell can't be mismatched. (Programmatic value sets during edit don't fire 'change'.)
form.querySelector('input[name="symbol"]').addEventListener("change", (e) => {
  const sym = e.target.value.trim().toUpperCase();
  const match = sellableHoldings.find((h) => (h.symbol || "").toUpperCase() === sym);
  if (match) {
    form.querySelector('input[name="isin"]').value = match.isin || "";
    form.querySelector('select[name="segment"]').value = match.segment;
  }
});

document.getElementById("btn-add").addEventListener("click", () => {
  form.reset();
  form.querySelector('input[name="id"]').value = "";
  document.getElementById("txn-form-title").textContent = "Add transaction";
  form.querySelector('input[name="trade_date"]').valueAsDate = new Date();
  refreshSymbolPicker();   // fire-and-forget; list is ready before the user submits
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
  // Guard new sells against typos: only allow selling instruments you actually hold,
  // and not more than the held quantity. Skipped for edits (id present) because the
  // current holdings already reflect that historical sell.
  if (!id && payload.trade_type === "sell") {
    if (!sellableHoldings.length) await refreshSymbolPicker();
    const sym = (payload.symbol || "").trim().toUpperCase();
    const match = sellableHoldings.find(
      (h) => (h.symbol || "").toUpperCase() === sym && h.segment === payload.segment
    );
    if (!match) {
      alert(`You don't currently hold "${payload.symbol}" in ${payload.segment}.\n` +
            `Sells are only allowed for instruments you hold — pick one from the dropdown.`);
      return;
    }
    if (payload.quantity > match.quantity + 1e-6) {
      alert(`You only hold ${fmtQty(match.quantity)} units of ${payload.symbol}, ` +
            `so you can't sell ${fmtQty(payload.quantity)}.`);
      return;
    }
  }

  const url = id ? `/api/transactions/${id}` : "/api/transactions";
  const method = id ? "PATCH" : "POST";
  const saveBtn = document.getElementById("txn-save");
  saveBtn.disabled = true;
  saveBtn.textContent = "Saving…";
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
  } finally {
    saveBtn.disabled = false;
    saveBtn.textContent = "Save";
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

// --- privacy mode toggle ---
// Blur the chart canvases themselves (only the graph, not surrounding text like
// the Net Worth headline). Safe because we only ever blur AFTER charts are drawn
// and never recreate a chart while it's blurred (see unblurCharts + chartsPaused).
function applyChartBlur() {
  document.querySelectorAll("canvas").forEach((c) => c.classList.toggle("chart-blur", privacyMode));
}
function unblurCharts() {
  document.querySelectorAll("canvas").forEach((c) => c.classList.remove("chart-blur"));
}
// Reflects current state on <body> (icon) + blurs chart wrappers + button title.
function applyPrivacyUI() {
  document.body.classList.toggle("privacy-on", privacyMode);
  const btn = document.getElementById("btn-privacy");
  if (btn) btn.title = privacyMode ? "Privacy mode ON — click to show amounts" : "Privacy mode — hide amounts";
  applyChartBlur();
}
applyPrivacyUI();  // apply saved state before first render below
document.getElementById("btn-privacy")?.addEventListener("click", () => {
  privacyMode = !privacyMode;
  localStorage.setItem("privacy-mode", privacyMode ? "on" : "off");
  applyPrivacyUI();        // instant: icon + blur existing chart wrappers
  refreshAll(false, true); // re-render text only; skip chart recreation (no crash)
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

// Indeterminate top progress bar — shown while refreshAll's Phase 1 is in flight
function showLoading() { document.getElementById("loading-bar")?.classList.add("active"); }
function hideLoading() { document.getElementById("loading-bar")?.classList.remove("active"); }

async function refreshAll(tabSwitch = false, fast = false) {
  // `fast` (privacy toggle): re-render text only — keep charts (chartsPaused),
  // reuse cached data (_useCache), and skip the slow Phase 2 network calls.
  chartsPaused = fast;
  _useCache = fast;
  if (!fast) unblurCharts();   // charts will be (re)created — clear blur so Chart.js can't resize-loop
  const realizedPeriod = document.getElementById("realized-period-select").value;
  const xirrPeriod    = document.getElementById("xirr-period-select").value;

  // Phase 1 — fast (DB-only, no price fetching): render immediately
  const phase1 = [
    loadSummary(),
    loadHoldings(),
    loadFiHoldings(),    // pure math — no API calls, always fast
    loadFiCharts(),      // pure math — maturity timeline + cashflow forecast + FY interest
    loadSipSchedules(),  // DB read only — fast
    loadRealizedPnl(realizedPeriod),
    loadDataQuality(),
  ];
  // Management panels don't change on tab switch — skip them for speed
  if (!tabSwitch) {
    phase1.push(loadTransactions(), loadCorporateActions(), loadSymbolAliases());
  }
  showLoading();
  try {
    await Promise.all(phase1);
  } finally {
    hideLoading();
  }

  // Privacy toggle: re-mask Net Worth from cached data and stop — skip the slow
  // Phase 2 (yfinance/global, equity curve, XIRR recompute). Charts stay as-is,
  // blurred. Other tabs (EPF/Global/Bonds) re-mask when next selected.
  if (fast) {
    if (nwData) _renderNetWorth(nwData);
    applyChartBlur();
    return;
  }

  // Phase 2 — slow (equity curve + full XIRR + net worth): fire and forget
  Promise.all([
    loadEquityCurve(),
    loadXirrAnalysis(xirrPeriod),
    loadNetWorth(),
    loadEpfSection(),
    loadGlobalSection(),
    loadGlobalAnalytics(),   // XIRR + curve + tax (involves yfinance, slow)
    loadBondsSection(),
  ]).then(applyChartBlur).catch(console.error);  // re-blur wrappers of recreated charts
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
