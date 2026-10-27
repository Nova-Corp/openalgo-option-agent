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
    j = Journal(tmp_path / "state", date(2026, 10, 26))
    t = Trade.new(date="2026-10-26", mode="paper", symbol="NIFTY27OCT2625000CE", exchange="NFO", option_type="CE",
                  lots=1, qty=65, entry_price=100, exit_price=150, pnl=3250, status="CLOSED", exit_reason="TARGET")
    j.add(t)
    j2 = Journal(tmp_path / "state", date(2026, 10, 27))
    o = Trade.new(date="2026-10-27", mode="paper", symbol="NIFTY03NOV2625000PE", exchange="NFO", option_type="PE",
                  lots=1, qty=65, entry_price=80, status="OPEN", stop={"entry": 80, "stop": 60, "target": 120, "highest": 80})
    j2.add(o)
    return o


def test_summary_and_http(tmp_path):
    o = seed(tmp_path)
    cfg = Config()
    cfg.project_dir = str(tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "agent.log").write_text("line1\nline2\n")
    b = FakeBroker(make_candles(date(2026, 10, 27), [25000, 25010]))
    b.set_ltp(o.symbol, 90.0)
    b.positions[o.symbol] = 65
    s = Summary(cfg, b, cache_sec=0)
    p = s.build()
    assert len(p["trades"]) == 2 and p["trades"][0]["date"] == "2026-10-27"  # newest first
    assert p["open_trade"]["symbol"] == o.symbol and p["open_trade"]["ltp"] == 90.0 and p["open_trade"]["unrealized"] == 650.0
    assert p["broker"]["paper"] is True and p["broker"]["positions"][0]["symbol"] == o.symbol
    assert p["log"] == ["line1", "line2"]
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
        assert js["open_trade"]["unrealized"] == 650.0
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
