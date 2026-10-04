"""Entry point: poll channel -> parse -> validate -> execute on MT5."""
from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import replace
from pathlib import Path

from .budget import Backoff, BudgetExceeded, RequestBudget
from .config import Config, ConfigError
from .mt5_client import make_client, split_lots
from .parser import Rejection, Signal, parse_signal
from .symbols import resolve_symbol
from .telegram_source import TelegramSource

log = logging.getLogger("sigbridge")


def _floor_step(vol: float, step: float = 0.01) -> float:
    """Round a volume DOWN to the tradable step."""
    import math
    return round(math.floor(round(vol / step, 9)) * step, 2)


def setup_logging(logfile: str = "sigbridge.log") -> None:
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt,
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(logfile)])


class Executor:
    def __init__(self, cfg, client):
        self.cfg, self.client = cfg, client
        self.seen: set[int] = set()
        self._seen_file = Path(".seen_ids")
        self._load_seen()

    def _load_seen(self):
        try:
            self.seen = {int(x) for x in self._seen_file.read_text().split()}
        except Exception:
            self.seen = set()

    def _mark(self, mid: int):
        self.seen.add(mid)
        try:
            self._seen_file.write_text("\n".join(str(i) for i in sorted(self.seen)[-5000:]))
        except Exception:
            pass

    def handle(self, mid: int, text: str) -> None:
        if mid in self.seen:
            return
        log.info("message %s received: %s", mid, text.replace("\n", " | ")[:300])

        # Parse WITHOUT the broker symbol list: resolution happens below, so a
        # broker-decorated ticker (XAUUSD.p) is no longer a parse failure.
        result = parse_signal(text)

        if isinstance(result, Rejection):
            log.warning("message %s %s", mid, result)
            self._mark(mid)
            return

        sig: Signal = result
        log.info("message %s parsed: %s", mid, sig)

        if sig.stop_loss is None and not self.cfg.allow_missing_sl:
            log.warning("message %s REJECTED: no stop loss and ALLOW_MISSING_SL is off", mid)
            self._mark(mid)
            return

        # --- symbol resolution -------------------------------------------
        broker_ticker, why = resolve_symbol(
            sig.symbol, self.client.symbols(),
            self.cfg.symbol_map, self.cfg.symbol_suffix)
        if broker_ticker is None:
            log.error("message %s REJECTED: %s", mid, why)
            self._mark(mid)
            return
        if broker_ticker != sig.symbol:
            log.info("message %s symbol: %s", mid, why)
            sig = replace(sig, symbol=broker_ticker)

        tps = sig.take_profits or [None]

        # --- position sizing --------------------------------------------
        try:
            total_lot, reason = self.client.size_for(sig)
        except Exception as e:
            total_lot, reason = self.cfg.fallback_lot, f"fallback: sizing error ({e})"
        log.info("message %s sizing: %s -> total %s lot", mid, reason, total_lot)
        if "capped at MAX_LOT" in reason:
            log.warning("message %s: MAX_LOT is binding - size is BELOW target. "
                        "Raise MAX_LOT if that is unintended.", mid)
        if reason.startswith("fallback"):
            log.warning("message %s: risk sizing unavailable, using fallback %s lot "
                        "- this is NOT %s%% risk", mid, total_lot, self.cfg.risk_percent)

        # --- aggregate open-exposure cap ---------------------------------
        open_vol = self.client.open_volume()
        if open_vol is None:
            log.error("message %s REJECTED: cannot read current open volume, so the "
                      "%s lot concurrent cap cannot be enforced - refusing to add "
                      "exposure blindly", mid, self.cfg.max_open_lots)
            self._mark(mid)
            return

        # Floor, never round: rounding up here would let a signal breach the
        # cap by a fraction of a step (0.995 open would yield 0.01 of "room").
        room = _floor_step(self.cfg.max_open_lots - open_vol)
        log.info("message %s exposure: %s lot open, %s lot cap, %s lot room",
                 mid, open_vol, self.cfg.max_open_lots, room)

        if room <= 0:
            log.warning("message %s SKIPPED: concurrent exposure cap reached "
                        "(%s / %s lot open)", mid, open_vol, self.cfg.max_open_lots)
            self._mark(mid)
            return

        if total_lot > room:
            trimmed = room
            if trimmed <= 0:
                log.warning("message %s SKIPPED: only %s lot of room left, below the "
                            "minimum tradable step", mid, room)
                self._mark(mid)
                return
            log.warning("message %s: trimming size %s -> %s lot to stay within the "
                        "%s lot concurrent cap", mid, total_lot, trimmed,
                        self.cfg.max_open_lots)
            total_lot = trimmed

        lots = split_lots(total_lot, len(tps))
        if len(lots) < len(tps):
            tps = tps[:1]          # cannot split -> nearest TP only
            log.info("message %s: volume too small to split, using nearest TP %s", mid, tps[0])

        total = sum(lots)
        if total > self.cfg.max_lot:
            log.error("message %s REJECTED: total volume %s exceeds MAX_LOT %s",
                      mid, total, self.cfg.max_lot)
            self._mark(mid)
            return

        placed = 0.0
        for lot, tp in zip(lots, tps):
            # Re-check room as we go: each fill consumes headroom, so a
            # multi-TP signal must not blow through the cap on later legs.
            if round(placed + lot, 2) > room:
                log.warning("message %s: stopping after %s lot, remaining legs would "
                            "breach the %s lot cap", mid, placed, self.cfg.max_open_lots)
                break
            try:
                res = self.client.place(sig, lot, tp)
                log.info("message %s AUDIT %s", mid, res.audit())
                if res.ok:
                    placed = round(placed + lot, 2)
            except BudgetExceeded:
                raise
            except Exception as e:
                log.error("message %s order error: %s", mid, e)
        self._mark(mid)


