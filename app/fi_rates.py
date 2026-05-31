"""User-configurable benchmark rates for Fixed Income comparison.

Stored in data/fi_rates.json so changes persist across restarts.
Rates are annual percentages (e.g. 3.0 means 3.0% p.a.).

Indian reference rates (as of mid-2026):
  savings_rate    : 3.0%   SBI/HDFC/ICICI savings account (2.7–3.5% range)
  std_fd_rate     : 6.5%   Typical 1-year FD at major banks (SBI ~6.8%, HDFC ~7%)
  inflation_rate  : 4.5%   RBI CPI target zone; FY2024-25 avg ~4.8%

Update these whenever RBI changes rates or your bank changes its savings rate.
"""

from __future__ import annotations

import json
from pathlib import Path

_RATES_FILE = Path(__file__).resolve().parent.parent / "data" / "fi_rates.json"

_DEFAULTS: dict[str, float] = {
    "savings_rate":   3.0,   # major Indian bank savings account
    "std_fd_rate":    6.5,   # 1-year FD at SBI / major banks
    "inflation_rate": 4.5,   # RBI CPI target band midpoint
}


def load_fi_rates() -> dict[str, float]:
    """Load rates from file, filling missing keys with defaults."""
    if _RATES_FILE.exists():
        try:
            stored = json.loads(_RATES_FILE.read_text())
            return {**_DEFAULTS, **stored}
        except Exception:
            pass
    return dict(_DEFAULTS)


def save_fi_rates(rates: dict[str, float]) -> None:
    _RATES_FILE.write_text(json.dumps(rates, indent=2))


def update_fi_rate(key: str, value: float) -> dict[str, float]:
    if key not in _DEFAULTS:
        raise ValueError(f"Unknown rate key '{key}'. Valid: {list(_DEFAULTS)}")
    rates = load_fi_rates()
    rates[key] = value
    save_fi_rates(rates)
    return rates
