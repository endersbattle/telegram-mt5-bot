"""Hard request budget shared across Telegram + MT5 calls.

Keeps total requests strictly under a rolling 24h cap (default 9000, a margin
below the 10000 limit) so the brokerage never sees spam-like activity.
State is persisted so restarts cannot reset the budget.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path

WINDOW = 24 * 60 * 60


class BudgetExceeded(RuntimeError):
    pass


class RequestBudget:
    def __init__(self, max_requests: int = 9000, state_file: str = ".budget.json"):
        self.max_requests = max_requests
        # Emergency pool reserved for order-management calls, so routine
        # polling can never starve an exit. Scales down for small budgets.
        self.reserve = min(100, max_requests // 10)
        self.state_file = Path(state_file)
        self._lock = threading.Lock()
        self._events: deque[float] = deque()
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text())
            now = time.time()
            self._events = deque(t for t in data.get("events", [])
                                 if now - t < WINDOW)
        except Exception:
            self._events = deque()

    def _save(self) -> None:
        try:
            self.state_file.write_text(json.dumps({"events": list(self._events)}))
        except Exception:
            pass

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
        """Record n requests. Raises BudgetExceeded if the cap would break.

        critical=True may draw on the reserved emergency pool.
        """
        with self._lock:
            self._prune()
            cap = self.max_requests if critical else self.max_requests - self.reserve
            if len(self._events) + n > cap:
                raise BudgetExceeded(
                    f"request budget exhausted: {len(self._events)}/{self.max_requests} "
                    f"used in rolling 24h")
            now = time.time()
            for _ in range(n):
                self._events.append(now)
            self._save()


class Backoff:
    """Exponential backoff with cap, reset on success."""

    def __init__(self, base: float = 5.0, cap: float = 900.0):
        self.base, self.cap, self.n = base, cap, 0

    def fail(self) -> float:
        self.n += 1
        return min(self.cap, self.base * (2 ** (self.n - 1)))

    def ok(self) -> None:
        self.n = 0
