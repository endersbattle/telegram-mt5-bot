"""Authenticated Windows-side MT5 bridge.

The bridge owns MT5 credentials locally. Remote clients authenticate only with
BRIDGE_SECRET. Keep it on localhost unless ALLOW_NONLOCAL_BRIDGE=true is set
explicitly and a trusted network layer protects it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import MetaTrader5 as mt5  # noqa: N813

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("bridge")

HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1").strip()
PORT = int(os.environ.get("BRIDGE_PORT", "8765"))
SECRET = os.environ.get("BRIDGE_SECRET", "").strip()
LOGIN = os.environ.get("MT5_LOGIN", "").strip()
PASSWORD = os.environ.get("MT5_PASSWORD", "")
SERVER = os.environ.get("MT5_SERVER", "").strip()
STATE_DB = os.environ.get("BRIDGE_STATE_DB", ".bridge_state.sqlite3").strip()
ALLOW_NONLOCAL = os.environ.get("ALLOW_NONLOCAL_BRIDGE", "").strip().lower() in {"1", "true", "yes", "on"}

if not SECRET:
    raise RuntimeError("BRIDGE_SECRET is required")
if not LOGIN or not PASSWORD or not SERVER:
    raise RuntimeError("MT5_LOGIN, MT5_PASSWORD and MT5_SERVER are required on the bridge host")
if HOST not in {"127.0.0.1", "localhost", "::1"} and not ALLOW_NONLOCAL:
    raise RuntimeError(
        "refusing non-loopback BRIDGE_HOST without ALLOW_NONLOCAL_BRIDGE=true"
    )

if not mt5.initialize(login=int(LOGIN), password=PASSWORD, server=SERVER):
    raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")

DB = sqlite3.connect(STATE_DB, timeout=30, check_same_thread=False)
DB.row_factory = sqlite3.Row
with DB:
    DB.execute("PRAGMA journal_mode=WAL")
    DB.execute("PRAGMA synchronous=FULL")
    DB.execute("""
        CREATE TABLE IF NOT EXISTS orders(
            idempotency_key TEXT PRIMARY KEY,
            comment TEXT NOT NULL,
            status TEXT NOT NULL,
            ticket INTEGER,
            retcode INTEGER,
            detail TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
        )
    """)

KIND = {
    ("buy", "market"): mt5.ORDER_TYPE_BUY,
    ("sell", "market"): mt5.ORDER_TYPE_SELL,
    ("buy", "limit"): mt5.ORDER_TYPE_BUY_LIMIT,
    ("sell", "limit"): mt5.ORDER_TYPE_SELL_LIMIT,
    ("buy", "stop"): mt5.ORDER_TYPE_BUY_STOP,
    ("sell", "stop"): mt5.ORDER_TYPE_SELL_STOP,
}


def _comment(key: str) -> str:
    digest = hashlib.sha256(key.encode()).hexdigest()[:20]
    return "tg:" + digest


def _choose_filling(si, order_kind: str) -> int:
    if order_kind != "market":
        return mt5.ORDER_FILLING_RETURN
    market_exec = getattr(mt5, "SYMBOL_TRADE_EXECUTION_MARKET", 2)
    if int(getattr(si, "trade_exemode", 0)) != market_exec:
        return mt5.ORDER_FILLING_RETURN
    filling = int(getattr(si, "filling_mode", 0))
    if filling & getattr(mt5, "SYMBOL_FILLING_IOC", 2):
        return mt5.ORDER_FILLING_IOC
    if filling & getattr(mt5, "SYMBOL_FILLING_FOK", 1):
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_FOK


def _classify(res):
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
    if res is not None and ret in accepted:
        return "ACCEPTED"
    if res is None or ret in ambiguous:
        return "UNKNOWN"
    return "REJECTED"


def _record(key: str, comment: str, status: str, ticket=None, retcode=None, detail=""):
    with DB:
        DB.execute(
            """INSERT INTO orders(idempotency_key,comment,status,ticket,retcode,detail,updated_at)
               VALUES(?,?,?,?,?,?,strftime('%s','now'))
               ON CONFLICT(idempotency_key) DO UPDATE SET
                 status=excluded.status,
                 ticket=COALESCE(excluded.ticket,orders.ticket),
                 retcode=excluded.retcode,
                 detail=excluded.detail,
                 updated_at=excluded.updated_at""",
            (key, comment, status, ticket, retcode, detail[:1000]),
        )


def _row(key: str):
    return DB.execute("SELECT * FROM orders WHERE idempotency_key=?", (key,)).fetchone()


def _find_by_comment(comment: str):
    for getter in (mt5.orders_get, mt5.positions_get):
        rows = getter()
        for row in rows or ():
            if str(getattr(row, "comment", "")) == comment:
                ticket = int(getattr(row, "ticket", 0) or 0) or None
                return ticket
    start = datetime.now() - timedelta(days=7)
    end = datetime.now() + timedelta(minutes=1)
    for row in mt5.history_orders_get(start, end) or ():
        if str(getattr(row, "comment", "")) == comment:
            return int(getattr(row, "ticket", 0) or 0) or None
    return None


def _reconcile(key: str) -> dict:
    row = _row(key)
    if row and row["status"] in {"ACCEPTED", "REJECTED"}:
        return {
            "ok": row["status"] == "ACCEPTED",
            "state": row["status"],
            "ticket": row["ticket"],
            "retcode": row["retcode"],
            "detail": row["detail"],
        }
    comment = row["comment"] if row else _comment(key)
    ticket = _find_by_comment(comment)
    if ticket is not None:
        _record(key, comment, "ACCEPTED", ticket=ticket, detail="reconciled from MT5")
        return {"ok": True, "state": "ACCEPTED", "ticket": ticket, "detail": "reconciled from MT5"}
    return {"ok": False, "state": "UNKNOWN", "ticket": None, "detail": "no matching MT5 record found"}


def _spec(symbol: str):
    if not mt5.symbol_select(symbol, True):
        return None
    return mt5.symbol_info(symbol)


def _build_order(req: dict):
    symbol = str(req["symbol"])
    direction = str(req["direction"])
    order_kind = str(req["order_kind"])
    if (direction, order_kind) not in KIND:
        raise ValueError("unsupported direction/order_kind")

    si = _spec(symbol)
    if si is None:
        raise ValueError(f"cannot select {symbol}")
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise ValueError(f"no tick for {symbol}")

    volume = float(req["volume"])
    step = float(si.volume_step)
    volume = (int((volume / step) + 1e-9)) * step
    volume = round(volume, 8)
    if volume < float(si.volume_min) or volume > float(si.volume_max):
        raise ValueError("volume outside broker limits")

    is_buy = direction == "buy"
    if order_kind == "market":
        price = float(tick.ask if is_buy else tick.bid)
        action = mt5.TRADE_ACTION_DEAL
    else:
        if req.get("entry") is None:
            raise ValueError("pending order requires entry")
        price = float(req["entry"])
        action = mt5.TRADE_ACTION_PENDING
        if order_kind == "limit":
            if is_buy and not price < float(tick.ask):
                raise ValueError("buy limit must be below ask")
            if not is_buy and not price > float(tick.bid):
                raise ValueError("sell limit must be above bid")
        else:
            if is_buy and not price > float(tick.ask):
                raise ValueError("buy stop must be above ask")
            if not is_buy and not price < float(tick.bid):
                raise ValueError("sell stop must be below bid")

    sl = float(req["sl"])
    tp = float(req["tp"]) if req.get("tp") is not None else None
    if is_buy:
        if sl >= price:
            raise ValueError("buy SL must be below executable entry")
        if tp is not None and tp <= price:
            raise ValueError("buy TP must be above executable entry")
    else:
        if sl <= price:
            raise ValueError("sell SL must be above executable entry")
        if tp is not None and tp >= price:
            raise ValueError("sell TP must be below executable entry")

    point = float(si.point or si.trade_tick_size)
    min_dist = int(getattr(si, "trade_stops_level", 0)) * point
    if min_dist:
        if abs(price - sl) + 1e-12 < min_dist:
            raise ValueError("SL inside broker minimum stop distance")
        if tp is not None and abs(tp - price) + 1e-12 < min_dist:
            raise ValueError("TP inside broker minimum stop distance")

    key = str(req["idempotency_key"])
    order = {
        "action": action,
        "symbol": symbol,
        "volume": volume,
        "type": KIND[(direction, order_kind)],
        "price": price,
        "sl": sl,
        "deviation": int(req.get("deviation", 20)),
        "magic": int(req.get("magic", 770077)),
        "comment": _comment(key),
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _choose_filling(si, order_kind),
    }
    if tp is not None:
        order["tp"] = tp
    return key, order


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        supplied = self.headers.get("X-Bridge-Secret", "")
        if not hmac.compare_digest(supplied, SECRET):
            self._send(403, {"ok": False, "detail": "forbidden"})
            return False
        return True

    def do_GET(self):
        if not self._authed():
            return
        split = urlsplit(self.path)
        path = split.path.rstrip("/")
        q = {k: v[0] for k, v in parse_qs(split.query).items()}
        try:
            if path == "/symbols":
                rows = mt5.symbols_get()
                if rows is None:
                    return self._send(500, {"ok": False, "detail": str(mt5.last_error())})
                return self._send(200, {"ok": True, "symbols": [s.name for s in rows]})

            if path == "/account":
                info = mt5.account_info()
                if info is None:
                    return self._send(500, {"ok": False, "detail": "account_info returned None"})
                real_mode = getattr(mt5, "ACCOUNT_TRADE_MODE_REAL", 2)
                return self._send(200, {
                    "ok": True,
                    "login": info.login,
                    "server": info.server,
                    "balance": info.balance,
                    "equity": info.equity,
                    "margin_free": info.margin_free,
                    "trade_mode": getattr(info, "trade_mode", None),
                    "is_real": getattr(info, "trade_mode", None) == real_mode,
                    "currency": info.currency,
                })

            if path == "/spec":
                symbol = q.get("symbol", "")
                si = _spec(symbol)
                if si is None:
                    return self._send(200, {"ok": False, "detail": f"no info for {symbol}"})
                return self._send(200, {
                    "ok": True,
                    "tick_value": si.trade_tick_value,
                    "tick_size": si.trade_tick_size or si.point,
                    "volume_min": si.volume_min,
                    "volume_max": si.volume_max,
                    "volume_step": si.volume_step,
                    "digits": si.digits,
                    "point": si.point,
                    "trade_stops_level": getattr(si, "trade_stops_level", 0),
                    "trade_freeze_level": getattr(si, "trade_freeze_level", 0),
                    "filling_mode": getattr(si, "filling_mode", 0),
                    "trade_exemode": getattr(si, "trade_exemode", 0),
                    "order_mode": getattr(si, "order_mode", 0),
                    "trade_mode": getattr(si, "trade_mode", 0),
                })

            if path == "/exposure":
                positions = mt5.positions_get()
                pending = mt5.orders_get()
                if positions is None and pending is None:
                    return self._send(500, {"ok": False, "detail": "cannot read exposure"})
                total = sum(float(p.volume) for p in (positions or ()))
                total += sum(float(o.volume_current) for o in (pending or ()))
                return self._send(200, {"ok": True, "open_volume": round(total, 8)})

            if path == "/price":
                symbol = q.get("symbol", "")
                if _spec(symbol) is None:
                    return self._send(200, {"ok": False, "detail": f"cannot select {symbol}"})
                tick = mt5.symbol_info_tick(symbol)
                if tick is None:
                    return self._send(200, {"ok": False, "detail": f"no tick for {symbol}"})
                return self._send(200, {"ok": True, "bid": tick.bid, "ask": tick.ask})

            if path == "/riskloss":
                symbol = q.get("symbol", "")
                direction = q.get("direction", "")
                entry = float(q["entry"])
                sl = float(q["sl"])
                otype = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
                value = mt5.order_calc_profit(otype, symbol, 1.0, entry, sl)
                return self._send(200, {
                    "ok": value is not None,
                    "loss_per_lot": abs(float(value)) if value is not None else None,
                    "detail": "" if value is not None else str(mt5.last_error()),
                })

            if path == "/reconcile":
                key = q.get("idempotency_key", "")
                if not key:
                    return self._send(400, {"ok": False, "detail": "idempotency_key required"})
                return self._send(200, _reconcile(key))

            return self._send(404, {"ok": False, "detail": "not found"})
        except Exception as e:
            log.exception("GET %s failed", path)
            self._send(500, {"ok": False, "detail": str(e)})

    def do_POST(self):
        if not self._authed():
            return
        if urlsplit(self.path).path.rstrip("/") != "/order":
            return self._send(404, {"ok": False, "detail": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            key = str(req.get("idempotency_key", "")).strip()
            if not key:
                return self._send(400, {"ok": False, "detail": "idempotency_key required"})

            existing = _row(key)
            if existing:
                return self._send(200, _reconcile(key))

            key, order = _build_order(req)
            comment = order["comment"]
            _record(key, comment, "SUBMITTING", detail="intent persisted before order_send")

            check = mt5.order_check(order)
            check_ret = getattr(check, "retcode", None)
            if check is None or int(check_ret) not in (0, getattr(mt5, "TRADE_RETCODE_DONE", 10009)):
                detail = (
                    f"order_check retcode={check_ret} "
                    f"{getattr(check, 'comment', mt5.last_error())}"
                )
                _record(key, comment, "REJECTED", retcode=check_ret, detail=detail)
                return self._send(200, {
                    "ok": False, "state": "REJECTED", "ticket": None,
                    "retcode": check_ret, "detail": detail,
                })

            try:
                res = mt5.order_send(order)
            except Exception as e:
                detail = f"order_send raised after submission attempt: {e}"
                _record(key, comment, "UNKNOWN", detail=detail)
                return self._send(200, {
                    "ok": False, "state": "UNKNOWN", "ticket": None, "detail": detail,
                })

            state = _classify(res)
            ret = getattr(res, "retcode", None)
            ticket = int(getattr(res, "order", 0) or getattr(res, "deal", 0) or 0) or None
            detail = f"retcode={ret} {getattr(res, 'comment', mt5.last_error())}"
            _record(key, comment, state, ticket=ticket, retcode=ret, detail=detail)
            log.info("order %s key=%s ticket=%s retcode=%s", state, key, ticket, ret)
            return self._send(200, {
                "ok": state == "ACCEPTED",
                "state": state,
                "ticket": ticket,
                "retcode": ret,
                "detail": detail,
            })
        except Exception as e:
            log.exception("order failed")
            self._send(500, {"ok": False, "state": "UNKNOWN", "detail": str(e)})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    info = mt5.account_info()
    log.info(
        "bridge connected account=%s server=%s; listening on %s:%s",
        getattr(info, "login", None), getattr(info, "server", None), HOST, PORT,
    )
    HTTPServer((HOST, PORT), Handler).serve_forever()
