"""In-memory broker that mimics OpenAlgo responses for tests."""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta

import pandas as pd

from agent.clock import IST


def make_candles(day, closes: list[float], start="09:15", step_min=5, rng=5.0, volume=0) -> pd.DataFrame:
    hh, mm = map(int, start.split(":"))
    t0 = datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)
    rows, idx = [], []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        hi = max(o, c) + rng
        lo = min(o, c) - rng
        rows.append({"open": o, "high": hi, "low": lo, "close": c, "volume": volume, "oi": 0})
        idx.append(t0 + timedelta(minutes=step_min * i))
        prev = c
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, name="timestamp"))


class FakeBroker:
    def __init__(self, candles: pd.DataFrame, lotsize: int = 65, analyzer: bool = True, ws: bool = True,
                 expiries=("27-OCT-26", "03-NOV-26")):
        self.candles = candles
        self.lotsize = lotsize
        self.analyzer = analyzer
        self.ws = ws
        self.expiry_list = list(expiries)
        self.ltp: dict[str, float] = {}
        self.positions: dict[str, int] = {}
        self.orders: dict[str, dict] = {}
        self.trades: list[dict] = []
        self._ids = itertools.count(1)
        self.callback = None
        self.reject_next = False
        self.hold_next = False  # next order stays 'open' until release()
        self.calls: list[str] = []
        self.feed_connected = False

    # ---- helpers for tests
    def set_ltp(self, symbol: str, price: float):
        self.ltp[symbol] = price

    def push(self, symbol: str, exchange: str, price: float):
        self.ltp[symbol] = price
        if self.callback:
            self.callback({"type": "market_data", "symbol": symbol, "exchange": exchange, "mode": 1, "data": {"ltp": price}})

    def release(self):
        for o in self.orders.values():
            if o["order_status"] == "open":
                self._fill(o)

    def _fill(self, o):
        o["order_status"] = "complete"
        o["average_price"] = self.ltp.get(o["symbol"], 0.0)
        sign = 1 if o["action"] == "BUY" else -1
        self.positions[o["symbol"]] = self.positions.get(o["symbol"], 0) + sign * o["quantity"]
        self.trades.append({"symbol": o["symbol"], "action": o["action"], "quantity": o["quantity"],
                            "average_price": o["average_price"], "orderid": o["orderid"]})

    # ---- Broker interface
    def ping(self):
        return {"message": "pong", "broker": "fake"}

    def analyzer_mode(self):
        return self.analyzer

    def funds(self):
        return {"availablecash": 1_000_000.0}

    def quotes(self, symbol, exchange):
        self.calls.append(f"quotes:{symbol}")
        return {"ltp": self.ltp.get(symbol, 0.0)}

    def history(self, symbol, exchange, interval, start, end):
        self.calls.append("history")
        df = self.candles
        d = df.index.date
        return df[(d >= pd.Timestamp(start).date()) & (d <= pd.Timestamp(end).date())]

    def expiries(self, symbol, exchange):
        return list(self.expiry_list)

    def option_symbol(self, underlying, exchange, expiry_date, offset, option_type):
        u = self.ltp.get(underlying, 25000.0)
        strike = int(round(u / 50.0) * 50)
        return {"status": "success", "symbol": f"{underlying}{expiry_date}{strike}{option_type}", "exchange": "NFO",
                "lotsize": self.lotsize, "tick_size": 0.05, "underlying_ltp": u}

    def symbol_info(self, symbol, exchange):
        return {"symbol": symbol, "lotsize": self.lotsize}

    def place_order(self, symbol, exchange, action, quantity, product, pricetype="MARKET", price=0, trigger_price=0):
        from agent.broker import OrderRejected

        self.calls.append(f"order:{action}:{symbol}:{quantity}")
        oid = f"SB-{next(self._ids):06d}"
        if getattr(self, "timeout_next", False):
            # the request reached OpenAlgo and filled, but the client saw a timeout
            self.timeout_next = False
            o = {"orderid": oid, "symbol": symbol, "action": action, "quantity": int(quantity),
                 "order_status": "open", "average_price": 0.0}
            self.orders[oid] = o
            self._fill(o)
            from agent.broker import BrokerError
            raise BrokerError("Request timed out", transient=True)
        if self.reject_next:
            self.reject_next = False
            self.orders[oid] = {"orderid": oid, "symbol": symbol, "action": action, "quantity": int(quantity),
                                "order_status": "rejected", "average_price": 0.0}
            return oid
        o = {"orderid": oid, "symbol": symbol, "action": action, "quantity": int(quantity),
             "order_status": "open", "average_price": 0.0}
        self.orders[oid] = o
        if self.hold_next:
            self.hold_next = False
        else:
            self._fill(o)
        return oid

    def order_status(self, orderid):
        o = self.orders[orderid]
        return {"orderid": orderid, "order_status": o["order_status"], "average_price": o["average_price"],
                "quantity": o["quantity"], "symbol": o["symbol"], "raw": o}

    def cancel_order(self, orderid):
        o = self.orders[orderid]
        if o["order_status"] == "open":
            o["order_status"] = "cancelled"

    def open_position(self, symbol, exchange, product):
        return self.positions.get(symbol, 0)

    def positionbook(self):
        return [{"symbol": s, "exchange": "NFO", "product": "MIS", "quantity": str(q), "average_price": "0", "ltp": "0", "pnl": "0"}
                for s, q in self.positions.items()]

    def tradebook(self):
        return list(self.trades)

    def close_all_positions(self):
        for s, q in list(self.positions.items()):
            if q:
                self.place_order(s, "NFO", "SELL" if q > 0 else "BUY", abs(q), "MIS")
        return "All Open Positions Squared Off"

    def holidays(self, year):
        return set()

    def connect_feed(self):
        self.feed_connected = self.ws
        return self.ws

    def subscribe_ltp(self, instruments, callback):
        self.callback = callback
        return True

    def unsubscribe_ltp(self, instruments):
        pass

    def disconnect_feed(self):
        self.feed_connected = False

    def telegram(self, username, message):
        pass
