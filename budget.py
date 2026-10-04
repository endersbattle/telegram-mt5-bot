"""Hard rolling request budget shared across Telegram + MT5 calls."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

WINDOW = 24 * 60 * 60


class BudgetExceeded(RuntimeError):
    pass


class BudgetPersistenceError(RuntimeError):
    pass


class RequestBudget:
    def __init__(self, max_requests: int = 9000, state_file: str = ".budget.json"):
        self.max_requests = max_requests
        self.reserve = min(100, max_requests // 10)
        self.state_file = Path(state_file)
        self._lock = threading.Lock()
        self._events: deque[float] = deque()
        self._load()

    def _load(self) -> None:
        if not self.state_file.exists():
            self._events = deque()
            return
        try:
            data = json.loads(self.state_file.read_text())
            now = time.time()
            self._events = deque(
                float(t) for t in data.get("events", []) if now - float(t) < WINDOW
            )
        except Exception as e:
            raise BudgetPersistenceError(
                f"cannot read request-budget state {self.state_file}: {e}"
            ) from e

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=self.state_file.name + ".", dir=str(self.state_file.parent), text=True
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"events": list(self._events)}, f, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_file)
        except Exception as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise BudgetPersistenceError(
                f"cannot persist request-budget state {self.state_file}: {e}"
            ) from e

    def _prune(self) -> None:
        now = time.time()
        while self._events and now - self._events[0] >= WINDOW:
            self._events.popleft()

    @property
    def used(self) -> int:
        with self._lock:
            self._prune()
            return len(self._events)

    @property
    def remaining(self) -> int:
        return self.max_requests - self.used

    def spend(self, n: int = 1, critical: bool = False) -> None:
        with self._lock:
            self._prune()
            cap = self.max_requests if critical else self.max_requests - self.reserve
            if len(self._events) + n > cap:
                raise BudgetExceeded(
                    f"request budget exhausted: {len(self._events)}/{self.max_requests} "
                    "used in rolling 24h"
                )
            now = time.time()
            for _ in range(n):
                self._events.append(now)
            try:
                self._save()
            except Exception:
                for _ in range(n):
                    if self._events:
                        self._events.pop()
                raise


class Backoff:
    def __init__(self, base: float = 5.0, cap: float = 900.0):
        self.base, self.cap, self.n = base, cap, 0

    def fail(self) -> float:
        self.n += 1
        return min(self.cap, self.base * (2 ** (self.n - 1)))

    def ok(self) -> None:
        self.n = 0
