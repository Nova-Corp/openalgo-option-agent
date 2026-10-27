import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from agent.clock import IST, FakeClock
from agent.config import Config
from agent.data import add_indicators
from agent.engine import Engine, ModeNotAllowed
from agent.journal import Journal
from fake_broker import FakeBroker, make_candles

DAY = date(2026, 10, 27)
PREV = date(2026, 10, 26)


class ScriptedClock(FakeClock):
    """FakeClock that fires scripted events when simulated time passes them."""

    def __init__(self, start, events=None):
        super().__init__(start)
        self.events = sorted(events or [], key=lambda e: e[0])

    def sleep(self, seconds):
        super().sleep(seconds)
        while self.events and self.events[0][0] <= self._now:
            _, fn = self.events.pop(0)
            fn()


def candles():
    prev = [24700 + i * 4 for i in range(75)]
    today = [25000, 25020, 25010, 25060] + [25070 + i * 3 for i in range(71)]
    return pd.concat([make_candles(PREV, prev), make_candles(DAY, today)])


def make_cfg(tmp_path, **exec_over):
    cfg = Config()
    cfg.project_dir = str(tmp_path)
    cfg.execution.poll_interval_sec = 1.0
    for k, v in exec_over.items():
        setattr(cfg.execution, k, v)
    (tmp_path / "state").mkdir(exist_ok=True)
    return cfg


def test_full_day_target_hit(tmp_path):
    b = FakeBroker(candles())
    b.set_ltp("NIFTY", 25060)
    opt = "NIFTY27OCT2625050CE"
    b.set_ltp(opt, 100.0)
    start = datetime(2026, 10, 27, 9, 35, 6, tzinfo=IST)
    clock = ScriptedClock(start, [
        (start + timedelta(seconds=5), lambda: b.push(opt, "NFO", 110.0)),
        (start + timedelta(seconds=8), lambda: b.push(opt, "NFO", 152.0)),
    ])
    eng = Engine(make_cfg(tmp_path), b, clock)
    eng.run(max_iterations=40)
    j = Journal(tmp_path / "state", DAY)
    ts = j.trades
    assert len(ts) == 1
    t = ts[0]
    assert t.status == "CLOSED" and t.option_type == "CE" and t.symbol == opt
    assert t.entry_price == 100.0 and t.exit_price == 152.0
    assert t.qty == 65 and t.pnl == 52 * 65
    assert t.exit_reason.startswith("TARGET")
    assert b.positions[opt] == 0
    assert (tmp_path / "state" / "trades.csv").exists()
    assert t.mode == "paper"


def test_stoploss_and_no_reentry_same_direction(tmp_path):
    b = FakeBroker(candles())
    b.set_ltp("NIFTY", 25060)
    opt = "NIFTY27OCT2625050CE"
    b.set_ltp(opt, 100.0)
    start = datetime(2026, 10, 27, 9, 35, 6, tzinfo=IST)
    clock = ScriptedClock(start, [(start + timedelta(seconds=4), lambda: b.push(opt, "NFO", 70.0))])
    eng = Engine(make_cfg(tmp_path), b, clock)
    eng.run(max_iterations=400)  # runs through the 9:40 candle too
    j = Journal(tmp_path / "state", DAY)
    assert len(j.trades) == 1 and j.trades[0].exit_reason.startswith("STOPLOSS")
    assert j.realized_pnl() == -30 * 65


def test_live_mode_refused_without_latch(tmp_path):
    b = FakeBroker(candles(), analyzer=False)
    eng = Engine(make_cfg(tmp_path), b, FakeClock(datetime(2026, 10, 27, 9, 35, 6, tzinfo=IST)))
    with pytest.raises(ModeNotAllowed):
        eng.run(max_iterations=2)
    assert not any(c.startswith("order") for c in b.calls)


def test_live_mode_caps_lots(tmp_path):
    b = FakeBroker(candles(), analyzer=False)
    b.set_ltp("NIFTY", 25060)
    opt = "NIFTY27OCT2625050CE"
    b.set_ltp(opt, 20.0)  # cheap premium -> paper would size 2 lots (max_lots)
    cfg = make_cfg(tmp_path, allow_live=True, live_max_lots=1)
    eng = Engine(cfg, b, FakeClock(datetime(2026, 10, 27, 9, 35, 6, tzinfo=IST)))
    eng.run(max_iterations=3)
    j = Journal(tmp_path / "state", DAY)
    assert j.trades[0].lots == 1 and j.trades[0].mode == "LIVE"


def test_rejected_entry_marked_failed(tmp_path):
    b = FakeBroker(candles())
    b.set_ltp("NIFTY", 25060)
    b.set_ltp("NIFTY27OCT2625050CE", 100.0)
    b.reject_next = True
    eng = Engine(make_cfg(tmp_path), b, FakeClock(datetime(2026, 10, 27, 9, 35, 6, tzinfo=IST)))
    eng.run(max_iterations=3)
    j = Journal(tmp_path / "state", DAY)
    assert j.trades[0].status == "FAILED" and j.open_trade() is None


def test_restart_resumes_open_trade_and_square_off(tmp_path):
    # First run: enter, then stop the loop while the trade is open.
    b = FakeBroker(candles())
    b.set_ltp("NIFTY", 25060)
    opt = "NIFTY27OCT2625050CE"
    b.set_ltp(opt, 100.0)
    eng = Engine(make_cfg(tmp_path), b, FakeClock(datetime(2026, 10, 27, 9, 35, 6, tzinfo=IST)))
    eng.run(max_iterations=3)
    j = Journal(tmp_path / "state", DAY)
    assert j.open_trade() is not None and b.positions[opt] == 65
    # Restart just before square-off with the position still open at the broker.
    b.set_ltp(opt, 120.0)
    eng2 = Engine(make_cfg(tmp_path), b, FakeClock(datetime(2026, 10, 27, 15, 9, 58, tzinfo=IST)))
    eng2.run(max_iterations=10)
    j2 = Journal(tmp_path / "state", DAY)
    t = j2.trades[0]
    assert t.status == "CLOSED" and t.exit_reason.startswith("square-off") and t.exit_price == 120.0
    assert b.positions[opt] == 0


