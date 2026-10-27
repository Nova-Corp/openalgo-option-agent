"""Logging to console + rotating file, optional Telegram via OpenAlgo."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.logging import RichHandler


def setup_logging(log_dir: str | Path, level: str = "INFO") -> logging.Logger:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if getattr(root, "_agent_configured", False):
        return logging.getLogger("agent")
    root.setLevel(logging.DEBUG)
    console = RichHandler(rich_tracebacks=False, show_path=False, markup=False)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    fileh = RotatingFileHandler(log_dir / "agent.log", maxBytes=5_000_000, backupCount=10)
    fileh.setLevel(logging.DEBUG)
    fileh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(console)
    root.addHandler(fileh)
    for noisy in ("httpx", "httpcore", "websocket", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._agent_configured = True  # type: ignore[attr-defined]
    return logging.getLogger("agent")


class Notifier:
    def __init__(self, broker, telegram_username: str = ""):
        self.broker = broker
        self.username = telegram_username
        self.log = logging.getLogger("agent.notify")

    def send(self, message: str) -> None:
        self.log.info("NOTIFY: %s", message)
        if self.username:
            try:
                self.broker.telegram(self.username, message)
            except Exception as e:
                self.log.debug("telegram failed: %s", e)
