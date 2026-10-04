# Telegram → MT5 bridge

A strict Telegram signal copier for MetaTrader 5 with conservative execution
safety. Ambiguous or unverifiable signals are rejected rather than guessed.

## Safety model

This branch uses durable SQLite state and per-leg idempotency. A signal and each
TP leg are persisted before broker submission. If the process or HTTP link dies
during submission, the order is recorded as `UNKNOWN` and reconciled against
MT5 before any retry; it is never blindly sent twice.

Other fail-closed controls include:

- `DRY_RUN=true` by default.
- Live orders additionally require `ALLOW_LIVE_TRADING=true`.
- Real MT5 accounts additionally require `ALLOW_REAL_ACCOUNT=true`.
- Stop loss is mandatory in the parser.
- Edited Telegram posts are never treated as fresh trade instructions.
- Stale signals are rejected with `MAX_SIGNAL_AGE_SECONDS`.
- Market sizing uses the current executable side (ask for buys, bid for sells).
- Optional advertised-entry deviation guard via `MAX_REFERENCE_DEVIATION_PCT`.
- Risk sizing uses account equity and MT5 `order_calc_profit`; it never falls
  back to an arbitrary lot when risk cannot be calculated.
- Broker volume step/min/max, stop-level geometry and pending-order direction
  are checked before submission.
- `order_check` runs before `order_send`.
- MT5 `PLACED`, `DONE`, and `DONE_PARTIAL` are treated as accepted.
  Timeout/connection ambiguity is `UNKNOWN`, not a normal rejection.
- Total open lots remain capped by `MAX_OPEN_LOTS`.
- Monetary loss-to-stop across existing plus proposed exposure is capped by
  `MAX_OPEN_RISK_PCT`; exposure without a measurable SL causes a rejection.
- Daily equity loss and high-water drawdown kill switches are enforced.
- Request-budget persistence is atomic and fails closed if its state is corrupt.

## Configuration

Copy the template and fill it locally:

```bash
cp .env.example .env
```

Never commit `.env` or real credentials.

### Native Windows mode

The bot and MT5 terminal run on the same Windows machine:

```text
MT5_MODE=native
MT5_LOGIN=...
MT5_PASSWORD=...
MT5_SERVER=...
```

### HTTP bridge mode

Run `bridge_server.py` beside MT5 on Windows. The bridge owns the MT5
credentials locally; the remote bot sends only `X-Bridge-Secret`.

Windows bridge:

```text
MT5_LOGIN=...
MT5_PASSWORD=...
MT5_SERVER=...
BRIDGE_SECRET=<long-random-secret>
BRIDGE_HOST=127.0.0.1
```

Bot machine:

```text
MT5_MODE=http
MT5_HTTP_URL=http://127.0.0.1:8765
BRIDGE_SECRET=<same-long-random-secret>
```

Keep the bridge bound to localhost and reach it through an SSH tunnel/VPN.
A non-loopback bind is refused unless `ALLOW_NONLOCAL_BRIDGE=true` is set
explicitly.

## Live-trading gates

The safe default is:

```text
DRY_RUN=true
ALLOW_LIVE_TRADING=false
ALLOW_REAL_ACCOUNT=false
```

To enable a demo account deliberately:

```text
DRY_RUN=false
ALLOW_LIVE_TRADING=true
ALLOW_REAL_ACCOUNT=false
```

For a real account, `ALLOW_REAL_ACCOUNT=true` is also required after the
terminal reports that the connected account is real.

## Risk settings

```text
SIZING_MODE=fixed
DEFAULT_LOT=0.01
RISK_PERCENT=1.0
MAX_LOT=1.0
MAX_OPEN_LOTS=1.0
MAX_OPEN_RISK_PCT=5.0
MAX_DAILY_EQUITY_LOSS_PCT=3.0
MAX_EQUITY_DRAWDOWN_PCT=5.0
MAX_SIGNAL_AGE_SECONDS=300
MAX_REFERENCE_DEVIATION_PCT=0.25
```

In `SIZING_MODE=risk`, an unavailable equity/spec/price/SL-loss calculation is
a rejection. The bot will not substitute a fallback lot.

## Durable state

Runtime state lives in:

- `.trader_state.sqlite3` — Telegram offset, signals, TP legs, equity state.
- `.bridge_state.sqlite3` — bridge-side idempotency ledger.
- `.budget.json` — rolling request budget.

SQLite uses WAL and synchronous commits for the execution ledger. Telegram
updates are acknowledged only after processing state is committed.

## Commands

```bash
python -m sigbridge.main --check
python -m sigbridge.main --parse "BUY EURUSD @ 1.0850 SL 1.0800 TP 1.0900"
python -m sigbridge.main --resolve EURUSD
python -m sigbridge.main --symbols EUR
python -m sigbridge.main --once
python -m sigbridge.main
```

Run on a demo account first and verify symbol mapping, stop geometry, sizing and
audit logs before enabling any live account.

## Security note

If credentials were ever committed to a public Git repository, replacing them
in the latest file does not remove them from Git history. Rotate those
credentials/tokens.
