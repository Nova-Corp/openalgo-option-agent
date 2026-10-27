"""Order placement with fill confirmation and position verification."""
from __future__ import annotations

import logging
from datetime import datetime

from .broker import BrokerError, OrderRejected
from .clock import Clock
from .config import ExecutionCfg
from .instruments import OptionContract
from .journal import Trade

log = logging.getLogger("agent.exec")

FILLED = {"complete", "completed", "filled", "executed"}
DEAD = {"rejected", "cancelled", "canceled", "expired"}


class Executor:
    def __init__(self, broker, cfg: ExecutionCfg, clock: Clock):
        self.broker = broker
        self.cfg = cfg
        self.clock = clock

    # ---------------------------------------------------------- helpers
    def _wait_fill(self, orderid: str) -> tuple[str, float]:
        """Poll orderstatus until complete/dead or timeout. Returns (status, avg_price)."""
        deadline = self.clock.now().timestamp() + self.cfg.fill_timeout_sec
        last = {"order_status": "unknown", "average_price": 0.0}
        while True:
            try:
                last = self.broker.order_status(orderid)
            except BrokerError as e:
                log.warning("orderstatus %s: %s", orderid, e)
            st = last.get("order_status", "")
            if st in FILLED and last.get("average_price", 0) > 0:
                return st, float(last["average_price"])
            if st in FILLED:  # filled but avg not reported yet; poll once more
                pass
            if st in DEAD:
                return st, 0.0
            if self.clock.now().timestamp() >= deadline:
                return st or "timeout", float(last.get("average_price") or 0.0)
            self.clock.sleep(self.cfg.poll_interval_sec)

    def _fill_price_from_tradebook(self, symbol: str, action: str) -> float:
        try:
            rows = [r for r in self.broker.tradebook() if r.get("symbol") == symbol and str(r.get("action", "")).upper() == action]
            if rows:
                return float(rows[-1].get("average_price") or 0)
        except BrokerError as e:
            log.warning("tradebook lookup failed: %s", e)
        return 0.0

    # ------------------------------------------------------------ entry
    def enter(self, trade: Trade, contract: OptionContract, fallback_price: float) -> Trade:
        """Places a BUY market order and confirms the fill. Raises OrderRejected on failure."""
        oid = self.broker.place_order(contract.symbol, contract.exchange, "BUY", trade.qty, trade.product)
        trade.entry_orderid = oid
        log.info("entry order %s placed: BUY %d %s", oid, trade.qty, contract.symbol)
        st, avg = self._wait_fill(oid)
        if st in DEAD:
            trade.status = "FAILED"
            raise OrderRejected(f"entry order {oid} {st}")
        if st not in FILLED:
            # Unknown: check the position before deciding anything.
            qty = self.broker.open_position(contract.symbol, contract.exchange, trade.product)
            if qty <= 0:
                try:
                    self.broker.cancel_order(oid)
                except BrokerError as e:
                    log.warning("cancel after timeout failed: %s", e)
                qty = self.broker.open_position(contract.symbol, contract.exchange, trade.product)
                if qty <= 0:
                    trade.status = "FAILED"
                    raise OrderRejected(f"entry order {oid} not filled within {self.cfg.fill_timeout_sec}s (status {st})")
            log.warning("entry %s status %s but position shows %d; treating as filled", oid, st, qty)
            if qty != trade.qty:
                log.warning("position qty %d differs from ordered %d; using position qty", qty, trade.qty)
                trade.qty = qty
        if avg <= 0:
            avg = self._fill_price_from_tradebook(contract.symbol, "BUY") or fallback_price
        trade.entry_price = float(avg)
        trade.entry_time = self.clock.now().isoformat(timespec="seconds")
        trade.status = "OPEN"
        log.info("entry filled: %s x%d @ %.2f", contract.symbol, trade.qty, trade.entry_price)
        return trade

    # ------------------------------------------------------------- exit
    def exit(self, trade: Trade, reason: str, fallback_price: float) -> Trade:
        """Sells the whole position with retries and verifies the position is flat."""
        trade.status = "EXITING"
        trade.exit_reason = reason
        last_err: Exception | None = None
        for attempt in range(1, self.cfg.exit_retries + 1):
            try:
                qty = self.broker.open_position(trade.symbol, trade.exchange, trade.product)
            except BrokerError as e:
                log.warning("openposition failed (%s); assuming %d", e, trade.qty)
                qty = trade.qty
            if qty <= 0:
                if attempt == 1:
                    log.warning("no open position for %s at exit; marking closed (external exit?)", trade.symbol)
                    price = self._fill_price_from_tradebook(trade.symbol, "SELL") or fallback_price
                    return self._close(trade, "", price, reason + " (position already flat)")
                price = self._fill_price_from_tradebook(trade.symbol, "SELL") or fallback_price
                return self._close(trade, trade.exit_orderid, price, reason)
            try:
                oid = self.broker.place_order(trade.symbol, trade.exchange, "SELL", qty, trade.product)
            except BrokerError as e:
                last_err = e
                log.error("exit order attempt %d failed: %s", attempt, e)
                self.clock.sleep(1.0)
                continue
            trade.exit_orderid = oid
            log.info("exit order %s placed: SELL %d %s (%s)", oid, qty, trade.symbol, reason)
            st, avg = self._wait_fill(oid)
            if st in FILLED:
                price = avg or self._fill_price_from_tradebook(trade.symbol, "SELL") or fallback_price
                return self._close(trade, oid, price, reason)
            if st in DEAD:
                log.error("exit order %s %s; retrying", oid, st)
                continue
            # Timeout: never leave an unconfirmed order alive while sending another one.
            log.warning("exit order %s unconfirmed (%s); cancelling before re-checking position", oid, st)
            try:
                self.broker.cancel_order(oid)
            except BrokerError as e:
                log.warning("cancel %s failed (%s); it may have filled", oid, e)
            try:
                st2 = self.broker.order_status(oid)
                if st2.get("order_status") in FILLED:
                    price = st2.get("average_price") or self._fill_price_from_tradebook(trade.symbol, "SELL") or fallback_price
                    return self._close(trade, oid, price, reason)
            except BrokerError as e:
                log.warning("orderstatus after cancel failed: %s", e)
        raise BrokerError(f"could not exit {trade.symbol} after {self.cfg.exit_retries} attempts: {last_err}")

    def _close(self, trade: Trade, oid: str, price: float, reason: str) -> Trade:
        trade.exit_orderid = oid
        trade.exit_price = float(price)
        trade.exit_time = self.clock.now().isoformat(timespec="seconds")
        trade.exit_reason = reason
        trade.pnl = round((trade.exit_price - trade.entry_price) * trade.qty, 2)
        trade.status = "CLOSED"
        log.info("exit filled: %s x%d @ %.2f pnl=%.2f (%s)", trade.symbol, trade.qty, trade.exit_price, trade.pnl, reason)
        return trade

    def panic_close_all(self) -> str:
        return self.broker.close_all_positions()
