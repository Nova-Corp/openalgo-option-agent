from datetime import date

import pytest

from agent.instruments import choose_expiry, fmt_expiry, parse_expiry, strike_from_symbol


def test_parse_and_format():
    assert parse_expiry("27-OCT-26") == date(2026, 10, 27)
    assert parse_expiry("27OCT26") == date(2026, 10, 27)
    assert fmt_expiry(date(2026, 11, 3)) == "03NOV26"


def test_choose_expiry_nearest_and_next():
    exps = ["03-NOV-26", "27-OCT-26", "09-NOV-26"]
    assert choose_expiry(exps, date(2026, 10, 27), "nearest") == date(2026, 10, 27)
    assert choose_expiry(exps, date(2026, 10, 27), "next") == date(2026, 11, 3)
    assert choose_expiry(exps, date(2026, 10, 27), "nearest", min_days=1) == date(2026, 11, 3)
    assert choose_expiry(exps, date(2026, 10, 28), "nearest") == date(2026, 11, 3)
    with pytest.raises(ValueError):
        choose_expiry(exps, date(2026, 12, 1), "nearest")


def test_strike_from_symbol():
    assert strike_from_symbol("NIFTY27OCT2625000CE") == 25000
    assert strike_from_symbol("VEDL25APR24292.5PE") == 292.5
    assert strike_from_symbol("NIFTY") == 0


def test_strike_from_sensex_native_symbol():
    assert strike_from_symbol("SENSEX29thOCT84000CE") == 84000


def test_resolve_via_search_fallback():
    from datetime import date as _d

    from agent.config import InstrumentCfg
    from agent.instruments import resolve_contract
    from fake_broker import FakeBroker, make_candles

    b = FakeBroker(make_candles(_d(2026, 10, 27), [73600]), expiries=("29-OCT-26", "05-NOV-26"))
    b.optionsymbol_fails = True
    b.set_ltp("SENSEX", 73598.0)
    b.contracts = [{"symbol": f"SENSEX29thOCT{k}{ot}", "exchange": "BFO", "instrumenttype": ot, "expiry": "29-OCT-26",
                    "strike": float(k), "lotsize": 20, "tick_size": 0.05}
                   for k in range(73000, 74100, 100) for ot in ("CE", "PE")]
    icfg = InstrumentCfg(underlying="SENSEX", underlying_exchange="BSE_INDEX", option_exchange="BFO", offset="ATM")
    c = resolve_contract(b, icfg, "CE", _d(2026, 10, 27))
    assert c.symbol == "SENSEX29thOCT73600CE" and c.lotsize == 20 and c.strike == 73600 and c.expiry == "29OCT26"
    icfg.offset = "OTM1"
    assert resolve_contract(b, icfg, "CE", _d(2026, 10, 27)).symbol == "SENSEX29thOCT73700CE"
    assert resolve_contract(b, icfg, "PE", _d(2026, 10, 27)).symbol == "SENSEX29thOCT73500PE"
