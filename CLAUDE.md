# Portfolio Tracker — Claude Context

Self-hosted personal finance dashboard for Indian markets. FastAPI + SQLite backend,
vanilla JS + Chart.js frontend, no build step. Owner: Kshitij Darwhekar.

---

## Running locally

```bash
cd portfolio-tracker
.venv\Scripts\activate          # Windows
source .venv/bin/activate        # macOS/Linux
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000. The SQLite DB lives at `data/portfolio.db`.

## Running in Docker

```bash
docker compose up -d --build
```

`./data/` is bind-mounted — DB persists across rebuilds.

## Target hosting environment

**Raspberry Pi 5** running Ubuntu Server + CasaOS + Docker.
Access via **Twingate** (zero-trust VPN) — same network as Nextcloud and Jellyfin.
No public domain / HTTPS needed; Twingate handles secure remote access.

The app runs as a Docker container on the RPi alongside other CasaOS services.
`./data/` should be mounted to a path on the NAS or RPi's storage so the DB
survives container rebuilds.

Do NOT suggest installing Twingate on a work laptop — only personal devices.

---

## Architecture

```
app/
  main.py                  FastAPI app — all HTTP endpoints
  db.py                    SQLAlchemy models + init_db() with inline migrations
  analytics.py             Holdings, XIRR, equity curve, allocation breakdown
  prices.py                yfinance / AMFI price fetching + INSTRUMENT_META_OVERRIDES
  fi_calc.py               Pure math: FD/RD interest, maturity, XIRR cashflows
  importer.py              Zerodha tradebook CSV/XLSX import
  mf_importer.py           CAMS + KFintech Combined CAS PDF import
  epf_importer.py          EPFO passbook PDF import
  global_equity_importer.py  INDMoney/Alpaca XLS import (US stocks)
  corporate_actions.py     Splits, bonuses, demergers, dividends
  sip_processor.py         SIP schedule → transactions (with AMFI NAV)
  bond_analytics.py        SGB / corporate bond holdings + interest
  networth.py              Net Worth dashboard across all asset classes
  categorizer.py           Asset class auto-detection (equity/debt/gold/hybrid)
  global_equity_analytics.py  US equity XIRR, P&L, tax (STCG/LTCG)
  fi_rates.py              Configurable benchmark rates (data/fi_rates.json)
  xirr.py                  XIRR solver (brentq + Newton-Raphson fallback)
  static/
    index.html             Single-page app shell
    app.js                 All frontend logic
    styles.css             All styles
