"""Time helpers. Everything in the agent runs on IST (Asia/Kolkata)."""
from __future__ import annotations

import time as _time
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def now_ist() -> datetime:
    return datetime.now(IST)


def parse_hhmm(s: str) -> time:
    hh, mm = s.strip().split(":")
    return time(int(hh), int(mm))


def at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=IST)


def interval_seconds(interval: str) -> int:
    unit = interval[-1]
    n = int(interval[:-1])
    if unit == "s":
        return n
    if unit == "m":
        return n * 60
    if unit == "h":
        return n * 3600
    raise ValueError(f"unsupported intraday interval: {interval}")


class Clock:
    """Real wall clock. Tests replace it with FakeClock."""

    def now(self) -> datetime:
        return now_ist()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            _time.sleep(seconds)


class FakeClock(Clock):
    def __init__(self, start: datetime, step_seconds: float = 1.0):
        self._now = start if start.tzinfo else start.replace(tzinfo=IST)
        self.step = timedelta(seconds=step_seconds)

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        # Advance by the requested sleep so loops progress deterministically.
        self._now += timedelta(seconds=max(seconds, 0)) if seconds else self.step

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def set(self, dt: datetime) -> None:
        self._now = dt if dt.tzinfo else dt.replace(tzinfo=IST)
