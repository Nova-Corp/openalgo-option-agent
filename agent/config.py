"""Configuration: config.yaml for strategy/risk, .env for secrets."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator


class OpenAlgoCfg(BaseModel):
    host: str = "http://127.0.0.1:5000"
    ws_port: int = 8765
    api_key: str = ""
    timeout_sec: float = 15.0


class InstrumentCfg(BaseModel):
    underlying: str = "NIFTY"
    underlying_exchange: str = "NSE_INDEX"
    option_exchange: str = "NFO"
    # Symbol used for candles/signals. Defaults to the underlying index.
    # An index has no volume, so VWAP falls back to a time-weighted average.
    # Set this to the current-month future (e.g. NIFTY24NOV26FUT on NFO) for a true VWAP.
    signal_symbol: str | None = None
    signal_exchange: str | None = None
    expiry: Literal["nearest", "next"] = "nearest"
    min_days_to_expiry: int = 0
    offset: str = "ATM"  # ATM, OTM1, ITM1 ...
    product: Literal["MIS", "NRML"] = "MIS"


class StrategyCfg(BaseModel):
    name: Literal["orb_trend"] = "orb_trend"
    candle_interval: str = "5m"
    history_days: int = 5  # extra days fetched so EMAs are warmed up at the open
    opening_range_minutes: int = 15
    entry_start: str = "09:30"
    entry_end: str = "14:30"
    ema_fast: int = 9
    ema_slow: int = 21
    use_vwap_filter: bool = True
    use_ema_filter: bool = True
    min_range_pct: float = 0.10  # opening range as % of price
    max_range_pct: float = 0.80
    max_extension_pct: float = 0.30  # do not chase: close must be within this % beyond the range
    # breakout candle quality: skip indecisive (doji) candles and wait for a decisive one
    min_body_pct: float = 30.0  # body (|close-open|) must be at least this % of the candle's high-low range
    require_direction: bool = True  # body must point the breakout way (green for CE, red for PE)
    min_close_position_pct: float = 66.0  # CE: close in the top third of the candle; PE: bottom third
    max_trades_per_day: int = 2
    max_trades_per_direction: int = 1
    cooldown_minutes: int = 10
    underlying_exit: bool = True  # exit if 5m close comes back through VWAP / slow EMA


class RiskCfg(BaseModel):
    capital: float = 100_000
    risk_per_trade_pct: float = 1.0
    max_daily_loss_pct: float = 2.0
    max_lots: int = 2
    min_lots: int = 1
    stop_loss_pct: float = 25.0  # of entry premium
    target_pct: float = 50.0
    breakeven_at_pct: float = 15.0
    trail_activate_pct: float = 20.0
    trail_step_pct: float = 12.0  # trail stop this % below the highest premium seen
    min_premium: float = 20.0
    max_premium: float = 400.0
    square_off_time: str = "15:10"

    @field_validator("stop_loss_pct", "target_pct")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("must be > 0")
        return v


class ExecutionCfg(BaseModel):
    strategy_tag: str = "IntradayOptionAgent"
    fill_timeout_sec: float = 20.0
    exit_retries: int = 3
    poll_interval_sec: float = 1.0
    candle_delay_sec: int = 5  # wait this long after a candle boundary before fetching it
    candle_grace_sec: int = 20  # if the broker still lacks the candle after this, build it from live ticks
    ws_stale_sec: float = 10.0
    quote_fallback_interval_sec: float = 2.0
    allow_live: bool = False  # second safety latch; analyzer OFF + allow_live false = refuse to trade
    live_max_lots: int = 1
    adopt_unknown_positions: bool = False


class NotifyCfg(BaseModel):
    telegram_username: str = ""  # OpenAlgo telegram bot username to notify, blank = off


# Exchanges per index. Everything else (lot size, expiry list, strikes) comes from OpenAlgo at runtime.
UNDERLYING_PRESETS: dict[str, dict] = {
    "NIFTY": {"underlying_exchange": "NSE_INDEX", "option_exchange": "NFO"},
    "BANKNIFTY": {"underlying_exchange": "NSE_INDEX", "option_exchange": "NFO"},
    "FINNIFTY": {"underlying_exchange": "NSE_INDEX", "option_exchange": "NFO"},
    "MIDCPNIFTY": {"underlying_exchange": "NSE_INDEX", "option_exchange": "NFO"},
    "SENSEX": {"underlying_exchange": "BSE_INDEX", "option_exchange": "BFO"},
    "BANKEX": {"underlying_exchange": "BSE_INDEX", "option_exchange": "BFO"},
}


class Config(BaseModel):
    openalgo: OpenAlgoCfg = Field(default_factory=OpenAlgoCfg)
    instrument: InstrumentCfg = Field(default_factory=InstrumentCfg)
    strategy: StrategyCfg = Field(default_factory=StrategyCfg)
    risk: RiskCfg = Field(default_factory=RiskCfg)
    execution: ExecutionCfg = Field(default_factory=ExecutionCfg)
    notify: NotifyCfg = Field(default_factory=NotifyCfg)
    # Per-index overrides applied on top of the sections above, e.g.
    # underlyings: {BANKNIFTY: {risk: {max_premium: 900}}, SENSEX: {risk: {max_premium: 900}}}
    underlyings: dict[str, dict] = Field(default_factory=dict)
    active_underlyings: list[str] = Field(default_factory=lambda: ["NIFTY"])  # what ./morning.sh starts by default
    state_dir: str = "state"
    log_dir: str = "logs"
    project_dir: str = "."

    def apply_underlying(self, name: str) -> "Config":
        """Switch the config to another index: exchanges from the preset, then user overrides."""
        name = name.strip().upper()
        if name not in UNDERLYING_PRESETS:
            raise ValueError(f"unknown underlying {name}; choose one of {', '.join(UNDERLYING_PRESETS)}")
        self.instrument.underlying = name
        self.instrument.underlying_exchange = UNDERLYING_PRESETS[name]["underlying_exchange"]
        self.instrument.option_exchange = UNDERLYING_PRESETS[name]["option_exchange"]
        self.instrument.signal_symbol = None
        self.instrument.signal_exchange = None
        for section, values in (self.underlyings.get(name) or {}).items():
            target = getattr(self, section, None)
            if target is None or not isinstance(values, dict):
                raise ValueError(f"underlyings.{name}.{section}: unknown section or not a mapping")
            for k, v in values.items():
                if not hasattr(target, k):
                    raise ValueError(f"underlyings.{name}.{section}.{k}: unknown setting")
                setattr(target, k, v)
        return self

    @property
    def index_state_dir(self) -> Path:
        """state/<UNDERLYING>/ : each index runs as its own agent with its own journal, lock and run info."""
        return Path(self.project_dir) / self.state_dir / self.instrument.underlying

    @property
    def signal_symbol(self) -> str:
        return self.instrument.signal_symbol or self.instrument.underlying

    @property
    def signal_exchange(self) -> str:
        return self.instrument.signal_exchange or self.instrument.underlying_exchange


def load_config(path: str | Path = "config.yaml", underlying: str | None = None) -> Config:
    path = Path(path)
    project_dir = path.resolve().parent
    load_dotenv(project_dir / ".env")
    raw: dict = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
    cfg = Config(**raw)
    cfg.project_dir = str(project_dir)
    cfg.apply_underlying(underlying or os.getenv("AGENT_UNDERLYING") or cfg.instrument.underlying)
    cfg.openalgo.api_key = os.getenv("OPENALGO_API_KEY", cfg.openalgo.api_key)
    cfg.openalgo.host = os.getenv("OPENALGO_HOST", cfg.openalgo.host)
    cfg.openalgo.ws_port = int(os.getenv("OPENALGO_WS_PORT", cfg.openalgo.ws_port))
    for d in (cfg.state_dir, cfg.log_dir):
        (project_dir / d).mkdir(parents=True, exist_ok=True)
    return cfg
