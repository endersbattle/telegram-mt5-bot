"""Symbol resolution: map a signal's instrument name to the broker's ticker.

Brokers decorate the same instrument in many ways - XAUUSD, XAUUSD.p,
XAUUSD_i, XAUUSDm, XAUUSD.pro, GOLD. A signal channel rarely uses the same
form as your broker.

Resolution order (first hit wins):
  1. explicit SYMBOL_MAP entry
  2. exact match
  3. case-insensitive match
  4. built-in alias (GOLD -> XAUUSD), re-run through steps 2-3 and 5
  5. base + broker suffix match (XAUUSD -> XAUUSD.p)
  6. signal carries a suffix the broker lacks (XAUUSD.p -> XAUUSD)

If step 5 finds MORE THAN ONE candidate the result is ambiguous and the
signal is refused, never guessed. Set SYMBOL_SUFFIX or an explicit
SYMBOL_MAP entry to disambiguate.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

# Common vendor names for the same instrument. Conservative on purpose:
# only unambiguous, widely agreed equivalences belong here.
BUILTIN_ALIASES: Dict[str, str] = {
    "GOLD": "XAUUSD",
    "XAUUSD": "GOLD",
    "SILVER": "XAGUSD",
    "XAGUSD": "SILVER",
}

# A suffix a broker may append: optional separator plus a short tag.
SUFFIX_RE = re.compile(r"^[._\-]?[A-Za-z0-9]{0,5}$")


def _core(s: str) -> str:
    """Strip separators and case for loose comparison."""
    return re.sub(r"[^A-Za-z0-9]", "", s).upper()


def parse_symbol_map(raw: str) -> Dict[str, str]:
    """Parse 'XAUUSD=XAUUSD.p,US30=DJ30' into a dict, keys upper-cased."""
    out: Dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"SYMBOL_MAP entry {part!r} must be SIGNAL=BROKER")
        k, v = part.split("=", 1)
        k, v = k.strip().upper(), v.strip()
        if not k or not v:
            raise ValueError(f"SYMBOL_MAP entry {part!r} has an empty side")
        out[k] = v
    return out


def _suffix_candidates(sig: str, broker: Iterable[str]) -> List[str]:
    """Broker tickers that are `sig` plus a short suffix."""
    sig_u = sig.upper()
    hits = []
    for b in broker:
        bu = b.upper()
        if bu == sig_u:
            continue
        if bu.startswith(sig_u) and SUFFIX_RE.match(b[len(sig):]):
            hits.append(b)
    return hits


def _strip_candidates(sig: str, broker: Iterable[str]) -> List[str]:
    """Broker tickers matching `sig` once the signal's own suffix is removed."""
    core = _core(sig)
    hits = []
    for b in broker:
        bc = _core(b)
        if bc == core:
            hits.append(b)
        elif core.startswith(bc) and SUFFIX_RE.match(core[len(bc):]):
            hits.append(b)
    return hits


def resolve_symbol(
    signal_symbol: str,
    broker_symbols: Optional[Iterable[str]],
    symbol_map: Optional[Dict[str, str]] = None,
    preferred_suffix: str = "",
) -> Tuple[Optional[str], str]:
    """Return (broker_ticker, reason). broker_ticker is None when unresolvable."""
    sig = (signal_symbol or "").strip()
    if not sig:
        return None, "empty symbol"

    symbol_map = symbol_map or {}

    # 1. explicit map - always wins, even over an exact match
    mapped = symbol_map.get(sig.upper())
    if mapped:
        if broker_symbols and mapped not in broker_symbols:
            return None, (f"SYMBOL_MAP points {sig} -> {mapped}, but the broker "
                          f"does not offer {mapped}")
        return mapped, f"mapped {sig} -> {mapped} via SYMBOL_MAP"

    # No symbol list available: pass through unchanged and let MT5 decide.
    if not broker_symbols:
        return sig, f"{sig} used as-is (broker symbol list unavailable)"

    broker = list(broker_symbols)
    by_upper = {b.upper(): b for b in broker}

    # 2/3. exact, then case-insensitive
    if sig in broker:
        return sig, f"{sig} matched exactly"
    if sig.upper() in by_upper:
        hit = by_upper[sig.upper()]
        return hit, f"{sig} matched {hit} (case-insensitive)"

    # 5. base + broker suffix
    cands = _suffix_candidates(sig, broker)
    resolved, reason = _pick(sig, cands, preferred_suffix, "suffix")
    if resolved or reason:
        return resolved, reason

    # 6. signal has a suffix the broker lacks
    cands = _strip_candidates(sig, broker)
    resolved, reason = _pick(sig, cands, preferred_suffix, "base")
    if resolved or reason:
        return resolved, reason

    # 4. built-in alias, then retry matching on the alias
    alias = BUILTIN_ALIASES.get(sig.upper())
    if alias:
        if alias in broker:
            return alias, f"{sig} resolved to {alias} via built-in alias"
        if alias.upper() in by_upper:
            hit = by_upper[alias.upper()]
            return hit, f"{sig} resolved to {hit} via built-in alias"
        cands = _suffix_candidates(alias, broker)
        resolved, reason = _pick(sig, cands, preferred_suffix,
                                 f"alias {alias} + suffix")
        if resolved or reason:
            return resolved, reason

    return None, (f"{sig} not offered by broker and no mapping found; "
                  f"add SYMBOL_MAP={sig.upper()}=<broker ticker>")


def _pick(sig: str, cands: List[str], preferred_suffix: str,
          how: str) -> Tuple[Optional[str], str]:
    """Choose among candidates. Returns (None, '') to mean 'keep looking'."""
    if not cands:
        return None, ""
    if len(cands) == 1:
        return cands[0], f"{sig} resolved to {cands[0]} via {how} match"

    # Multiple candidates - only a configured preference may break the tie.
    if preferred_suffix:
        want = [c for c in cands
                if c.upper().endswith(preferred_suffix.upper())]
        if len(want) == 1:
            return want[0], (f"{sig} resolved to {want[0]} via {how} match "
                             f"using SYMBOL_SUFFIX={preferred_suffix}")

    return None, (f"{sig} is ambiguous - broker offers {sorted(cands)}; "
                  f"set SYMBOL_SUFFIX or SYMBOL_MAP={sig.upper()}=<ticker> "
                  f"to choose")
