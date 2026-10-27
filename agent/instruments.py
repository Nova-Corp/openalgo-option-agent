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


def _ordinal(d: int) -> str:
    if 11 <= d % 100 <= 13:
        return f"{d}th"
    return f"{d}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(d % 10, 'th') }"


def _offset_steps(offset: str, option_type: str) -> int:
    """ATM -> 0; OTMn -> +n strikes away from the money; ITMn -> -n (direction depends on CE/PE)."""
    o = offset.strip().upper()
    if o == "ATM":
        return 0
    m = re.fullmatch(r"(OTM|ITM)(\d+)", o)
    if not m:
        raise ValueError(f"bad offset {offset}")
    n = int(m.group(2)) * (1 if m.group(1) == "OTM" else -1)
    return n if option_type == "CE" else -n


def resolve_via_search(broker, icfg: InstrumentCfg, option_type: str, exp: date) -> dict:
    """Fallback for brokers whose master contract uses the exchange's own naming (AliceBlue BFO:
    SENSEX29thOCT84000CE). Finds the contract by prefix search around the current ATM strike."""
    ltp = float(broker.quotes(icfg.underlying, icfg.underlying_exchange).get("ltp") or 0)
    if ltp <= 0:
        raise ValueError(f"no LTP for {icfg.underlying}")
    mon = exp.strftime("%b").upper()
    prefixes = [f"{icfg.underlying}{_ordinal(exp.day)}{mon}", f"{icfg.underlying}{exp.day:02d}{mon}{exp:%y}"]
    exp_str = exp.strftime("%d-%b-%y").upper()
    rows: list[dict] = []
    for prefix in prefixes:
        # narrow the search with the leading digits of the price so the 500-row cap is never hit
        for lead in (str(int(ltp))[:2], str(int(ltp))[:1], ""):
            rows = [r for r in broker.search(prefix + lead, icfg.option_exchange)
                    if r.get("instrumenttype") == option_type and str(r.get("expiry", "")).upper() == exp_str]
            if len(rows) >= 3:
                break
        if rows:
            break
    if not rows:
        raise ValueError(f"no {icfg.underlying} {option_type} contracts found for {exp_str} via search")
    strikes = sorted({float(r["strike"]) for r in rows})
    step = min(b - a for a, b in zip(strikes, strikes[1:])) if len(strikes) > 1 else 100.0
    atm = min(strikes, key=lambda k: abs(k - ltp))
    target = atm + _offset_steps(icfg.offset, option_type) * step
    row = min(rows, key=lambda r: abs(float(r["strike"]) - target))
    return {"symbol": row["symbol"], "exchange": row.get("exchange", icfg.option_exchange), "lotsize": row.get("lotsize"),
            "tick_size": row.get("tick_size"), "underlying_ltp": ltp, "resolved_by": "search"}


def resolve_contract(broker, icfg: InstrumentCfg, option_type: str, today: date) -> OptionContract:
    expiries = broker.expiries(icfg.underlying, icfg.option_exchange)
    exp = choose_expiry(expiries, today, icfg.expiry, icfg.min_days_to_expiry)
    try:
        res = broker.option_symbol(icfg.underlying, icfg.underlying_exchange, fmt_expiry(exp), icfg.offset, option_type)
    except Exception as e:  # BrokerError: "No strikes found ..." for exchange-native symbol formats
        if "no strikes" not in str(e).lower() and "not found" not in str(e).lower():
            raise
        res = resolve_via_search(broker, icfg, option_type, exp)
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