def test_restart_when_position_closed_externally(tmp_path):
    b = FakeBroker(candles())
    b.set_ltp("NIFTY", 25060)
    opt = "NIFTY27OCT2625050CE"
    b.set_ltp(opt, 100.0)
    eng = Engine(make_cfg(tmp_path), b, FakeClock(datetime(2026, 10, 27, 9, 35, 6, tzinfo=IST)))
    eng.run(max_iterations=3)
    # someone closed it in the OpenAlgo UI
    b.set_ltp(opt, 90.0)
    b.place_order(opt, "NFO", "SELL", 65, "MIS")
    eng2 = Engine(make_cfg(tmp_path), b, FakeClock(datetime(2026, 10, 27, 9, 36, 0, tzinfo=IST)))
    eng2.run(max_iterations=2)
    t = Journal(tmp_path / "state", DAY).trades[0]
    assert t.status == "CLOSED" and "outside the agent" in t.exit_reason and t.exit_price == 90.0


def test_stop_file_squares_off(tmp_path):
    b = FakeBroker(candles())
    b.set_ltp("NIFTY", 25060)
    opt = "NIFTY27OCT2625050CE"
    b.set_ltp(opt, 100.0)
    start = datetime(2026, 10, 27, 9, 35, 6, tzinfo=IST)
    clock = ScriptedClock(start, [(start + timedelta(seconds=3), lambda: (tmp_path / "STOP").touch())])
    eng = Engine(make_cfg(tmp_path), b, clock)
    eng.run(max_iterations=50)
    t = Journal(tmp_path / "state", DAY).trades[0]
    assert t.status == "CLOSED" and "STOP" in t.exit_reason
    assert eng.stopped_reason == "STOP file"


def test_exit_timeout_then_position_check(tmp_path):
    """Exit order sits 'open' past the fill timeout; the executor re-checks the position and re-sends."""
    b = FakeBroker(candles())
    b.set_ltp("NIFTY", 25060)
    opt = "NIFTY27OCT2625050CE"
    b.set_ltp(opt, 100.0)
    start = datetime(2026, 10, 27, 9, 35, 6, tzinfo=IST)

    def hold_then_crash():
        b.hold_next = True
        b.push(opt, "NFO", 60.0)

    clock = ScriptedClock(start, [(start + timedelta(seconds=3), hold_then_crash),
                                  (start + timedelta(seconds=30), b.release)])
    cfg = make_cfg(tmp_path, fill_timeout_sec=5)
    eng = Engine(cfg, b, clock)
    eng.run(max_iterations=80)
    t = Journal(tmp_path / "state", DAY).trades[0]
    assert t.status == "CLOSED" and b.positions[opt] == 0


def test_entry_timeout_is_settled_from_position(tmp_path):
    b = FakeBroker(candles())
    b.set_ltp("NIFTY", 25060)
    opt = "NIFTY27OCT2625050CE"
    b.set_ltp(opt, 100.0)
    b.timeout_next = True
    start = datetime(2026, 10, 27, 9, 35, 6, tzinfo=IST)
    clock = ScriptedClock(start, [(start + timedelta(seconds=10), lambda: b.push(opt, "NFO", 155.0))])
    eng = Engine(make_cfg(tmp_path), b, clock)
    eng.run(max_iterations=40)
    t = Journal(tmp_path / "state", DAY).trades[0]
    assert t.status == "CLOSED" and t.entry_price == 100.0 and t.exit_price == 155.0
    assert b.positions[opt] == 0 and sum(c.startswith("order:BUY") for c in b.calls) == 1


def test_next_session_start_skips_weekend_and_after_squareoff(tmp_path):
    b = FakeBroker(candles())
    eng = Engine(make_cfg(tmp_path), b, FakeClock(datetime(2026, 10, 27, 9, 0, tzinfo=IST)))
    # before the session on a Tuesday -> today 09:05
    assert eng.next_session_start(datetime(2026, 10, 27, 9, 0, tzinfo=IST)) == datetime(2026, 10, 27, 9, 5, tzinfo=IST)
    # after square-off on Friday 30 Oct -> Monday 2 Nov
    assert eng.next_session_start(datetime(2026, 10, 30, 16, 0, tzinfo=IST)) == datetime(2026, 11, 2, 9, 5, tzinfo=IST)


def test_run_forever_waits_then_trades_then_stops(tmp_path):
    b = FakeBroker(candles())
    b.set_ltp("NIFTY", 25060)
    opt = "NIFTY27OCT2625050CE"
    b.set_ltp(opt, 100.0)
    start = datetime(2026, 10, 27, 9, 0, tzinfo=IST)
    clock = ScriptedClock(start, [
        (datetime(2026, 10, 27, 9, 35, 10, tzinfo=IST), lambda: b.push(opt, "NFO", 160.0)),
        (datetime(2026, 10, 27, 9, 36, 0, tzinfo=IST), lambda: (tmp_path / "STOP").touch()),
    ])
    eng = Engine(make_cfg(tmp_path), b, clock)
    eng.run_forever()
    t = Journal(tmp_path / "state", DAY).trades[0]
    assert t.status == "CLOSED" and t.exit_reason.startswith("TARGET")
