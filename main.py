"""Entry point: poll Telegram -> validate -> persist intent -> execute on MT5."""
from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import replace
from datetime import datetime

from .budget import Backoff, BudgetExceeded, RequestBudget
from .config import Config, ConfigError
from .mt5_client import make_client, split_lots
from .parser import Rejection, Signal, parse_signal
from .sizing import SizingError, floor_to_step
from .state import StateStore
from .symbols import resolve_symbol
from .telegram_source import TelegramSource, TelegramUpdate

log = logging.getLogger("sigbridge")


def setup_logging(logfile: str = "sigbridge.log") -> None:
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(logfile)],
    )


class Executor:
    def __init__(self, cfg, client, state: StateStore):
        self.cfg, self.client, self.state = cfg, client, state

    def reconcile_unsettled(self) -> None:
        for leg in list(self.state.unsettled_legs()):
            res = self.client.reconcile(leg.idempotency_key)
            if res.state == "ACCEPTED":
                self.state.set_leg_status(
                    leg.chat_id, leg.message_id, leg.leg_index,
                    "ACCEPTED", res.ticket, res.detail,
                )
                log.warning(
                    "reconciled prior ambiguous order %s/%s leg %s -> ticket %s",
                    leg.chat_id, leg.message_id, leg.leg_index, res.ticket,
                )
            else:
                self.state.set_leg_status(
                    leg.chat_id, leg.message_id, leg.leg_index,
                    "UNKNOWN", detail=res.detail,
                )
                log.error(
                    "order remains UNKNOWN %s/%s leg %s: %s; not resubmitting",
                    leg.chat_id, leg.message_id, leg.leg_index, res.detail,
                )

    def _equity_guard(self, update: TelegramUpdate) -> bool:
        info = self.client.account_info()
        if info is None or info.equity <= 0:
            self.state.set_signal_status(
                update.chat_id, update.message_id, "REJECTED",
                "account equity unavailable; cannot enforce kill switches",
            )
            log.error("message %s REJECTED: account equity unavailable", update.message_id)
            return False

        day = datetime.utcnow().date().isoformat()
        start, high = self.state.update_equity(day, info.equity)
        daily_loss = max(0.0, (start - info.equity) / start * 100.0)
        drawdown = max(0.0, (high - info.equity) / high * 100.0)
        if daily_loss >= self.cfg.max_daily_equity_loss_pct:
            msg = (
                f"daily equity loss {daily_loss:.2f}% reached limit "
                f"{self.cfg.max_daily_equity_loss_pct:.2f}%"
            )
            self.state.set_signal_status(update.chat_id, update.message_id, "REJECTED", msg)
            log.error("message %s REJECTED: %s", update.message_id, msg)
            return False
        if drawdown >= self.cfg.max_equity_drawdown_pct:
            msg = (
                f"equity drawdown {drawdown:.2f}% reached limit "
                f"{self.cfg.max_equity_drawdown_pct:.2f}%"
            )
            self.state.set_signal_status(update.chat_id, update.message_id, "REJECTED", msg)
            log.error("message %s REJECTED: %s", update.message_id, msg)
            return False
        return True

    def handle(self, update: TelegramUpdate) -> None:
        try:
            existing = self.state.begin_signal(
                update.chat_id, update.message_id, update.update_id,
                update.message_date, update.text,
            )
        except RuntimeError as e:
            log.error("message %s ignored: %s", update.message_id, e)
            return

        if existing in {"COMPLETE", "REJECTED", "PARTIAL", "IGNORED"}:
            return

        if update.edited:
            self.state.set_signal_status(
                update.chat_id, update.message_id, "IGNORED",
                "edited Telegram messages are never treated as new trade instructions",
            )
            log.warning("message %s IGNORED: edited post", update.message_id)
            return

        if (
            self.cfg.max_signal_age_seconds > 0
            and update.message_date is not None
            and time.time() - update.message_date > self.cfg.max_signal_age_seconds
        ):
            age = int(time.time() - update.message_date)
            detail = f"signal is stale ({age}s old; max {self.cfg.max_signal_age_seconds}s)"
            self.state.set_signal_status(update.chat_id, update.message_id, "REJECTED", detail)
            log.warning("message %s REJECTED: %s", update.message_id, detail)
            return

        log.info("message %s received: %s", update.message_id, update.text.replace("\n", " | ")[:300])
        result = parse_signal(update.text)
        if isinstance(result, Rejection):
            self.state.set_signal_status(
                update.chat_id, update.message_id, "REJECTED", str(result)
            )
            log.warning("message %s %s", update.message_id, result)
            return

        sig: Signal = result
        broker_symbols = self.client.symbols()
        if not broker_symbols:
            detail = "broker symbol discovery unavailable; refusing to guess ticker"
            self.state.set_signal_status(update.chat_id, update.message_id, "REJECTED", detail)
            log.error("message %s REJECTED: %s", update.message_id, detail)
            return

        broker_ticker, why = resolve_symbol(
            sig.symbol, broker_symbols, self.cfg.symbol_map, self.cfg.symbol_suffix
        )
        if broker_ticker is None:
            self.state.set_signal_status(update.chat_id, update.message_id, "REJECTED", why)
            log.error("message %s REJECTED: %s", update.message_id, why)
            return
        if broker_ticker != sig.symbol:
            sig = replace(sig, symbol=broker_ticker)
            log.info("message %s symbol: %s", update.message_id, why)

        if not self._equity_guard(update):
            return

        try:
            total_lot, reason = self.client.size_for(sig)
        except (SizingError, Exception) as e:
            detail = f"sizing unavailable: {e}"
            self.state.set_signal_status(update.chat_id, update.message_id, "REJECTED", detail)
            log.error("message %s REJECTED: %s", update.message_id, detail)
            return
        log.info("message %s sizing: %s -> total %s lot", update.message_id, reason, total_lot)

        # Monetary open-risk cap. Lot caps remain a secondary guard because
        # equal lot sizes can represent very different stop-loss risk.
        acct = self.client.account_info()
        open_risk = self.client.open_risk()
        if acct is None or acct.equity <= 0 or open_risk is None:
            detail = "cannot measure account/open risk safely; refusing new exposure"
            self.state.set_signal_status(update.chat_id, update.message_id, "REJECTED", detail)
            log.error("message %s REJECTED: %s", update.message_id, detail)
            return
        if sig.order_kind == "market":
            risk_entry = self.client.market_price(sig.symbol, sig.direction)
        else:
            risk_entry = float(sig.entry) if sig.entry is not None else None
        if risk_entry is None:
            detail = "cannot determine entry price for monetary risk check"
            self.state.set_signal_status(update.chat_id, update.message_id, "REJECTED", detail)
            return
        per_lot_loss = self.client.loss_per_lot(
            sig.symbol, sig.direction, float(risk_entry), float(sig.stop_loss)
        )
        if per_lot_loss is None or per_lot_loss <= 0:
            detail = "cannot calculate monetary loss to stop loss"
            self.state.set_signal_status(update.chat_id, update.message_id, "REJECTED", detail)
            return
        proposed_risk = per_lot_loss * float(total_lot)
        combined_pct = (open_risk + proposed_risk) / acct.equity * 100.0
        if combined_pct > self.cfg.max_open_risk_pct:
            detail = (
                f"open risk would become {combined_pct:.2f}% of equity "
                f"(limit {self.cfg.max_open_risk_pct:.2f}%)"
            )
            self.state.set_signal_status(update.chat_id, update.message_id, "REJECTED", detail)
            log.warning("message %s REJECTED: %s", update.message_id, detail)
            return

        spec = self.client.symbol_spec(sig.symbol)
        if spec is None:
            detail = "symbol specification unavailable"
            self.state.set_signal_status(update.chat_id, update.message_id, "REJECTED", detail)
            return

        open_vol = self.client.open_volume()
        if open_vol is None:
            detail = "cannot read current open volume; exposure cap cannot be enforced"
            self.state.set_signal_status(update.chat_id, update.message_id, "REJECTED", detail)
            log.error("message %s REJECTED: %s", update.message_id, detail)
            return

        room = floor_to_step(max(0.0, self.cfg.max_open_lots - open_vol), spec.volume_step)
        if room < spec.volume_min:
            detail = (
                f"concurrent exposure cap reached ({open_vol} / "
                f"{self.cfg.max_open_lots} lot open)"
            )
            self.state.set_signal_status(update.chat_id, update.message_id, "REJECTED", detail)
            log.warning("message %s SKIPPED: %s", update.message_id, detail)
            return
        if total_lot > room:
            total_lot = room
            log.warning(
                "message %s trimmed to %s lot by concurrent exposure cap",
                update.message_id, total_lot,
            )

        tps = sig.take_profits or [None]
        lots = split_lots(
            total_lot, len(tps), step=spec.volume_step, minimum=spec.volume_min
        )
        if not lots:
            detail = "volume is below broker minimum after exposure/risk limits"
            self.state.set_signal_status(update.chat_id, update.message_id, "REJECTED", detail)
            return
        if len(lots) < len(tps):
            tps = tps[: len(lots)]
            log.info(
                "message %s volume too small for every TP; using %s leg(s)",
                update.message_id, len(lots),
            )

        self.state.set_signal_status(update.chat_id, update.message_id, "VALIDATED")
        for idx, (lot, tp) in enumerate(zip(lots, tps)):
            self.state.upsert_leg(
                update.chat_id, update.message_id, idx, sig.symbol, sig.direction,
                sig.order_kind, lot,
                float(sig.entry) if sig.entry is not None else None,
                float(sig.stop_loss),
                float(tp) if tp is not None else None,
            )

        states: list[str] = []
        for idx, (lot, tp) in enumerate(zip(lots, tps)):
            leg = self.state.get_leg(update.chat_id, update.message_id, idx)
            if leg.status == "ACCEPTED":
                states.append("ACCEPTED")
                continue
            if leg.status in {"SUBMITTING", "UNKNOWN"}:
                res = self.client.reconcile(leg.idempotency_key)
                if res.state == "ACCEPTED":
                    self.state.set_leg_status(
                        update.chat_id, update.message_id, idx,
                        "ACCEPTED", res.ticket, res.detail,
                    )
                    states.append("ACCEPTED")
                else:
                    self.state.set_leg_status(
                        update.chat_id, update.message_id, idx,
                        "UNKNOWN", detail=res.detail,
                    )
                    states.append("UNKNOWN")
                continue

            self.state.set_leg_status(
                update.chat_id, update.message_id, idx,
                "SUBMITTING", detail="intent persisted before broker submission",
            )
            try:
                res = self.client.place(sig, lot, tp, leg.idempotency_key)
            except BudgetExceeded:
                raise
            except Exception as e:
                res = None
                detail = f"submission exception: {e}"
                self.state.set_leg_status(
                    update.chat_id, update.message_id, idx, "UNKNOWN", detail=detail
                )
                log.exception("message %s leg %s submission error", update.message_id, idx)

            if res is None:
                states.append("UNKNOWN")
                continue
            log.info("message %s AUDIT %s", update.message_id, res.audit())
            self.state.set_leg_status(
                update.chat_id, update.message_id, idx,
                res.state, res.ticket, res.detail,
            )
            states.append(res.state)

        if states and all(s == "ACCEPTED" for s in states):
            status = "COMPLETE"
        elif "UNKNOWN" in states:
            status = "UNKNOWN"
        elif "ACCEPTED" in states:
            status = "PARTIAL"
        else:
            status = "REJECTED"
        self.state.set_signal_status(
            update.chat_id, update.message_id, status,
            "leg states: " + ",".join(states),
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Telegram -> MT5 signal bridge")
    ap.add_argument("--once", action="store_true", help="single poll then exit")
    ap.add_argument("--check", action="store_true", help="verify connections and exit")
    ap.add_argument("--parse", metavar="TEXT", help="parse one message, no trading")
    ap.add_argument("--symbols", metavar="FILTER", nargs="?", const="")
    ap.add_argument("--resolve", metavar="SYMBOL")
    args = ap.parse_args()
    setup_logging()

    if args.parse:
        print(parse_signal(args.parse))
        return 0

    try:
        cfg = Config.from_env()
        state = StateStore(cfg.state_db)
        budget = RequestBudget(cfg.max_requests_24h)
        client = make_client(cfg, budget)
    except (ConfigError, Exception) as e:
        log.error("startup/configuration error: %s", e)
        return 2

    if args.symbols is not None or args.resolve:
        syms = client.symbols()
        if not syms:
            log.error("broker returned no symbol list")
            return 3
        if args.resolve:
            ticker, why = resolve_symbol(
                args.resolve, syms, cfg.symbol_map, cfg.symbol_suffix
            )
            print(f"{args.resolve} -> {ticker or 'UNRESOLVED'}\n  {why}")
            return 0 if ticker else 1
        flt = (args.symbols or "").upper()
        hits = sorted(s for s in syms if flt in s.upper())
        for s in hits:
            print(s)
        print(f"\n{len(hits)} of {len(syms)} tickers", file=sys.stderr)
        return 0

    info = client.account_info()
    if info is None:
        log.error("cannot read MT5 account info")
        return 3

    if cfg.dry_run:
        log.warning("DRY_RUN is ON - orders will be validated/logged but not sent.")
    else:
        log.warning("=" * 68)
        log.warning("LIVE MODE ENABLED - orders can be sent to account %s on %s", info.login, info.server)
        log.warning("live opt-in: ALLOW_LIVE_TRADING=true; real-account opt-in=%s", cfg.allow_real_account)
        log.warning("=" * 68)

    try:
        tg = TelegramSource(cfg, budget, state)
        log.info("telegram bot: %s, watching channel %s", tg.verify(), cfg.tg_channel)
    except Exception as e:
        log.error("Telegram startup failed: %s", e)
        return 3

    if args.check:
        log.info("connection check OK")
        return 0

    ex = Executor(cfg, client, state)
    ex.reconcile_unsettled()
    backoff = Backoff()

    while True:
        try:
            for update in tg.poll():
                ex.handle(update)
                # ACK only after processing state has been durably committed.
                tg.ack(update.update_id)
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
