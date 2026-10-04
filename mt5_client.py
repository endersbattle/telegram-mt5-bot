"""MT5 execution layer with fail-closed validation and idempotent reconciliation."""
from __future__ import annotations

import json
import logging
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Optional

from .budget import RequestBudget
from .sizing import SymbolSpec, SizingError, compute_lot, floor_to_step

log = logging.getLogger("sigbridge.mt5")


@dataclass
class AccountInfo:
    login: Optional[int]
    server: str
    balance: float
    equity: float
    margin_free: float
    trade_mode: Optional[int] = None


@dataclass
class OrderResult:
    state: str
    ticket: Optional[int]
    symbol: str
    direction: str
    lot: float
    entry: Optional[float]
    sl: float
    tp: Optional[float]
    detail: str = ""
    retcode: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.state == "ACCEPTED"

    def audit(self) -> str:
        e = "market" if self.entry is None else f"{self.entry}"
        return (
            f"[{self.state}] {self.direction.upper()} {self.symbol} "
            f"lot={self.lot} entry={e} SL={self.sl} TP={self.tp} "
            f"ticket={self.ticket} retcode={self.retcode} {self.detail}"
        ).strip()


class Mt5Error(RuntimeError):
    pass


class BaseClient:
    def __init__(self, cfg, budget: RequestBudget):
        self.cfg, self.budget = cfg, budget
        self._symbols: Optional[set] = None
        self._spec_cache: dict[str, Optional[SymbolSpec]] = {}

    def symbols(self) -> Optional[set]:
        if self._symbols is None:
            try:
                syms = self._fetch_symbols()
                self._symbols = set(syms) if syms else set()
            except Exception as e:
                log.error("symbol list unavailable: %s", e)
                return None
        return self._symbols or None

    def _fetch_symbols(self) -> set:
        raise NotImplementedError

    def account_info(self) -> Optional[AccountInfo]:
        raise NotImplementedError

    def symbol_spec(self, symbol: str) -> Optional[SymbolSpec]:
        if symbol not in self._spec_cache:
            try:
                self._spec_cache[symbol] = self._fetch_spec(symbol)
            except Exception as e:
                log.error("contract specs for %s unavailable: %s", symbol, e)
                self._spec_cache[symbol] = None
        return self._spec_cache[symbol]

    def _fetch_spec(self, symbol: str) -> Optional[SymbolSpec]:
        raise NotImplementedError

    def market_price(self, symbol: str, direction: str) -> Optional[float]:
        raise NotImplementedError

    def loss_per_lot(self, symbol: str, direction: str, entry: float, sl: float) -> Optional[float]:
        raise NotImplementedError

    def open_volume(self) -> Optional[float]:
        raise NotImplementedError

    def open_risk(self) -> Optional[float]:
        """Current worst-case loss to attached stop losses, in account currency.

        None means risk cannot be measured safely (for example an exposure has
        no stop), so callers must refuse to add new exposure.
        """
        raise NotImplementedError

    def size_for(self, sig) -> tuple[float, str]:
        spec = self.symbol_spec(sig.symbol)
        if spec is None:
            raise SizingError("broker contract specs unavailable")

        if self.cfg.sizing_mode == "fixed":
            lot = min(self.cfg.default_lot, self.cfg.max_lot, spec.volume_max)
            lot = floor_to_step(lot, spec.volume_step)
            if lot < spec.volume_min:
                raise SizingError("fixed lot is below broker minimum")
            return lot, f"fixed size {lot} lot"

        if sig.order_kind == "market":
            ref = self.market_price(sig.symbol, sig.direction)
            if ref is None:
                raise SizingError("live market price unavailable")
            if sig.entry is not None and self.cfg.max_reference_deviation_pct > 0:
                advertised = float(sig.entry)
                pct = abs(ref - advertised) / advertised * 100.0
                if pct > self.cfg.max_reference_deviation_pct:
                    raise SizingError(
                        f"market moved {pct:.4f}% from advertised entry; "
                        f"limit is {self.cfg.max_reference_deviation_pct}%"
                    )
        else:
            if sig.entry is None:
                raise SizingError("pending order has no entry")
            ref = float(sig.entry)

        info = self.account_info()
        if info is None:
            raise SizingError("account info unavailable")
        loss = self.loss_per_lot(sig.symbol, sig.direction, ref, float(sig.stop_loss))
        return compute_lot(
            equity=info.equity,
            risk_percent=self.cfg.risk_percent,
            loss_per_lot=loss,
            spec=spec,
            max_lot=self.cfg.max_lot,
        )

    def place(self, sig, lot: float, tp: Optional[Decimal], idempotency_key: str) -> OrderResult:
        raise NotImplementedError

    def reconcile(self, idempotency_key: str) -> OrderResult:
        raise NotImplementedError


