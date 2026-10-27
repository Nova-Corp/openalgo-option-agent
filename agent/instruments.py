"""Expiry selection and option contract resolution."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime

from .config import InstrumentCfg

# NIFTY27OCT2625000CE -> 25000 ; SENSEX3rdDEC66100CE (weekly BSE format) -> 66100 ; VEDL25APR24292.5CE -> 292.5
_STRIKE_RE = re.compile(r"(?:\d{2}[A-Z]{3}\d{2}|\d{1,2}(?:ST|ND|RD|TH)[A-Z]{3})(\d+(?:\.\d+)?)(CE|PE)$", re.IGNORECASE)


@dataclass
class OptionContract:
    symbol: str
    exchange: str
    option_type: str
    lotsize: int
    tick_size: float
    expiry: str  # DDMMMYY as sent to OpenAlgo
    expiry_date: str  # ISO
    strike: float
    underlying_ltp: float

    def to_dict(self) -> dict:
        return asdict(self)


def parse_expiry(s: str) -> date:
    """'27-OCT-26' or '27OCT26' -> date."""
    s = s.strip().upper().replace("-", "")
    return datetime.strptime(s, "%d%b%y").date()


def fmt_expiry(d: date) -> str:
    return d.strftime("%d%b%y").upper()


def choose_expiry(expiries: list[str], today: date, mode: str = "nearest", min_days: int = 0) -> date:
    ds = sorted({parse_expiry(e) for e in expiries})
    eligible = [d for d in ds if (d - today).days >= max(min_days, 0)]
    if not eligible:
        raise ValueError(f"no expiry on/after {today} with min_days={min_days}: {expiries}")
    idx = 0 if mode == "nearest" else 1
    if idx >= len(eligible):
        idx = len(eligible) - 1
    return eligible[idx]


def strike_from_symbol(symbol: str) -> float:
    m = _STRIKE_RE.search(symbol)
    return float(m.group(1)) if m else 0.0


def resolve_contract(broker, icfg: InstrumentCfg, option_type: str, today: date) -> OptionContract:
    expiries = broker.expiries(icfg.underlying, icfg.option_exchange)
    exp = choose_expiry(expiries, today, icfg.expiry, icfg.min_days_to_expiry)
    res = broker.option_symbol(icfg.underlying, icfg.underlying_exchange, fmt_expiry(exp), icfg.offset, option_type)
    symbol = res["symbol"]
    return OptionContract(
        symbol=symbol,
        exchange=res.get("exchange", icfg.option_exchange),
        option_type=option_type,
        lotsize=int(res.get("lotsize") or 0),
        tick_size=float(res.get("tick_size") or 0.05),
        expiry=fmt_expiry(exp),
        expiry_date=exp.isoformat(),
        strike=strike_from_symbol(symbol),
        underlying_ltp=float(res.get("underlying_ltp") or 0),
    )
