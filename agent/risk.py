"""Position sizing, daily limits and premium-based stop management."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .clock import parse_hhmm
from .config import ExecutionCfg, RiskCfg


class RiskManager:
    def __init__(self, cfg: RiskCfg, exec_cfg: ExecutionCfg):
        self.cfg = cfg
        self.exec_cfg = exec_cfg
        self.square_off = parse_hhmm(cfg.square_off_time)

    @property
    def max_daily_loss(self) -> float:
        return self.cfg.capital * self.cfg.max_daily_loss_pct / 100.0

    @property
    def risk_per_trade(self) -> float:
        return self.cfg.capital * self.cfg.risk_per_trade_pct / 100.0

    def daily_loss_hit(self, realized_pnl: float) -> bool:
        return realized_pnl <= -self.max_daily_loss

    def can_enter(self, now: datetime, realized_pnl: float, premium: float) -> str | None:
        """Returns a reason string if entry is NOT allowed, else None."""
        if now.time() >= self.square_off:
            return "past square-off time"
        if self.daily_loss_hit(realized_pnl):
            return f"daily loss limit hit (realized {realized_pnl:.0f} <= -{self.max_daily_loss:.0f})"
        if premium < self.cfg.min_premium:
            return f"premium {premium:.2f} below min {self.cfg.min_premium}"
        if premium > self.cfg.max_premium:
            return f"premium {premium:.2f} above max {self.cfg.max_premium}"
        return None

    def lots_for(self, premium: float, lotsize: int, live: bool) -> int:
        """Lots so that stop_loss * qty <= risk per trade; clamped to config."""
        if premium <= 0 or lotsize <= 0:
            return 0
        if self.cfg.stop_loss_points is not None and self.cfg.stop_loss_points > 0:
            risk_per_lot = self.cfg.stop_loss_points * lotsize
        else:
            risk_per_lot = premium * self.cfg.stop_loss_pct / 100.0 * lotsize
        if risk_per_lot <= 0:
            return 0
        lots = math.floor(self.risk_per_trade / risk_per_lot)
        lots = max(self.cfg.min_lots, min(lots, self.cfg.max_lots))
        if live:
            lots = min(lots, self.exec_cfg.live_max_lots)
        # Even one lot must not risk more than 2x the per-trade budget.
        if risk_per_lot * lots > 2 * self.risk_per_trade:
            return 0
        return lots


@dataclass
class StopState:
    entry: float
    stop: float
    target: float
    highest: float
    breakeven_done: bool = False
    trailing: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StopState":
        return cls(**d)


class StopManager:
    """Fixed stop + target on the option premium, then breakeven and trailing."""

    def __init__(self, cfg: RiskCfg, state: StopState):
        self.cfg = cfg
        self.s = state

    @classmethod
    def new(cls, cfg: RiskCfg, entry: float) -> "StopManager":
        if cfg.stop_loss_points is not None and cfg.stop_loss_points > 0:
            stop = round(max(0.05, entry - cfg.stop_loss_points), 2)
        else:
            stop = round(entry * (1 - cfg.stop_loss_pct / 100), 2)

        if cfg.target_points is not None and cfg.target_points > 0:
            target = round(entry + cfg.target_points, 2)
        else:
            target = round(entry * (1 + cfg.target_pct / 100), 2)

        st = StopState(entry=entry, stop=stop, target=target, highest=entry)
        return cls(cfg, st)

    def update(self, ltp: float) -> str | None:
        """Feed an LTP; returns an exit reason or None. Also ratchets the stop."""
        s = self.s
        if ltp <= s.stop:
            return f"STOPLOSS ltp {ltp:.2f} <= stop {s.stop:.2f}"
        if ltp >= s.target:
            return f"TARGET ltp {ltp:.2f} >= target {s.target:.2f}"
        if ltp > s.highest:
            s.highest = ltp
        gain = s.highest - s.entry
        gain_pct = (gain / s.entry * 100) if s.entry > 0 else 0

        # Breakeven check
        be_hit = False
        if self.cfg.breakeven_points is not None and self.cfg.breakeven_points > 0:
            be_hit = gain >= self.cfg.breakeven_points
        elif self.cfg.breakeven_at_pct > 0 and self.cfg.breakeven_points is None:
            be_hit = gain_pct >= self.cfg.breakeven_at_pct

        if not s.breakeven_done and be_hit:
            s.stop = max(s.stop, s.entry)
            s.breakeven_done = True

        # Trailing check
        trail_hit = False
        if self.cfg.trail_activate_points is not None and self.cfg.trail_activate_points > 0:
            trail_hit = gain >= self.cfg.trail_activate_points
        elif self.cfg.trail_activate_pct > 0 and self.cfg.trail_activate_points is None:
            trail_hit = gain_pct >= self.cfg.trail_activate_pct

        if trail_hit:
            s.trailing = True

        if s.trailing:
            if self.cfg.trail_step_points is not None and self.cfg.trail_step_points > 0:
                trail = round(s.highest - self.cfg.trail_step_points, 2)
            else:
                trail = round(s.highest * (1 - self.cfg.trail_step_pct / 100), 2)
            if trail > s.stop:
                s.stop = trail
        return None
