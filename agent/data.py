"""Candles with indicators, and a live LTP feed with REST fallback."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .clock import IST, Clock, interval_seconds

log = logging.getLogger("agent.data")


def add_indicators(df: pd.DataFrame, fast: int, slow: int) -> pd.DataFrame:
    """Adds ema_fast, ema_slow and per-day vwap columns. Index must be tz-aware IST."""
    df = df.copy()
    for c in ("open", "high", "low", "close", "volume"):
        if c not in df.columns:
            df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ema_fast"] = df["close"].ewm(span=fast, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=slow, adjust=False).mean()
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    day = df.index.tz_convert(IST).date if df.index.tz is not None else df.index.date
    vol = df["volume"].fillna(0.0)
    pv = tp * vol
    cum_pv = pv.groupby(day).cumsum()
    cum_v = vol.groupby(day).cumsum()
    # Index symbols have no volume: fall back to the running mean of typical price.
    twap = tp.groupby(day).expanding().mean().reset_index(level=0, drop=True).reindex(df.index)
    vwap = np.where(cum_v > 0, cum_pv / cum_v.replace(0, np.nan), twap)
    df["vwap"] = vwap
    df["day"] = day
    return df


class CandleSource:
    """Fetches closed candles for the signal symbol via the history API."""

    def __init__(self, broker, symbol: str, exchange: str, interval: str, history_days: int, clock: Clock):
        self.broker = broker
        self.symbol = symbol
        self.exchange = exchange
        self.interval = interval
        self.secs = interval_seconds(interval)
        self.history_days = history_days
        self.clock = clock

    def fetch(self, now: datetime | None = None) -> pd.DataFrame:
        now = now or self.clock.now()
        start = (now - timedelta(days=self.history_days + 4)).date().isoformat()
        end = now.date().isoformat()
        df = self.broker.history(self.symbol, self.exchange, self.interval, start, end)
        if df is None or len(df) == 0:
            return pd.DataFrame()
        if df.index.tz is None:
            df.index = df.index.tz_localize(IST)
        else:
            df.index = df.index.tz_convert(IST)
        # Keep only CLOSED candles: a candle starting at t is closed once t + interval <= now.
        cutoff = now - timedelta(seconds=self.secs)
        df = df[df.index <= cutoff]
        return df


class LtpFeed:
    """Live LTPs over WebSocket with a REST quotes fallback when the socket is stale."""

    def __init__(self, broker, clock: Clock, stale_sec: float = 10.0, fallback_interval: float = 2.0):
        self.broker = broker
        self.clock = clock
        self.stale_sec = stale_sec
        self.fallback_interval = fallback_interval
        self._lock = threading.Lock()
        self._ltp: dict[str, tuple[float, float]] = {}  # key -> (ltp, monotonic ts)
        self._last_rest: dict[str, float] = {}
        self._subs: set[tuple[str, str]] = set()
        self.ws_ok = False
        self.rest_hits = 0

    @staticmethod
    def key(symbol: str, exchange: str) -> str:
        return f"{exchange}:{symbol}"

    def start(self) -> None:
        self.ws_ok = bool(self.broker.connect_feed())
        if not self.ws_ok:
            log.warning("WebSocket feed unavailable; using REST quotes polling every %.1fs", self.fallback_interval)

    def _on_tick(self, msg: dict) -> None:
        try:
            sym = msg.get("symbol")
            exch = msg.get("exchange")
            data = msg.get("data") or {}
            ltp = data.get("ltp", msg.get("ltp"))
            if sym and exch and ltp is not None:
                with self._lock:
                    self._ltp[self.key(sym, exch)] = (float(ltp), time.monotonic())
        except Exception as e:  # never let a bad tick kill the socket thread
            log.debug("bad tick %s: %s", msg, e)

    def subscribe(self, symbol: str, exchange: str) -> None:
        self._subs.add((symbol, exchange))
        if self.ws_ok:
            ok = self.broker.subscribe_ltp([{"symbol": symbol, "exchange": exchange}], self._on_tick)
            if not ok:
                log.warning("subscribe failed for %s:%s; REST fallback will be used", exchange, symbol)

    def unsubscribe(self, symbol: str, exchange: str) -> None:
        self._subs.discard((symbol, exchange))
        if self.ws_ok:
            self.broker.unsubscribe_ltp([{"symbol": symbol, "exchange": exchange}])

    def ltp(self, symbol: str, exchange: str) -> float | None:
        k = self.key(symbol, exchange)
        with self._lock:
            v = self._ltp.get(k)
        if v and (time.monotonic() - v[1]) <= self.stale_sec:
            return v[0]
        # stale or missing: REST fallback, rate limited
        last = self._last_rest.get(k, 0.0)
        if time.monotonic() - last < self.fallback_interval:
            return v[0] if v else None
        self._last_rest[k] = time.monotonic()
        try:
            q = self.broker.quotes(symbol, exchange)
            self.rest_hits += 1
            ltp = float(q.get("ltp") or 0)
            if ltp > 0:
                with self._lock:
                    self._ltp[k] = (ltp, time.monotonic())
                return ltp
        except Exception as e:
            log.warning("quotes fallback failed for %s: %s", k, e)
        return v[0] if v else None

    def stop(self) -> None:
        try:
            self.broker.disconnect_feed()
        except Exception:
            pass
