"""OPTIONAL: run this on the Windows box beside the MT5 terminal.

It exposes the two endpoints HttpClient expects, so the bot itself can run on
Linux/macOS/Docker. Bind to localhost and reach it over an SSH tunnel or VPN -
do NOT expose this to the public internet.

    pip install MetaTrader5
    python bridge_server.py            # listens on 127.0.0.1:8765
"""
from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

import MetaTrader5 as mt5  # noqa: N813

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("bridge")

HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("BRIDGE_PORT", "8765"))
# Shared secret the client must send; set the same value on both sides.
SECRET = os.environ.get("BRIDGE_SECRET", "")

KIND = {
    ("buy", "market"): mt5.ORDER_TYPE_BUY,
    ("sell", "market"): mt5.ORDER_TYPE_SELL,
    ("buy", "limit"): mt5.ORDER_TYPE_BUY_LIMIT,
    ("sell", "limit"): mt5.ORDER_TYPE_SELL_LIMIT,
    ("buy", "stop"): mt5.ORDER_TYPE_BUY_STOP,
    ("sell", "stop"): mt5.ORDER_TYPE_SELL_STOP,
}


def ensure_login(h) -> None:
    login = int(h.headers.get("X-MT5-Login"))
    pw = h.headers.get("X-MT5-Password")
    server = h.headers.get("X-MT5-Server")
    if not mt5.initialize(login=login, password=pw, server=server):
        raise RuntimeError(f"initialize failed: {mt5.last_error()}")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        if SECRET and self.headers.get("X-Bridge-Secret") != SECRET:
            self._send(403, {"ok": False, "detail": "bad secret"})
            return False
        return True

    def do_GET(self):
        if not self._authed():
            return
        path = self.path.split("?")[0].rstrip("/")
        query = {}
        if "?" in self.path:
            from urllib.parse import parse_qs
            query = {k: v[0] for k, v in parse_qs(self.path.split("?", 1)[1]).items()}
        try:
            ensure_login(self)
            if path == "/symbols":
                return self._send(200, {"symbols": [s.name for s in (mt5.symbols_get() or ())]})

            if path == "/account":
                info = mt5.account_info()
                if info is None:
                    return self._send(500, {"ok": False, "detail": "account_info returned None"})
                return self._send(200, {"ok": True, "balance": info.balance,
                                        "equity": info.equity,
                                        "currency": info.currency})

            if path == "/spec":
                symbol = query.get("symbol", "")
                if not mt5.symbol_select(symbol, True):
                    return self._send(200, {"ok": False, "detail": f"cannot select {symbol}"})
                si = mt5.symbol_info(symbol)
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
                })

            if path == "/exposure":
                positions = mt5.positions_get()
                pending = mt5.orders_get()
                if positions is None and pending is None:
                    return self._send(500, {"ok": False,
                                            "detail": "cannot read positions/orders"})
                total = sum(float(p.volume) for p in (positions or ()))
                total += sum(float(o.volume_current) for o in (pending or ()))
                return self._send(200, {"ok": True, "open_volume": round(total, 2),
                                        "positions": len(positions or ()),
                                        "pending": len(pending or ())})

            if path == "/price":
                symbol = query.get("symbol", "")
                if not mt5.symbol_select(symbol, True):
                    return self._send(200, {"ok": False, "detail": f"cannot select {symbol}"})
                tick = mt5.symbol_info_tick(symbol)
                if tick is None:
                    return self._send(200, {"ok": False, "detail": f"no tick for {symbol}"})
                return self._send(200, {"ok": True, "bid": tick.bid, "ask": tick.ask})

            self._send(404, {"ok": False, "detail": "not found"})
        except Exception as e:
            log.exception("GET %s failed", path)
            self._send(500, {"ok": False, "detail": str(e)})

    def do_POST(self):
        if not self._authed():
            return
        if self.path.rstrip("/") != "/order":
            return self._send(404, {"ok": False, "detail": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            ensure_login(self)

            symbol = req["symbol"]
            if not mt5.symbol_select(symbol, True):
                raise RuntimeError(f"cannot select {symbol}")
            otype = KIND[(req["direction"], req["order_kind"])]

            if req["order_kind"] == "market":
                tick = mt5.symbol_info_tick(symbol)
                if tick is None:
                    raise RuntimeError(f"no tick for {symbol}")
                price = tick.ask if req["direction"] == "buy" else tick.bid
                action = mt5.TRADE_ACTION_DEAL
            else:
                price = float(req["entry"])
                action = mt5.TRADE_ACTION_PENDING

            order = {
                "action": action, "symbol": symbol,
                "volume": float(req["volume"]), "type": otype,
                "price": float(price), "sl": float(req["sl"]),
                "deviation": 20, "magic": int(req.get("magic", 770077)),
                "comment": req.get("comment", "tg-signal"),
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            if req.get("tp") is not None:
                order["tp"] = float(req["tp"])

            res = mt5.order_send(order)
            if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
                detail = f"retcode={getattr(res, 'retcode', None)} {getattr(res, 'comment', mt5.last_error())}"
                log.error("order rejected: %s", detail)
                return self._send(200, {"ok": False, "ticket": None, "detail": detail})
            log.info("order placed ticket=%s %s", res.order, order)
            self._send(200, {"ok": True, "ticket": res.order, "detail": ""})
        except Exception as e:
            log.exception("order failed")
            self._send(500, {"ok": False, "detail": str(e)})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    log.info("bridge listening on %s:%s", HOST, PORT)
    HTTPServer((HOST, PORT), Handler).serve_forever()
