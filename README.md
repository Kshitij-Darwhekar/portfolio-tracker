# Portfolio Tracker

Self-hosted personal finance dashboard for Indian equities (NSE/BSE) and mutual funds. Tracks XIRR, compares against NIFTY benchmarks, handles corporate actions, and gives FY-wise realized P&L with STCG/LTCG split.

---

## Features

### Equity (Stocks)
- **Zerodha tradebook import** — CSV/XLSX, auto-detects separators, dedupes by `trade_id`
- **Corporate actions** — splits, bonuses, demergers, dividends; auto-sync from yfinance; fractional share payouts tracked
- **Symbol renames** — built-in map (ZOMATO→ETERNAL, GOLDETFADD→GOLDADD) + user-defined aliases
- **Orphan sell detector** — flags IPO allotment sells with no matching buy (common for BAJAJHFL, TATATECH, etc.)
- **Live prices** — yfinance for NSE/BSE; `.BO` fallback; AMFI NAV fallback for ETFs yfinance can't find

### Mutual Funds
- **CAMS + KFintech Combined CAS PDF import** — full history from folio inception; handles SIP, lumpsum, switch-in/out, redemption; skips stamp duty lines automatically
- **Live NAV** — AMFI NAVAll.txt + mfapi.in historical NAV; cached daily
- Full scheme name display (e.g. "Parag Parikh Flexi Cap Fund - Regular Plan Growth") with folio number

### Fixed Income (FDs & RDs)
- **Manual entry** for Bank FDs (cumulative & non-cumulative) and Recurring Deposits
- **Correct Indian bank math** — cumulative FD: `P × (1 + r/n)^(n×t)` with quarterly compounding (RBI standard); RD: each instalment compounded for its remaining tenure
- **Tax metrics** — interest accrued this financial year (taxable as income), TDS warning when aggregate FY interest from one bank exceeds ₹40,000
- **Three FI-specific charts**: Portfolio growth curve vs configurable benchmarks (savings rate, standard FD rate, CPI inflation); Maturity Timeline (Gantt); Cashflow Forecast (next 24 months); FY Interest Income bar chart
- **Configurable benchmark rates** stored in `data/fi_rates.json` — update savings rate, standard FD rate, and inflation without touching code
- FI XIRR shown separately with appropriate benchmarks (savings/inflation) — not mixed into equity XIRR where NIFTY comparison would be meaningless

### Analytics
- **Holdings table** — qty/units, avg cost, current price/NAV, invested, current value, P&L, % return, XIRR per holding; sortable by any column
- **Portfolio XIRR** — correct benchmark comparison: same rupees, same dates, replayed into the index. Index sell proceeds use the index's actual value (not the stock's windfall price)
- **XIRR by period** — All-time | 1Y | 3Y | 5Y | Current FY | last 4 FYs | Custom. Correct subperiod XIRR using opening/closing portfolio values as cashflows
- **Realized P&L by period** — same period selector; STCG (< 12 months, taxed at 20%) and LTCG (≥ 12 months, 12.5% above ₹1.25L) split for Indian tax planning
- **Equity curve** — portfolio vs NIFTY 50 / NIFTY 500 / NIFTY Midcap 150 / NIFTY Smallcap 250; all rebased to 100 at first transaction date on the same cash-deployment schedule
- **Data quality warnings** — surfaces orphan sells (likely IPO allotments) with total missing proceeds

### Portfolio filter
- **All | Equities | Mutual Funds** tabs — every panel (summary cards, XIRR analysis, realized P&L, equity curve, holdings) responds to the filter in real time

### UI
- Sticky header, collapsible sections (state saved in localStorage)
- Holdings quick-filter (type symbol to instantly search)
- Indian number formatting (₹1.52L, ₹10.5Cr)
- Keyboard shortcuts: `N` = Add transaction, `R` = Refresh prices, `/` = Focus filter
- Benchmarks in XIRR cards show `+X% vs index` delta
- Top progress bar while the dashboard reloads; "Saving…" feedback on transaction commit
- **Privacy mode** — eye-icon toggle hides all ₹/$ amounts and portfolio size (rendered as `••••`, charts blurred) while keeping percentages visible; for safe screenshots/demos. Persists across reloads
- **Sell guard** — new sells are restricted to held instruments (autocomplete from holdings + quantity check) to prevent typos creating orphan sells
- **Responsive** — adapts to phone/tablet (used over Twingate); summary amounts reveal their exact value on hover/tap

---

## Tech stack

