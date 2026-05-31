# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [0.3.0] — 2026-05-31

### Added
- **Fixed Income section** — Bank FDs (cumulative & non-cumulative) and Recurring Deposits
  - Manual entry via a modal form: bank, principal/instalment, rate, compounding, start/maturity date, payout frequency, 80C tax-saver flag, optional opening deposit for RDs
  - Interest math: cumulative FD uses `P × (1 + r/n)^(n×t)`; non-cumulative FD tracks periodic payouts; RD uses standard Indian bank quarterly-compounding formula with each instalment compounded for its remaining tenure
  - Current value, maturity value, interest earned, this-FY interest, TDS warning (flags when aggregate FY interest from one bank > ₹40,000 at 10% TDS)
- **Fixed Income tab** in the portfolio filter (All | Equities | Mutual Funds | **Fixed Income**)
  - FI view hides equity/MF panels and shows FI-specific content only
  - FI holdings summary cards (Invested, Current Value, Interest Earned, This FY Interest, FI XIRR) at the top
  - Detail table: bank, type, principal, rate, start→maturity, current value, maturity value, FY interest, TDS flag, status badge
- **FI Analysis section** with three charts:
  - **Portfolio growth curve** — actual FI portfolio value vs configurable benchmarks (savings rate, standard FD rate, CPI inflation) — four lines, colour-coded
  - **Maturity Timeline** — horizontal Gantt bars per instrument showing start→maturity with days remaining
  - **Cashflow Forecast** — bar chart of expected inflows (maturity payouts + non-cumulative interest) over next 24 months
  - **FY Interest Income** — bar chart of taxable interest per financial year (last 5 FYs + current)
- **Configurable benchmark rates** stored in `data/fi_rates.json` (persists across restarts)
  - Defaults: savings 3.0% (SBI/major bank), standard FD 6.5%, inflation 4.5% (RBI CPI target zone)
  - "Edit rates" link in XIRR Analysis updates rates without server restart
  - Endpoints: `GET /api/fi/rates`, `PATCH /api/fi/rates`
- **Three-tier XIRR display on All tab**: equity+MF XIRR vs NIFTY benchmarks (top row) + FI XIRR vs savings/standard FD/inflation (bottom row, with divider)
  - FI cashflows intentionally excluded from equity XIRR — NIFTY benchmark comparison is only meaningful for market-linked assets
  - FI contributes to total wealth in summary cards (invested + current value)
- **Favicon** — SVG icon (upward line chart, dark background) served at `/favicon.ico` and linked in HTML; eliminates 404 errors in browser console
- **RD initial deposit field** — optional extra opening lump-sum deposited on start date in addition to monthly instalments (for non-standard RD products); standard RDs should leave this at 0

