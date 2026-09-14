# AI access via MCP (read-only)

The tracker can expose your portfolio to AI tools (Claude Desktop, Claude Code, ChatGPT,
Gemini, Cursor, …) through a **read-only** [MCP](https://modelcontextprotocol.io) server, so you
can ask things like *"is my small-cap allocation aggressive for my goals?"* or *"summarise my
realised gains this FY"* against your real numbers.

It's **off by default**. One server, defined once (`app/mcp_server.py`), is served two ways:

- **Streamable HTTP** — mounted into the app at `http://<host>:8000/mcp` (same port + VPN as the
  dashboard). For remote/networked clients.
- **stdio** — `python -m app.mcp_server`. For local clients that spawn a subprocess.

---

## Read this first — what "safe" does and doesn't mean

MCP controls **what** is exposed and **how** it's reached, but the AI **model is cloud-hosted**.
So the moment a tool result is analysed, those numbers are sent to the AI provider (Anthropic /
OpenAI / Google). MCP cannot change that. The safeguards here are:

- **Read-only.** No tool can write, delete, or place trades.
- **PII-stripped.** ISIN, folio, trade/order id, UAN, member/account/client id and PAN are
  removed from every payload (`_strip()` in `app/mcp_server.py`). Symbols and scheme names stay.
- **Off by default + token-gated.** Enable explicitly; protect with a bearer token.
- **VPN-only by default.** Bind it to your Twingate network, not the public internet.

If you want a setup where data *never* leaves your network, that requires a **local LLM**
(e.g. Ollama) — cloud tools like ChatGPT/Gemini inherently receive what they analyse.

---

## Enable it

In your `.env` (see `.env.example`):

```bash
ENABLE_MCP=1
# generate a long random secret:
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
MCP_TOKEN=<paste-the-secret>
# the address you reach the app on (so DNS-rebinding protection allows it):
MCP_ALLOWED_HOSTS=<host>:8000        # or a Twingate hostname, or "*" to disable the check
```

Then restart (`docker compose up -d` on the Pi, or restart uvicorn locally). The endpoint is
`http://<host>:8000/mcp`, authenticated with `Authorization: Bearer <MCP_TOKEN>`.

> Note: the human dashboard's Basic Auth (`APP_PASSWORD`) does **not** apply to `/mcp` — the MCP
> endpoint uses its own bearer token instead, because MCP clients don't speak HTTP Basic.

---

## Client setup

### Claude Code
```bash
claude mcp add portfolio --transport http http://<host>:8000/mcp \
  --header "Authorization: Bearer <MCP_TOKEN>"
```

### Claude Desktop
- **Remote:** Settings → Connectors → *Add custom connector* → URL `http://<host>:8000/mcp`.
- **Local (stdio)** — edit `claude_desktop_config.json` (Settings → Developer → Edit config). No
  token needed; it reads the live DB directly on the same machine:
  ```json
  {
    "mcpServers": {
      "portfolio": {
        "command": "/absolute/path/to/.venv/bin/python",
        "args": ["-m", "app.mcp_server"],
        "cwd": "/absolute/path/to/portfolio-tracker"
      }
    }
  }
  ```
  On Windows use the venv interpreter, e.g. `"command": "E:\\Github Repos\\portfolio-tracker\\.venv\\Scripts\\python.exe"`.

### ChatGPT
Settings → **Connectors** (Plus/Pro/Business; enable *Developer mode* for custom MCP) → add an MCP
server with URL `http://<host>:8000/mcp` and the bearer token. **Reachability:** the ChatGPT
client must be able to reach the host — the **web app at chatgpt.com cannot reach a Twingate-only
Pi**. See *Reachability* below.

### Gemini
Gemini CLI — `gemini mcp add portfolio --transport http http://<host>:8000/mcp --header "Authorization: Bearer <MCP_TOKEN>"`, or add to `~/.gemini/settings.json`:
```json
{ "mcpServers": { "portfolio": {
  "httpUrl": "http://<host>:8000/mcp",
  "headers": { "Authorization": "Bearer <MCP_TOKEN>" }
} } }
```

---

## Reachability (the one caveat with cloud web apps)

A client can only use the server if it can **reach the host**:

- **Local clients on the VPN** (Claude Desktop/Code, Gemini CLI, Cursor on a Twingate-connected
  device) → reach `http://<host>:8000/mcp` directly. ✅ This is the safe, recommended path.
- **Cloud web apps** (chatgpt.com, gemini.google.com) run on the provider's servers and **cannot
  reach** a Twingate-only Pi. To use those you'd have to **expose the endpoint publicly** —
  e.g. a Cloudflare Tunnel or a Caddy reverse proxy with HTTPS, still requiring `MCP_TOKEN`. That
  publishes a financial endpoint to the internet, so it's a deliberate, separate decision and is
  **not enabled here**. If you want it, keep the token long and rotate it periodically.

---

## Tools exposed

| Tool | Returns |
|---|---|
| `list_holdings(segment?)` | per-holding qty, avg cost, invested, value, P&L, % return, XIRR, day change |
| `portfolio_summary(segment?)` | invested / current / P&L / % return / day's gain / headline XIRR |
| `allocation_breakdown()` | equity by market-cap + sector; MF by SEBI category |
| `xirr_analysis(period?, segment?)` | portfolio vs benchmark XIRR (period: all/1y/3y/5y/current_fy/prev_fy/fy_YYYY) |
| `realized_pnl(period?, segment?)` | realised gains with STCG/LTCG split |
| `net_worth()` | total across all asset classes + per-class breakdown |
| `data_quality()` | orphan-sell flags (likely IPO allotments / corporate actions) |

`segment` ∈ `EQ | MF | FI | EPF | GLOBAL | BONDS` (omit for everything).

---

## Turning it off / rotating access

- **Revoke a token:** change `MCP_TOKEN` and restart — old clients stop working immediately.
- **Disable entirely:** unset `ENABLE_MCP` (or set to empty) and restart — `/mcp` returns 404.