| Layer | Choice |
|---|---|
| Backend | FastAPI + SQLAlchemy + SQLite |
| Price data | yfinance (equities), AMFI NAVAll.txt + mfapi.in (MFs) |
| XIRR | scipy.optimize.brentq with Newton-Raphson fallback |
| Frontend | Vanilla JS + Chart.js (no build step) |
| Deployment | Docker + docker-compose; nginx reverse proxy for VPS |

---

## Run locally

```bash
cd portfolio-tracker
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000.

## Run in Docker (local or VPS)

```bash
cp .env.example .env      # then edit .env if needed (DB path, auth password)
docker compose up -d --build
```

The DB host path comes from `DATA_DIR` in `.env` (defaults to `./data`), bind-mounted to `/app/data` — persists across rebuilds. On a Raspberry Pi / CasaOS host, set `DATA_DIR=/DATA/AppData/portfolio-tracker` in `.env` instead of editing the tracked compose file.

## Configuration (`.env`)

All configuration is via environment variables (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `DATA_DIR` | `./data` | Host directory for the SQLite DB |
| `APP_USERNAME` | `admin` | Basic Auth username (only used if a password is set) |
| `APP_PASSWORD` | _(empty)_ | Set to require HTTP Basic Auth on every request. Empty = no auth. |
| `ENABLE_DEBUG_ENDPOINTS` | _(empty)_ | Set to `1` to expose `/api/debug/*` (troubleshooting only) |
| `NEXTCLOUD_URL` / `NEXTCLOUD_USER` / `NEXTCLOUD_APP_PASSWORD` | _(empty)_ | Off-device backup target for `scripts/backup.sh` (WebDAV). Use a Nextcloud **app password**, not your account password. |
| `NEXTCLOUD_REMOTE_DIR` | `Backups/portfolio` | Folder inside Nextcloud to upload the DB to |

## Automation (`scripts/`)

| Script | What it does | Example cron |
|---|---|---|
| `scripts/backup.sh` | Timestamped SQLite online backup, 7-snapshot retention, optional off-device upload to Nextcloud (WebDAV) | `13 2 * * *` (nightly 02:13) |
| `scripts/refresh-prices.sh` | Triggers a price/NAV refresh (auth-aware) | `47 18 * * 1-5` (weekdays after close) |
| `scripts/update.sh` | Backup → `git pull` → rebuild, in one command | run manually after pushing |

Edit a crontab with `crontab -e`. Each script takes config via environment variables (see the header comment in each file).

## VPS deployment with HTTPS (nginx + certbot)

```nginx
server {
    listen 443 ssl http2;
    server_name portfolio.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/portfolio.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/portfolio.yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Set `APP_PASSWORD` in `.env` to enable built-in HTTP Basic Auth, and/or run behind a VPN, `nginx auth_basic`, or Authelia.

### Security notes

- **Auth is opt-in.** Without `APP_PASSWORD`, every endpoint (including transaction delete/edit) is open to anyone who can reach the port. That's acceptable *only* behind a trusted VPN — a home LAN includes guests and IoT devices, so set `APP_PASSWORD` if the Pi isn't isolated.
- **Container runs as non-root** (`appuser`, uid 1000) as of v0.6.2.
- **Remaining hardening follow-up** (low severity): cap upload size to prevent a large-file OOM during import. Low risk for a single-user, authenticated deployment.

---

## Importing data

### Zerodha tradebook (equities)

Expected column layout (auto-detected, case/separator-insensitive):
```
symbol, isin, trade_date, exchange, segment, series, trade_type,
auction, quantity, price, trade_id, order_id, order_execution_time
```
- `segment = EQ` for stocks, `MF` for MFs bought via Zerodha
- Re-import is safe — deduped by `trade_id`

### CAMS + KFintech Combined CAS (mutual funds)

1. Download your Combined CAS from **camsonline.com** or **kfintech.com** (Detailed, Since Inception)
2. If password-protected: open in Acrobat/Chrome and save a password-free copy
3. Click **Import CAS PDF (MF)** in the Transactions section

The importer handles all transaction types: Purchase, SIP, Switch-in/out, Redemption; skips stamp duty (0.005%) lines automatically. Re-import is safe — deduped by `CAS|folio|date|nav|units`.

### IPO allotments (equities)

IPO allotments don't appear as buy transactions in Zerodha's tradebook. The dashboard detects "orphan sells" (sells with no matching buy) and shows a warning. Fix: add a manual buy transaction at the IPO issue price.

---

## Corporate actions

The **Sync from yfinance** button fetches splits and dividends for all held equities automatically.

For demergers (e.g. ITC → ITC Hotels), add manually via **Corporate Actions → Add manually**:
- Type: `Demerger`
- Ratio: fraction of cost **retained** in the parent (e.g. `0.8649` for ITC after Hotels spin-off)
- The demerged child shares should be added as a manual buy transaction at the SEBI-allocated cost

For bonus issues (e.g. 1:1 bonus = ratio `2.0`), switch-outs that create fractional shares floor to integer and the fractional cash payout is tracked in XIRR.

---

## XIRR explained

**Per holding:** buy outflows (qty × price + fees) + sell inflows + today's market value of remaining qty.

**Portfolio:** same, plus dividend inflows from corporate actions data.

**Benchmark:** replay the identical rupee amounts on the identical dates into the index. On each stock sell, sell the proportional index position at the index's actual value — not the stock's price. This prevents IPO windfalls from artificially inflating the benchmark XIRR.

**Subperiod XIRR** (e.g. FY2024-25):
- Opening portfolio value on Apr 1 2024 → negative cashflow (carry-in)
- All transactions Apr 2024 – Mar 2025 → cashflows
- Closing portfolio value on Mar 31 2025 → positive cashflow
- XIRR isolates that year without distortion from earlier or later periods.

XIRR solver: `scipy.optimize.brentq([-99%, 10000%])` with Newton-Raphson fallback.

---

## Realized P&L and taxes

The **Realized P&L** panel uses FIFO to attribute holding period per lot:
- **STCG** (held < 12 months): taxed at 20%
- **LTCG** (held ≥ 12 months): ₹1.25L exempt, 12.5% above that

Filter by Current FY, Previous FY, or any date range to match what you'd report in ITR.

---

## Prices and NAV

| Asset | Source | Cache |
|---|---|---|
| Equities (NSE) | yfinance `.NS` | Daily, `price_cache` table |
| Equities (BSE fallback) | yfinance `.BO` | Daily |
| ETFs (yfinance-unavailable) | AMFI NAVAll.txt + mfapi.in | Daily |
| Mutual fund NAV (historical) | mfapi.in JSON | On first fetch |
| Benchmark indices | yfinance `^NSEI`, `^CRSLDX`, ETF proxies | Daily |

The **Refresh prices** button force-fetches today's closes for all held instruments.

---

## Keyboard shortcuts

| Key | Action |
|---|---|
| `N` | Open Add Transaction dialog |
| `R` | Refresh prices |
| `/` | Focus holdings filter |

---

## Version history

See [CHANGELOG.md](CHANGELOG.md) for the full version history.

| Version | Date | Highlights |
|---|---|---|
| **0.8.0** | 2026-06-14 | X-Ray Insights panel — data-driven allocation observations + auto-rotating cited educational tips (education, not advice) |
| **0.7.4** | 2026-06-14 | Cap classification (Large/Mid/Small) now uses the official NSE/AMFI ranking (NIFTY 100/Midcap 150/Smallcap 250) instead of live market cap — fixes misclassifications |
| **0.7.3** | 2026-06-14 | Nextcloud backup keeps a dated 7-day history (`portfolio-YYYY-MM-DD.db`) + `-latest` pointer, with WebDAV pruning |
| **0.7.2** | 2026-06-14 | Responsive mobile/tablet layout; hover/tap to reveal exact amounts (₹4.32L → ₹4,32,123.45) in Equities/MF cards + holdings |
| **0.7.1** | 2026-06-14 | Analytics result caching — repeat dashboard loads ~3× faster (summary 1.1s→14ms, net worth 2.1s→0.2s); K formatting scoped to the Net Worth summary |
| **0.7.0** | 2026-06-14 | Privacy mode — eye-icon toggle hides all amounts/portfolio size (charts blurred, percentages kept) for screenshots/demos |
| **0.6.2** | 2026-06-14 | Container runs as non-root (`appuser`, uid 1000) — drops root while keeping DB write access |
| **0.6.1** | 2026-06-13 | Off-device backup to Nextcloud (WebDAV) in `backup.sh`; secrets read from `.env`, not crontab |
| **0.6.0** | 2026-06-13 | Optional HTTP Basic Auth, debug endpoints gated, env-driven compose (`.env`/`DATA_DIR`), backup + price-refresh + update scripts, internal security review |
| **0.5.1** | 2026-06-13 | Sell guard (only sell what you hold, with autocomplete + quantity check), loading progress bar on refresh, "Saving…" button feedback, segment-aware XIRR caption fix |
| **0.5.0** | 2026-06-05 | Portfolio X-Ray tab: market cap proportion bar + pill tabs, sector donut, MF category drill-down with Others grouping, smart insight, ETF/REIT sector overrides |
| **0.4.4** | 2026-06-04 | Active vs Closed XIRR split in Portfolio XIRR card; MF Holdings P&L fix (unrealised only for active positions) |
| **0.4.3** | 2026-06-03 | Net Worth load time 19.8s→3.5s (batch queries + TTL cache + frontend stale-while-revalidate) |
| **0.4.2** | 2026-06-02 | NW actual milestone dots, projection rate fix (12% all-time XIRR), 5-year horizon |
| **0.4.1** | 2026-06-02 | SIP scheduler (AMFI NAV, stamp duty, preview mode), Canara Robeco PDF import, switch cost basis |
| **0.4.0** | 2026-06-01 | EPF passbook import, Global Equities (INDMoney), Bonds/SGB, Net Worth dashboard, asset class categorisation |
| **0.3.0** | 2026-05-31 | Fixed Income (FDs & RDs): interest math, TDS, growth curve, maturity timeline, favicon |
| **0.2.2** | 2026-05-31 | Fix XIRR correctness regression from cache_only over-application |
| **0.2.1** | 2026-05-31 | Performance: 220× faster holdings, 37× faster equity curve, two-phase loading |
| **0.2.0** | 2026-05-31 | Mutual Funds (CAS import), segment filter, XIRR by period, STCG/LTCG P&L, collapsible UI |
| **0.1.0** | 2026-05-31 | Initial release: equity tracking, corporate actions, XIRR vs benchmarks |

---

### SIP Scheduler
- Register recurring SIPs once (ISIN, amount, day of month, start date) — re-import monthly with one click instead of downloading a new CAS
- **Preview before commit**: shows every transaction with exact AMFI NAV, units, stamp duty, holiday adjustment — nothing writes to the DB until you confirm
- Stamp duty (0.005% SEBI-mandated) applied automatically — matches official CAS values to 4 decimal places
- Holiday handling: uses AMFI NAV absence as the holiday detector; automatically shifts to next trading day
- Safe to run alongside CAS imports — deduplicates against already-imported transactions
- Supports regular and direct plans via plan-specific ISIN

### EPF
- **EPFO Member Passbook PDF import** — handles CAMS+KFintech format including page-break splits; PII (UAN, Member ID, Name) never stored
- Balance breakdown: employee vs employer contributions, EPS pension, interest credited
- Monthly contribution chart, year-wise interest, balance growth curve, contribution ledger

### Global Equities
- **INDMoney/Alpaca XLS importer** — fractional US shares, USD amounts, auto-fetches historical USD/INR rates
- P&L shown in both USD and INR; live prices from yfinance (US tickers)
- XIRR vs S&P 500 and NASDAQ 100 (same cashflow-replay benchmark method as Indian equity)
- Equity curve: portfolio vs US indices
- **Indian tax analysis**: STCG (< 24 months, slab rate) / LTCG (≥ 24 months, 12.5%, **no ₹1.25L exemption** for foreign equity); lot-by-lot breakdown with INR P&L using historical exchange rates

### Bonds (SGBs, corporate bonds, G-Secs)
- Self-contained — position tracked in bond metadata, no equity tradebook entry needed
- SGB pricing: live gold proxy via GOLDBEES × 100; manual price override for NSE/RBI price
- Interest schedule: configurable coupon rate, semi-annual/annual/quarterly, next coupon date and amount
- **Tax treatment**: SGB capital gains at RBI maturity = TAX EXEMPT (Section 47(viic)); interest taxable at slab rate
- SGBDE31III pre-seeded (ISIN IN0020230168, ₹6,149 issue price, 2.5% coupon, Dec 2031 maturity)

### Net Worth Dashboard
- Headline total across all asset classes (Equity + MF + FI + EPF + Bonds + Global)
- Two views: **By Account** and **By Asset Class** (Equity | Debt | Gold | Hybrid | Silver)
- Asset class logic: EPF → Debt; Cash/Liquid funds → Debt; SGBs/Gold ETFs → Gold
- 36-month projected net worth with milestone markers (₹50K → ₹1Cr)
- Per-instrument category override via "Edit categories" in XIRR Analysis section

---

## Known limitations

- Optional HTTP Basic Auth (`APP_PASSWORD`); otherwise run behind a VPN or reverse-proxy auth layer
- INR only; no multi-currency support
- Mutual fund dividend reinvestment is captured from the CAS (appears as a buy transaction); separate dividend payouts are not yet tracked as cashflows
- No tax reports or capital-gains schedules (use the Realized P&L panel as a starting point)
- Corporate actions for MFs (bonus units, fund mergers) must be entered manually if not in Zerodha's tradebook