### Fixed
- **RD instalment counting off-by-one** — `(today.month - start.month)` was giving 2 for March→May when 3 instalments have been paid; all elapsed-instalment calculations now use `+1` to count the start-month payment correctly. Affects: `rd_value_on`, `interest_this_fy`, `compute_fi_summary`, combined invested totals
- **FI summary cards showing zeros on FI tab** — the generic summary cards queried the `transactions` table for `segment=FI` entries (which don't exist); fixed by hiding those cards on the FI tab and relying on the FI section's own mini-cards

### Changed
- FD/RD data stored in a separate `fixed_income` table (not the `transactions` table) — CRUD via `/api/fi` endpoints
- Combined portfolio XIRR (All tab) **no longer includes FI cashflows** — this was reverted because mixing guaranteed FD returns with equity XIRR makes benchmark comparison against NIFTY meaningless

---

## [0.2.2] — 2026-05-31

### Fixed
- **XIRR correctness regression** — `cache_only=True` was applied too broadly to `latest_close()` in holdings computation. On cold cache (e.g. newly imported MF schemes with no cached prices), holdings showed ₹0 current value, deflating combined XIRR from 12.62% to 7.85%. Root cause: the `have_start AND have_end` boundary probe in `get_close_series` fails when the exact boundary date has no market data (weekend/holiday), triggering a full multi-year history download even for a simple current-price lookup.
- **`latest_close()` rewritten** to use a direct `ORDER BY on_date DESC LIMIT 1` query — no boundary probing, no range scanning. Warm cache: ~0.3ms per instrument. Cold cache: fetches only the last 30 days, not full history.
- `cache_only=True` now only applied to `get_close_series()` for full multi-year historical ranges (equity curve, benchmark series) where it prevents years of redundant API calls. Current-price lookups always use live data as a fallback.

---

## [0.2.1] — 2026-05-31

### Performance
- **220× faster holdings** — analytics now read price_cache from SQLite only; live API calls (yfinance, AMFI) restricted to "Refresh prices" button
- **37× faster equity curve** — replaced per-day `qty_held_on()` calls (O(days × transactions)) with a single chronological event sweep (O(days + transactions))
- **Two-phase frontend loading** — summary cards and holdings table render in ~200ms; equity curve and XIRR analysis load in the background without blocking the page
- **Smarter tab switching** — switching between All / Equities / MF tabs skips reloading management panels (Transactions, Corporate Actions, Symbol Renames) that don't change on segment switch

### Changed
- `refresh-prices` endpoint now explicitly pre-warms the full price-cache date range for every held instrument and all four benchmark series, so the first post-refresh page load is fast
- Equity curve shows a subtle opacity fade while loading in the background

---

## [0.2.0] — 2026-05-31

### Added
- **Mutual Fund section** — CAS PDF importer for CAMS + KFintech Combined Consolidated Account Statement (full history from folio inception)
  - Handles: SIP, lumpsum purchases, switch-in/out, redemptions
  - Auto-skips stamp duty lines (0.005% entries) and administrative events
  - Deduplication by `CAS|folio|date|nav|units` — safe to re-import
  - Displays full scheme names (e.g. "Parag Parikh Flexi Cap Fund - Regular Plan Growth") + folio number
- **Portfolio filter tabs** — All | Equities | Mutual Funds; every panel responds (summary, holdings, XIRR, realized P&L, equity curve)
- **XIRR Analysis section** with period selector: All-time | 1Y | 3Y | 5Y | Current FY | last 4 FYs | Custom date range
- **Realized P&L with STCG/LTCG split** — same period selector; STCG (< 12 months, 20%) and LTCG (≥ 12 months, 12.5% above ₹1.25L) for Indian tax planning
- **Subperiod XIRR** — uses opening portfolio value as carry-in cashflow and closing value as terminal, correctly isolating a year without distortion from other periods
- **Data quality warnings** — detects orphan sells (IPO allotments without matching buy) and surfaces them with total missing proceeds
- **Collapsible sections** — every card has a ▲/▼ toggle; state saved in localStorage; management sections (Corporate Actions, Symbol Renames, Transactions) collapsed by default
- **Holdings quick-filter** — type a symbol to instantly filter the holdings table
- **Indian number formatting** — ₹1.52L, ₹10.5Cr instead of raw digits
- **Keyboard shortcuts** — `N` = Add transaction, `R` = Refresh prices, `/` = Focus holdings filter
- **Sticky header** — Refresh prices button stays visible while scrolling
- `folio` column added to transactions table (auto-migrated on startup)

### Fixed
- **Benchmark XIRR simulation** — sell side now uses the index position's actual value at the sell date, not the stock's sell price; this prevented IPO windfalls from artificially inflating benchmark XIRRs
- **Subperiod XIRR terminal value** — was using unfiltered holdings (EQ + MF together) even when a segment filter was active, causing inflated XIRRs (e.g. 90% instead of 24% for equities)

### Changed
- Summary cards split into Invested | Current Value | Unrealized P&L | Realized P&L | Total P&L (unrealized is directly comparable to Zerodha's portfolio widget)
- Equity curve subtitle now shows the actual base date and explains what a value of 200 means
- Default-collapsed: Corporate Actions, Symbol Renames, Transactions sections

---

## [0.1.0] — 2026-05-31

### Added
- **Zerodha tradebook import** — CSV and XLSX, auto-detects separator and encoding (including BOM), deduplicates by `trade_id`
- **Manual transaction entry** — add/edit/delete buy and sell transactions via a modal form
- **Live prices** — yfinance for NSE (`.NS`) with `.BO` BSE fallback; AMFI NAVAll.txt + mfapi.in for mutual funds and ETFs yfinance can't resolve
- **Holdings table** — qty, avg cost (split-adjusted), current price, invested, current value, P&L, % return, per-holding XIRR; sortable by every column
- **Corporate actions** — splits, bonuses, dividends, mergers, demergers; auto-sync from yfinance; fractional share payouts tracked in XIRR
  - Demerger type adjusts cost basis only (no qty change) — used for ITC Hotels spin-off from ITC
  - Correct chronological event replay: buys processed before sells on the same day (handles intraday trades correctly)
- **Symbol renames** — built-in map (ZOMATO → ETERNAL, GOLDETFADD → GOLDADD) + user-defined aliases persisted to `data/symbol_aliases.json`
- **Orphan sell detector** — identifies IPO allotment sells missing a matching buy
- **Portfolio XIRR** vs four benchmarks (NIFTY 50, NIFTY 500, NIFTY Midcap 150, NIFTY Smallcap 250) using correct cashflow-replay method
- **Equity curve** — portfolio vs benchmarks, rebased to 100 at first transaction date on the same cash-deployment schedule
- **XIRR solver** — `scipy.optimize.brentq` over [−99%, 10000%] with Newton-Raphson fallback
- **CSV export** of all transactions
- **Docker + docker-compose** — `./data` volume mounted for SQLite persistence across rebuilds
- **nginx reverse proxy snippet** for VPS HTTPS deployment
- SQLite with daily price cache — no external database required
