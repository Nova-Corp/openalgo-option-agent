from datetime import date, datetime, timedelta

import pandas as pd

from agent.clock import IST
from agent.config import StrategyCfg
from agent.data import add_indicators
from agent.strategy import DayContext, OrbTrendStrategy
from fake_broker import make_candles

DAY = date(2026, 10, 27)
PREV = date(2026, 10, 26)


def build(today_closes, prev_trend="up"):
    n = 75
    if prev_trend == "up":
        prev = [24700 + i * 4 for i in range(n)]
    else:
        prev = [25300 - i * 4 for i in range(n)]
    df = pd.concat([make_candles(PREV, prev), make_candles(DAY, today_closes)])
    return add_indicators(df, 9, 21)


def test_ce_breakout_signal():
    # opening range 9:15-9:30 (3 candles) between ~24995 and ~25035, breakout candle at 9:30 closes 25060
    closes = [25000, 25020, 25010, 25060]
    df = build(closes)
    s = OrbTrendStrategy(StrategyCfg())
    now = datetime(2026, 10, 27, 9, 35, 5, tzinfo=IST)
    sig, note = s.evaluate(df, now, DayContext())
    assert sig is not None, note
    assert sig.option_type == "CE"
    assert sig.orb_high == 25025 + 0 or sig.orb_high > 25020


def test_pe_breakdown_signal():
    closes = [25000, 24980, 24990, 24940]
    df = build(closes, prev_trend="down")
    s = OrbTrendStrategy(StrategyCfg())
    now = datetime(2026, 10, 27, 9, 35, 5, tzinfo=IST)
    sig, note = s.evaluate(df, now, DayContext())
    assert sig is not None and sig.option_type == "PE", note


def test_no_signal_inside_range_and_limits():
    closes = [25000, 25020, 25010, 25015]
    df = build(closes)
    s = OrbTrendStrategy(StrategyCfg())
    now = datetime(2026, 10, 27, 9, 35, 5, tzinfo=IST)
    sig, note = s.evaluate(df, now, DayContext())
    assert sig is None and "inside opening range" in note
    # breakout but limits reached
    df2 = build([25000, 25020, 25010, 25060])
    sig, note = s.evaluate(df2, now, DayContext(trades_today=2))
    assert sig is None and "max trades" in note
    sig, note = s.evaluate(df2, now, DayContext(trades_ce=1))
    assert sig is None and "CE" in note
    sig, note = s.evaluate(df2, now, DayContext(last_exit_time=now - timedelta(minutes=3)))
    assert sig is None and "cooldown" in note


def test_filters_block_chase_and_trend():
    s = OrbTrendStrategy(StrategyCfg())
    now = datetime(2026, 10, 27, 9, 35, 5, tzinfo=IST)
    # breakout far beyond the range -> chase block
    sig, note = s.evaluate(build([25000, 25020, 25010, 25200]), now, DayContext())
    assert sig is None and "extended" in note
    # breakout against the EMA trend (prev day down) -> blocked
    sig, note = s.evaluate(build([25000, 25020, 25010, 25060], prev_trend="down"), now, DayContext())
    assert sig is None and "EMA" in note
    # before opening range completes
    sig, note = s.evaluate(build([25000, 25020]), datetime(2026, 10, 27, 9, 30, 5, tzinfo=IST), DayContext())
    assert sig is None
    # outside entry window
    sig, note = s.evaluate(build([25000, 25020, 25010, 25060]), datetime(2026, 10, 27, 14, 45, tzinfo=IST), DayContext())
    assert sig is None and "window" in note


def test_underlying_exit():
    s = OrbTrendStrategy(StrategyCfg())
    df = build([25000, 25020, 25010, 25060, 25070, 24900])
    entry = datetime(2026, 10, 27, 9, 35, tzinfo=IST)
    assert s.underlying_exit(df, "CE", entry) is not None
    assert s.underlying_exit(df, "PE", entry) is None
