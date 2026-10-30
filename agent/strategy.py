"""Opening-range breakout with trend filters, for intraday option buying.

Signal rules (evaluated on each CLOSED candle of the signal symbol):
  CE: close breaks above the opening-range high, close > VWAP, EMA fast > EMA slow
  PE: close breaks below the opening-range low,  close < VWAP, EMA fast < EMA slow
Filters: opening range width within [min_range_pct, max_range_pct] of price,
         close not extended more than max_extension_pct beyond the range (no chasing),
         entry window, per-day / per-direction trade limits, cooldown after an exit.
Underlying exit: a closed candle back through VWAP (or the slow EMA) against the trade.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

import pandas as pd

from .clock import MARKET_OPEN, at, interval_seconds, parse_hhmm
from .config import StrategyCfg


@dataclass
class Signal:
    option_type: str  # CE or PE
    reason: str
    candle_time: datetime
    close: float
    orb_high: float
    orb_low: float


@dataclass
class DayContext:
    trades_today: int = 0
    trades_ce: int = 0
    trades_pe: int = 0
    last_exit_time: datetime | None = None


class OrbTrendStrategy:
    def __init__(self, cfg: StrategyCfg):
        self.cfg = cfg
        self.entry_start = parse_hhmm(cfg.entry_start)
        self.entry_end = parse_hhmm(cfg.entry_end)
        self.interval_secs = interval_seconds(cfg.candle_interval)
        self.orb_candles = max(1, (cfg.opening_range_minutes * 60) // self.interval_secs)

    # ----------------------------------------------------------- helpers
    def today_candles(self, df: pd.DataFrame, day) -> pd.DataFrame:
        if df.empty:
            return df
        return df[df.index.date == day]

    def opening_range(self, df_day: pd.DataFrame, day) -> tuple[float, float] | None:
        end = at(day, MARKET_OPEN) + timedelta(minutes=self.cfg.opening_range_minutes)
        orb = df_day[(df_day.index >= at(day, MARKET_OPEN)) & (df_day.index < end)]
        if orb.empty or len(orb) < self.orb_candles:
            return None  # the opening range is complete only once all its candles have closed
        return float(orb["high"].max()), float(orb["low"].min())

    def _limits_ok(self, option_type: str, now: datetime, ctx: DayContext) -> str | None:
        c = self.cfg
        if ctx.trades_today >= c.max_trades_per_day:
            return "max trades per day reached"
        n_dir = ctx.trades_ce if option_type == "CE" else ctx.trades_pe
        if n_dir >= c.max_trades_per_direction:
            return f"max {option_type} trades reached"
        if ctx.last_exit_time and (now - ctx.last_exit_time) < timedelta(minutes=c.cooldown_minutes):
            return "cooldown after last exit"
        return None

    # ---------------------------------------------------- candle quality
    def candle_quality(self, candle, option_type: str) -> str | None:
        """Returns a reason if the breakout candle is indecisive (doji, wrong colour, rejection wick)."""
        c = self.cfg
        o, h, l, cl = float(candle["open"]), float(candle["high"]), float(candle["low"]), float(candle["close"])
        rng = h - l
        if rng <= 0:
            return "flat candle"
        body_pct = abs(cl - o) / rng * 100
        if body_pct < c.min_body_pct:
            return f"doji-like candle (body {body_pct:.0f}% of range)"
        if c.require_direction and ((option_type == "CE" and cl <= o) or (option_type == "PE" and cl >= o)):
            return "candle colour against the breakout"
        pos = (cl - l) / rng * 100  # 100 = closed at the high, 0 = closed at the low
        if option_type == "CE" and pos < c.min_close_position_pct:
            return f"close only {pos:.0f}% up the candle (upper wick rejection)"
        if option_type == "PE" and pos > 100 - c.min_close_position_pct:
            return f"close only {100 - pos:.0f}% down the candle (lower wick rejection)"
        return None

    # ------------------------------------------------------------ entry
    def evaluate(self, df: pd.DataFrame, now: datetime, ctx: DayContext) -> tuple[Signal | None, str]:
        """Returns (signal, note). note explains why there is no signal."""
        c = self.cfg
        if df.empty:
            return None, "no candles"
        day = now.date()
        d = self.today_candles(df, day)
        if d.empty:
            return None, "no candles for today yet"
        last = d.iloc[-1]
        t = d.index[-1]
        if not (self.entry_start <= now.time() <= self.entry_end):
            return None, "outside entry window"
        rng = self.opening_range(d, day)
        if rng is None:
            return None, "opening range not complete"
        orb_high, orb_low = rng
        if t < at(day, MARKET_OPEN) + timedelta(minutes=c.opening_range_minutes):
            return None, "still inside opening range"
        close = float(last["close"])
        width_pct = (orb_high - orb_low) / close * 100
        if width_pct < c.min_range_pct:
            return None, f"opening range too narrow ({width_pct:.2f}%)"
        if width_pct > c.max_range_pct:
            return None, f"opening range too wide ({width_pct:.2f}%)"
        vwap = float(last["vwap"])
        ef, es = float(last["ema_fast"]), float(last["ema_slow"])

        option_type = None
        if close > orb_high:
            ext = (close - orb_high) / orb_high * 100
            if ext > c.max_extension_pct:
                return None, f"CE breakout too extended ({ext:.2f}%)"
            if c.use_vwap_filter and close <= vwap:
                return None, "CE breakout but close below VWAP"
            if c.use_ema_filter and ef <= es:
                return None, "CE breakout but EMA fast below slow"
            why = self.candle_quality(last, "CE")
            if why:
                return None, f"CE breakout but {why}; waiting for a decisive candle"
            if c.require_full_body_outside and float(last["open"]) <= orb_high:
                return None, (f"CE breakout but candle opened inside the range (open {float(last['open']):.1f} <= "
                              f"high {orb_high:.1f}); waiting for a full-body candle above it")
            option_type = "CE"
        elif close < orb_low:
            ext = (orb_low - close) / orb_low * 100
            if ext > c.max_extension_pct:
                return None, f"PE breakdown too extended ({ext:.2f}%)"
            if c.use_vwap_filter and close >= vwap:
                return None, "PE breakdown but close above VWAP"
            if c.use_ema_filter and ef >= es:
                return None, "PE breakdown but EMA fast above slow"
            why = self.candle_quality(last, "PE")
            if why:
                return None, f"PE breakdown but {why}; waiting for a decisive candle"
            if c.require_full_body_outside and float(last["open"]) >= orb_low:
                return None, (f"PE breakdown but candle opened inside the range (open {float(last['open']):.1f} >= "
                              f"low {orb_low:.1f}); waiting for a full-body candle below it")
            option_type = "PE"
        else:
            return None, f"inside opening range [{orb_low:.1f}, {orb_high:.1f}] close={close:.1f}"

        why = self._limits_ok(option_type, now, ctx)
        if why:
            return None, why
        reason = (f"{option_type} ORB {'breakout' if option_type == 'CE' else 'breakdown'}: close={close:.1f} "
                  f"range=[{orb_low:.1f},{orb_high:.1f}] vwap={vwap:.1f} ema={ef:.1f}/{es:.1f}")
        return Signal(option_type, reason, t.to_pydatetime(), close, orb_high, orb_low), "signal"

    # ------------------------------------------------------------- exit
    def underlying_exit(self, df: pd.DataFrame, option_type: str, entry_time: datetime) -> str | None:
        """Exit if the last closed candle (after entry) closed back through VWAP or the slow EMA."""
        if not self.cfg.underlying_exit or df.empty:
            return None
        last = df.iloc[-1]
        if df.index[-1] <= entry_time:
            return None
        close, vwap, es = float(last["close"]), float(last["vwap"]), float(last["ema_slow"])
        if option_type == "CE" and close < min(vwap, es):
            return f"underlying closed below VWAP/EMA ({close:.1f} < {min(vwap, es):.1f})"
        if option_type == "PE" and close > max(vwap, es):
            return f"underlying closed above VWAP/EMA ({close:.1f} > {max(vwap, es):.1f})"
        return None
