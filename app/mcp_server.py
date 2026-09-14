"""Read-only MCP (Model Context Protocol) server exposing portfolio analytics to AI tools.

This is a thin, **read-only** adapter over the existing analytics layer. It lets any
MCP-capable client (Claude Desktop, Claude Code, ChatGPT, Gemini, Cursor, …) read the
portfolio for conversational insights. It never writes to the DB.

Two transports, one set of tools:
  - **Streamable HTTP** — `build_http_app()` returns an ASGI app that main.py mounts at
    `/mcp` (so it shares the FastAPI port, Twingate exposure, and is guarded by MCP_TOKEN).
  - **stdio** — `python -m app.mcp_server` runs the same tools over stdin/stdout for local
    clients that prefer spawning a subprocess.

Privacy posture (see also docs/mcp.md):
  - Every payload is passed through `_strip()`, which recursively removes identifier fields
    (ISIN, folio, trade/order id, UAN, member/account/client id, PAN). Symbols and scheme
    names are kept — they're needed for insight and aren't sensitive identifiers.
  - The model is still cloud-hosted, so whatever a tool returns *is* sent to the AI provider
    when analysed. Keep the server behind the VPN and set MCP_TOKEN.

Config (env):
  - MCP_TOKEN          — bearer token required on the HTTP transport (enforced in main.py).
  - MCP_ALLOWED_HOSTS  — comma-separated Host allowlist for DNS-rebinding protection.
                         Add the Pi's address (e.g. "<host>:8000") or a Twingate
                         hostname. Use "*" to disable host checking (fine for VPN + token).
                         localhost/127.0.0.1 are always allowed.
"""

from __future__ import annotations

import dataclasses
import os
from datetime import date

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .db import SessionLocal
from .analytics import (
    compute_allocation_breakdown,
    compute_holdings,
    compute_period_xirr,
    compute_realized_pnl_by_period,
    compute_summary,
    compute_xirr_split,
    find_orphan_sells,
)
from .networth import compute_networth

# --- identifiers stripped from every payload before it leaves the server ---
_PII_KEYS = {
    "isin", "folio", "folio_number", "trade_id", "order_id", "order_execution_time",
    "uan", "member_id", "account_number", "account_no", "client_id", "pan",
}


def _strip(obj):
    """Recursively drop identifier keys (case-insensitive) from dicts/lists. Symbols,
    scheme names, sectors, categories and all numbers are preserved."""
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k.lower() not in _PII_KEYS}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def _period_to_dates(period: str | None) -> tuple[date | None, date | None]:
    """Map a period shortcut to (from_date, to_date). Mirrors main.py's xirr_analysis:
    all | 1y | 3y | 5y | current_fy | prev_fy | fy_YYYY. None/'all' → (None, None)."""
    today = date.today()
    if period in (None, "all"):
        return None, None
    if period == "1y":
        return today.replace(year=today.year - 1), None
    if period == "3y":
        return today.replace(year=today.year - 3), None
    if period == "5y":
        return today.replace(year=today.year - 5), None
    if period == "current_fy":
        return date(today.year if today.month >= 4 else today.year - 1, 4, 1), None
    if period == "prev_fy":
        fy = today.year - 1 if today.month >= 4 else today.year - 2
        return date(fy, 4, 1), date(fy + 1, 3, 31)
    if period.startswith("fy_"):
        y = int(period[3:])
        return date(y, 4, 1), date(y + 1, 3, 31)
    return None, None


def _transport_security() -> TransportSecuritySettings:
    raw = os.environ.get("MCP_ALLOWED_HOSTS", "").strip()
    if raw == "*":
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    hosts = ["localhost", "127.0.0.1", "localhost:*", "127.0.0.1:*"]
    origins = ["http://localhost", "http://127.0.0.1"]
    for h in (x.strip() for x in raw.split(",") if x.strip()):
        hosts.extend([h, f"{h}:*"])
        origins.extend([f"http://{h}", f"https://{h}"])
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)


mcp = FastMCP(
    "portfolio-tracker",
    instructions=(
        "Read-only access to a personal portfolio of Indian equities, mutual funds, fixed "
        "income, EPF, bonds and US/global equities. All amounts are in INR unless a tool says "
        "otherwise. XIRR and P&L follow Indian tax conventions (STCG <12m, LTCG >=12m for "
        "equity). Values are point-in-time and read-only; you cannot place trades or change "
        "data. Use these tools to explain allocation, returns and risk — frame output as "
        "education, not personalised financial advice."
    ),
    stateless_http=True,            # one-shot tool calls; no per-client session state to keep
    streamable_http_path="/",       # mounted at /mcp by main.py → final URL is /mcp
    transport_security=_transport_security(),
)


# ----------------------------- read-only tools -----------------------------

