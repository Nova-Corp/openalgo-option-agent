"""The trading loop: a small state machine driven by closed candles and live LTPs.

States: FLAT -> (signal) -> ENTERING -> OPEN -> (stop/target/underlying/time) -> EXITING -> FLAT
Everything persisted in the daily journal so a restart resumes the open trade.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from .broker import BrokerError, OrderRejected
from .clock import MARKET_OPEN, Clock, at, parse_hhmm
from datetime import time as _time

SESSION_PREP = _time(9, 5)
from .config import Config
from .data import CandleSource, LtpFeed, add_indicators
from .execution import Executor
from .instruments import OptionContract, resolve_contract
from .journal import Journal, Trade
from .notify import Notifier
from .risk import RiskManager, StopManager, StopState
from .strategy import DayContext, OrbTrendStrategy, Signal

log = logging.getLogger("agent.engine")


class ModeNotAllowed(Exception):
    pass


class Engine:
    def __init__(self, cfg: Config, broker, clock: Clock | None = None):
        self.cfg = cfg
        self.broker = broker
        self.clock = clock or Clock()
        self.project = Path(cfg.project_dir)
        self.strategy = OrbTrendStrategy(cfg.strategy)
        self.risk = RiskManager(cfg.risk, cfg.execution)
        self.candles = CandleSource(broker, cfg.signal_symbol, cfg.signal_exchange,
                                    cfg.strategy.candle_interval, cfg.strategy.history_days, self.clock)
        self.feed = LtpFeed(broker, self.clock, cfg.execution.ws_stale_sec, cfg.execution.quote_fallback_interval_sec)
        self.executor = Executor(broker, cfg.execution, self.clock)
        self.notifier = Notifier(broker, cfg.notify.telegram_username)
        self.journal: Journal | None = None
        self.paper: bool = True
        self.square_off = parse_hhmm(cfg.risk.square_off_time)
        self.entry_end = parse_hhmm(cfg.strategy.entry_end)
        self._last_candle_id: int | None = None
        self._pending_candle_id: int | None = None
        self._last_mode_check = 0.0
        self._last_fetch_ts = 0.0
        self._holidays: dict[int, set[str]] = {}
        self._last_stop_persist: dict | None = None
        self._df = None
        self.stop_mgr: StopManager | None = None
        self.stopped_reason: str | None = None

    # ------------------------------------------------------------ mode
    def check_mode(self) -> bool:
        """Returns True when in paper (analyzer) mode. Raises ModeNotAllowed for live without the latch."""
        self.paper = self.broker.analyzer_mode()
        if not self.paper and not self.cfg.execution.allow_live:
            raise ModeNotAllowed("OpenAlgo analyzer mode is OFF (live trading) but execution.allow_live is false in config.yaml. "
                                 "Refusing to trade. Turn analyzer back on, or set allow_live: true deliberately.")
        return self.paper

    @property
    def mode_name(self) -> str:
        return "paper" if self.paper else "LIVE"

    # --------------------------------------------------------- preflight
    def preflight(self) -> dict:
        """Checks everything the run needs. Returns a dict of results (used by `check`)."""
        out: dict = {}
        out["ping"] = self.broker.ping()
        out["paper_mode"] = self.broker.analyzer_mode()
        out["allow_live"] = self.cfg.execution.allow_live
        out["funds"] = self.broker.funds()
        today = self.clock.now().date()
        for ot in ("CE", "PE"):
            c = resolve_contract(self.broker, self.cfg.instrument, ot, today)
            q = self.broker.quotes(c.symbol, c.exchange)
            out[f"contract_{ot}"] = {**c.to_dict(), "ltp": q.get("ltp")}
        df = self.candles.fetch()
        out["candles"] = {"symbol": self.cfg.signal_symbol, "exchange": self.cfg.signal_exchange,
                          "rows": int(len(df)), "last": str(df.index[-1]) if len(df) else None}
        out["holidays_loaded"] = len(self.broker.holidays(today.year))
        out["ws_connected"] = bool(self.broker.connect_feed())
        self.broker.disconnect_feed()
        return out

    # --------------------------------------------------------- control
    def _control_file(self, name: str) -> bool:
        return (self.project / name).exists()

    def _is_trading_day(self, d) -> bool:
        if d.weekday() >= 5:
            return False
        if d.year not in self._holidays:
            self._holidays[d.year] = self.broker.holidays(d.year)
        return d.isoformat() not in self._holidays[d.year]

    def next_session_start(self, now: datetime) -> datetime:
        """09:05 on the next trading day (today if the session has not ended yet)."""
        d = now.date()
        if now.time() >= self.square_off:
            d += timedelta(days=1)
        while not self._is_trading_day(d):
            d += timedelta(days=1)
        return at(d, SESSION_PREP)

    def wait_for_broker(self) -> bool:
        """Before the open: keep pinging until OpenAlgo has a broker session. Returns False on STOP."""
        warned = False
        while True:
            if self._control_file("STOP"):
                return False
            try:
                self.broker.ping()
                if warned:
                    log.info("broker session is back; continuing")
                return True
            except BrokerError as e:
                if not warned:
                    log.error("OpenAlgo not ready (%s). Log in to the broker in the OpenAlgo app. Retrying every 30s.", e)
                    self.notifier.send(f"agent waiting: OpenAlgo not ready ({e}). Log in to AliceBlue in OpenAlgo.")
                    warned = True
                self.clock.sleep(30)

    def run_forever(self) -> None:
        """Trade every session until a STOP file appears. Waits through nights, weekends and holidays."""
        while True:
            now = self.clock.now()
            if self._control_file("STOP"):
                log.warning("STOP file present; not starting another session. Delete it to resume.")
                return
            try:
                start = self.next_session_start(now)
            except BrokerError as e:
                log.warning("cannot determine next session (%s); retrying in 60s", e)
                self.clock.sleep(60)
                continue
            if now < start:
                wait = (start - now).total_seconds()
                log.info("next session %s; sleeping %.0f min", start.strftime("%a %d %b %H:%M"), wait / 60)
                while self.clock.now() < start:
                    if self._control_file("STOP"):
                        log.warning("STOP file present while waiting; exiting")
                        return
                    self.clock.sleep(min(60, max(1, (start - self.clock.now()).total_seconds())))
            if not self.wait_for_broker():
                return
            try:
                self.run()
            except ModeNotAllowed as e:
                log.error("%s", e)
                log.error("sleeping 5 min, then re-checking the mode")
                self.clock.sleep(300)
                continue
            except BrokerError as e:
                log.error("session aborted: %s; retrying in 60s", e)
                self.clock.sleep(60)
                continue
            if self.stopped_reason:
                return
            # session finished normally: make sure we do not re-enter today's loop
            self.clock.sleep(60)

    # --------------------------------------------------------- restart
    def _reconcile(self) -> None:
        """On start: resume or close out the journal's open trade; warn about unknown positions."""
        assert self.journal
        t = self.journal.open_trade()
        if t:
            qty = self.broker.open_position(t.symbol, t.exchange, t.product)
            if t.status == "ENTERING" and qty <= 0:
                t.status = "FAILED"
                self.journal.update(t)
                log.warning("journal trade %s was ENTERING with no position; marked FAILED", t.id)
            elif qty <= 0:
                price = self.executor._fill_price_from_tradebook(t.symbol, "SELL")
                self.executor._close(t, t.exit_orderid, price or t.entry_price, "closed outside the agent (restart reconcile)")
                self.journal.update(t)
                self.notifier.send(f"[{self.mode_name}] {t.symbol} was closed outside the agent; pnl={t.pnl:.2f}")
            else:
                if qty != t.qty:
                    log.warning("resuming %s with position qty %d (journal had %d)", t.symbol, qty, t.qty)
                    t.qty = qty
                if t.entry_price <= 0:  # entry outcome was unknown when we last saved
                    price = self.executor._fill_price_from_tradebook(t.symbol, "BUY")
                    if price <= 0:
                        price = float(self.broker.quotes(t.symbol, t.exchange).get("ltp") or 0)
                    t.entry_price = price
                    t.entry_time = t.entry_time or self.clock.now().isoformat(timespec="seconds")
                    log.warning("entry price for %s recovered as %.2f", t.symbol, price)
                t.status = "OPEN"
                self.journal.update(t)
                self.stop_mgr = StopManager(self.cfg.risk, StopState.from_dict(t.stop)) if t.stop else StopManager.new(self.cfg.risk, t.entry_price)
                self.feed.subscribe(t.symbol, t.exchange)
                log.warning("RESUMED open trade %s x%d @ %.2f stop=%.2f", t.symbol, t.qty, t.entry_price, self.stop_mgr.s.stop)
        # Positions we did not open
        try:
            ours = {t.symbol for t in self.journal.trades}
            for p in self.broker.positionbook():
                q = int(float(p.get("quantity", 0) or 0))
                if q != 0 and p.get("symbol") not in ours and str(p.get("symbol", "")).startswith(self.cfg.instrument.underlying):
                    log.warning("UNMANAGED position: %s qty=%d (not opened by this agent, left alone)", p.get("symbol"), q)
        except BrokerError as e:
            log.warning("positionbook check failed: %s", e)

    # ------------------------------------------------------------- run
    def run(self, max_iterations: int | None = None) -> None:
        now = self.clock.now()
        self.check_mode()
        log.info("=" * 50)
        log.info("MODE: %s  (analyzer %s)  underlying=%s  signal=%s:%s  interval=%s",
                 self.mode_name, "ON" if self.paper else "OFF", self.cfg.instrument.underlying,
                 self.cfg.signal_exchange, self.cfg.signal_symbol, self.cfg.strategy.candle_interval)
        if not self.paper:
            log.warning("*** LIVE MODE: real orders will be sent to the broker. Max lots capped at %d ***", self.cfg.execution.live_max_lots)
        log.info("=" * 50)
        self.journal = Journal(self.cfg.index_state_dir, now.date())
        self.journal.data["mode"] = self.mode_name
        self.journal.save()
        self.stopped_reason = None
        self._last_candle_id = None
        self._pending_candle_id = None
        if not self._is_trading_day(now.date()):
            log.info("%s is not a trading day. Exiting.", now.date())
            return
        self.feed.start()
        self.feed.subscribe(self.cfg.signal_symbol, self.cfg.signal_exchange)
        self._reconcile()
        self.notifier.send(f"[{self.mode_name}] agent started {now:%Y-%m-%d %H:%M}")
        iterations = 0
        try:
            while True:
                iterations += 1
                if max_iterations and iterations > max_iterations:
                    break
                now = self.clock.now()
                if now.time() < MARKET_OPEN:
                    self.clock.sleep(min(30, (at(now.date(), MARKET_OPEN) - now).total_seconds()))
                    continue
                if self._step(now):
                    break
                self.clock.sleep(self.cfg.execution.poll_interval_sec)
        finally:
            self.feed.stop()
            if self.journal:
                log.info("day summary: trades=%d realized_pnl=%.2f", self.journal.count(), self.journal.realized_pnl())

    def _step(self, now: datetime) -> bool:
        """One loop iteration. Returns True when the day is done."""
        assert self.journal
        trade = self.journal.open_trade()

        # kill switch / pause
        if self._control_file("STOP"):
            log.warning("STOP file found: squaring off and exiting")
            if trade:
                self._exit(trade, "STOP file")
            self.stopped_reason = "STOP file"
            return True
        paused = self._control_file("PAUSE")

        # square-off
        if now.time() >= self.square_off:
            if trade:
                self._exit(trade, f"square-off {self.cfg.risk.square_off_time}")
            log.info("square-off time reached; done for the day")
            return True

        # periodic mode check (analyzer toggled mid-session?)
        if now.timestamp() - self._last_mode_check >= 60:
            self._last_mode_check = now.timestamp()
            was_paper = self.paper
            try:
                self.check_mode()
            except ModeNotAllowed as e:
                log.error(str(e))
                if trade:
                    self._exit(trade, "mode switched to live without allow_live")
                self.stopped_reason = "mode not allowed"
                return True
            except BrokerError as e:
                log.warning("mode check failed: %s", e)
            if was_paper != self.paper:
                self.notifier.send(f"analyzer mode changed mid-session: now {self.mode_name}")

        # open trade: manage stop on every tick
        if trade and self.stop_mgr:
            ltp = self.feed.ltp(trade.symbol, trade.exchange)
            if ltp:
                reason = self.stop_mgr.update(ltp)
                self._persist_stop(trade)
                if reason:
                    self._exit(trade, reason, ltp)
                    return False

        # candle boundary -> refresh candles, evaluate
        if self._candle_ready(now):
            try:
                self._on_candle(now, paused)
            except BrokerError as e:
                log.warning("candle processing failed: %s", e)
        return False

    # ----------------------------------------------------------- candles
    def _candle_ready(self, now: datetime) -> bool:
        secs = self.candles.secs
        since_open = (now - at(now.date(), MARKET_OPEN)).total_seconds()
        if since_open < 0:
            return False
        cid = int(since_open // secs)
        if cid == self._last_candle_id:
            return False
        if since_open - cid * secs < self.cfg.execution.candle_delay_sec:
            return False
        # while waiting for a late candle, re-fetch at most every 5s
        if self._pending_candle_id == cid and now.timestamp() - self._last_fetch_ts < 5:
            return False
        self._pending_candle_id = cid
        return True

    def _on_candle(self, now: datetime, paused: bool) -> None:
        assert self.journal
        self._last_fetch_ts = now.timestamp()
        df = self.candles.fetch(now)
        if df.empty:
            log.warning("no candles returned for %s", self.cfg.signal_symbol)
            return
        expected_last = at(now.date(), MARKET_OPEN) + timedelta(seconds=(self._pending_candle_id - 1) * self.candles.secs)
        if df.index[-1] < expected_last:
            # broker has not published the just-closed candle yet; try again on the next tick
            lag = (now - expected_last).total_seconds() - self.candles.secs
            if lag > 90:
                log.warning("candle %s still missing after %.0fs; skipping it", expected_last.time(), lag)
                self._last_candle_id = self._pending_candle_id
            return
        self._last_candle_id = self._pending_candle_id
        self._df = add_indicators(df, self.cfg.strategy.ema_fast, self.cfg.strategy.ema_slow)
        last = self._df.iloc[-1]
        log.info("candle %s close=%.2f vwap=%.2f ema=%.2f/%.2f", self._df.index[-1].strftime("%H:%M"),
                 last["close"], last["vwap"], last["ema_fast"], last["ema_slow"])

        trade = self.journal.open_trade()
        if trade:
            entry_time = datetime.fromisoformat(trade.entry_time) if trade.entry_time else now
            why = self.strategy.underlying_exit(self._df, trade.option_type, entry_time)
            if why:
                self._exit(trade, why)
            return
        if paused:
            log.info("PAUSE file present: no new entries")
            return
        ctx = DayContext(self.journal.count(), self.journal.count("CE"), self.journal.count("PE"), self.journal.last_exit_time())
        sig, note = self.strategy.evaluate(self._df, now, ctx)
        if sig is None:
            log.info("no entry: %s", note)
            return
        self._enter(sig, now)

    # ------------------------------------------------------------- entry
    def _enter(self, sig: Signal, now: datetime) -> None:
        assert self.journal
        log.info("SIGNAL %s", sig.reason)
        try:
            contract = resolve_contract(self.broker, self.cfg.instrument, sig.option_type, now.date())
            q = self.broker.quotes(contract.symbol, contract.exchange)
        except BrokerError as e:
            log.error("could not resolve contract: %s", e)
            return
        premium = float(q.get("ltp") or 0)
        why = self.risk.can_enter(now, self.journal.realized_pnl(), premium)
        if why:
            log.info("entry blocked: %s", why)
            return
        lots = self.risk.lots_for(premium, contract.lotsize, live=not self.paper)
        if lots <= 0:
            log.info("entry blocked: premium %.2f too large for risk budget %.0f", premium, self.risk.risk_per_trade)
            return
        trade = Trade.new(date=now.date().isoformat(), mode=self.mode_name, symbol=contract.symbol,
                          exchange=contract.exchange, option_type=sig.option_type, lots=lots,
                          qty=lots * contract.lotsize, signal=sig.reason, product=self.cfg.instrument.product,
                          lotsize=contract.lotsize, underlying=self.cfg.instrument.underlying)
        self.journal.add(trade)
        log.info("ENTER %s %s lots=%d qty=%d premium~%.2f expiry=%s", self.mode_name, contract.symbol, lots, trade.qty, premium, contract.expiry)
        try:
            self.executor.enter(trade, contract, premium)
        except OrderRejected as e:
            trade.status = "FAILED"
            trade.exit_reason = str(e)
            self.journal.update(trade)
            log.error("entry failed: %s", e)
            self.notifier.send(f"[{self.mode_name}] entry FAILED {contract.symbol}: {e}")
            return
        except BrokerError as e:
            # Unknown outcome (e.g. timeout): settle it now from the broker's position.
            self.journal.update(trade)
            log.error("entry outcome unknown (%s); reconciling against the position book", e)
            self.clock.sleep(2.0)
            try:
                self._reconcile()
            except BrokerError as e2:
                log.critical("reconcile after unknown entry failed: %s. Check OpenAlgo orderbook manually.", e2)
            if self.journal.open_trade() is None or self.stop_mgr is None:
                return
            trade = self.journal.open_trade()
        self.stop_mgr = StopManager.new(self.cfg.risk, trade.entry_price)
        trade.stop = self.stop_mgr.s.to_dict()
        self.journal.update(trade)
        self.feed.subscribe(trade.symbol, trade.exchange)
        self.notifier.send(f"[{self.mode_name}] BUY {trade.symbol} x{trade.qty} @ {trade.entry_price:.2f} "
                           f"SL {self.stop_mgr.s.stop:.2f} TGT {self.stop_mgr.s.target:.2f}")

    # -------------------------------------------------------------- exit
    def _exit(self, trade: Trade, reason: str, ltp: float | None = None) -> None:
        assert self.journal
        fallback = ltp or self.feed.ltp(trade.symbol, trade.exchange) or trade.entry_price
        log.info("EXIT %s: %s", trade.symbol, reason)
        try:
            self.executor.exit(trade, reason, fallback)
        except BrokerError as e:
            log.critical("EXIT FAILED for %s: %s -- trying closeposition", trade.symbol, e)
            try:
                msg = self.executor.panic_close_all()
                log.warning("closeposition: %s", msg)
                self.executor._close(trade, "closeposition", fallback, reason + " (via closeposition)")
            except BrokerError as e2:
                log.critical("closeposition also failed: %s. MANUAL ACTION REQUIRED.", e2)
                self.notifier.send(f"[{self.mode_name}] EXIT FAILED {trade.symbol}: {e2}. CLOSE MANUALLY.")
                self.journal.update(trade)
                return
        self.journal.update(trade)
        self.feed.unsubscribe(trade.symbol, trade.exchange)
        self.stop_mgr = None
        self._last_stop_persist = None
        self.notifier.send(f"[{self.mode_name}] SELL {trade.symbol} x{trade.qty} @ {trade.exit_price:.2f} "
                           f"pnl={trade.pnl:+.2f} ({reason}) day={self.journal.realized_pnl():+.2f}")
        if self.risk.daily_loss_hit(self.journal.realized_pnl()):
            log.warning("daily loss limit reached; no more entries today")

    def _persist_stop(self, trade: Trade) -> None:
        if not self.stop_mgr:
            return
        d = self.stop_mgr.s.to_dict()
        if d != self._last_stop_persist:
            trade.stop = d
            self.journal.update(trade)  # type: ignore[union-attr]
            self._last_stop_persist = dict(d)
