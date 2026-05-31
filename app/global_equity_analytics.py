"""Analytics for Global Equity holdings — XIRR, equity curve, Indian tax analysis.

All monetary calculations are in USD unless explicitly noted as INR.
Benchmarks: S&P 500 (^GSPC) and NASDAQ 100 (^NDX).

Indian tax rules for foreign equity (post Budget 2024, effective July 23, 2024):
  - STCG (held < 24 months): taxed at slab rate (no flat rate; typically 30% for
    high earners in the ₹15L+ bracket)
  - LTCG (held ≥ 24 months): taxed at 12.5% flat, NO ₹1.25L exemption
    (the ₹1.25L LTCG exemption applies only to listed Indian securities)
  - Cost basis in INR = USD cost × USD/INR at purchase date
  - Sale proceeds in INR = USD proceeds × USD/INR at sale date
  - P&L in INR = INR proceeds − INR cost
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import GlobalEquityTransaction
from .xirr import xirr as compute_xirr

log = logging.getLogger(__name__)

US_BENCHMARKS = {
    "^GSPC":  "S&P 500",
    "^NDX":   "NASDAQ 100",
}

# Indian tax constants
LTCG_MONTHS      = 24      # ≥ 24 months = LTCG for foreign equity
LTCG_RATE        = 0.125   # 12.5% (no indexation, no exemption)
STCG_SLAB_RATES  = {       # Indicative slab rates; user should verify
    "30%": 0.30,
    "20%": 0.20,
}


def _fetch_us_price_series(ticker: str, start: date, end: date,
                            cache: dict) -> dict[date, float]:
    """Fetch daily closes for a US ticker, using in-memory cache."""
    key = (ticker, start, end)
    if key in cache:
        return cache[key]
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(
            start=start.isoformat(),
            end=(end + timedelta(days=2)).isoformat(),
            auto_adjust=True,
        )
        if df is None or df.empty:
            cache[key] = {}
            return {}
        series = {}
        for ts, row in df.iterrows():
            d = ts.date() if hasattr(ts, "date") else ts
            try:
                series[d] = float(row["Close"])
            except Exception:
                pass
        cache[key] = series
        return series
    except Exception as e:
        log.warning("US price fetch failed for %s: %s", ticker, e)
        cache[key] = {}
        return {}


def _fill_forward(series: dict[date, float], start: date, end: date) -> dict[date, float]:
    out: dict[date, float] = {}
    last = None
    d = start
    while d <= end:
        if d in series:
            last = series[d]
        if last is not None:
            out[d] = last
        d += timedelta(days=1)
    return out


def compute_global_xirr(db: Session) -> dict:
    """Portfolio XIRR + S&P 500 / NASDAQ 100 benchmark XIRR (all in USD)."""
    today = date.today()
    txns = db.execute(
        select(GlobalEquityTransaction).order_by(GlobalEquityTransaction.trade_date)
    ).scalars().all()

    if not txns:
        return {"portfolio_xirr": None, "benchmarks": {}, "as_of": today.isoformat()}

    price_cache: dict = {}

    # Portfolio cashflows (USD)
    port_flows: list[tuple[date, float]] = []
    holdings: dict[str, float] = defaultdict(float)
    costs: dict[str, float] = defaultdict(float)

    for t in txns:
        if t.trade_type == "buy":
            port_flows.append((t.trade_date, -(t.amount_usd + t.fees_usd)))
            holdings[t.symbol] += t.quantity
            costs[t.symbol]   += t.amount_usd + t.fees_usd
        else:
            sell_amt = t.amount_usd - t.fees_usd
            port_flows.append((t.trade_date, sell_amt))
            if holdings[t.symbol] > 0:
                frac = min(t.quantity, holdings[t.symbol]) / holdings[t.symbol]
                costs[t.symbol] *= (1 - frac)
                holdings[t.symbol] = max(0.0, holdings[t.symbol] - t.quantity)

    # Terminal value: current portfolio in USD
    terminal_usd = 0.0
    for sym, qty in holdings.items():
        if qty < 1e-9:
            continue
        series = _fetch_us_price_series(sym, today - timedelta(days=10), today, price_cache)
        ff = _fill_forward(series, today - timedelta(days=10), today)
        if ff:
            cur = ff.get(today) or ff[max(ff)]
            terminal_usd += qty * cur

    if terminal_usd > 0:
        port_flows.append((today, terminal_usd))
    portfolio_xirr = compute_xirr(port_flows)

    # Benchmark XIRR — replay same USD cashflows into each index
    benchmarks: dict[str, dict] = {}
    start_date = min(t.trade_date for t in txns)

    for ticker, name in US_BENCHMARKS.items():
        idx_series = _fetch_us_price_series(ticker, start_date, today, price_cache)
        if not idx_series:
            benchmarks[ticker] = {"name": name, "xirr": None}
            continue
        ff = _fill_forward(idx_series, start_date, today)
        sorted_dates = sorted(ff.keys())
        first_px = ff[sorted_dates[0]] if sorted_dates else None
        if not first_px:
            benchmarks[ticker] = {"name": name, "xirr": None}
            continue

        units = 0.0
        sim_flows: list[tuple[date, float]] = []
        for t in sorted(txns, key=lambda t: (t.trade_date, t.id)):
            px = ff.get(t.trade_date) or first_px
            cash = t.amount_usd + t.fees_usd
            if t.trade_type == "buy":
                units += cash / px
                sim_flows.append((t.trade_date, -cash))
            else:
                units_to_sell = min(units, cash / px)
                idx_proceeds = units_to_sell * px
                units -= units_to_sell
                if idx_proceeds > 0:
                    sim_flows.append((t.trade_date, idx_proceeds))

        avail = [d for d in idx_series if d <= today]
        today_px = idx_series[max(avail)] if avail else None
        sim_value = units * today_px if today_px else 0
        if sim_value > 0:
            sim_flows.append((today, sim_value))

        benchmarks[ticker] = {
            "name":     name,
            "xirr":     compute_xirr(sim_flows) if sim_flows else None,
            "sim_value": round(sim_value, 2),
        }

    return {
        "portfolio_xirr": portfolio_xirr,
        "benchmarks":     benchmarks,
        "as_of":          today.isoformat(),
    }


def compute_global_equity_curve(db: Session) -> dict:
    """Portfolio value vs S&P 500 / NASDAQ, rebased to 100 at first transaction."""
    today = date.today()
    txns = db.execute(
        select(GlobalEquityTransaction).order_by(GlobalEquityTransaction.trade_date)
    ).scalars().all()

    if not txns:
        return {"dates": [], "series": {}}

    price_cache: dict = {}
    start = min(t.trade_date for t in txns)

    # Get all unique symbols
    symbols = list({t.symbol for t in txns})

    # Fetch price series for each stock
    stock_prices: dict[str, dict[date, float]] = {}
    for sym in symbols:
        raw = _fetch_us_price_series(sym, start, today, price_cache)
        stock_prices[sym] = _fill_forward(raw, start, today)

    # Build daily qty per symbol (running accumulator)
    # Sort events: buys before sells on same day
    all_events: list[tuple] = []
    for t in sorted(txns, key=lambda t: (t.trade_date, t.id)):
        all_events.append((t.trade_date, 0 if t.trade_type == "buy" else 1, t))
    all_events.sort(key=lambda x: (x[0], x[1]))

    sym_qty: dict[str, float] = defaultdict(float)
    ev_idx = 0
    days: list[date] = []
    port_vals: list[float] = []

    d = start
    while d <= today:
        while ev_idx < len(all_events) and all_events[ev_idx][0] <= d:
            _, _, t = all_events[ev_idx]
            if t.trade_type == "buy":
                sym_qty[t.symbol] += t.quantity
            else:
                sym_qty[t.symbol] = max(0.0, sym_qty[t.symbol] - t.quantity)
            ev_idx += 1
        val = sum(
            sym_qty[sym] * stock_prices[sym].get(d, 0)
            for sym in symbols
            if sym_qty[sym] > 1e-9
        )
        days.append(d)
        port_vals.append(val)
        d += timedelta(days=1)

    # Benchmark curves
    bench_vals: dict[str, list[float]] = {}
    for ticker, name in US_BENCHMARKS.items():
        raw = _fetch_us_price_series(ticker, start, today, price_cache)
        ff = _fill_forward(raw, start, today)
        sorted_bd = sorted(ff.keys())
        first_px = ff[sorted_bd[0]] if sorted_bd else None
        if not first_px:
            continue

        units = 0.0
        ev_idx2 = 0
        sorted_txns = sorted(txns, key=lambda t: (t.trade_date, t.id))
        vals: list[float] = []
        for day in days:
            while ev_idx2 < len(sorted_txns) and sorted_txns[ev_idx2].trade_date <= day:
                t = sorted_txns[ev_idx2]
                px = ff.get(t.trade_date) or first_px
                cash = t.amount_usd + t.fees_usd
                if t.trade_type == "buy":
                    units += cash / px
                else:
                    units = max(0.0, units - cash / px)
                ev_idx2 += 1
            day_px = ff.get(day) or first_px
            vals.append(units * day_px if day_px else 0.0)
        bench_vals[ticker] = vals

    # Rebase to 100 at first nonzero portfolio value
    base_idx = next((i for i, v in enumerate(port_vals) if v > 0), 0)
    base_p   = port_vals[base_idx] or 1
    rebased_p = [(v / base_p * 100) if i >= base_idx else None
                 for i, v in enumerate(port_vals)]

    rebased_b: dict[str, list] = {}
    for ticker, vals in bench_vals.items():
        bbase = vals[base_idx] if base_idx < len(vals) and vals[base_idx] > 0 else None
        if not bbase:
            bidx = next((i for i, v in enumerate(vals) if v > 0), None)
            bbase = vals[bidx] if bidx is not None else None
        if not bbase:
            continue
        rebased_b[ticker] = [(v / bbase * 100) if v else None for v in vals]

    return {
        "dates":     [d.isoformat() for d in days],
        "base_date": days[base_idx].isoformat() if days else None,
        "series": {
            "Portfolio": rebased_p,
            **{US_BENCHMARKS[t]: rebased_b[t] for t in rebased_b},
        },
    }


def compute_global_tax(db: Session) -> dict:
    """Indian capital gains tax analysis on global equity transactions.

    Rules (post Budget 2024, effective July 23, 2024):
      STCG (< 24 months): taxed at slab rate (typically 30% for ₹15L+ earners)
      LTCG (≥ 24 months): 12.5% — NO ₹1.25L exemption (foreign equity)

    Cost basis and proceeds are converted to INR at the historical exchange rate
    stored with each transaction.
    """
    today = date.today()
    txns = db.execute(
        select(GlobalEquityTransaction).order_by(GlobalEquityTransaction.trade_date)
    ).scalars().all()

    if not txns:
        return {"stcg_inr": 0, "ltcg_inr": 0, "total_realized_inr": 0,
                "breakdown": [], "tax_estimate": {}}

    # Build lot tracking per symbol (FIFO)
    lots: dict[str, list[tuple[date, float, float, float | None]]] = defaultdict(list)
    # Each lot: (buy_date, qty, usd_per_share, exchange_rate_at_buy)

    stcg_inr = 0.0
    ltcg_inr = 0.0
    breakdown: list[dict] = []

    for t in txns:
        sym = t.symbol
        if t.trade_type == "buy":
            lots[sym].append((
                t.trade_date,
                t.quantity,
                (t.amount_usd + t.fees_usd) / t.quantity,
                t.exchange_rate,
            ))
        else:
            remaining_sell = t.quantity
            sell_proceeds_usd = t.amount_usd - t.fees_usd
            sell_rate = t.exchange_rate  # USD/INR at sell date

            while remaining_sell > 1e-9 and lots[sym]:
                buy_date, lot_qty, buy_cost_usd, buy_rate = lots[sym][0]
                used = min(remaining_sell, lot_qty)
                held_months = (t.trade_date.year - buy_date.year) * 12 + \
                              (t.trade_date.month - buy_date.month)

                # P&L in USD (proportional to qty used)
                cost_usd  = used * buy_cost_usd
                proc_usd  = (used / t.quantity) * sell_proceeds_usd if t.quantity > 0 else 0

                # P&L in INR using respective exchange rates
                cost_inr  = cost_usd  * (buy_rate  or 84.0)
                proc_inr  = proc_usd  * (sell_rate or 84.0)
                pnl_inr   = proc_inr - cost_inr
                pnl_usd   = proc_usd - cost_usd

                is_ltcg = held_months >= LTCG_MONTHS
                if is_ltcg:
                    ltcg_inr += pnl_inr
                else:
                    stcg_inr += pnl_inr

                breakdown.append({
                    "symbol":       sym,
                    "buy_date":     buy_date.isoformat(),
                    "sell_date":    t.trade_date.isoformat(),
                    "held_months":  held_months,
                    "qty":          round(used, 8),
                    "cost_usd":     round(cost_usd, 4),
                    "proceeds_usd": round(proc_usd, 4),
                    "pnl_usd":      round(pnl_usd, 4),
                    "cost_inr":     round(cost_inr, 2),
                    "proceeds_inr": round(proc_inr, 2),
                    "pnl_inr":      round(pnl_inr, 2),
                    "type":         "LTCG" if is_ltcg else "STCG",
                    "buy_rate":     buy_rate,
                    "sell_rate":    sell_rate,
                })

                remaining_sell -= used
                if used >= lot_qty - 1e-9:
                    lots[sym].pop(0)
                else:
                    lots[sym][0] = (buy_date, lot_qty - used, buy_cost_usd, buy_rate)

    total_inr = stcg_inr + ltcg_inr

    # Tax estimates
    tax_stcg_30 = max(0, stcg_inr * 0.30)
    tax_stcg_20 = max(0, stcg_inr * 0.20)
    tax_ltcg    = max(0, ltcg_inr * LTCG_RATE)

    return {
        "stcg_inr":           round(stcg_inr, 2),
        "ltcg_inr":           round(ltcg_inr, 2),
        "total_realized_inr": round(total_inr, 2),
        "breakdown":          breakdown,
        "tax_estimate": {
            "stcg_at_30_pct": round(tax_stcg_30, 2),
            "stcg_at_20_pct": round(tax_stcg_20, 2),
            "ltcg_at_12_5":   round(tax_ltcg, 2),
            "note": (
                "STCG taxed at your income slab rate (20% or 30% shown as estimate). "
                "LTCG at 12.5% flat with NO ₹1.25L exemption (foreign equity). "
                "P&L computed in INR using USD/INR rates at purchase and sale dates."
            ),
        },
        "rules": {
            "ltcg_threshold_months": LTCG_MONTHS,
            "ltcg_rate_pct": LTCG_RATE * 100,
            "stcg": "At income tax slab rate",
            "exemption": "None — ₹1.25L LTCG exemption does not apply to foreign equity",
        },
        "as_of": today.isoformat(),
    }
