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

## SEBI market cap thresholds (used in prices.py)

| Category | Market cap |
|---|---|
| Large cap | ≥ ₹40,000 Cr |
| Mid cap | ₹8,000 – ₹40,000 Cr |
| Small cap | < ₹8,000 Cr |

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
- Switch cost basis: ₹1,482 discrepancy vs INDMoney for switched MF positions
- MF dividend reinvestment captured as buy; separate dividend payouts not tracked as cashflows
- Phase 2 MF look-through (which stocks inside MFs) not yet implemented
- No mobile-optimised layout yet

---

## Version

Current: **v0.5.0** — Portfolio X-Ray tab (market cap, sector, MF category breakdown).
See CHANGELOG.md for full history.
