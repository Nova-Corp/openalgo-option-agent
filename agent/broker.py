"""Thin, strict wrapper over the OpenAlgo Python SDK.

Every call returns plain Python values or raises BrokerError. Read calls retry
on transient errors; order calls retry ONLY on rate-limit (429), never on a
timeout, because a timed-out order may already have reached the broker.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Protocol

import pandas as pd

from .config import OpenAlgoCfg

log = logging.getLogger("agent.broker")

TRANSIENT_TYPES = {"timeout_error", "connection_error", "http_error"}
TRANSIENT_CODES = {429, 500, 502, 503, 504}


class BrokerError(Exception):
    def __init__(self, message: str, payload: Any = None, transient: bool = False):
        super().__init__(message)
        self.payload = payload
        self.transient = transient


class OrderRejected(BrokerError):
    pass


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


class Broker(Protocol):
    """Interface the engine depends on. tests/fake_broker.py implements it too."""

    def ping(self) -> dict: ...
    def analyzer_mode(self) -> bool: ...
    def funds(self) -> dict: ...
    def quotes(self, symbol: str, exchange: str) -> dict: ...
    def history(self, symbol: str, exchange: str, interval: str, start: str, end: str) -> pd.DataFrame: ...
    def expiries(self, symbol: str, exchange: str) -> list[str]: ...
    def option_symbol(self, underlying: str, exchange: str, expiry_date: str, offset: str, option_type: str) -> dict: ...
    def symbol_info(self, symbol: str, exchange: str) -> dict: ...
    def search(self, query: str, exchange: str) -> list[dict]: ...
    def place_order(self, symbol: str, exchange: str, action: str, quantity: int, product: str,
                    pricetype: str = "MARKET", price: float = 0, trigger_price: float = 0) -> str: ...
    def order_status(self, orderid: str) -> dict: ...
    def cancel_order(self, orderid: str) -> None: ...
    def open_position(self, symbol: str, exchange: str, product: str) -> int: ...
    def positionbook(self) -> list[dict]: ...
    def tradebook(self) -> list[dict]: ...
    def close_all_positions(self) -> str: ...
    def holidays(self, year: int) -> set[str]: ...
    def connect_feed(self) -> bool: ...
    def subscribe_ltp(self, instruments: list[dict], callback: Callable[[dict], None]) -> bool: ...
    def unsubscribe_ltp(self, instruments: list[dict]) -> None: ...
    def disconnect_feed(self) -> None: ...
    def telegram(self, username: str, message: str) -> None: ...


class OpenAlgoBroker:
    def __init__(self, cfg: OpenAlgoCfg, strategy_tag: str):
        from openalgo import api  # imported lazily so tests do not need the SDK

        if not cfg.api_key:
            raise BrokerError("OPENALGO_API_KEY is empty. Copy .env.example to .env and paste your key.")
        self.cfg = cfg
        self.tag = strategy_tag
        self.api = api(api_key=cfg.api_key, host=cfg.host, ws_port=cfg.ws_port,
                       timeout=cfg.timeout_sec, verbose=False, auto_reconnect=True)
        self._feed_connected = False

    # ------------------------------------------------------------------ core
    def _call(self, fn: Callable, *, retries: int = 2, order: bool = False, **kw) -> Any:
        delay = 0.5
        attempt = 0
        while True:
            attempt += 1
            res = fn(**kw)
            if isinstance(res, pd.DataFrame):
                return res
            if isinstance(res, dict) and res.get("status") == "success":
                return res
            msg = (res or {}).get("message", "unknown error") if isinstance(res, dict) else str(res)
            code = (res or {}).get("code") if isinstance(res, dict) else None
            etype = (res or {}).get("error_type") if isinstance(res, dict) else None
            m = re.search(r"HTTP (\d{3})", str(msg))
            http_code = code or (int(m.group(1)) if m else None)
            rate_limited = http_code == 429 or "rate limit" in str(msg).lower()
            if http_code and 400 <= http_code < 500 and not rate_limited:
                transient = False  # bad key, validation error, not found: retrying will not help
            else:
                transient = rate_limited or etype in TRANSIENT_TYPES or http_code in TRANSIENT_CODES
            # Orders: retry only when we KNOW the request was not accepted.
            retry_ok = rate_limited if order else transient
            if retry_ok and attempt <= retries:
                log.warning("transient error from %s (attempt %d): %s", getattr(fn, "__name__", fn), attempt, msg)
                time.sleep(delay)
                delay = min(delay * 2, 5)
                continue
            raise BrokerError(f"{getattr(fn, '__name__', 'call')}: {msg}", payload=res, transient=transient)

    # --------------------------------------------------------------- account
    def reset_connection(self) -> None:
        """Drop the SDK's pooled HTTP connection so the next call opens a fresh TCP socket.

        On macOS the AirPlay Receiver also listens on port 5000 (on every address). If an agent
        starts a few seconds before OpenAlgo has bound 127.0.0.1:5000, its first connection lands
        on AirPlay, which answers HTTP 403 with an empty body, and the SDK's keep-alive pool would
        reuse that same socket forever. Closing the client is the only way out.
        """
        import httpx

        old = getattr(self.api, "client", None)
        try:
            if old is not None:
                old.close()
        except Exception:  # pragma: no cover - best effort
            pass
        self.api.client = httpx.Client(
            timeout=self.cfg.timeout_sec,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=120.0),
        )

    @staticmethod
    def _looks_like_wrong_server(res: Any) -> bool:
        """HTTP 403 with an empty body is not OpenAlgo; OpenAlgo always answers JSON."""
        if not isinstance(res, dict):
            return False
        # The SDK only sets raw_response when the body was not JSON; OpenAlgo's own 403s
        # (e.g. "Invalid openalgo apikey" before login) are JSON and must not be mislabelled.
        return res.get("code") == 403 and "raw_response" in res and not str(res["raw_response"]).strip()

    def ping(self) -> dict:
        # The SDK has no ping() method; use the REST endpoint directly.
        res = self.api._post("ping", {"apikey": self.cfg.api_key})
        if not isinstance(res, dict) or res.get("status") != "success":
            # Whatever went wrong, do not keep the socket: a stale keep-alive connection to the
            # wrong listener (or to a restarted OpenAlgo) would make every retry fail the same way.
            self.reset_connection()
            msg = (res or {}).get("message") if isinstance(res, dict) else res
            if self._looks_like_wrong_server(res):
                msg = (f"{msg} - port 5000 answered by another program (macOS AirPlay Receiver?), "
                       f"OpenAlgo is not listening yet; retrying on a fresh connection")
            raise BrokerError(f"ping: {msg}", payload=res, transient=True)
        return res.get("data", {})

    def analyzer_mode(self) -> bool:
        res = self._call(self.api.analyzerstatus)
        return bool(res["data"]["analyze_mode"])

    def funds(self) -> dict:
        d = self._call(self.api.funds)["data"]
        return {k: _f(v) for k, v in d.items()}

    # ------------------------------------------------------------------ data
    def quotes(self, symbol: str, exchange: str) -> dict:
        d = self._call(self.api.quotes, symbol=symbol, exchange=exchange)["data"]
        return {k: _f(v) for k, v in d.items()}

    def history(self, symbol: str, exchange: str, interval: str, start: str, end: str) -> pd.DataFrame:
        res = self._call(self.api.history, retries=3, symbol=symbol, exchange=exchange,
                         interval=interval, start_date=start, end_date=end)
        if not isinstance(res, pd.DataFrame):
            raise BrokerError(f"history returned no DataFrame: {res}")
        return res

    def expiries(self, symbol: str, exchange: str) -> list[str]:
        return list(self._call(self.api.expiry, symbol=symbol, exchange=exchange, instrumenttype="options")["data"])

    def option_symbol(self, underlying: str, exchange: str, expiry_date: str, offset: str, option_type: str) -> dict:
        return self._call(self.api.optionsymbol, underlying=underlying, exchange=exchange,
                          expiry_date=expiry_date, offset=offset, option_type=option_type)

    def symbol_info(self, symbol: str, exchange: str) -> dict:
        return self._call(self.api.symbol, symbol=symbol, exchange=exchange)["data"]

    def search(self, query: str, exchange: str) -> list[dict]:
        return list(self._call(self.api.search, query=query, exchange=exchange).get("data") or [])

    def holidays(self, year: int) -> set[str]:
        try:
            res = self._call(self.api.holidays, year=year)
        except BrokerError as e:
            log.warning("holidays unavailable (%s); falling back to weekday check", e)
            return set()
        out = set()
        for h in res.get("data", []):
            closed = h.get("closed_exchanges") or []
            if not closed or "NFO" in closed or "NSE" in closed:
                out.add(h["date"])
        return out

    # ---------------------------------------------------------------- orders
    def place_order(self, symbol: str, exchange: str, action: str, quantity: int, product: str,
                    pricetype: str = "MARKET", price: float = 0, trigger_price: float = 0) -> str:
        kw = dict(strategy=self.tag, symbol=symbol, exchange=exchange, action=action,
                  price_type=pricetype, product=product, quantity=int(quantity))
        if pricetype in ("LIMIT", "SL"):
            kw["price"] = price
        if pricetype in ("SL", "SL-M"):
            kw["trigger_price"] = trigger_price
        try:
            res = self._call(self.api.placeorder, order=True, retries=2, **kw)
        except BrokerError as e:
            if e.transient:
                raise
            raise OrderRejected(str(e), payload=e.payload) from e
        oid = res.get("orderid")
        if not oid:
            raise OrderRejected(f"placeorder returned no orderid: {res}", payload=res)
        return str(oid)

    def order_status(self, orderid: str) -> dict:
        d = self._call(self.api.orderstatus, order_id=orderid, strategy=self.tag)["data"]
        return {
            "orderid": str(d.get("orderid", orderid)),
            "order_status": str(d.get("order_status", "")).lower(),
            "average_price": _f(d.get("average_price")),
            "quantity": _i(d.get("quantity")),
            "symbol": d.get("symbol"),
            "raw": d,
        }

    def cancel_order(self, orderid: str) -> None:
        self._call(self.api.cancelorder, order=True, retries=2, order_id=orderid, strategy=self.tag)

    def open_position(self, symbol: str, exchange: str, product: str) -> int:
        res = self._call(self.api.openposition, strategy=self.tag, symbol=symbol, exchange=exchange, product=product)
        return _i(res.get("quantity"))

    def positionbook(self) -> list[dict]:
        return list(self._call(self.api.positionbook).get("data") or [])

    def tradebook(self) -> list[dict]:
        return list(self._call(self.api.tradebook).get("data") or [])

    def close_all_positions(self) -> str:
        res = self._call(self.api.closeposition, order=True, retries=2, strategy=self.tag)
        return str(res.get("message", "ok"))

    # ------------------------------------------------------------------ feed
    def connect_feed(self) -> bool:
        try:
            self._feed_connected = bool(self.api.connect())
        except Exception as e:  # SDK raises on socket failures
            log.warning("websocket connect failed: %s", e)
            self._feed_connected = False
        return self._feed_connected

    def subscribe_ltp(self, instruments: list[dict], callback: Callable[[dict], None]) -> bool:
        if not self._feed_connected:
            return False
        try:
            return bool(self.api.subscribe_ltp(instruments, on_data_received=callback))
        except Exception as e:
            log.warning("subscribe_ltp failed: %s", e)
            return False

    def unsubscribe_ltp(self, instruments: list[dict]) -> None:
        if not self._feed_connected:
            return
        try:
            self.api.unsubscribe_ltp(instruments)
        except Exception as e:
            log.debug("unsubscribe_ltp: %s", e)

    def disconnect_feed(self) -> None:
        if self._feed_connected:
            try:
                self.api.disconnect()
            except Exception:
                pass
            self._feed_connected = False

    def telegram(self, username: str, message: str) -> None:
        try:
            self.api.telegram(username=username, message=message)
        except Exception as e:
            log.debug("telegram notify failed: %s", e)
