"""Local web dashboard: open position, trade history, filters, P&L, auto refresh.

Serves agent/dashboard.html at http://127.0.0.1:<port>/ and JSON at /api/summary.
Reads the journal files in state/ and asks OpenAlgo for mode, funds, LTP and positions.
"""
from __future__ import annotations

import copy
import csv
import json

import pandas as pd
import logging
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .broker import BrokerError
from .clock import IST
from .config import Config
from .data import add_indicators, merge_candles
from .journal import Trade

STATIC_DIR = Path(__file__).with_name("static")
IST_OFFSET = 19800  # chart library renders UTC; shift epochs so the axis shows IST


def _chart_time(ts) -> int:
    return int(ts.timestamp()) + IST_OFFSET

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
                    "indices": sorted(p.name for p in self.state_dir.iterdir() if p.is_dir()) or ["NIFTY"],
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


class ChartData:
    """Builds /api/chart payloads: a day's candles with indicators, ORB, trades, premium paths."""

    def __init__(self, cfg: Config, broker):
        self.cfg = cfg
        self.broker = broker
        self.state_dir = Path(cfg.project_dir) / cfg.state_dir
        self._lock = threading.Lock()
        self._candles: dict[tuple[str, str], tuple[float, list]] = {}
        self._source: dict[tuple[str, str], str] = {}

    def _tick_candles(self, index: str, day: date) -> "pd.DataFrame":
        """Candles the agent built from live ticks that day (state/<INDEX>/tickcandles-<date>.csv)."""
        path = self.state_dir / index / f"tickcandles-{day.isoformat()}.csv"
        cols = ["open", "high", "low", "close", "volume", "oi"]
        if not path.exists():
            return pd.DataFrame(columns=cols)
        rows, idx = [], []
        try:
            with path.open() as f:
                for r in csv.DictReader(f):
                    idx.append(datetime.fromisoformat(r["time"]).astimezone(IST))
                    rows.append({"open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
                                 "close": float(r["close"]), "volume": 0, "oi": 0})
        except (OSError, ValueError, KeyError) as e:
            log.debug("tick candles unreadable %s: %s", path, e)
            return pd.DataFrame(columns=cols)
        if not rows:
            return pd.DataFrame(columns=cols)
        return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, name="timestamp").tz_convert(IST))

    def _cfg_for(self, index: str) -> Config:
        c = copy.deepcopy(self.cfg)
        c.apply_underlying(index)
        return c

    def candles(self, index: str, day: date) -> list[dict]:
        key = (index, day.isoformat())
        today = datetime.now(IST).date()
        ttl = 60 if day >= today else 24 * 3600
        with self._lock:
            hit = self._candles.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
        c = self._cfg_for(index)
        start = (day - timedelta(days=c.strategy.history_days + 4)).isoformat()
        tdf = self._tick_candles(index, day)
        try:
            df = self.broker.history(c.signal_symbol, c.signal_exchange, c.strategy.candle_interval, start, day.isoformat())
            if df.index.tz is None:
                df.index = df.index.tz_localize(IST)
            else:
                df.index = df.index.tz_convert(IST)
        except Exception as e:
            if hit and hit[1]:
                log.warning("broker history failed for %s %s (%s); keeping the previous %d candles", index, day, e, len(hit[1]))
                with self._lock:
                    self._candles[key] = (time.monotonic() - ttl + 5, hit[1])
                return hit[1]
            if tdf.empty:
                raise
            log.warning("broker history failed for %s %s (%s); using %d tick-built candles", index, day, e, len(tdf))
            df = pd.DataFrame(columns=["open", "high", "low", "close", "volume", "oi"])
        df, used = merge_candles(df, tdf)
        self._source[key] = "broker" if not used else ("ticks" if used == len(df[df.index.date == day]) else "broker+ticks")
        df = add_indicators(df, c.strategy.ema_fast, c.strategy.ema_slow)
        rows = []
        for ts, r in df[df.index.date == day].iterrows():
            rows.append({"time": _chart_time(ts), "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
                         "close": float(r["close"]), "ema_fast": round(float(r["ema_fast"]), 2),
                         "ema_slow": round(float(r["ema_slow"]), 2), "vwap": round(float(r["vwap"]), 2)})
        with self._lock:
            if not rows and hit and hit[1]:
                # the broker hiccupped and returned nothing for a day we already have: keep the last good
                # candles and retry soon instead of showing a blank chart for the whole cache window
                log.warning("broker returned no candles for %s %s; keeping the previous %d", index, day, len(hit[1]))
                self._candles[key] = (time.monotonic() - ttl + 5, hit[1])
                return hit[1]
            if not rows:
                return rows  # nothing cached: do not cache the empty answer either
            self._candles[key] = (time.monotonic(), rows)
        return rows

    def _premium(self, index: str, t: dict, day: date) -> tuple[list[dict], str]:
        path = self.state_dir / index / f"ticks-{t['id']}.csv"
        if path.exists():
            out, last = [], None
            with path.open() as f:
                for row in csv.DictReader(f):
                    try:
                        tt = _chart_time(datetime.fromisoformat(row["time"]))
                        if tt == last:
                            continue
                        out.append({"time": tt, "value": float(row["ltp"])})
                        last = tt
                    except (ValueError, KeyError):
                        continue
            if out:
                return out, "ticks"
        try:  # fall back to the option's own 1-minute history (works while the contract is alive)
            df = self.broker.history(t["symbol"], t["exchange"], "1m", day.isoformat(), day.isoformat())
            if df.index.tz is None:
                df.index = df.index.tz_localize(IST)
            else:
                df.index = df.index.tz_convert(IST)
            df = df[df.index.date == day]
            return [{"time": _chart_time(ts), "value": float(r["close"])} for ts, r in df.iterrows()], "history"
        except Exception as e:
            log.debug("premium history unavailable for %s: %s", t["symbol"], e)
            return [], "none"

    def build(self, index: str, day: date) -> dict:
        c = self._cfg_for(index)
        candles = self.candles(index, day)
        orb = None
        if candles:
            secs = int(c.strategy.opening_range_minutes) * 60
            open_t = candles[0]["time"]
            inside = [x for x in candles if x["time"] < open_t + secs]
            if len(inside) * (60 * int(c.strategy.candle_interval[:-1])) >= secs:
                orb = {"high": max(x["high"] for x in inside), "low": min(x["low"] for x in inside),
                       "start": open_t, "end": open_t + secs}
        trades = []
        jpath = self.state_dir / index / f"{day.isoformat()}.json"
        if jpath.exists():
            try:
                for raw in json.loads(jpath.read_text()).get("trades", []):
                    t = Trade.from_dict(raw).to_dict()
                    if t["status"] == "FAILED":
                        continue
                    prem, src = self._premium(index, t, day)
                    t["premium"] = prem
                    t["premium_source"] = src
                    for h in t.get("stop_history") or []:
                        try:
                            h["time"] = _chart_time(datetime.fromisoformat(h["t"]))
                        except (ValueError, KeyError):
                            pass
                    for k in ("entry_time", "exit_time", "signal_time"):
                        try:
                            t[k + "_ts"] = _chart_time(datetime.fromisoformat(t[k])) if t.get(k) else None
                        except ValueError:
                            t[k + "_ts"] = None
                    trades.append(t)
            except (OSError, json.JSONDecodeError):
                pass
        source = self._source.get((index, day.isoformat()), "broker" if candles else "none")
        note = ""
        if not candles:
            note = ("market not open yet" if day == datetime.now(IST).date() and datetime.now(IST).hour < 9
                    else "the broker returned no candles for this day and the agent built none from ticks")
        return {"index": index, "date": day.isoformat(), "interval": c.strategy.candle_interval, "candles": candles,
                "candle_source": source, "note": note, "orb": orb, "trades": trades,
                "ema": [c.strategy.ema_fast, c.strategy.ema_slow]}


class Handler(BaseHTTPRequestHandler):
    chart: ChartData  # set by serve()
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
        elif path.startswith("/static/"):
            f = (STATIC_DIR / path[len("/static/"):]).resolve()
            if f.is_file() and STATIC_DIR.resolve() in f.parents:
                ctype = "application/javascript" if f.suffix == ".js" else "text/plain"
                self._send(200, f.read_bytes(), ctype)
            else:
                self._send(404, b"not found", "text/plain")
        elif path == "/api/chart":
            from urllib.parse import parse_qs

            q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            index = (q.get("index") or ["NIFTY"])[0].upper()
            try:
                day = date.fromisoformat((q.get("date") or [datetime.now(IST).date().isoformat()])[0])
                body = json.dumps(self.chart.build(index, day), default=str).encode()
                self._send(200, body, "application/json")
            except Exception as e:
                log.exception("chart failed")
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
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
    Handler.chart = ChartData(cfg, broker)
    srv = ThreadingHTTPServer((host, port), Handler)
    log.info("dashboard at http://%s:%d/  (Ctrl+C to stop)", host, port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
