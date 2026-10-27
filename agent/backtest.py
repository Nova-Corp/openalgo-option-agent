"""Signal backtest on historical candles of the signal symbol with an option-premium PROXY.

Option history is not available far back, so the premium is approximated:
  entry premium = close * proxy_premium_pct / 100   (ATM weekly premium as % of index)
  premium move  = delta * index move                 (delta 0.5 for ATM)
This is good enough to compare signal rules, trade counts and hit rates. It is NOT a
P&L forecast: real premiums also decay with time and change with volatility.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from .clock import IST, at, parse_hhmm
from .config import Config
from .data import add_indicators
from .journal import Trade
from .risk import RiskManager, StopManager
from .strategy import DayContext, OrbTrendStrategy

log = logging.getLogger("agent.backtest")


@dataclass
class BtTrade:
    date: str
    option_type: str
    entry_time: str
    exit_time: str
    index_entry: float
    index_exit: float
    premium_entry: float
    premium_exit: float
    lots: int
    qty: int
    pnl: float
    reason: str


def run_backtest(cfg: Config, broker, days: int = 20, proxy_premium_pct: float = 0.35,
                 delta: float = 0.5, lotsize: int = 65) -> tuple[list[BtTrade], pd.DataFrame]:
    end = pd.Timestamp.now(tz=IST).date()
    start = end - timedelta(days=int(days * 1.6) + 7)
    df = broker.history(cfg.signal_symbol, cfg.signal_exchange, cfg.strategy.candle_interval,
                        start.isoformat(), end.isoformat())
    if df.index.tz is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)
    df = add_indicators(df, cfg.strategy.ema_fast, cfg.strategy.ema_slow)
    strat = OrbTrendStrategy(cfg.strategy)
    risk = RiskManager(cfg.risk, cfg.execution)
    secs = strat.interval_secs
    square_off = parse_hhmm(cfg.risk.square_off_time)
    all_days = sorted(set(df["day"]))[-days:]
    trades: list[BtTrade] = []

    for day in all_days:
        rows = df[df["day"] == day]
        ctx = DayContext()
        realized = 0.0
        i_positions = [df.index.get_loc(ts) for ts in rows.index]
        k = 0
        while k < len(i_positions):
            i = i_positions[k]
            now = df.index[i] + timedelta(seconds=secs)  # candle close time
            sub = df.iloc[: i + 1]
            sig, _ = strat.evaluate(sub, now, ctx)
            if sig is None or risk.can_enter(now, realized, sig.close * proxy_premium_pct / 100):
                k += 1
                continue
            prem_entry = round(sig.close * proxy_premium_pct / 100, 2)
            lots = risk.lots_for(prem_entry, lotsize, live=False)
            if lots <= 0:
                k += 1
                continue
            qty = lots * lotsize
            sm = StopManager.new(cfg.risk, prem_entry)
            sign = 1 if sig.option_type == "CE" else -1
            ref = sig.close
            exit_prem, exit_reason, exit_time, idx_exit = None, "", None, ref
            j = k + 1
            while j < len(i_positions):
                r = df.iloc[i_positions[j]]
                t_close = df.index[i_positions[j]] + timedelta(seconds=secs)
                adverse = r["low"] if sign == 1 else r["high"]
                favour = r["high"] if sign == 1 else r["low"]
                p_adverse = prem_entry + delta * sign * (adverse - ref)
                p_favour = prem_entry + delta * sign * (favour - ref)
                p_close = prem_entry + delta * sign * (r["close"] - ref)
                reason = sm.update(max(p_adverse, 0.05))
                if reason:
                    exit_prem, exit_reason, exit_time, idx_exit = sm.s.stop, "STOPLOSS", t_close, adverse
                    break
                reason = sm.update(max(p_favour, 0.05))
                if reason:
                    exit_prem, exit_reason, exit_time, idx_exit = sm.s.target, "TARGET", t_close, favour
                    break
                sm.update(max(p_close, 0.05))
                why = strat.underlying_exit(df.iloc[: i_positions[j] + 1], sig.option_type, df.index[i])
                if why:
                    exit_prem, exit_reason, exit_time, idx_exit = p_close, "UNDERLYING", t_close, r["close"]
                    break
                if t_close.time() >= square_off:
                    exit_prem, exit_reason, exit_time, idx_exit = p_close, "SQUAREOFF", t_close, r["close"]
                    break
                j += 1
            if exit_prem is None:  # ran out of candles: close at the last close
                r = df.iloc[i_positions[-1]]
                exit_prem = prem_entry + delta * sign * (r["close"] - ref)
                exit_reason, exit_time, idx_exit = "EOD", df.index[i_positions[-1]] + timedelta(seconds=secs), r["close"]
                j = len(i_positions) - 1
            pnl = round((exit_prem - prem_entry) * qty, 2)
            realized += pnl
            trades.append(BtTrade(str(day), sig.option_type, now.strftime("%H:%M"), exit_time.strftime("%H:%M"),
                                  ref, float(idx_exit), prem_entry, round(float(exit_prem), 2), lots, qty, pnl, exit_reason))
            ctx.trades_today += 1
            if sig.option_type == "CE":
                ctx.trades_ce += 1
            else:
                ctx.trades_pe += 1
            ctx.last_exit_time = exit_time
            k = j + 1
    return trades, df


def summarize(trades: list[BtTrade]) -> dict:
    if not trades:
        return {"trades": 0}
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    by_reason: dict[str, int] = {}
    for t in trades:
        by_reason[t.reason] = by_reason.get(t.reason, 0) + 1
    return {
        "trades": len(trades),
        "days": len({t.date for t in trades}),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1),
        "total_pnl": round(sum(pnls), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "max_drawdown": round(dd, 2),
        "exits": by_reason,
    }
