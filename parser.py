"""Signal parsing and validation.

Design rule: this module NEVER guesses. Any field that is missing, ambiguous,
or self-inconsistent produces a Rejection, not a best-effort Signal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import List, Optional

BUY_WORDS = {"buy", "long", "bull", "buying"}
SELL_WORDS = {"sell", "short", "bear", "selling"}

# "buy limit", "sell stop", etc.
PENDING_KINDS = {"limit", "stop"}

NUM = r"([0-9]+(?:[.,][0-9]+)?)"


@dataclass(frozen=True)
class Signal:
    symbol: str
    direction: str            # "buy" | "sell"
    order_kind: str           # "market" | "limit" | "stop"
    entry: Optional[Decimal]  # None only for market orders
    stop_loss: Decimal
    take_profits: List[Decimal] = field(default_factory=list)
    raw: str = ""

    def __str__(self) -> str:
        e = "market" if self.entry is None else f"@{self.entry}"
        tps = ", ".join(str(t) for t in self.take_profits) or "none"
        return (f"{self.direction.upper()} {self.symbol} {self.order_kind} {e} "
                f"SL={self.stop_loss} TP=[{tps}]")


@dataclass(frozen=True)
class Rejection:
    reason: str
    raw: str = ""

    def __str__(self) -> str:
        return f"REJECTED: {self.reason}"


def _dec(tok: str) -> Optional[Decimal]:
    """Parse a number. Treats ',' as a decimal separator only when it is
    unambiguous (single comma, 1-3 trailing digits and no '.' present)."""
    tok = tok.strip()
    try:
        if "," in tok and "." not in tok:
            if tok.count(",") > 1:
                return None
            tok = tok.replace(",", ".")
        elif "," in tok:
            tok = tok.replace(",", "")
        return Decimal(tok)
    except (InvalidOperation, ValueError):
        return None


def _find_all(pattern: str, text: str) -> List[Decimal]:
    out: List[Decimal] = []
    for m in re.finditer(pattern, text, re.IGNORECASE):
        for g in m.groups():
            if g is None:
                continue
            d = _dec(g)
            if d is not None:
                out.append(d)
    return out


def parse_signal(text: str, known_symbols: Optional[set] = None):
    """Parse one Telegram message into a Signal or a Rejection."""
    if not text or not text.strip():
        return Rejection("empty message", text or "")

    raw = text
    t = text.replace("\u2013", "-").replace("\u2014", "-")
    low = t.lower()

    # ---- direction -------------------------------------------------
    hits = set()
    for w in BUY_WORDS:
        if re.search(rf"\b{w}\b", low):
            hits.add("buy")
    for w in SELL_WORDS:
        if re.search(rf"\b{w}\b", low):
            hits.add("sell")
    if not hits:
        return Rejection("no direction keyword (buy/sell) found", raw)
    if len(hits) > 1:
        return Rejection("conflicting directions (both buy and sell present)", raw)
    direction = hits.pop()

    # ---- symbol ----------------------------------------------------
    sym_m = re.search(
        r"\b(?:buy|sell|long|short)(?:\s+(?:limit|stop))?\s*:?\s*"
        r"([A-Za-z][A-Za-z0-9]{2,11}(?:\.[A-Za-z]{1,4})?)\b",
        t, re.IGNORECASE)
    symbol = sym_m.group(1).upper() if sym_m else None
    if symbol is None:
        cands = re.findall(r"\b([A-Z]{6}|XAU[A-Z]{3}|XAG[A-Z]{3}|[A-Z]{2,6}USD)\b", t)
        uniq = list(dict.fromkeys(cands))
        if len(uniq) == 1:
            symbol = uniq[0]
        elif len(uniq) > 1:
            return Rejection(f"ambiguous symbol, multiple candidates: {uniq}", raw)
    if not symbol:
        return Rejection("could not identify an instrument symbol", raw)
    if symbol.lower() in BUY_WORDS | SELL_WORDS | PENDING_KINDS:
        return Rejection("could not identify an instrument symbol", raw)
    if known_symbols is not None and symbol not in known_symbols:
        return Rejection(f"symbol {symbol} not offered by broker", raw)

    # ---- order kind --------------------------------------------------
    kind_m = re.search(r"\b(?:buy|sell|long|short)\s+(limit|stop)\b", low)
    order_kind = kind_m.group(1) if kind_m else "market"

    # ---- stop loss ---------------------------------------------------
    sls = _find_all(rf"\b(?:sl|s/l|stop\s*loss|stoploss)\b\s*[:=@]?\s*{NUM}", t)
    if not sls:
        return Rejection("no stop loss found", raw)
    if len(set(sls)) > 1:
        return Rejection(f"multiple conflicting stop losses: {sls}", raw)
    stop_loss = sls[0]

    # ---- take profits ------------------------------------------------
    tps = _find_all(rf"\b(?:tp|t/p|take\s*profit)\s*[0-9]?\b\s*[:=@]?\s*{NUM}", t)
    # de-duplicate, preserve order
    tps = list(dict.fromkeys(tps))

    # ---- entry -------------------------------------------------------
    entry: Optional[Decimal] = None
    ent = _find_all(
        rf"(?:\bentry\b|\benter\b|\bentry\s*price\b|\bprice\b|@)\s*[:=@]?\s*{NUM}", t)
    if not ent:
        m = re.search(
            rf"\b(?:buy|sell|long|short)(?:\s+(?:limit|stop))?\s+"
            rf"[A-Za-z][A-Za-z0-9.]*\s+(?:at\s+|@\s*)?{NUM}", t, re.IGNORECASE)
        if m:
            d = _dec(m.group(1))
            if d is not None:
                ent = [d]
    if ent:
        if len(set(ent)) > 1:
            return Rejection(f"ambiguous entry, multiple prices: {ent}", raw)
        entry = ent[0]

    if order_kind in PENDING_KINDS and entry is None:
        return Rejection(f"{order_kind} order requires an entry price", raw)

    # ---- geometry sanity --------------------------------------------
    ref = entry
    if ref is not None:
        if direction == "buy" and stop_loss >= ref:
            return Rejection(f"buy stop loss {stop_loss} is not below entry {ref}", raw)
        if direction == "sell" and stop_loss <= ref:
            return Rejection(f"sell stop loss {stop_loss} is not above entry {ref}", raw)
        for tp in tps:
            if direction == "buy" and tp <= ref:
                return Rejection(f"buy take profit {tp} is not above entry {ref}", raw)
            if direction == "sell" and tp >= ref:
                return Rejection(f"sell take profit {tp} is not below entry {ref}", raw)
    else:
        for tp in tps:
            if direction == "buy" and tp <= stop_loss:
                return Rejection(f"buy take profit {tp} is below stop loss {stop_loss}", raw)
            if direction == "sell" and tp >= stop_loss:
                return Rejection(f"sell take profit {tp} is above stop loss {stop_loss}", raw)

    # order TPs nearest-first relative to entry/SL
    anchor = entry if entry is not None else stop_loss
    tps.sort(key=lambda x: abs(x - anchor))

    return Signal(symbol=symbol, direction=direction, order_kind=order_kind,
                  entry=entry, stop_loss=stop_loss, take_profits=tps, raw=raw)