class NativeClient(BaseClient):
    def __init__(self, cfg, budget):
        super().__init__(cfg, budget)
        import MetaTrader5 as mt5  # noqa: N813

        self.mt5 = mt5
        self.budget.spend(1, critical=True)
        if not mt5.initialize(
            login=int(cfg.mt5_login), password=cfg.mt5_password, server=cfg.mt5_server
        ):
            raise Mt5Error(f"MT5 initialize failed: {mt5.last_error()}")
        info = self.account_info()
        if info is None:
            raise Mt5Error("MT5 connected but account_info() returned None")
        real_mode = getattr(mt5, "ACCOUNT_TRADE_MODE_REAL", 2)
        if not cfg.dry_run and info.trade_mode == real_mode and not cfg.allow_real_account:
            raise Mt5Error(
                "real account detected; set ALLOW_REAL_ACCOUNT=true deliberately to trade it"
            )
        log.info(
            "MT5 connected: account=%s server=%s balance=%s equity=%s",
            info.login, info.server, info.balance, info.equity,
        )

    def _fetch_symbols(self) -> set:
        self.budget.spend(1)
        rows = self.mt5.symbols_get()
        if rows is None:
            raise Mt5Error(f"symbols_get failed: {self.mt5.last_error()}")
        return {s.name for s in rows}

    def account_info(self) -> Optional[AccountInfo]:
        try:
            self.budget.spend(1, critical=True)
            info = self.mt5.account_info()
            if info is None:
                return None
            return AccountInfo(
                int(info.login), str(info.server), float(info.balance), float(info.equity),
                float(info.margin_free), int(getattr(info, "trade_mode", 0)),
            )
        except Exception as e:
            log.error("account_info failed: %s", e)
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
            point=float(si.point),
            trade_stops_level=int(getattr(si, "trade_stops_level", 0)),
            trade_freeze_level=int(getattr(si, "trade_freeze_level", 0)),
            filling_mode=int(getattr(si, "filling_mode", 0)),
            trade_exemode=int(getattr(si, "trade_exemode", 0)),
            order_mode=int(getattr(si, "order_mode", 0)),
            trade_mode=int(getattr(si, "trade_mode", 0)),
        )

    def market_price(self, symbol: str, direction: str) -> Optional[float]:
        self.budget.spend(1, critical=True)
        tick = self.mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        return float(tick.ask if direction == "buy" else tick.bid)

    def loss_per_lot(self, symbol: str, direction: str, entry: float, sl: float) -> Optional[float]:
        try:
            order_type = self.mt5.ORDER_TYPE_BUY if direction == "buy" else self.mt5.ORDER_TYPE_SELL
            self.budget.spend(1, critical=True)
            value = self.mt5.order_calc_profit(order_type, symbol, 1.0, float(entry), float(sl))
            if value is None:
                return None
            return abs(float(value))
        except Exception as e:
            log.error("order_calc_profit failed: %s", e)
            return None

    def open_volume(self) -> Optional[float]:
        try:
            self.budget.spend(1, critical=True)
            positions = self.mt5.positions_get()
            self.budget.spend(1, critical=True)
            pending = self.mt5.orders_get()
            if positions is None and pending is None:
                return None
            return round(
                sum(float(p.volume) for p in (positions or ()))
                + sum(float(o.volume_current) for o in (pending or ())),
                8,
            )
        except Exception as e:
            log.error("open volume fetch failed: %s", e)
            return None

    def open_risk(self) -> Optional[float]:
        try:
            self.budget.spend(1, critical=True)
            positions = self.mt5.positions_get()
            self.budget.spend(1, critical=True)
            pending = self.mt5.orders_get()
            if positions is None and pending is None:
                return None
            total = 0.0
            buy_pos = getattr(self.mt5, "POSITION_TYPE_BUY", 0)
            buy_order_types = {
                getattr(self.mt5, "ORDER_TYPE_BUY", 0),
                getattr(self.mt5, "ORDER_TYPE_BUY_LIMIT", 2),
                getattr(self.mt5, "ORDER_TYPE_BUY_STOP", 4),
                getattr(self.mt5, "ORDER_TYPE_BUY_STOP_LIMIT", 6),
            }
            for p in positions or ():
                sl = float(getattr(p, "sl", 0.0) or 0.0)
                if sl <= 0:
                    return None
                direction = "buy" if int(getattr(p, "type", -1)) == buy_pos else "sell"
                loss = self.loss_per_lot(
                    str(p.symbol), direction, float(p.price_open), sl
                )
                if loss is None:
                    return None
                total += loss * float(p.volume)
            for o in pending or ():
                sl = float(getattr(o, "sl", 0.0) or 0.0)
                if sl <= 0:
                    return None
                direction = "buy" if int(getattr(o, "type", -1)) in buy_order_types else "sell"
                loss = self.loss_per_lot(
                    str(o.symbol), direction, float(o.price_open), sl
                )
                if loss is None:
                    return None
                total += loss * float(o.volume_current)
            return float(total)
        except Exception as e:
            log.error("open risk fetch failed: %s", e)
            return None

    def _choose_filling(self, spec: SymbolSpec, order_kind: str) -> int:
        mt5 = self.mt5
        if order_kind != "market":
            return mt5.ORDER_FILLING_RETURN
        market_exec = getattr(mt5, "SYMBOL_TRADE_EXECUTION_MARKET", 2)
        if spec.trade_exemode != market_exec:
            return mt5.ORDER_FILLING_RETURN
        ioc_flag = getattr(mt5, "SYMBOL_FILLING_IOC", 2)
        fok_flag = getattr(mt5, "SYMBOL_FILLING_FOK", 1)
        if spec.filling_mode & ioc_flag:
            return mt5.ORDER_FILLING_IOC
        if spec.filling_mode & fok_flag:
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_FOK

    def _validate_order(self, sig, lot: float, tp: Optional[Decimal]):
        mt5 = self.mt5
        spec = self.symbol_spec(sig.symbol)
        if spec is None or not spec.valid():
            raise Mt5Error("invalid or missing symbol specification")

        lot = floor_to_step(float(lot), spec.volume_step)
        if lot < spec.volume_min or lot > min(spec.volume_max, self.cfg.max_lot):
            raise Mt5Error(f"volume {lot} outside allowed range")

        self.budget.spend(1, critical=True)
        tick = mt5.symbol_info_tick(sig.symbol)
        if tick is None:
            raise Mt5Error(f"no tick data for {sig.symbol}")
        ask, bid = float(tick.ask), float(tick.bid)
        is_buy = sig.direction == "buy"

        if sig.order_kind == "market":
            price = ask if is_buy else bid
            action = mt5.TRADE_ACTION_DEAL
            otype = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        else:
            if sig.entry is None:
                raise Mt5Error("pending order requires an entry")
            price = float(sig.entry)
            action = mt5.TRADE_ACTION_PENDING
            if sig.order_kind == "limit":
                otype = mt5.ORDER_TYPE_BUY_LIMIT if is_buy else mt5.ORDER_TYPE_SELL_LIMIT
                if is_buy and not price < ask:
                    raise Mt5Error("buy limit entry must be below current ask")
                if not is_buy and not price > bid:
                    raise Mt5Error("sell limit entry must be above current bid")
            else:
                otype = mt5.ORDER_TYPE_BUY_STOP if is_buy else mt5.ORDER_TYPE_SELL_STOP
                if is_buy and not price > ask:
                    raise Mt5Error("buy stop entry must be above current ask")
                if not is_buy and not price < bid:
                    raise Mt5Error("sell stop entry must be below current bid")

        sl = float(sig.stop_loss)
        tpv = float(tp) if tp is not None else None
        if is_buy:
            if sl >= price:
                raise Mt5Error("buy stop loss must be below executable entry")
            if tpv is not None and tpv <= price:
                raise Mt5Error("buy take profit must be above executable entry")
        else:
            if sl <= price:
                raise Mt5Error("sell stop loss must be above executable entry")
            if tpv is not None and tpv >= price:
                raise Mt5Error("sell take profit must be below executable entry")

        min_dist = max(0, spec.trade_stops_level) * (spec.point or spec.tick_size)
        if min_dist:
            if abs(price - sl) + 1e-12 < min_dist:
                raise Mt5Error(f"stop loss is inside broker minimum stop distance {min_dist}")
            if tpv is not None and abs(tpv - price) + 1e-12 < min_dist:
                raise Mt5Error(f"take profit is inside broker minimum stop distance {min_dist}")
            if sig.order_kind != "market":
                market_ref = ask if is_buy else bid
                if abs(price - market_ref) + 1e-12 < min_dist:
                    raise Mt5Error(f"pending entry is inside broker minimum stop distance {min_dist}")

        return spec, lot, price, action, otype, sl, tpv

    def place(self, sig, lot, tp, idempotency_key):
        mt5 = self.mt5
        self.budget.spend(1, critical=True)
        if not mt5.symbol_select(sig.symbol, True):
            raise Mt5Error(f"cannot select symbol {sig.symbol}")

        spec, lot, price, action, otype, sl, tpv = self._validate_order(sig, lot, tp)
        comment = "tg:" + idempotency_key[:20]
        req = {
            "action": action,
            "symbol": sig.symbol,
            "volume": float(lot),
            "type": otype,
            "price": float(price),
            "sl": sl,
            "deviation": self.cfg.deviation_points,
            "magic": self.cfg.magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._choose_filling(spec, sig.order_kind),
        }
        if tpv is not None:
            req["tp"] = tpv

        if self.cfg.dry_run:
            return OrderResult(
                "ACCEPTED", None, sig.symbol, sig.direction, lot,
                None if sig.order_kind == "market" else price, sl, tpv,
                "DRY_RUN - not sent",
            )

        self.budget.spend(1, critical=True)
        check = mt5.order_check(req)
        if check is None or int(getattr(check, "retcode", -1)) not in (0, getattr(mt5, "TRADE_RETCODE_DONE", 10009)):
            return OrderResult(
                "REJECTED", None, sig.symbol, sig.direction, lot, price, sl, tpv,
                f"order_check retcode={getattr(check, 'retcode', None)} "
                f"{getattr(check, 'comment', mt5.last_error())}",
                getattr(check, "retcode", None),
            )

        try:
            self.budget.spend(1, critical=True)
            res = mt5.order_send(req)
        except Exception as e:
            return OrderResult(
                "UNKNOWN", None, sig.symbol, sig.direction, lot, price, sl, tpv,
                f"order_send raised after submission attempt: {e}",
            )

        ret = getattr(res, "retcode", None)
        accepted = {
            getattr(mt5, "TRADE_RETCODE_PLACED", 10008),
            getattr(mt5, "TRADE_RETCODE_DONE", 10009),
            getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010),
        }
        ambiguous = {
            getattr(mt5, "TRADE_RETCODE_TIMEOUT", 10012),
            getattr(mt5, "TRADE_RETCODE_CONNECTION", 10031),
        }
        detail = f"retcode={ret} {getattr(res, 'comment', mt5.last_error())}"
        if res is not None and ret in accepted:
            ticket = int(getattr(res, "order", 0) or getattr(res, "deal", 0) or 0) or None
            return OrderResult("ACCEPTED", ticket, sig.symbol, sig.direction, lot, price, sl, tpv, detail, ret)
        if res is None or ret in ambiguous:
            return OrderResult("UNKNOWN", None, sig.symbol, sig.direction, lot, price, sl, tpv, detail, ret)
        return OrderResult("REJECTED", None, sig.symbol, sig.direction, lot, price, sl, tpv, detail, ret)

    def reconcile(self, idempotency_key: str) -> OrderResult:
        comment = "tg:" + idempotency_key[:20]
        try:
            self.budget.spend(1, critical=True)
            for row in (self.mt5.orders_get() or ()):
                if str(getattr(row, "comment", "")) == comment:
                    return OrderResult("ACCEPTED", int(row.ticket), row.symbol, "", float(row.volume_current),
                                       float(row.price_open), float(row.sl), float(row.tp) if row.tp else None,
                                       "reconciled from open order")
            self.budget.spend(1, critical=True)
            for row in (self.mt5.positions_get() or ()):
                if str(getattr(row, "comment", "")) == comment:
                    return OrderResult("ACCEPTED", int(row.ticket), row.symbol, "", float(row.volume),
                                       float(row.price_open), float(row.sl), float(row.tp) if row.tp else None,
                                       "reconciled from open position")
            start = datetime.now() - timedelta(days=7)
            end = datetime.now() + timedelta(minutes=1)
            self.budget.spend(1, critical=True)
            for row in (self.mt5.history_orders_get(start, end) or ()):
                if str(getattr(row, "comment", "")) == comment:
                    return OrderResult("ACCEPTED", int(row.ticket), row.symbol, "", float(row.volume_initial),
                                       float(row.price_open), float(row.sl), float(row.tp) if row.tp else None,
                                       "reconciled from order history")
        except Exception as e:
            return OrderResult("UNKNOWN", None, "", "", 0.0, None, 0.0, None, f"reconciliation failed: {e}")
        return OrderResult("UNKNOWN", None, "", "", 0.0, None, 0.0, None, "no matching broker record found")


