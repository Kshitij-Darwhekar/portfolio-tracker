# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [0.9.0] — 2026-06-14

### Added
- **Daily gain (day's P&L)** — the change since the previous close, in three places:
  - A sortable **Day** column in the Equities/MF holdings table (per-holding ₹ + %).
  - A **"Day's Gain"** summary card on All / Equities / Mutual Funds.
  - A **"Today ±₹… (…%)"** line under the Net Worth headline (combined EQ+MF; % of total net worth).
  - Computed as `qty × (latest close − previous close)` straight from `price_cache` — reuses `latest_close` (prev close = `latest_close(today − 1 day)`), no extra fetch. Reflects the change since the last price refresh (cache-based, not a live ticker). FD/EPF have no daily market price and don't contribute.

---

## [0.8.0] — 2026-06-14

### Added
- **X-Ray "Insights" panel** — replaces the single one-line smart insight with a small panel of:
  - **Data-driven observations** computed from your live allocation each load — sector concentration, small-cap / large-cap tilt, **top-3 single-stock concentration**, MF style tilt — each with a cited source. These update as the portfolio changes.
  - **Rotating educational tips** (Markowitz / Peter Lynch / John Bogle / SEBI-AMFI) that **auto-cycle every 12 s** with a fade while the X-Ray tab is open (the timer self-stops on leaving the tab); easy to extend via the `CURATED_TIPS` list.
  - Framed as **education, not advice** (explicit footer). Frontend-only — reuses the existing `/api/allocation` data, no backend change.

---

## [0.7.4] — 2026-06-14

### Changed
- **Large/Mid/Small-cap classification now uses the official NSE/AMFI ranking** instead of live yfinance market cap. Category is determined by membership in **NIFTY 100 / Midcap 150 / Smallcap 250** (the same top-100 / next-150 / next-250 ranking SEBI/AMFI use), matched to holdings by **ISIN, then symbol**.
  - Fixes misclassifications (e.g. APARINDS now correctly **Mid**, not Large) and stops a stock's category from flipping with its share price.
  - New `app/cap_classification.py` fetches the three NSE constituent CSVs (browser UA), caches the parsed map to `data/cap_classification.json` (disk-persisted, 7-day TTL) so a fetch failure / offline run falls back to the last good copy. Only the **Refresh market data** action hits the network.
  - `fetch_instrument_meta` now sources cap from the list; yfinance `marketCap` is only a fallback for the rare holding outside the top 500. Sector still comes from yfinance.

---

## [0.7.3] — 2026-06-14

### Changed
- **Nextcloud backup keeps a dated 7-day history** — `backup.sh` now uploads a dated copy (`portfolio-YYYY-MM-DD.db`) each run *in addition* to the `portfolio-latest.db` pointer, and prunes dated copies older than `KEEP` (default 7) days via WebDAV `DELETE` (sweeps a week of dates so gaps from days the Pi was off are cleaned up too). Previously only the single overwritten `-latest` file lived off-device, so there was no version history in Nextcloud.

---

## [0.7.2] — 2026-06-14

### Added
- **Responsive layout (mobile / tablet)** — the dashboard now adapts to small screens (it's used on a phone over Twingate). Summary cards go 2-per-row, multi-column grids (FI charts, realized P&L) collapse to one column, the Net Worth donut and X-Ray sector donut stack below their text, charts shorten, segment tabs wrap, and dialogs fit small screens. Previously there were *no* media queries at all.
- **Hover / tap to reveal exact amounts** — in the Equities/MF/All summary cards and holdings table, compact values (₹4.32L) reveal the exact figure (₹4,32,123.45) on **hover** (desktop) or **tap** (mobile) via a new `amt()` helper. Only abbreviated amounts (≥ ₹1L) get it; respects privacy mode (no reveal when masked); the Net Worth summary stays compact.

### Fixed
- **Mobile overflow / alignment** — section-header button groups (Transactions, Corporate Actions, Bonds) now wrap instead of pushing the page wider than the viewport; added a page-level `overflow-x` guard (tables keep their own scroll).
- **Multi-line button/badge labels** — "Import Tradebook (CSV/XLSX)", "Capital gains EXEMPT at maturity", etc. now stay on one line and flow to the next row instead of wrapping into 3–4 lines.
- **XIRR Analysis on mobile** — tiles now show 2-per-row with the Portfolio box full-width (was 5 tall stacked tiles).

---

## [0.7.1] — 2026-06-14

### Performance
- **Analytics result caching** — the heavy read endpoints (holdings, equity curve, summary, XIRR analysis, realized P&L, data-quality, and net worth) are now cached in-process, keyed by a cheap data fingerprint (transaction count + max-id + qty/price/fees sums + corporate-action count). Repeat dashboard loads drop from ~6s of backend work to ~0.2s per endpoint (e.g. summary 1.12s → 14ms, net worth 2.1s → 0.22s, equity-curve 1.1s → 0.22s).
  - 30-minute TTL; **any data change** (add/edit/delete txn, import, corporate action) busts the cache automatically via the fingerprint, so values are never stale.
  - **Refresh prices** invalidates the caches explicitly (prices change without a transaction change) and **pre-warms** holdings/curve/net-worth, so the first load after a refresh — including the nightly cron — is fast, not cold.
  - In-memory only: no schema/DB changes, computed values are identical. The *first* load after a backend restart is still a full ~6s compute; every load after that (within the TTL) is fast.

### Changed
- **`K` formatting scoped to the Net Worth summary** — the headline + by-account breakdown use the compact form (₹70.4K); detailed segment tabs (Equities/MF/Fixed Income/Bonds/etc.) keep full values (₹70,419.81) via a separate `fmtINRshort` helper.

---

## [0.7.0] — 2026-06-14

### Added
- **Privacy mode** — an eye-icon toggle in the header hides all monetary amounts and portfolio size across every tab (Equities, MF, FI, EPF, Global, Bonds, X-Ray, Net Worth) so the dashboard can be screenshotted/demoed without revealing wealth (like Zerodha Kite's privacy toggle).
  - ₹ and $ amounts plus quantities render as `••••`; **percentages stay visible** (XIRR, % returns, allocation %) so the performance story still shows.
  - All charts are blurred and their hover tooltips disabled (a `<canvas>` can't show dot-masked text).
  - State persists in `localStorage` and is applied before first paint.
  - Implemented via privacy-aware formatters (`fmtINR` / `fmtQty` / new `fmtUSD`); Global-tab USD values and the FD/EPF TDS warning were routed through them. Charts are blurred via a `.chart-blur` class on the canvas, and chart re-creation is skipped during a toggle (avoids a Chart.js resize-loop hang); the toggle reuses cached data and skips slow Phase 2 calls so it's fast.
- **`K` (thousands) number formatting** — `fmtINR` now shortens ₹1,000–99,999 to `K` (e.g. ₹70,419 → ₹70.4K), consistent with the existing L/Cr formatting.

---

## [0.6.2] — 2026-06-14

### Security
- **Container no longer runs as root** — the image now creates a non-root `appuser` (uid 1000, matching the host DB owner) and switches to it via `USER`. Keeps write access to the bind-mounted `./data` while dropping root, so a compromise via a malicious upload no longer yields root inside the container.

---

## [0.6.1] — 2026-06-13

### Added
- **Off-device backup to Nextcloud** — `scripts/backup.sh` can now upload the DB to Nextcloud over WebDAV (so it registers in Nextcloud and syncs to your devices), instead of only copying to a local folder. Configured via `NEXTCLOUD_URL` / `NEXTCLOUD_USER` / `NEXTCLOUD_APP_PASSWORD` / `NEXTCLOUD_REMOTE_DIR` in `.env` (use a Nextcloud **app password**, never the account password). `backup.sh` now sources `.env`, so backup secrets stay out of the crontab. Disabled when the vars are empty (plain local snapshots only).

---

## [0.6.0] — 2026-06-13

### Security
- **Optional HTTP Basic Auth** — set `APP_PASSWORD` (and optionally `APP_USERNAME`, default `admin`) to require a login on every request. Uses constant-time credential comparison. **Off by default** — when `APP_PASSWORD` is unset the app behaves exactly as before. Closes the "anyone on the LAN can read/modify financial data" gap for deployments where VPN-only isn't enough.
- **Debug endpoints gated** — `/api/debug/cashflows` and `/api/epf/debug-pdf` now return 404 unless `ENABLE_DEBUG_ENDPOINTS=1`. They are off by default, removing debug tooling from the production attack surface.
- Internal security review (no findings in committed code): SQL is fully parameterized (no injection), no secrets/PII/personal data in source or git history, uploaded PII-bearing PDFs are reliably deleted from `/tmp`, and EPF imports persist only financial fields. Remaining hardening (non-root container user, upload size limit) documented as follow-ups in the README.

### Added
- **Environment-driven configuration** — `docker-compose.yml` now reads `DATA_DIR`, `APP_USERNAME`, `APP_PASSWORD`, and `ENABLE_DEBUG_ENDPOINTS` from a `.env` file (see `.env.example`). The DB host path is no longer hard-coded, so the same tracked compose file works for local dev and the Pi without per-host edits — and `git pull` never conflicts on it.
- **Automation scripts** (`scripts/`):
  - `backup.sh` — timestamped SQLite online backup (`.backup`), 7-snapshot retention, optional off-device copy to a Nextcloud-synced folder
  - `refresh-prices.sh` — cron-friendly price/NAV refresh (auth-aware; reads creds from `.env`)
  - `update.sh` — backup → `git pull` → rebuild, in one command

---

## [0.5.1] — 2026-06-13

### Added
- **Sell guard** — adding a *new* sell is now restricted to instruments you actually hold. The Symbol field has a `<datalist>` autocomplete populated from current holdings (qty > 0, across all segments); picking a held instrument auto-fills its ISIN and segment. On submit, a sell is blocked with a clear message if the symbol isn't held or if the quantity exceeds the held units. Buys (including new instruments) and edits of historical sells are unaffected.
- **Loading indicator** — a thin indeterminate progress bar at the top of the page shows whenever the dashboard is reloading (Phase 1 of `refreshAll`) and clears when data is ready. Covers sells, tab switches, and price refreshes — removes the "frozen with no feedback" feeling after committing a transaction.
- **Save button feedback** — the transaction Save button disables and shows "Saving…" while the transaction commits and the dashboard reloads, preventing double-submits.

### Fixed
- **XIRR caption mislabelled on segment tabs** — the Portfolio XIRR sub-label always read "equities + mutual funds" even on the Equities or Mutual Funds tab. The XIRR value was always correctly segment-scoped (via `segQS`); only the caption was wrong. It now reads "equities", "mutual funds", or "equities + mutual funds" to match the active tab.

---

## [0.5.0] — 2026-06-05

### Added
- **Portfolio X-Ray** — dedicated tab (alongside All / Equities / Mutual Funds / Fixed Income / EPF / Global / Bonds) for allocation drill-down; lazy-loaded only when the tab is active
  - **Market Cap Allocation** — full-width segmented proportion bar (Large / Mid / Small / Unclassified) with pill tabs; clicking a pill filters the holdings table to that cap category with value, weight, return, and XIRR per stock
  - **Sector Allocation** — compact donut chart (left) + scrollable sector rows (right); clicking any row or donut segment expands an inline holdings table; chevron rotates on expand
  - **Mutual Fund Categories** — SEBI mandate category inferred from scheme name; top categories (Large Cap, Mid Cap, Large & Mid Cap, Flexi Cap, Small Cap) shown individually; all others collapsed into a single **Others** row that expands to reveal sub-categories, each further expandable to individual fund holdings
  - **Smart insight** — auto-generated one-liner at the top (e.g. sector concentration warning, small cap overweight notice)
  - **Scheme name as primary** in MF holdings tables — full scheme name shown in bold; ISIN shown as secondary muted text (previously reversed)

- **Market cap + sector tagging** — two new nullable columns on the `instruments` table: `market_cap_category` (`large` | `mid` | `small`) and `sector` (e.g. `"Technology"`)
  - Auto-migrated on startup
  - Populated via **Refresh market data** button → `POST /api/refresh-market-meta`; uses yfinance `.info` for equities, SEBI thresholds (Large ≥ ₹40,000 Cr, Mid ₹8,000–40,000 Cr, Small < ₹8,000 Cr)
  - Static override map (`INSTRUMENT_META_OVERRIDES`) for instruments yfinance cannot classify — applied at Refresh time (DB write) and at render time (runtime fallback): Gold ETFs → Gold; Liquid ETFs → Liquid / Debt; REITs → Real Estate / REITs; Index ETFs → Index ETF; SGBs → Gold / SGB
  - Overrides applied automatically at X-Ray render time so GOLDBEES, LIQUIDCASE, EMBASSY etc. show correct sectors even before clicking Refresh

### Fixed
- **MF category misclassification** — scheme names using single-word variants (`midcap`, `multicap`) or ampersand (`Large & Mid Cap`) now correctly classified. Tata Nifty Midcap 150 Index Fund now maps to Mid Cap (not Index/ETF). Axis Consumption Fund maps to Sectoral/Thematic. 30+ keyword patterns added; all keywords sorted longest-first to prevent shorter patterns shadowing longer ones (e.g. `"large and mid cap"` checked before `"large cap"`)
- **ETFs and REITs in Unclassified** — GOLDBEES, GOLDETFADD → Gold; LIQUIDCASE → Liquid/Debt; EMBASSY → Real Estate/REITs. yfinance returns `None` for sector on funds/trusts; these are now handled via the static override map

### API
- `GET /api/allocation` — market cap + sector breakdown for direct EQ; SEBI-category breakdown for MFs; each group includes holdings sorted by value
- `POST /api/refresh-market-meta` — fetches sector + market_cap_category from yfinance for all EQ instruments; skips known overrides

---

## [0.4.4] — 2026-06-04

### Added
- **Active vs Closed XIRR split** — the Portfolio XIRR card in the XIRR Analysis section now shows a secondary footnote on the all-time view: `Active: X% · Exited: Y%`
  - **Active XIRR**: cashflows for all instruments where quantity > 0, with today's market value as the terminal. Answers "how is my current portfolio performing?"
  - **Exited XIRR**: cashflows for fully closed positions (qty = 0, at least one sell), no terminal value. Answers "when I exited, how well did I do historically?"
  - Only shown on the all-time view — subperiod views (1Y, 3Y, FY) use opening/closing portfolio values so the split wouldn't map cleanly onto them
  - Implemented in `compute_xirr_split()` in `analytics.py`; endpoint `/api/xirr-analysis` returns `active_xirr` + `closed_xirr` when `from_date` is None

### Fixed
- **MF Holdings P&L display** — active positions (qty > 0) now show **unrealised P&L only** instead of unrealised + realised combined. Previously, Canara Robeco Large Cap showed +₹13,725 P&L beside "Invested ₹1,000 / Current ₹909" — caused by a past Lateral Shift Out gain being added to the unrealised number. Closed positions (qty = 0) continue to show realised P&L. For positions with a non-trivial realised amount (e.g. partially switched funds), a small `₹X realised` note appears beneath the main P&L figure

---

## [0.4.3] — 2026-06-03

### Performance
- **Net Worth computation: 19.8s → 3.5s cold, <1ms warm** — two targeted fixes:
  - Batch price queries: replaced ~1,590 individual SQL lookups (one per instrument per month) with one range query per instrument covering the full historical span; result joined in memory
  - In-process TTL cache: result stored in a thread-safe Python dict for 3 minutes; subsequent loads return in <1ms until data changes. Cache uses transaction count as a fingerprint so it auto-invalidates after any import or data write
- **Frontend stale-while-revalidate**: browser sessionStorage caches the `/api/networth` response; on page reload the chart renders instantly from cache while fresh data loads in the background

### Fixed
- **Canara Robeco scheme names missing** — `INF760K01167` (Large and Mid Cap) and `INF760K01AR3` (Large Cap) showed raw ISINs instead of scheme names because the one-time PDF import created instrument records without the `name` field. Names now set directly: "Canara Robeco Large and Mid Cap Fund - Regular Growth" and "Canara Robeco Large Cap Fund - Regular Growth"

---

## [0.4.2] — 2026-06-02

### Added
- **NW Actual Milestone Dots** — overlay your real net worth checkpoints (from personal tracking spreadsheet) on the Net Worth Journey chart as yellow dots, so you can immediately see where the computed historical line matches your actual records
  - `NWSnapshot` table stores date + amount + label; seeded with 8 milestones from Sheet2 of the Excel tracker (Feb 2022 start → May 2026 ₹8.02L)
  - Endpoints: `GET/POST/DELETE /api/nw-snapshots`
  - Dots aligned by year-month (not exact date) so "2024-07-08" correctly maps to the "2024-07" point on the monthly chart

### Fixed
- **Net Worth projection rate** — was using trailing 1-year XIRR which produced 529% (Canara Robeco historical imports flooded the 12-month window). Now uses all-time portfolio XIRR (~12%) which correctly projects ₹13.5L by 2029 instead of the incorrect ₹30.77L. Clamped to 30% max (was 50%)
- **Projection horizon** — extended from 36 months to 60 months (5 years) so the compounding curve shape is more visible
- **NW XIRR** — was `NameError: NWSnapshot not defined` because `NWSnapshot` was missing from `networth.py` imports
- **NW milestone dots** — only 1 dot appeared because exact date matching failed for non-month-start dates (e.g. "2024-07-08" didn't match label "2024-07-01"); fixed by matching on year-month prefix
- **NW XIRR variable name** — `txns` referenced before assignment in `compute_networth`; corrected to `all_txns`

### Changed
- Net Worth projection subtitle now shows: `"projected at 11.95% p.a. (all-time portfolio XIRR) · EPF at 8.25% · dots = your actual records"`

---

## [0.4.1] — 2026-06-02

### Added
- **SIP Scheduler** — register recurring SIPs once, auto-import monthly using official AMFI NAV
  - Preview mode: shows every transaction with exact NAV, units, stamp duty, holiday adjustment before committing — nothing touches the DB until you confirm
  - Correct stamp duty: 0.005% SEBI-mandated deduction applied to all SIPs (e.g. ₹3,000 → ₹2,999.85 net; matches official CAS values to 4 decimal places)
  - Holiday handling: uses AMFI NAV absence as the holiday detector — if a SIP date has no NAV published, automatically uses the next trading day's NAV (same logic fund houses use for allotment)
  - Deduplication: skips SIPs already imported from CAS using ±3-day window + approximate amount match — safe to use even after importing a CAS that covers the same period
  - Regular vs Direct funds: just use the plan-specific ISIN; AMFI returns the correct NAV automatically
  - SIP Schedules section under Mutual Funds tab with table, per-schedule Preview, global Sync All button
  - Endpoints: `GET/POST/PATCH/DELETE /api/sip-schedules`, `GET /{id}/preview`, `POST /{id}/confirm`, `POST /sync-all`
- **Canara Robeco MF import** — one-time direct PDF parser for KFintech account statement format
  - 50 transactions: 45 monthly SIPs (₹1,000, Feb 2022–Jan 2026), 2 lump-sum purchases, 1 Lateral Shift Out (switch to Large Mid Cap), 1 Lateral Shift In, 1 post-switch SIP
  - Handles page-break transaction splits (date on one page, amounts on next)
  - Folio 17738223505 — was missing from Combined CAS because it was registered under a different phone number/email

### Fixed
- **Switch cost basis explanation** — the ₹1,482 "invested" discrepancy between dashboard and INDMoney is the unrealised gain made on Canara Robeco Large Cap before switching. INDMoney preserves original purchase cost through switches; the dashboard records switch-in at market value. Documented in `_is_transfer()` and `compute_bank_invested()` helpers
- **`_is_transfer()` helper** — correctly identifies Lateral Shift In/Out, Switch Over In/Out, STP In/Out as portfolio redeployments (not bank outflows)

---

## [0.4.0] — 2026-06-01

### Added
- **EPF section** — EPFO Member Passbook PDF importer (CAMS+KFintech combined format)
  - Handles both normal format (label / month / amounts) and reverse page-break format (amounts on one page, month code first line of next page)
  - Strips PII: UAN, Member ID, Name, DOB, mobile never stored or logged
  - Summary cards: total balance, employee vs employer split, EPS pension, this-FY contributions
  - Three charts: monthly contributions (stacked bar), year-wise interest, balance growth curve
  - Contribution ledger table with running balance
  - Retirement projection coming from EPF tab
- **Global Equities section** — INDMoney/Alpaca order book XLS importer
  - Columns: Stock Name, Symbol, Execution Time, Transaction Type, Quantity (fractional), Price (USD), Amount (USD), Brokerage
  - Auto-fetches historical USD/INR rates at each transaction date via yfinance
  - Holdings table showing P&L in both USD and INR
  - XIRR vs S&P 500 and NASDAQ 100 (same cashflow-replay method as Indian equity vs NIFTY)
  - Equity curve: portfolio vs S&P 500 vs NASDAQ 100, rebased to 100
  - **Indian tax analysis** with correct rules: STCG (< 24 months, slab rate) and LTCG (≥ 24 months, 12.5%, NO ₹1.25L exemption for foreign equity). Lot-by-lot breakdown with INR P&L using historical exchange rates
  - Manual entry for future transactions not from INDMoney
- **Bonds section** — self-contained (no equity tradebook entry needed)
  - SGB (Sovereign Gold Bonds): priced via GOLDBEES × 100 gold proxy (live yfinance fetch); manual price override for NSE/RBI price
  - Interest income: configurable coupon rate, semi-annual schedule, total earned, this-FY interest, next coupon date and amount, full 16-payment schedule
  - **Tax treatment display**: SGB capital gains at RBI maturity = TAX EXEMPT (Section 47(viic) IT Act); interest taxable at slab rate
  - SGBDE31III pre-seeded with correct NSE ticker, ISIN IN0020230168, issue price ₹6,149, 2.5% coupon, Dec 2031 maturity
  - Supports corporate bonds and G-Secs with configurable tax treatment
  - Edit button on bond card to update price override and quantity
- **Net Worth dashboard** (All tab headline)
  - Donut chart with total net worth and allocation by account type
  - Two-toggle view: **By Account** (Equity | MF | Fixed Income | EPF | Bonds) and **By Asset Class** (Equity | Debt | Gold | Hybrid | Silver)
  - **EPF correctly classified as Debt** — earns fixed 8.25%, no market risk; standard Indian financial planning practice
  - **Cash/Liquid funds merged into Debt** — money-market instruments are the most liquid sub-category of debt
  - SGB/Gold ETFs → Gold asset class
  - Projected net worth chart with 36-month projection and milestone markers (₹50K → ₹1Cr)
  - Bond holdings now included in total net worth and Gold asset class
- **Asset class categorisation** (`/api/instrument-categories`)
  - Auto-detects category from instrument name (gold ETFs, liquid funds, hybrid funds etc.)
  - Per-instrument override via edit modal; stored in `instruments.asset_category`
  - "Edit categories" link in XIRR Analysis section
  - Re-run auto-detection button

### Fixed
- **Portfolio XIRR mismatch** (23.07% in summary card vs 12.62% in XIRR Analysis) — `compute_summary` was using `current_value` inflated with FI as the XIRR terminal value while outflows were equity+MF-only. Fixed by capturing `eq_mf_value` before adding FI to the display numbers
- **Bond net worth invisible** — bonds in `bond_details` table were never read by `compute_networth`; SGB value was missing from total net worth and Gold asset class
- **EPF passbook page-break parsing** — reverse split case (amounts on page N, month code first line of page N+1) now handled; both November 2024 and May 2026 entries correctly imported
- **RD instalment off-by-one** — persists from v0.3.0; also applied to `compute_fi_summary`

### Changed
- **Segment tabs**: All | Equities | Mutual Funds | Fixed Income | EPF | Global | Bonds
- Each special tab hides equity-specific panels (XIRR, Realized P&L, Equity Curve, Holdings, CAs) and shows only tab-specific content

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
