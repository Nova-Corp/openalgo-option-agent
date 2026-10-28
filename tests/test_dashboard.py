import json
import threading
import urllib.request
from datetime import date, datetime
from http.server import ThreadingHTTPServer

from agent.clock import IST
from agent.config import Config
from agent.dashboard import Handler, Summary, load_all_trades
from agent.journal import Journal, Trade
from fake_broker import FakeBroker, make_candles


def seed(tmp_path):
    j = Journal(tmp_path / "state" / "NIFTY", date(2026, 10, 26))
    t = Trade.new(date="2026-10-26", mode="paper", symbol="NIFTY27OCT2625000CE", exchange="NFO", option_type="CE",
                  lots=1, qty=65, entry_price=100, exit_price=150, pnl=3250, status="CLOSED", exit_reason="TARGET")
    j.add(t)
    j2 = Journal(tmp_path / "state" / "SENSEX", date(2026, 10, 27))
    o = Trade.new(date="2026-10-27", mode="paper", symbol="NIFTY03NOV2625000PE", exchange="NFO", option_type="PE",
                  lots=1, qty=65, entry_price=80, status="OPEN", stop={"entry": 80, "stop": 60, "target": 120, "highest": 80})
    j2.add(o)
    return o


def test_summary_and_http(tmp_path):
    o = seed(tmp_path)
    cfg = Config()
    cfg.project_dir = str(tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "agent-NIFTY.log").write_text("2026-10-27 09:40:05,000 INFO agent.engine: line1\n2026-10-27 09:41:05,000 INFO agent.engine: line2\n")
    b = FakeBroker(make_candles(date(2026, 10, 27), [25000, 25010]))
    b.set_ltp(o.symbol, 90.0)
    b.positions[o.symbol] = 65
    s = Summary(cfg, b, cache_sec=0)
    p = s.build()
    assert len(p["trades"]) == 2 and p["trades"][0]["date"] == "2026-10-27"  # newest first
    ot = p["open_trades"][0]
    assert ot["symbol"] == o.symbol and ot["ltp"] == 90.0 and ot["unrealized"] == 650.0 and len(p["open_trades"]) == 1
    assert p["broker"]["paper"] is True and p["broker"]["positions"][0]["symbol"] == o.symbol
    assert len(p["log"]) == 2 and p["log"][0].endswith("line1") and "[NIFTY]" in p["log"][0]
    # serve over HTTP
    Handler.summary = s
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        port = srv.server_address[1]
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
        assert "<title>Option Agent</title>" in html
        js = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/summary").read())
        assert js["open_trades"][0]["unrealized"] == 650.0
    finally:
        srv.shutdown()
        srv.server_close()


def test_summary_survives_broker_outage(tmp_path):
    seed(tmp_path)
    cfg = Config()
    cfg.project_dir = str(tmp_path)

    class Dead(FakeBroker):
        def analyzer_mode(self):
            from agent.broker import BrokerError
            raise BrokerError("connection refused")

    p = Summary(cfg, Dead(make_candles(date(2026, 10, 27), [25000])), cache_sec=0).build()
    assert p["broker"]["ok"] is False and "refused" in p["broker"]["error"]
    assert len(p["trades"]) == 2


