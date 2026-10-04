"""Risk-based position sizing.

Pure functions, no MT5 dependency, so they are unit-testable offline.

Rule: risk RISK_PERCENT of account balance on the distance between entry and
stop loss. If anything needed for that calculation is missing or implausible,
fall back to FALLBACK_LOT rather than guessing a size.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class SymbolSpec:
    """Broker contract details needed to convert price distance -> money."""
    name: str
    tick_value: float    # account-currency value of one tick, per 1.0 lot
    tick_size: float     # smallest price increment
    volume_min: float
    volume_max: float
    volume_step: float
    digits: int = 5

    def valid(self) -> bool:
        return (self.tick_value > 0 and self.tick_size > 0
                and self.volume_min > 0 and self.volume_step > 0
                and self.volume_max >= self.volume_min)


def _round_to_step(vol: float, step: float) -> float:
    """Round DOWN to the broker's volume step - never round risk upward."""
    if step <= 0:
        return vol
    steps = math.floor(round(vol / step, 9))
    # recover the step's decimal precision to avoid float dust (0.30000000004)
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(steps * step, decimals + 2)


def compute_lot(
    balance: Optional[float],
    risk_percent: float,
    sl_distance: Optional[float],
    spec: Optional[SymbolSpec],
    fallback_lot: float = 0.01,
    max_lot: Optional[float] = None,
) -> Tuple[float, str]:
    """Return (lot, human-readable reason).

    The reason string goes straight into the audit log so every sizing
    decision is explainable after the fact.
    """
    # --- guard every input the calculation depends on -------------------
    if balance is None or balance <= 0:
        return _cap(fallback_lot, max_lot, "fallback: account balance unavailable")
    if risk_percent <= 0:
        return _cap(fallback_lot, max_lot, "fallback: RISK_PERCENT not positive")
    if sl_distance is None or sl_distance <= 0:
        return _cap(fallback_lot, max_lot, "fallback: stop distance unavailable or zero")
    if spec is None or not spec.valid():
        return _cap(fallback_lot, max_lot, "fallback: broker contract specs unavailable")

    risk_money = balance * (risk_percent / 100.0)
    ticks = sl_distance / spec.tick_size
    risk_per_lot = ticks * spec.tick_value
    if risk_per_lot <= 0:
        return _cap(fallback_lot, max_lot, "fallback: computed risk per lot is zero")

    raw = risk_money / risk_per_lot
    lot = _round_to_step(raw, spec.volume_step)

    if lot < spec.volume_min:
        # Correct sizing would be smaller than the broker allows. Taking
        # volume_min would silently risk MORE than intended, so fall back and
        # say so loudly.
        return _cap(
            fallback_lot, max_lot,
            f"fallback: risk-based size {raw:.4f} below broker minimum "
            f"{spec.volume_min} (would over-risk)")

    if lot > spec.volume_max:
        lot = spec.volume_max
        return _cap(lot, max_lot,
                    f"clamped to broker maximum volume {spec.volume_max}")

    actual_risk = lot * risk_per_lot
    reason = (f"risk {risk_percent}% of {balance:.2f} = {risk_money:.2f}; "
              f"stop {sl_distance:.5f} = {risk_per_lot:.2f}/lot; "
              f"sized {lot} (actual risk {actual_risk:.2f})")
    return _cap(lot, max_lot, reason)


def _cap(lot: float, max_lot: Optional[float], reason: str) -> Tuple[float, str]:
    if max_lot is not None and lot > max_lot:
        return max_lot, reason + f"; capped at MAX_LOT {max_lot}"
    return lot, reason
