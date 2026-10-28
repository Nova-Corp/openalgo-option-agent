from datetime import datetime, timedelta

import pandas as pd

from agent.clock import IST
from agent.data import TickCandleBuilder, merge_candles
from fake_broker import make_candles


def test_builder_buckets_persist_and_reload(tmp_path):
    p = tmp_path / "tickcandles.csv"
    b = TickCandleBuilder(300, p)
    t0 = datetime(2026, 10, 28, 9, 15, 0, tzinfo=IST)
    prices = [100, 103, 99, 101]  # 09:15 bucket
    for i, px in enumerate(prices):
        b.add(t0 + timedelta(seconds=i * 60), px)
    b.add(t0 + timedelta(minutes=5, seconds=2), 102)  # first tick of 09:20 -> flushes 09:15 to disk
    b.add(t0 + timedelta(minutes=5, seconds=40), 104)
    assert b.add(t0 - timedelta(minutes=1), 50) is None  # pre-open tick ignored
    f = b.frame(t0 + timedelta(minutes=5, seconds=5))
    assert len(f) == 1 and f.iloc[0]["high"] == 103 and f.iloc[0]["low"] == 99 and f.iloc[0]["close"] == 101
    assert b.frame(t0 + timedelta(minutes=10)).shape[0] == 2
    text = p.read_text().splitlines()
    assert text[0] == "time,open,high,low,close,ticks,partial" and text[1].startswith("2026-10-28T09:15:00+05:30,100,103,99,101,4,0")
    b2 = TickCandleBuilder(300, p)  # reload after a restart
    assert len(b2.frame(t0 + timedelta(minutes=5, seconds=1))) == 1
    assert t0 + timedelta(minutes=5) not in b2.buckets or b2.buckets[t0 + timedelta(minutes=5)]["ticks"] >= 0


def test_partial_bucket_flag():
    b = TickCandleBuilder(300)
    t = datetime(2026, 10, 28, 9, 16, 51, tzinfo=IST)
    b.add(t, 100)
    assert datetime(2026, 10, 28, 9, 15, tzinfo=IST) in b.partial


def test_merge_prefers_broker_and_fills_gaps():
    day = datetime(2026, 10, 28).date()
    broker = make_candles(day, [100, 101, 102])  # 09:15, 09:20, 09:25
    ticks = make_candles(day, [900, 901, 902, 903, 904])  # 09:15..09:35
    merged, used = merge_candles(broker, ticks)
    assert used == 2 and len(merged) == 5
    assert merged.iloc[0]["close"] == 100 and merged.iloc[-1]["close"] == 904
    assert merge_candles(broker, pd.DataFrame())[1] == 0
    m2, u2 = merge_candles(pd.DataFrame(), ticks)
    assert u2 == 5 and len(m2) == 5