def test_chart_api(tmp_path):
    import urllib.request
    from datetime import datetime as _dt

    from agent.clock import IST
    from agent.dashboard import ChartData

    cfg = Config()
    cfg.project_dir = str(tmp_path)
    day = date(2026, 10, 27)
    closes = [25000, 25020, 25010, 25060] + [25070 + i * 3 for i in range(71)]
    b = FakeBroker(make_candles(day, closes))
    j = Journal(tmp_path / "state" / "NIFTY", day)
    t = Trade.new(date=day.isoformat(), mode="paper", symbol="NIFTY27OCT2625050CE", exchange="NFO", option_type="CE",
                  lots=1, qty=65, entry_price=100, exit_price=150, pnl=3250, status="CLOSED", exit_reason="TARGET",
                  entry_time="2026-10-27T09:35:06+05:30", exit_time="2026-10-27T09:35:18+05:30",
                  signal_time="2026-10-27T09:35:00+05:30", orb_high=25035, orb_low=24990,
                  stop_history=[{"t": "2026-10-27T09:35:06+05:30", "stop": 75, "target": 150},
                                {"t": "2026-10-27T09:35:10+05:30", "stop": 100, "target": 150}])
    j.add(t)
    (tmp_path / "state" / "NIFTY" / f"ticks-{t.id}.csv").write_text(
        "time,ltp\n2026-10-27T09:35:06+05:30,100.0\n2026-10-27T09:35:10+05:30,118.0\n2026-10-27T09:35:18+05:30,152.0\n")
    cd = ChartData(cfg, b)
    p = cd.build("NIFTY", day)
    assert len(p["candles"]) == 75 and p["candles"][0]["time"] == int(_dt(2026, 10, 27, 9, 15, tzinfo=IST).timestamp()) + 19800
    assert p["orb"]["high"] > p["orb"]["low"] and p["orb"]["end"] - p["orb"]["start"] == 900
    tr = p["trades"][0]
    assert tr["premium_source"] == "ticks" and [x["value"] for x in tr["premium"]] == [100.0, 118.0, 152.0]
    assert tr["stop_history"][1]["stop"] == 100 and tr["entry_time_ts"] and tr["signal_time_ts"]
    assert "ema_fast" in p["candles"][-1] and "vwap" in p["candles"][-1]
    # cached second call
    assert cd.build("NIFTY", day)["candles"] == p["candles"]


def test_chart_keeps_last_good_candles_on_broker_hiccup(tmp_path):
    from agent.broker import BrokerError
    from agent.dashboard import ChartData

    cfg = Config()
    cfg.project_dir = str(tmp_path)
    day = date(2026, 10, 27)
    good = make_candles(day, [25000, 25010, 25020])
    b = FakeBroker(good)
    cd = ChartData(cfg, b)
    assert len(cd.build("NIFTY", day)["candles"]) == 3
    cd._candles[("NIFTY", day.isoformat())] = (0.0, cd._candles[("NIFTY", day.isoformat())][1])  # expire the cache
    b.candles = make_candles(date(2026, 10, 20), [1, 2])  # broker now answers with nothing for that day
    assert len(cd.build("NIFTY", day)["candles"]) == 3  # previous candles kept
    cd._candles[("NIFTY", day.isoformat())] = (0.0, cd._candles[("NIFTY", day.isoformat())][1])

    def boom(*a, **k):
        raise BrokerError("Could not reach the broker")

    b.history = boom
    assert len(cd.build("NIFTY", day)["candles"]) == 3  # broker error: still the previous candles, no 500
    # with nothing cached at all, an empty answer is returned but not cached
    cd2 = ChartData(cfg, FakeBroker(make_candles(date(2026, 10, 20), [1, 2])))
    assert cd2.build("NIFTY", day)["candles"] == [] and ("NIFTY", day.isoformat()) not in cd2._candles


def test_chart_fills_from_tick_candles(tmp_path):
    from agent.broker import BrokerError
    from agent.dashboard import ChartData

    cfg = Config()
    cfg.project_dir = str(tmp_path)
    day = date(2026, 10, 28)
    d = tmp_path / "state" / "SENSEX"
    d.mkdir(parents=True)
    (d / f"tickcandles-{day}.csv").write_text(
        "time,open,high,low,close,ticks,partial\n"
        "2026-10-28T09:15:00+05:30,100,110,95,105,40,0\n"
        "2026-10-28T09:20:00+05:30,105,112,101,110,38,0\n"
        "2026-10-28T09:25:00+05:30,110,115,108,112,41,0\n")
    # broker serves only the first candle of the day -> ticks fill the rest
    b = FakeBroker(make_candles(day, [105]))
    cd = ChartData(cfg, b)
    p = cd.build("SENSEX", day)
    assert len(p["candles"]) == 3 and p["candle_source"] == "broker+ticks" and p["orb"]["high"] == 115
    # broker errors entirely -> all from ticks
    def boom(*a, **k):
        raise BrokerError("Could not reach the broker")
    b.history = boom
    cd2 = ChartData(cfg, b)
    p2 = cd2.build("SENSEX", day)
    assert len(p2["candles"]) == 3 and p2["candle_source"] == "ticks"
