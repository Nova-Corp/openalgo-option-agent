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
