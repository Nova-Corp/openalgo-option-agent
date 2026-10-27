"""Local web dashboard: open position, trade history, filters, P&L, auto refresh.

Serves agent/dashboard.html at http://127.0.0.1:<port>/ and JSON at /api/summary.
Reads the journal files in state/ and asks OpenAlgo for mode, funds, LTP and positions.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .broker import BrokerError
from .clock import IST
from .config import Config
from .journal import Trade

log = logging.getLogger("agent.dashboard")
HTML_PATH = Path(__file__).with_name("dashboard.html")


def load_all_trades(state_dir: Path) -> list[dict]:
    """Every trade from every state/<date>.json, newest day first."""
    out: list[dict] = []
    for p in sorted(state_dir.glob("????-??-??.json"), reverse=True):
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for t in data.get("trades", []):
            tr = Trade.from_dict(t)
            d = tr.to_dict()
            d["day_mode"] = data.get("mode", "")
            out.append(d)
    return out


class Summary:
    """Builds the /api/summary payload; caches broker calls briefly to respect rate limits."""

    def __init__(self, cfg: Config, broker, cache_sec: float = 2.0):
        self.cfg = cfg
        self.broker = broker
        self.cache_sec = cache_sec
        self._lock = threading.Lock()
        self._cache: tuple[float, dict] | None = None
        self.state_dir = Path(cfg.project_dir) / cfg.state_dir
        self.log_path = Path(cfg.project_dir) / cfg.log_dir / "agent.log"

    def _broker_block(self, open_trade: dict | None) -> dict:
        b: dict = {"ok": True, "error": ""}
        try:
            b["paper"] = self.broker.analyzer_mode()
            b["funds"] = self.broker.funds()
            b["positions"] = [p for p in self.broker.positionbook() if int(float(p.get("quantity", 0) or 0)) != 0]
            if open_trade:
                q = self.broker.quotes(open_trade["symbol"], open_trade["exchange"])
                b["open_ltp"] = float(q.get("ltp") or 0)
        except BrokerError as e:
            b["ok"] = False
            b["error"] = str(e)
        except Exception as e:  # keep the page alive whatever the SDK throws
            b["ok"] = False
            b["error"] = f"{type(e).__name__}: {e}"
        return b

    def _log_tail(self, n: int = 25) -> list[str]:
        try:
            with self.log_path.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 20000))
                lines = f.read().decode("utf-8", "replace").splitlines()
            return [l for l in lines if "agent.dashboard" not in l][-n:]
        except OSError:
            return []

    def build(self) -> dict:
        with self._lock:
            now = time.monotonic()
            if self._cache and now - self._cache[0] < self.cache_sec:
                return self._cache[1]
            trades = load_all_trades(self.state_dir)
            open_trade = next((t for t in trades if t["status"] in ("ENTERING", "OPEN", "EXITING")), None)
            brk = self._broker_block(open_trade)
            if open_trade and brk.get("open_ltp"):
                ltp = brk["open_ltp"]
                open_trade = dict(open_trade)
                open_trade["ltp"] = ltp
                open_trade["unrealized"] = round((ltp - open_trade["entry_price"]) * open_trade["qty"], 2)
            today = datetime.now(IST).date().isoformat()
            payload = {
                "generated": datetime.now(IST).isoformat(timespec="seconds"),
                "today": today,
                "config": {
                    "underlying": self.cfg.instrument.underlying,
                    "capital": self.cfg.risk.capital,
                    "allow_live": self.cfg.execution.allow_live,
                    "square_off": self.cfg.risk.square_off_time,
                    "max_daily_loss": self.cfg.risk.capital * self.cfg.risk.max_daily_loss_pct / 100,
                },
                "broker": brk,
                "open_trade": open_trade,
                "trades": trades,
                "log": self._log_tail(),
            }
            self._cache = (now, payload)
            return payload


class Handler(BaseHTTPRequestHandler):
    summary: Summary  # set by serve()

    def log_message(self, fmt, *args):  # the page polls every few seconds; do not fill the log with it
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/summary":
            try:
                body = json.dumps(self.summary.build(), default=str).encode()
                self._send(200, body, "application/json")
            except Exception as e:
                log.exception("summary failed")
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")


def serve(cfg: Config, broker, host: str = "127.0.0.1", port: int = 8787) -> None:
    Handler.summary = Summary(cfg, broker)
    srv = ThreadingHTTPServer((host, port), Handler)
    log.info("dashboard at http://%s:%d/  (Ctrl+C to stop)", host, port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
