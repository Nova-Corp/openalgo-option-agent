"""Single-instance lock so two copies of the agent can never trade at once."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path


class AlreadyRunning(Exception):
    pass


class SingleInstance:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def __enter__(self) -> "SingleInstance":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.seek(0)
            holder = self._fh.read().strip() or "unknown pid"
            self._fh.close()
            self._fh = None
            raise AlreadyRunning(f"another agent is already running (pid {holder}); lock {self.path}")
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return self

    def __exit__(self, *exc) -> None:
        if self._fh:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