def main() -> int:
    ap = argparse.ArgumentParser(description="Telegram -> MT5 signal bridge")
    ap.add_argument("--once", action="store_true", help="single poll then exit")
    ap.add_argument("--check", action="store_true", help="verify connections and exit")
    ap.add_argument("--parse", metavar="TEXT", help="parse one message, no trading")
    ap.add_argument("--symbols", metavar="FILTER", nargs="?", const="",
                    help="list broker tickers (optionally filtered) and exit")
    ap.add_argument("--resolve", metavar="SYMBOL",
                    help="show which broker ticker a signal symbol maps to")
    args = ap.parse_args()

    setup_logging()

    if args.parse:
        print(parse_signal(args.parse))
        return 0

    try:
        cfg = Config.from_env()
    except ConfigError as e:
        log.error("configuration error: %s", e)
        return 2

    budget = RequestBudget(cfg.max_requests_24h)
    log.info("request budget: %s used / %s cap (rolling 24h)", budget.used, budget.max_requests)

    # --- symbol discovery helpers: need MT5 only, never place orders ----
    if args.symbols is not None or args.resolve:
        try:
            client = make_client(cfg, budget)
            syms = client.symbols()
        except Exception as e:
            log.error("could not reach MT5: %s", e)
            return 3
        if syms is None:
            log.error("broker returned no symbol list")
            return 3
        if args.resolve:
            ticker, why = resolve_symbol(args.resolve, syms,
                                         cfg.symbol_map, cfg.symbol_suffix)
            print(f"{args.resolve} -> {ticker or 'UNRESOLVED'}\n  {why}")
            return 0 if ticker else 1
        flt = (args.symbols or "").upper()
        hits = sorted(s for s in syms if flt in s.upper())
        for s in hits:
            print(s)
        print(f"\n{len(hits)} of {len(syms)} tickers", file=sys.stderr)
        return 0

    if cfg.dry_run:
        log.warning("DRY_RUN is ON - orders will be logged, not sent.")
    else:
        log.warning("=" * 68)
        log.warning("LIVE MODE - orders WILL be sent to account %s on %s",
                    cfg.mt5_login, cfg.mt5_server)
        if cfg.sizing_mode == "fixed":
            log.warning("sizing: fixed %s lot per signal", cfg.default_lot)
        else:
            log.warning("sizing: %s%% of balance per signal, fallback %s lot",
                        cfg.risk_percent, cfg.fallback_lot)
        log.warning("caps: %s lot per signal, %s lot total open exposure",
                    cfg.max_lot, cfg.max_open_lots)
        log.warning("confirm this is a DEMO account before leaving it running")
        log.warning("=" * 68)

    try:
        tg = TelegramSource(cfg, budget)
        log.info("telegram bot: %s, watching channel %s", tg.verify(), cfg.tg_channel)
        client = make_client(cfg, budget)
    except Exception as e:
        log.error("startup failed: %s", e)
        return 3

    if args.check:
        log.info("connection check OK")
        return 0

    ex = Executor(cfg, client)
    backoff = Backoff()

    while True:
        try:
            for mid, text in tg.poll():
                ex.handle(mid, text)
            backoff.ok()
        except BudgetExceeded as e:
            log.error("%s - sleeping 1h", e)
            time.sleep(3600)
            continue
        except KeyboardInterrupt:
            log.info("shutdown requested")
            return 0
        except Exception as e:
            wait = backoff.fail()
            log.error("poll error (%s) - backing off %.0fs", e, wait)
            time.sleep(wait)
            continue

        if args.once:
            return 0


if __name__ == "__main__":
    sys.exit(main())
