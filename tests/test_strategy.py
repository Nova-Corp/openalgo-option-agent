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


def _day_with_breakout(open_, high, low, close):
    """Prev-day uptrend, 15-min range ~24995-25035, then a custom 09:30 candle."""
    prev = [24700 + i * 4 for i in range(75)]
    df = pd.concat([make_candles(PREV, prev), make_candles(DAY, [25000, 25020, 25010, 25010])])
    ts = datetime(2026, 10, 27, 9, 30, tzinfo=IST)
    df.loc[ts, ["open", "high", "low", "close"]] = [open_, high, low, close]
    return add_indicators(df, 9, 21)


NOW = datetime(2026, 10, 27, 9, 35, 5, tzinfo=IST)


def test_doji_breakout_is_rejected():
    s = OrbTrendStrategy(StrategyCfg())
    # closes above the range high but body is 5 points on a 60-point range -> doji
    sig, note = s.evaluate(_day_with_breakout(25055, 25090, 25030, 25060), NOW, DayContext())
    assert sig is None and "doji" in note and "waiting" in note


def test_red_candle_breakout_is_rejected_for_ce():
    s = OrbTrendStrategy(StrategyCfg())
    # opened at 25100, sold off to 25060: still above the range high, but a red candle
    sig, note = s.evaluate(_day_with_breakout(25100, 25105, 25055, 25060), NOW, DayContext())
    assert sig is None and "colour" in note


def test_upper_wick_rejection_is_rejected():
    s = OrbTrendStrategy(StrategyCfg())
    # green body 25010 -> 25060 but a spike to 25160: close sits only ~33% up the candle
    sig, note = s.evaluate(_day_with_breakout(25010, 25160, 25008, 25060), NOW, DayContext())
    assert sig is None and "wick" in note


def test_decisive_breakout_candle_is_accepted():
    s = OrbTrendStrategy(StrategyCfg())
    sig, note = s.evaluate(_day_with_breakout(25012, 25065, 25008, 25060), NOW, DayContext())
    assert sig is not None and sig.option_type == "CE", note


def test_candle_filters_can_be_disabled():
    s = OrbTrendStrategy(StrategyCfg(min_body_pct=0, require_direction=False, min_close_position_pct=0))
    sig, note = s.evaluate(_day_with_breakout(25100, 25105, 25055, 25060), NOW, DayContext())
    assert sig is not None and sig.option_type == "CE"


def test_pe_breakdown_needs_red_decisive_candle():
    s = OrbTrendStrategy(StrategyCfg())
    prev = [25300 - i * 4 for i in range(75)]
    df = pd.concat([make_candles(PREV, prev), make_candles(DAY, [25000, 24980, 24990, 24990])])
    ts = datetime(2026, 10, 27, 9, 30, tzinfo=IST)
    df.loc[ts, ["open", "high", "low", "close"]] = [24988, 24992, 24935, 24940]  # red, closes near the low
    sig, note = s.evaluate(add_indicators(df, 9, 21), NOW, DayContext())
    assert sig is not None and sig.option_type == "PE", note
    df.loc[ts, ["open", "high", "low", "close"]] = [24938, 24992, 24935, 24940]  # green hammer-ish: rejected
    sig, note = s.evaluate(add_indicators(df, 9, 21), NOW, DayContext())
    assert sig is None and ("colour" in note or "doji" in note)


def test_full_body_rule_rejects_candle_that_opened_inside_range():
    s = OrbTrendStrategy(StrategyCfg(require_full_body_outside=True))
    # decisive green candle 25012 -> 25060 but it opened below the ~25035 range high
    sig, note = s.evaluate(_day_with_breakout(25012, 25065, 25008, 25060), NOW, DayContext())
    assert sig is None and "opened inside the range" in note and "full-body" in note


def test_full_body_rule_accepts_candle_fully_above_range():
    s = OrbTrendStrategy(StrategyCfg(require_full_body_outside=True, max_extension_pct=0.5))
    # opens above the range high and closes higher: whole body outside
    sig, note = s.evaluate(_day_with_breakout(25040, 25075, 25038, 25070), NOW, DayContext())
    assert sig is not None and sig.option_type == "CE", note


def test_full_body_rule_off_keeps_old_behaviour():
    s = OrbTrendStrategy(StrategyCfg(require_full_body_outside=False))
    sig, note = s.evaluate(_day_with_breakout(25012, 25065, 25008, 25060), NOW, DayContext())
    assert sig is not None and sig.option_type == "CE", note