```

---

## DB models (app/db.py)

| Table | Purpose |
|---|---|
| `transactions` | EQ + MF buy/sell; deduped by `trade_id` |
| `instruments` | ISIN → symbol, yf_ticker, sector, market_cap_category, asset_category |
| `price_cache` | Daily closes (keyed by ISIN for MF, yf_ticker for EQ) |
| `corporate_actions` | Splits, bonuses, dividends, demergers |
| `fixed_income` | FDs (cumulative & non-cumulative) and RDs |
| `epf_entries` | EPFO passbook rows |
| `sip_schedules` | Recurring SIP config |
| `global_equity_transactions` | US/international stock trades in USD |
| `bond_details` | SGB / corporate bond metadata + position |
| `nw_snapshots` | User-recorded NW milestones for chart overlay |

Schema migrations are done inline in `init_db()` via `ALTER TABLE` — no Alembic.

---

## Segment filter

The `?segment=` query param on most API endpoints accepts:
`EQ` | `MF` | `FI` | `EPF` | `GLOBAL` | `BONDS`

Frontend tabs: **All | Equities | Mutual Funds | Fixed Income | EPF | Global | Bonds | X-Ray**

The X-Ray tab (`data-seg="XRAY"`) skips `refreshAll()` and calls `loadAllocation()` instead.

---

## Key conventions

**XIRR**: buy outflows are negative, sell inflows + current market value are positive.
Benchmark XIRR replays identical cashflows into the index — on each sell, the
proportional index position is sold at the index's *actual* value (not the stock price).

**Subperiod XIRR**: opening portfolio value on period start = negative cashflow (carry-in);
closing value on period end = positive cashflow.

**FIFO P&L**: `compute_realized_pnl_by_period()` attributes holding period per lot.
STCG < 12 months (20%), LTCG ≥ 12 months (12.5%, ₹1.25L exempt for Indian equity).

**MF dedup key**: `CAS|folio|date|nav|units` (in `mf_importer.py`).

**ETF/REIT sector classification**: yfinance returns `None` for these. Static overrides
live in `INSTRUMENT_META_OVERRIDES` dict in `prices.py`. Add new ETFs/REITs there.

**MF SEBI category matching**: `_MF_CAP_LABEL` in `analytics.py` — a list of
`(keyword, label)` tuples sorted longest-first so "large and mid cap" (16 chars)
matches before "large cap" (9 chars). Always sort longest-first when adding new entries.

**Two-phase loading**: Phase 1 = fast DB queries (holdings, summary). Phase 2 = slow
XIRR computations. X-Ray is lazy-loaded on tab click only (not in either phase).

**Indian number formatting**: `fmtINR(v)` in app.js — ₹1.52L, ₹10.5Cr.

---

## Segment values in the codebase

- EQ transactions: `segment = 'EQ'` in `transactions` table
- MF transactions: `segment = 'MF'` in `transactions` table
- MF ISINs start with `IN[F0]` — used in app.js to distinguish MF vs EQ in tables

---

## Market cap classification (Large / Mid / Small)

**Source of truth: the official NSE/AMFI rank-based lists** (`app/cap_classification.py`),
not a live market-cap number:

| Category | NSE index (rank) |
|---|---|
| Large cap | NIFTY 100 (1–100) |
| Mid cap | NIFTY Midcap 150 (101–250) |
| Small cap | NIFTY Smallcap 250 (251–500) |

Matched to holdings by **ISIN → symbol**; cached at `data/cap_classification.json`.
Refreshed via the X-Ray "Refresh market data" button.

**Stocks outside the top 500** (not in any of the three lists) are classified **small**
by SEBI definition — anything ranked beyond Smallcap 250 is still small-cap. So in
`prices.py`, when `cap_classification.lists_loaded()` is true but `get_cap_category`
returns `None`, we default to `"small"` (not the yfinance market-cap guess, which
mislabelled e.g. YATHARTH as mid). The old yfinance `marketCap` vs ₹40,000 Cr / ₹8,000 Cr
thresholds (`_LARGE_CAP_MIN_INR` / `_MID_CAP_MIN_INR`) are used **only** when the NSE
lists can't load at all.

---

## FI interest math (fi_calc.py)

- **Cumulative FD**: `P × (1 + r/n)^(n×t)` — quarterly compounding by default (RBI standard)
- **Non-cumulative FD**: principal unchanged; periodic interest = `P × r × days/365`
- **RD**: each instalment M earns `M × (1 + r/4)^((tenure_months − k)/3)` for month k

---

## Data files

- `data/portfolio.db` — SQLite DB (never commit this)
- `data/fi_rates.json` — configurable benchmark rates (savings rate, FD rate, CPI)
- `data/nav_cache/` — cached AMFI NAV text files

---

## Adding a new asset type (checklist)

1. Add model to `db.py`; add migration in `init_db()` if needed
2. Add analytics functions in a new `<type>_analytics.py` or `analytics.py`
3. Add CRUD endpoints in `main.py`
4. Add segment tab button in `index.html` (`data-seg="XYZ"`)
5. Add segment handler in `app.js` (show/hide correct panels)
6. Add styles in `styles.css`

---

## Known limitations

- No auth — run behind VPN or reverse-proxy auth
- Switch cost basis differs from INDMoney by ~₹1,600 — **intentional, not a defect**: we
  book a fund switch as redeem+re-buy (tax-correct), realising the switch gain into Realized
  P&L and setting the new fund's cost basis to its switch-in value. INDMoney appears to carry
  the old cost forward (higher unrealised, lower realised). Total return is identical. v0.9.1
  adds a ⓘ tooltip on MF Invested explaining this.
- MF dividend reinvestment captured as buy; separate dividend payouts not tracked as cashflows
- Phase 2 MF look-through (which stocks inside MFs) not yet implemented
- No mobile-optimised layout yet

---

## Version

**Versioning policy — [Semantic Versioning](https://semver.org) (`MAJOR.MINOR.PATCH`), paired with [Keep a Changelog](https://keepachangelog.com):**
- **MAJOR** — breaking changes. Pre-1.0, we stay at `0`.
- **MINOR** (`0.X.0`) — a new user-facing **feature** (e.g. privacy mode, mobile layout, the planned insights panel).
- **PATCH** (`0.0.X`) — **bug fixes, perf, and small improvements** (e.g. caching, dated backups, the cap-classification fix). Most changes are patches.

When in doubt: "does this add a capability the user didn't have?" → MINOR; "does it fix/improve something that existed?" → PATCH.

Current: **v0.9.1** — MF "Invested" ⓘ explainer (no logic change): clarifies that
switches are booked as redeem+re-buy (tax-correct), so Invested is higher than
apps that carry old cost forward; total return identical. Plus v0.9.0: Daily gain (day's P&L vs previous close): per-holding **Day**
column, "Day's Gain" summary card, and a Net Worth "Today" line. Computed in
`compute_holdings`/`compute_summary`/`compute_networth` via `latest_close(today-1)`.
Plus v0.8.0: X-Ray "Insights" panel: data-driven allocation observations
(`_xrayInsights` in app.js) + auto-rotating cited educational tips (`CURATED_TIPS`,
12s timer), framed as education-not-advice. Plus v0.7.4: Large/Mid/Small-cap classification now uses the official
NSE/AMFI ranking (NIFTY 100/Midcap 150/Smallcap 250 via `app/cap_classification.py`,
matched by ISIN→symbol, disk-cached) instead of live yfinance market cap; fixes
misclassifications. Plus v0.7.3: `backup.sh` keeps a dated 7-day history in Nextcloud
(`portfolio-YYYY-MM-DD.db` + `-latest`, WebDAV-pruned). Plus v0.7.2: Responsive mobile/tablet layout (media queries in styles.css;
none existed before) + hover/tap-to-reveal exact amounts (`amt()` helper) in
Equities/MF summary cards + holdings. Plus v0.7.1: Analytics result caching (in-process, fingerprint-keyed:
holdings/curve/summary/xirr/realized/data-quality/networth; `cached_call` +
`_data_fingerprint` + `invalidate_analytics_cache` in analytics.py; 30-min TTL;
refresh-prices invalidates + pre-warms). K formatting scoped to Net Worth summary
via `fmtINRshort`. Plus v0.7.0: Privacy mode (eye-icon toggle hides all amounts/
portfolio size, charts blurred, percentages kept; `body.privacy-on` + maskable
`fmtINR`/`fmtQty`/`fmtUSD`). Plus from v0.6.x: optional HTTP Basic Auth (`APP_PASSWORD`),
debug endpoints gated behind `ENABLE_DEBUG_ENDPOINTS`, env-driven compose
(`.env` / `DATA_DIR`), `scripts/` (backup, refresh-prices, update), Nextcloud
WebDAV off-device backup, non-root container (`appuser`, uid 1000).
See CHANGELOG.md for full history.

**Config**: all via env (see `.env.example`) — `DATA_DIR` (DB host path),
`APP_USERNAME`/`APP_PASSWORD` (Basic Auth, off when password empty),
`ENABLE_DEBUG_ENDPOINTS`. Auth middleware lives at the top of `main.py`.
