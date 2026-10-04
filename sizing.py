"""Risk-based position sizing helpers."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple


class SizingError(RuntimeError):
    pass


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    tick_value: float
    tick_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    digits: int = 5
    point: float = 0.0
    trade_stops_level: int = 0
    trade_freeze_level: int = 0
    filling_mode: int = 0
    trade_exemode: int = 0
    order_mode: int = 0
    trade_mode: int = 0

    def valid(self) -> bool:
        return (
            self.tick_size > 0
            and self.volume_min > 0
            and self.volume_step > 0
            and self.volume_max >= self.volume_min
        )


def floor_to_step(vol: float, step: float) -> float:
    if step <= 0:
        raise SizingError("broker volume step is invalid")
    steps = math.floor(round(vol / step, 10))
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(steps * step, decimals + 2)


def compute_lot(
    equity: Optional[float],
    risk_percent: float,
    loss_per_lot: Optional[float],
    spec: Optional[SymbolSpec],
    max_lot: Optional[float] = None,
) -> Tuple[float, str]:
    """Fail-closed risk sizing.

    No fallback lot is used: if the monetary loss at SL cannot be determined,
    the signal must not be traded in risk mode.
    """
    if equity is None or equity <= 0:
        raise SizingError("account equity unavailable")
    if risk_percent <= 0:
        raise SizingError("RISK_PERCENT must be positive")
    if loss_per_lot is None or loss_per_lot <= 0:
        raise SizingError("loss per lot at stop loss unavailable")
    if spec is None or not spec.valid():
        raise SizingError("broker contract specs unavailable")

    risk_money = equity * (risk_percent / 100.0)
    raw = risk_money / loss_per_lot
    limit = spec.volume_max
    if max_lot is not None:
        limit = min(limit, max_lot)
    raw = min(raw, limit)
    lot = floor_to_step(raw, spec.volume_step)

    if lot < spec.volume_min:
        raise SizingError(
            f"risk-based size {raw:.6f} is below broker minimum {spec.volume_min}; "
            "refusing to round up and over-risk"
        )
    actual = lot * loss_per_lot
    return lot, (
        f"risk {risk_percent}% of equity {equity:.2f} = {risk_money:.2f}; "
        f"loss/lot at SL {loss_per_lot:.2f}; sized {lot} "
        f"(estimated risk {actual:.2f})"
    )
