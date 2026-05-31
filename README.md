# Portfolio Tracker

Self-hosted portfolio dashboard for Indian stocks (NSE/BSE) and mutual funds, with XIRR and benchmark comparison vs NIFTY 50, NIFTY 500, NIFTY Midcap 150, and NIFTY Smallcap 250.

## Features

- One-click import of Zerodha tradebook (CSV/XLSX) with `trade_id`-based dedupe
- Manual buy/sell entry, edit, delete
- Live prices: yfinance for stocks, AMFI + mfapi.in for mutual funds
- Holdings table: qty, avg cost, current price, P&L, % return, XIRR per holding
- Portfolio XIRR vs benchmark XIRR (apples-to-apples cashflow replay)
- Equity curve chart: portfolio vs each index, rebased to 100 at first transaction
- CSV export of all transactions
- SQLite storage (single-file, no external DB)

## Run locally

```bash
cd portfolio-tracker
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000.

## Run in Docker (local or VPS)

```bash
docker compose up -d --build
```

The SQLite database lives in `./data/portfolio.db` and is mounted into the container so it persists across rebuilds.

## VPS deployment with HTTPS (nginx + certbot)

Once `docker compose up -d` is running on your VPS, put nginx in front for TLS:

```nginx
server {
    listen 443 ssl http2;
    server_name portfolio.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/portfolio.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/portfolio.yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

There is no built-in auth — run it behind a VPN, or add an nginx `auth_basic` block, or front it with something like Authelia.

## Importing your tradebook

The importer expects Zerodha's standard column layout:

```
symbol, isin, trade_date, exchange, segment, series, trade_type,
auction, quantity, price, trade_id, order_id, order_execution_time
```

`segment` is `EQ` for equities and `MF` for mutual funds; `trade_type` is `buy` or `sell`. ISIN is what links mutual funds to the AMFI scheme code (and therefore to NAV history) — keep it populated.

Re-importing the same file is safe: rows are deduped by `trade_id`.

## How XIRR is computed

- **Per holding**: cashflows = each buy (negative `qty*price + fees`), each sell (positive, net of fees), plus today's market value of remaining quantity.
- **Portfolio**: same construction over all transactions.
- **Benchmark**: each transaction is replayed into the index — on a buy date, "buy" `cash_amount / index_close_that_day` units; on a sell, sell proportionally. End with `units × today's index close`. XIRR over those simulated cashflows. This is the apples-to-apples comparison vs your actual XIRR over the exact same date range and contribution timing.

XIRR uses `scipy.optimize.brentq` over `[-99%, 10000%]`, with a Newton-Raphson fallback.

## Refreshing prices

Prices are cached daily in the `price_cache` table. The "Refresh prices" button forces a recompute (which lazily refetches today's closes for all held instruments + benchmarks). For background refresh, schedule a cron hitting `POST /api/refresh-prices`.

## Caveats / out of scope (v0.1)

- Corporate actions (splits, bonuses, MF dividends) are not modelled. yfinance returns split-adjusted close, so equity XIRR is reasonable; MF dividend reinvestment isn't tracked separately.
- INR only.
- No auth (run behind a VPN or reverse-proxy auth).
- No tax/capital-gains report.