class HttpClient(BaseClient):
    def _call(self, path: str, payload: Optional[dict] = None, critical: bool = False):
        url = self.cfg.mt5_http_url.rstrip("/") + path
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Bridge-Secret": self.cfg.bridge_secret,
            },
            method="POST" if data is not None else "GET",
        )
        self.budget.spend(1, critical=critical)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def _fetch_symbols(self) -> set:
        r = self._call("/symbols")
        if not r.get("ok", True):
            raise Mt5Error(str(r.get("detail", "symbols unavailable")))
        return set(r.get("symbols", []))

    def account_info(self) -> Optional[AccountInfo]:
        try:
            r = self._call("/account", critical=True)
            if not r.get("ok"):
                return None
            info = AccountInfo(
                r.get("login"), str(r.get("server", "")), float(r["balance"]),
                float(r["equity"]), float(r.get("margin_free", 0.0)),
                int(r["trade_mode"]) if r.get("trade_mode") is not None else None,
            )
            if not self.cfg.dry_run and bool(r.get("is_real")) and not self.cfg.allow_real_account:
                raise Mt5Error(
                    "real account detected; set ALLOW_REAL_ACCOUNT=true deliberately to trade it"
                )
            return info
        except Mt5Error:
            raise
        except Exception as e:
            log.error("account fetch failed: %s", e)
            return None

    def _fetch_spec(self, symbol: str) -> Optional[SymbolSpec]:
        r = self._call("/spec?symbol=" + urllib.parse.quote(symbol), critical=True)
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
            point=float(r.get("point", 0.0)),
            trade_stops_level=int(r.get("trade_stops_level", 0)),
            trade_freeze_level=int(r.get("trade_freeze_level", 0)),
            filling_mode=int(r.get("filling_mode", 0)),
            trade_exemode=int(r.get("trade_exemode", 0)),
            order_mode=int(r.get("order_mode", 0)),
            trade_mode=int(r.get("trade_mode", 0)),
        )

    def market_price(self, symbol: str, direction: str) -> Optional[float]:
        r = self._call("/price?symbol=" + urllib.parse.quote(symbol), critical=True)
        if not r.get("ok"):
            return None
        return float(r["ask"] if direction == "buy" else r["bid"])

    def loss_per_lot(self, symbol: str, direction: str, entry: float, sl: float) -> Optional[float]:
        q = urllib.parse.urlencode(
            {"symbol": symbol, "direction": direction, "entry": entry, "sl": sl}
        )
        r = self._call("/riskloss?" + q, critical=True)
        return float(r["loss_per_lot"]) if r.get("ok") and r.get("loss_per_lot") is not None else None

    def open_volume(self) -> Optional[float]:
        try:
            r = self._call("/exposure", critical=True)
            return float(r["open_volume"]) if r.get("ok") else None
        except Exception as e:
            log.error("exposure fetch failed: %s", e)
            return None

    def open_risk(self) -> Optional[float]:
        try:
            r = self._call("/risk", critical=True)
            return float(r["open_risk"]) if r.get("ok") and r.get("open_risk") is not None else None
        except Exception as e:
            log.error("open risk fetch failed: %s", e)
            return None

    def place(self, sig, lot, tp, idempotency_key):
        payload = {
            "idempotency_key": idempotency_key,
            "symbol": sig.symbol,
            "direction": sig.direction,
            "order_kind": sig.order_kind,
            "entry": float(sig.entry) if sig.entry is not None else None,
            "sl": float(sig.stop_loss),
            "tp": float(tp) if tp is not None else None,
            "volume": float(lot),
            "magic": self.cfg.magic,
            "deviation": self.cfg.deviation_points,
        }
        if self.cfg.dry_run:
            return OrderResult(
                "ACCEPTED", None, sig.symbol, sig.direction, lot, payload["entry"],
                payload["sl"], payload["tp"], "DRY_RUN - not sent",
            )
        try:
            r = self._call("/order", payload, critical=True)
        except (TimeoutError, urllib.error.URLError, ConnectionError) as e:
            return OrderResult(
                "UNKNOWN", None, sig.symbol, sig.direction, lot, payload["entry"],
                payload["sl"], payload["tp"], f"bridge response ambiguous: {e}",
            )
        state = str(r.get("state") or ("ACCEPTED" if r.get("ok") else "REJECTED"))
        return OrderResult(
            state, r.get("ticket"), sig.symbol, sig.direction, lot, payload["entry"],
            payload["sl"], payload["tp"], str(r.get("detail", "")), r.get("retcode"),
        )

    def reconcile(self, idempotency_key: str) -> OrderResult:
        try:
            r = self._call(
                "/reconcile?idempotency_key=" + urllib.parse.quote(idempotency_key),
                critical=True,
            )
        except Exception as e:
            return OrderResult("UNKNOWN", None, "", "", 0.0, None, 0.0, None, f"reconcile failed: {e}")
        return OrderResult(
            str(r.get("state", "UNKNOWN")), r.get("ticket"), "", "", 0.0, None, 0.0, None,
            str(r.get("detail", "")), r.get("retcode"),
        )


def make_client(cfg, budget) -> BaseClient:
    return NativeClient(cfg, budget) if cfg.mt5_mode == "native" else HttpClient(cfg, budget)


def split_lots(total: float, n: int, step: float = 0.01, minimum: float = 0.01) -> List[float]:
    if n <= 1:
        return [floor_to_step(total, step)]
    units = int(math.floor(round(total / step, 10)))
    min_units = int(math.ceil(minimum / step - 1e-9))
    if units < n * min_units:
        single = floor_to_step(total, step)
        return [single] if single >= minimum else []
    base, extra = divmod(units, n)
    parts = [(base + (1 if i < extra else 0)) * step for i in range(n)]
    return [round(x, 8) for x in parts]