@mcp.tool()
def list_holdings(segment: str | None = None) -> list[dict]:
    """Current holdings with per-holding economics.

    segment (optional): EQ | MF | FI | EPF | GLOBAL | BONDS. Omit for everything.
    Each row: symbol, display_name, segment, quantity, avg_cost, invested, current_price,
    current_value, realized_pnl, unrealized_pnl, pct_return, xirr (fraction, e.g. 0.18 = 18%),
    day_change (today's ₹ move) and day_change_pct. Identifiers (ISIN/folio) are omitted.
    """
    with SessionLocal() as db:
        rows = compute_holdings(db, segment=segment or None)
        return _strip([dataclasses.asdict(r) for r in rows])


@mcp.tool()
def portfolio_summary(segment: str | None = None) -> dict:
    """Aggregate totals: invested, current value, unrealized/realized P&L, % return, day's
    gain, and headline XIRR. segment (optional) scopes it (EQ | MF | FI | EPF | GLOBAL | BONDS).
    """
    with SessionLocal() as db:
        return _strip(compute_summary(db, segment=segment or None))


@mcp.tool()
def allocation_breakdown() -> dict:
    """Portfolio X-ray. Direct equity grouped by market-cap (large/mid/small) and by sector;
    mutual funds grouped by SEBI category. Useful for assessing diversification and cap tilt.
    """
    with SessionLocal() as db:
        return _strip(compute_allocation_breakdown(db))


@mcp.tool()
def xirr_analysis(period: str | None = None, segment: str | None = None) -> dict:
    """Annualised return (XIRR) of the portfolio vs benchmark indices for a period.

    period: all | 1y | 3y | 5y | current_fy | prev_fy | fy_YYYY (default all).
    segment (optional): EQ | MF | FI | EPF | GLOBAL | BONDS.
    Benchmarks replay the same cashflows into the index. XIRR values are fractions (0.15 = 15%).
    """
    frm, to = _period_to_dates(period)
    with SessionLocal() as db:
        result = compute_period_xirr(db, frm, to, segment=segment or None)
        if frm is None:   # active-vs-closed split only meaningful on the all-time view
            result.update(compute_xirr_split(db, segment=segment or None))
        return _strip(result)


@mcp.tool()
def realized_pnl(period: str | None = None, segment: str | None = None) -> dict:
    """Realised capital gains for sells in a period, split into STCG (<12 months, 20%) and
    LTCG (>=12 months, 12.5% above the ₹1.25L exemption for Indian equity) — for tax planning.

    period: all | 1y | 3y | 5y | current_fy | prev_fy | fy_YYYY (default all).
    segment (optional): EQ | MF | FI | EPF | GLOBAL | BONDS.
    """
    frm, to = _period_to_dates(period)
    with SessionLocal() as db:
        return _strip(compute_realized_pnl_by_period(db, frm, to, segment=segment or None))


@mcp.tool()
def net_worth() -> dict:
    """Total net worth across all asset classes (equity, mutual funds, fixed income, EPF,
    bonds, global) with the per-class breakdown and an as-of date."""
    with SessionLocal() as db:
        return _strip(compute_networth(db))


@mcp.tool()
def data_quality() -> list[dict]:
    """Data-quality flags: 'orphan sells' (a sell with no matching buy — usually an IPO
    allotment or corporate-action receipt) whose cost basis is missing. Each entry lists the
    symbol, date, quantity, price and the proceeds that aren't yet attributable to a buy."""
    with SessionLocal() as db:
        return _strip(find_orphan_sells(db))


# ----------------------------- HTTP transport -----------------------------

class _BearerGuard:
    """Pure-ASGI bearer-token gate. Implemented as raw ASGI (not BaseHTTPMiddleware) so it
    doesn't interfere with streamable-HTTP / SSE responses."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            import secrets
            from starlette.responses import PlainTextResponse

            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode("latin-1")
            ok = auth.startswith("Bearer ") and secrets.compare_digest(auth[7:], self.token)
            if not ok:
                resp = PlainTextResponse(
                    "Unauthorized", status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_http_app():
    """Build the streamable-HTTP ASGI app, wrapped with a bearer-token guard when MCP_TOKEN
    is set. Calling this also creates mcp.session_manager (accessed by main.py's lifespan)."""
    inner = mcp.streamable_http_app()
    token = os.environ.get("MCP_TOKEN", "").strip()
    if not token:
        import sys
        print(
            "WARNING: ENABLE_MCP=1 but MCP_TOKEN is empty — /mcp is running with NO "
            "authentication. Set MCP_TOKEN in .env unless you're certain this is intentional.",
            file=sys.stderr,
        )
    return _BearerGuard(inner, token) if token else inner


if __name__ == "__main__":
    # Local stdio transport for desktop/CLI clients (Claude Desktop, Claude Code, …).
    mcp.run()
