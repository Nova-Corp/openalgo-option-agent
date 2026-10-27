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


def _pid_alive(pid: int) -> bool:
    import os

    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
HTML_PATH = Path(__file__).with_name("dashboard.html")


def load_all_trades(state_dir: Path) -> list[dict]:
    """Every trade from state/<INDEX>/<date>.json (and legacy state/<date>.json), newest day first."""
    out: list[dict] = []
    files = list(state_dir.glob("*/????-??-??.json")) + list(state_dir.glob("????-??-??.json"))
    for p in sorted(files, key=lambda x: x.name, reverse=True):
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
        self.active = list(cfg.active_underlyings)

    def _broker_block(self, open_trades: list[dict]) -> dict:
        b: dict = {"ok": True, "error": "", "open_ltp": {}}
        try:
            b["paper"] = self.broker.analyzer_mode()
            b["funds"] = self.broker.funds()
            b["positions"] = [p for p in self.broker.positionbook() if int(float(p.get("quantity", 0) or 0)) != 0]
            for t in open_trades:
                q = self.broker.quotes(t["symbol"], t["exchange"])
                b["open_ltp"][t["symbol"]] = float(q.get("ltp") or 0)
        except BrokerError as e:
            b["ok"] = False
            b["error"] = str(e)
        except Exception as e:  # keep the page alive whatever the SDK throws
            b["ok"] = False
            b["error"] = f"{type(e).__name__}: {e}"
        return b

    def _log_tail(self, n: int = 30) -> list[str]:
        """Last n lines across logs/agent-*.log (one file per index), merged by timestamp."""
        lines: list[str] = []
        files = sorted(f for f in self.log_path.parent.glob("agent-*.log") if not f.name.endswith(".out.log"))
        if self.log_path.exists():
            files.append(self.log_path)
        for path in files:
            tag = path.stem.replace("agent-", "")
            try:
                with path.open("rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 20000))
                    chunk = f.read().decode("utf-8", "replace").splitlines()
            except OSError:
                continue
            for l in chunk[-n:]:
                if "agent.dashboard" in l or len(l) < 20 or not l[:4].isdigit():
                    continue
                lines.append(f"{l[:23]} [{tag}]{l[23:]}" if tag != "agent" else l)
        lines.sort(key=lambda l: l[:23])
        return lines[-n:]

    def build(self) -> dict:
        with self._lock:
            now = time.monotonic()
            if self._cache and now - self._cache[0] < self.cache_sec:
                return self._cache[1]
            trades = load_all_trades(self.state_dir)
            open_trades = [dict(t) for t in trades if t["status"] in ("ENTERING", "OPEN", "EXITING")]
            brk = self._broker_block(open_trades)
            for t in open_trades:
                ltp = brk.get("open_ltp", {}).get(t["symbol"])
                if ltp:
                    t["ltp"] = ltp
                    t["unrealized"] = round((ltp - t["entry_price"]) * t["qty"], 2)
            for t in trades:  # older records have no underlying: derive it from the symbol
                if not t.get("underlying"):
                    sym = t.get("symbol", "")
                    t["underlying"] = next((u for u in ("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTY", "SENSEX", "BANKEX") if sym.startswith(u)), "")
            running = []
            for rp in sorted(self.state_dir.glob("*/running.json")):
                try:
                    r = json.loads(rp.read_text())
                    r["alive"] = _pid_alive(int(r.get("pid", 0)))
                    running.append(r)
                except (OSError, json.JSONDecodeError, ValueError):
                    continue
            today = datetime.now(IST).date().isoformat()
            payload = {
                "generated": datetime.now(IST).isoformat(timespec="seconds"),
                "today": today,
                "config": {
                    "underlying": self.cfg.instrument.underlying,
                    "capital": self.cfg.risk.capital,
                    "allow_live": self.cfg.execution.allow_live,
                    "active_underlyings": self.active,
                    "square_off": self.cfg.risk.square_off_time,
                    "max_daily_loss": self.cfg.risk.capital * self.cfg.risk.max_daily_loss_pct / 100,
                },
                "broker": brk,
                "running": running,
                "open_trades": open_trades,
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
