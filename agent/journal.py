"""Crash-safe daily journal: state/<date>.json plus an append-only trades.csv."""
from __future__ import annotations

import csv
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from .clock import IST

TRADE_FIELDS = ["id", "date", "mode", "symbol", "exchange", "option_type", "lots", "qty", "entry_orderid",
                "entry_price", "entry_time", "exit_orderid", "exit_price", "exit_time", "exit_reason",
                "pnl", "status", "signal", "stop"]


@dataclass
class Trade:
    id: str
    date: str
    mode: str  # "paper" or "live"
    symbol: str
    exchange: str
    option_type: str
    lots: int
    qty: int
    entry_orderid: str = ""
    entry_price: float = 0.0
    entry_time: str = ""
    exit_orderid: str = ""
    exit_price: float = 0.0
    exit_time: str = ""
    exit_reason: str = ""
    pnl: float = 0.0
    status: str = "ENTERING"  # ENTERING, OPEN, EXITING, CLOSED, FAILED
    signal: str = ""
    stop: dict = field(default_factory=dict)
    product: str = "MIS"
    lotsize: int = 0

    @staticmethod
    def new(**kw) -> "Trade":
        return Trade(id=uuid.uuid4().hex[:10], **kw)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Trade":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class Journal:
    def __init__(self, state_dir: str | Path, day: date):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.day = day
        self.path = self.dir / f"{day.isoformat()}.json"
        self.csv_path = self.dir / "trades.csv"
        self.data: dict = {"date": day.isoformat(), "trades": [], "notes": []}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                bad = self.path.with_suffix(".corrupt.json")
                self.path.replace(bad)
                self.data["notes"].append(f"corrupt state moved to {bad.name}")
        self._trades: list[Trade] = [Trade.from_dict(t) for t in self.data.get("trades", [])]

    # --------------------------------------------------------------- io
    def save(self) -> None:
        self.data["trades"] = [t.to_dict() for t in self._trades]
        self.data["updated"] = datetime.now(IST).isoformat(timespec="seconds")
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, default=str))
        os.replace(tmp, self.path)

    def note(self, text: str) -> None:
        self.data.setdefault("notes", []).append(f"{datetime.now(IST).isoformat(timespec='seconds')} {text}")
        self.save()

    # ----------------------------------------------------------- trades
    @property
    def trades(self) -> list[Trade]:
        return list(self._trades)

    def add(self, t: Trade) -> Trade:
        self._trades.append(t)
        self.save()
        return t

    def update(self, t: Trade) -> None:
        for i, x in enumerate(self._trades):
            if x.id == t.id:
                self._trades[i] = t
                break
        else:
            self._trades.append(t)
        self.save()
        if t.status == "CLOSED":
            self._append_csv(t)

    def open_trade(self) -> Optional[Trade]:
        for t in self._trades:
            if t.status in ("ENTERING", "OPEN", "EXITING"):
                return t
        return None

    def realized_pnl(self) -> float:
        return float(sum(t.pnl for t in self._trades if t.status == "CLOSED"))

    def count(self, option_type: str | None = None) -> int:
        ts = [t for t in self._trades if t.status != "FAILED"]
        if option_type:
            ts = [t for t in ts if t.option_type == option_type]
        return len(ts)

    def last_exit_time(self) -> Optional[datetime]:
        times = [t.exit_time for t in self._trades if t.status == "CLOSED" and t.exit_time]
        if not times:
            return None
        return datetime.fromisoformat(max(times))

    def _append_csv(self, t: Trade) -> None:
        new = not self.csv_path.exists()
        with self.csv_path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            row = t.to_dict()
            row["stop"] = json.dumps(row["stop"])
            w.writerow(row)
