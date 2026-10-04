"""MT5 execution layer.

Two backends:
  native - the official MetaTrader5 python package (Windows + running terminal)
  http   - a REST bridge you host next to the terminal (works from Linux)

Both expose the same interface so the rest of the bot is backend-agnostic.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

from .budget import RequestBudget
from .sizing import SymbolSpec, compute_lot

log = logging.getLogger("sigbridge.mt5")


@dataclass
class OrderResult:
    ok: bool
    ticket: Optional[int]
    symbol: str
    direction: str
    lot: float
    entry: Optional[float]
    sl: float
    tp: Optional[float]
    detail: str = ""

    def audit(self) -> str:
        status = "PLACED" if self.ok else "FAILED"
        e = "market" if self.entry is None else f"{self.entry}"
        return (f"[{status}] {self.direction.upper()} {self.symbol} "
                f"lot={self.lot} entry={e} SL={self.sl} TP={self.tp} "
                f"ticket={self.ticket} {self.detail}".strip())


class Mt5Error(RuntimeError):
    pass


class BaseClient:
    def __init__(self, cfg, budget: RequestBudget):
        self.cfg, self.budget = cfg, budget
        self._symbols: Optional[set] = None
        self._spec_cache: dict = {}

    def symbols(self) -> Optional[set]:
        """Cached symbol list - fetched at most once per process run."""
        if self._symbols is None:
            try:
                self._symbols = self._fetch_symbols()
            except Exception as e:
                log.warning("symbol list unavailable (%s); skipping symbol check", e)
                self._symbols = set()
        return self._symbols or None

    def _fetch_symbols(self) -> set:
        raise NotImplementedError

    # --- sizing support ------------------------------------------------
    def balance(self) -> Optional[float]:
        """Account balance in account currency, or None if unavailable."""
        raise NotImplementedError

    def symbol_spec(self, symbol: str) -> Optional[SymbolSpec]:
        """Contract specs, cached per symbol for the process lifetime."""
        if symbol not in self._spec_cache:
            try:
                self._spec_cache[symbol] = self._fetch_spec(symbol)
            except Exception as e:
                log.warning("contract specs for %s unavailable: %s", symbol, e)
                self._spec_cache[symbol] = None
        return self._spec_cache[symbol]

    def _fetch_spec(self, symbol: str) -> Optional[SymbolSpec]:
        raise NotImplementedError

    def market_price(self, symbol: str, direction: str) -> Optional[float]:
        """Current fill-side price, used as the stop reference for market
        orders that carry no explicit entry."""
        raise NotImplementedError

    def open_volume(self) -> Optional[float]:
        """Total volume of all open positions AND pending orders, in lots.

        None means the figure could not be read - callers must treat that as
        "unknown" and refuse to add exposure rather than assuming zero.
        """
        raise NotImplementedError

    def size_for(self, sig) -> tuple:
        """Compute (lot, reason) for a parsed signal.

        SIZING_MODE=fixed -> always DEFAULT_LOT
        SIZING_MODE=risk  -> RISK_PERCENT of balance across the stop distance
        """
        if self.cfg.sizing_mode == "fixed":
            lot = min(self.cfg.default_lot, self.cfg.max_lot)
            note = f"fixed size {self.cfg.default_lot} lot"
            if lot < self.cfg.default_lot:
                note += f"; capped at MAX_LOT {self.cfg.max_lot}"
            return lot, note

        ref = float(sig.entry) if sig.entry is not None else \
            self.market_price(sig.symbol, sig.direction)
        sl_distance = abs(ref - float(sig.stop_loss)) if ref is not None else None
        return compute_lot(
            balance=self.balance(),
            risk_percent=self.cfg.risk_percent,
            sl_distance=sl_distance,
            spec=self.symbol_spec(sig.symbol),
            fallback_lot=self.cfg.fallback_lot,
            max_lot=self.cfg.max_lot,
        )

    def place(self, sig, lot: float, tp: Optional[Decimal]) -> OrderResult:
        raise NotImplementedError


class NativeClient(BaseClient):
    def __init__(self, cfg, budget):
        super().__init__(cfg, budget)
        import MetaTrader5 as mt5  # noqa: N813
        self.mt5 = mt5
        self.budget.spend(1, critical=True)
        if not mt5.initialize(login=int(cfg.mt5_login), password=cfg.mt5_password,
                              server=cfg.mt5_server):
            raise Mt5Error(f"MT5 initialize failed: {mt5.last_error()}")
        info = mt5.account_info()
        if info is None:
            raise Mt5Error("MT5 connected but account_info() returned None")
        log.info("MT5 connected: account=%s server=%s balance=%s",
                 info.login, info.server, info.balance)

    def _fetch_symbols(self) -> set:
        self.budget.spend(1)
        return {s.name for s in (self.mt5.symbols_get() or ())}

    def balance(self) -> Optional[float]:
        try:
            self.budget.spend(1, critical=True)
            info = self.mt5.account_info()
            return float(info.balance) if info else None
        except Exception as e:
            log.warning("account_info failed: %s", e)
            return None

    def _fetch_spec(self, symbol: str) -> Optional[SymbolSpec]:
        self.budget.spend(1, critical=True)
        if not self.mt5.symbol_select(symbol, True):
            return None
        self.budget.spend(1, critical=True)
        si = self.mt5.symbol_info(symbol)
        if si is None:
            return None
        return SymbolSpec(
            name=symbol,
            tick_value=float(si.trade_tick_value),
            tick_size=float(si.trade_tick_size or si.point),
            volume_min=float(si.volume_min),
            volume_max=float(si.volume_max),
            volume_step=float(si.volume_step),
            digits=int(si.digits),
        )

    def market_price(self, symbol: str, direction: str) -> Optional[float]:
        try:
            self.budget.spend(1, critical=True)
            tick = self.mt5.symbol_info_tick(symbol)
            if tick is None:
                return None
            return float(tick.ask if direction == "buy" else tick.bid)
        except Exception as e:
            log.warning("tick fetch failed for %s: %s", symbol, e)
            return None

    def open_volume(self) -> Optional[float]:
        try:
            self.budget.spend(1, critical=True)
            positions = self.mt5.positions_get()
            self.budget.spend(1, critical=True)
            pending = self.mt5.orders_get()
            if positions is None and pending is None:
                return None
            total = sum(float(p.volume) for p in (positions or ()))
            total += sum(float(o.volume_current) for o in (pending or ()))
            return round(total, 2)
        except Exception as e:
            log.warning("open volume fetch failed: %s", e)
            return None

    def place(self, sig, lot, tp):
        mt5 = self.mt5
        self.budget.spend(1, critical=True)
        if not mt5.symbol_select(sig.symbol, True):
            raise Mt5Error(f"cannot select symbol {sig.symbol}")
        self.budget.spend(1, critical=True)
        tick = mt5.symbol_info_tick(sig.symbol)
        if tick is None:
            raise Mt5Error(f"no tick data for {sig.symbol}")

        is_buy = sig.direction == "buy"
        if sig.order_kind == "market":
            otype = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
            price = tick.ask if is_buy else tick.bid
            action = mt5.TRADE_ACTION_DEAL
        else:
            price = float(sig.entry)
            action = mt5.TRADE_ACTION_PENDING
            if sig.order_kind == "limit":
                otype = mt5.ORDER_TYPE_BUY_LIMIT if is_buy else mt5.ORDER_TYPE_SELL_LIMIT
            else:
                otype = mt5.ORDER_TYPE_BUY_STOP if is_buy else mt5.ORDER_TYPE_SELL_STOP

        req = {
            "action": action, "symbol": sig.symbol, "volume": float(lot),
            "type": otype, "price": float(price), "sl": float(sig.stop_loss),
            "deviation": 20, "magic": 770077,
            "comment": "tg-signal",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if tp is not None:
            req["tp"] = float(tp)

        if self.cfg.dry_run:
            return OrderResult(True, None, sig.symbol, sig.direction, lot,
                               None if sig.order_kind == "market" else float(sig.entry),
                               float(sig.stop_loss), float(tp) if tp else None,
                               "DRY_RUN - not sent")

        self.budget.spend(1, critical=True)
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            detail = f"retcode={getattr(res, 'retcode', None)} {getattr(res, 'comment', mt5.last_error())}"
            return OrderResult(False, None, sig.symbol, sig.direction, lot,
                               req.get("price"), req["sl"], req.get("tp"), detail)
        return OrderResult(True, res.order, sig.symbol, sig.direction, lot,
                           req["price"], req["sl"], req.get("tp"), "")


class HttpClient(BaseClient):
    """Talks to a REST bridge. Expects POST /order and GET /symbols."""

    def _call(self, path: str, payload: Optional[dict] = None, critical=False):
        url = self.cfg.mt5_http_url.rstrip("/") + path
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json",
                     "X-MT5-Login": self.cfg.mt5_login,
                     "X-MT5-Password": self.cfg.mt5_password,
                     "X-MT5-Server": self.cfg.mt5_server},
            method="POST" if data else "GET")
        self.budget.spend(1, critical=critical)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def _fetch_symbols(self) -> set:
        return set(self._call("/symbols").get("symbols", []))

    def balance(self) -> Optional[float]:
        try:
            r = self._call("/account", critical=True)
            b = r.get("balance")
            return float(b) if b is not None else None
        except Exception as e:
            log.warning("account fetch failed: %s", e)
            return None

    def _fetch_spec(self, symbol: str) -> Optional[SymbolSpec]:
        r = self._call(f"/spec?symbol={symbol}", critical=True)
        if not r.get("ok"):
            return None
        return SymbolSpec(
            name=symbol,
            tick_value=float(r["tick_value"]),
            tick_size=float(r["tick_size"]),
            volume_min=float(r["volume_min"]),
            volume_max=float(r["volume_max"]),
            volume_step=float(r["volume_step"]),
            digits=int(r.get("digits", 5)),
        )

    def market_price(self, symbol: str, direction: str) -> Optional[float]:
        try:
            r = self._call(f"/price?symbol={symbol}", critical=True)
            return float(r["ask"] if direction == "buy" else r["bid"])
        except Exception as e:
            log.warning("price fetch failed for %s: %s", symbol, e)
            return None

    def open_volume(self) -> Optional[float]:
        try:
            r = self._call("/exposure", critical=True)
            v = r.get("open_volume")
            return float(v) if v is not None else None
        except Exception as e:
            log.warning("exposure fetch failed: %s", e)
            return None

    def place(self, sig, lot, tp):
        payload = {
            "symbol": sig.symbol, "direction": sig.direction,
            "order_kind": sig.order_kind,
            "entry": float(sig.entry) if sig.entry is not None else None,
            "sl": float(sig.stop_loss),
            "tp": float(tp) if tp is not None else None,
            "volume": float(lot), "magic": 770077, "comment": "tg-signal",
        }
        if self.cfg.dry_run:
            return OrderResult(True, None, sig.symbol, sig.direction, lot,
                               payload["entry"], payload["sl"], payload["tp"],
                               "DRY_RUN - not sent")
        r = self._call("/order", payload, critical=True)
        return OrderResult(bool(r.get("ok")), r.get("ticket"), sig.symbol,
                           sig.direction, lot, payload["entry"], payload["sl"],
                           payload["tp"], str(r.get("detail", "")))


def make_client(cfg, budget) -> BaseClient:
    return NativeClient(cfg, budget) if cfg.mt5_mode == "native" else HttpClient(cfg, budget)


def split_lots(total: float, n: int, step: float = 0.01) -> List[float]:
    """Split total volume across n TPs, respecting the broker volume step.
    Falls back to a single position when the size cannot be split."""
    if n <= 1:
        return [round(total, 2)]
    units = int(round(total / step))
    if units < n:
        return [round(total, 2)]          # too small to split -> nearest TP only
    base, extra = divmod(units, n)
    return [round((base + (1 if i < extra else 0)) * step, 2) for i in range(n)]
