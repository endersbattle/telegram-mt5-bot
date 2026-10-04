# tg-mt5-bridge

Reads trade signals from a Telegram channel, parses and validates them, and
places the matching order on MetaTrader 5.

The parser is deliberately strict: anything ambiguous, incomplete, or
self-contradictory is **rejected and logged**, never guessed at.

---

## 1. Download

The project is a plain folder — copy it to the machine that will run it.

```bash
# if you received it as a folder, just cd into it
cd tg-mt5-bridge

# or put it under version control / push to your own remote
git init && git add . && git commit -m "initial commit"
```

No third-party packages are needed for the bot itself — it is pure Python 3.9+
standard library. The only optional dependency (`MetaTrader5`) is installed on
the Windows machine that runs the terminal.

```bash
python3 -V                  # need 3.9 or newer
```

## 2. Try it with no credentials

The parser runs standalone, so you can see the behaviour before wiring up any
account:

```bash
python3 tests/test_parser.py

python3 -m sigbridge.main --parse "BUY EURUSD @ 1.0850
SL 1.0800
TP1 1.0900
TP2 1.0950"
# BUY EURUSD market @1.0850 SL=1.0800 TP=[1.0900, 1.0950]

python3 -m sigbridge.main --parse "BUY EURUSD @ 1.0850 TP 1.0900"
# REJECTED: no stop loss
```

## 3. Configure

```bash
cp .env.example .env
$EDITOR .env
```

You need two sets of credentials:

**Telegram** — create a bot with [@BotFather](https://t.me/BotFather) to get
`TELEGRAM_BOT_TOKEN`, then **add the bot as an administrator of the channel**
(a bot cannot read channel posts otherwise). To find the numeric channel id,
post any message in the channel and run:

```bash
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" | python3 -m json.tool
```

Look for `"chat": {"id": -100...}`.

**MetaTrader 5** — the login, password and server string from your broker. Use
a **demo account** first.

Load the file into the environment:

```bash
set -a && source .env && set +a
```

## 4. Choose a backend

The official `MetaTrader5` Python package is **Windows-only** and needs a
running MT5 terminal on the same machine.

**Option A — everything on Windows** (`MT5_MODE=native`):

```powershell
pip install MetaTrader5
# start the MT5 terminal, log in, and enable Algo Trading in the toolbar
python -m sigbridge.main --check
```

**Option B — bot on Linux/macOS/Docker** (`MT5_MODE=http`): run
`bridge_server.py` on the Windows box next to the terminal, then point the bot
at it. Keep the bridge on localhost and reach it over an SSH tunnel or VPN —
never expose it to the open internet.

```powershell
# on Windows, beside the terminal
pip install MetaTrader5
set BRIDGE_SECRET=some-long-random-string
python bridge_server.py
```

```bash
# on the Linux box
ssh -N -L 8765:127.0.0.1:8765 user@windows-host &
export MT5_MODE=http MT5_HTTP_URL=http://127.0.0.1:8765
python3 -m sigbridge.main --check
```

## 5. Run

```bash
# verify both connections, place nothing, exit
python3 -m sigbridge.main --check

# one poll cycle - useful for a first smoke test
python3 -m sigbridge.main --once

# continuous
python3 -m sigbridge.main
```

`DRY_RUN` defaults to `false`, so **orders reach the broker as soon as you fill
in credentials and start it**. On startup the log prints a banner naming the
account and server it is about to trade — check that line before walking away.
Set `DRY_RUN=true` to go back to logging orders without sending them.

## Position sizing

Two modes, set by `SIZING_MODE`:

**`fixed` (default)** — every signal uses `DEFAULT_LOT`, which defaults to
`0.01`. Simple and predictable; the size does not vary with stop distance.

**`risk`** — each signal is sized to risk `RISK_PERCENT` of the account balance
across the distance from entry to stop loss:

```
risk_money   = balance * RISK_PERCENT/100
risk_per_lot = (stop_distance / tick_size) * tick_value
lot          = risk_money / risk_per_lot     (rounded DOWN to volume step)
```

Risk mode falls back to `FALLBACK_LOT` (0.01) whenever the calculation cannot
be trusted — balance unavailable, stop distance missing, broker specs
unreadable, or the correct size landing below the broker's minimum volume. That
last case matters: taking `volume_min` instead would silently risk *more* than
your target, so it falls back and warns rather than over-risking.

## Symbol mapping

Signal channels rarely use your broker's exact ticker. A channel posts
`XAUUSD`; your broker lists `XAUUSD.p`. The bot resolves this automatically
against the broker's live symbol list, in this order:

1. An explicit `SYMBOL_MAP` entry
2. Exact match
3. Case-insensitive match (`xauusd` -> `XAUUSD`)
4. Built-in alias (`GOLD` <-> `XAUUSD`, `SILVER` <-> `XAGUSD`)
5. Base + broker suffix — `XAUUSD` -> `XAUUSD.s`, `XAUUSD.p`, `XAUUSD_i`,
   `XAUUSDm`, `XAUUSD.pro`, `XAUUSD-ECN`
6. The reverse — signal says `XAUUSD.p`, broker lists plain `XAUUSD`

**This install ships pre-configured for a `.s` broker.** `.env.example`
pins gold and silver explicitly and sets `.s` as the tie-breaking suffix:

```bash
SYMBOL_SUFFIX=.s
SYMBOL_MAP=XAUUSD=XAUUSD.s,GOLD=XAUUSD.s,XAGUSD=XAGUSD.s,SILVER=XAGUSD.s
```

So `XAUUSD`, `xauusd` and `GOLD` all route to `XAUUSD.s` deterministically via
the map, while everything else (`EURUSD` -> `EURUSD.s`, `US30` -> `US30.s`)
still resolves automatically by suffix. On a different broker, change or clear
those two lines.

Note the pin is **fail-closed**: if the broker does not actually offer
`XAUUSD.s`, gold signals are refused with a clear message rather than being
silently routed to some other gold ticker. Verify with `--resolve XAUUSD`
before your first live signal.

**Ambiguity is refused, not guessed.** If your broker offers both `XAUUSD.m`
and `XAUUSD.raw`, a bare `XAUUSD` signal is rejected with both options named,
because picking one would mean trading an instrument you did not choose:

```
message 413 REJECTED: XAUUSD is ambiguous - broker offers ['XAUUSD.m', 'XAUUSD.raw'];
            set SYMBOL_SUFFIX or SYMBOL_MAP=XAUUSD=<ticker> to choose
```

Resolve it either way:

```bash
SYMBOL_SUFFIX=.raw                      # prefer one account type globally
SYMBOL_MAP=XAUUSD=XAUUSD.raw            # or pin this one instrument
```

`SYMBOL_MAP` also handles names with no textual relationship
(`SYMBOL_MAP=US30=DJ30,NAS100=NDX100`). It is checked first, so it overrides
everything else.

Inspect what your broker actually offers, and dry-run the resolution:

```bash
python3 -m sigbridge.main --symbols            # every ticker
python3 -m sigbridge.main --symbols XAU        # just the gold ones
python3 -m sigbridge.main --resolve XAUUSD     # XAUUSD -> XAUUSD.p
python3 -m sigbridge.main --resolve GOLD       # GOLD -> XAUUSD.p via alias
```

Both talk to MT5 only and never place an order. The resolved ticker is what
appears in the audit log, so you can always see what was actually traded.

## Exposure caps

Two independent ceilings, both enforced before any order goes out:

| Setting | Default | Limits |
|---|---|---|
| `MAX_LOT` | 1.0 | Volume of a **single** signal |
| `MAX_OPEN_LOTS` | 1.0 | **Total** volume across all open positions *and* pending orders |

Before each signal the bot reads current exposure from the terminal
(`positions_get` + `orders_get`) and compares it against `MAX_OPEN_LOTS`:

- Room available, signal fits → placed in full.
- Room available but smaller than the signal → **trimmed** down to the
  remaining room, with a warning.
- No room, or less than one volume step left → **skipped**, with a warning.
- Exposure cannot be read → **refused**. The bot will not add exposure when it
  cannot verify the cap; it fails closed rather than assuming zero.

Remaining room is always floored to the volume step, never rounded, so the cap
cannot be breached by a fraction of a lot. On a multi-TP signal each leg
re-checks the remaining room, so later legs cannot push past the ceiling.

Every decision is logged with its arithmetic:

```
message 412 sizing: fixed size 0.01 lot -> total 0.01 lot
message 412 exposure: 0.9 lot open, 1.0 lot cap, 0.1 lot room
message 413: trimming size 0.3 -> 0.1 lot to stay within the 1.0 lot concurrent cap
message 414 SKIPPED: concurrent exposure cap reached (1.0 / 1.0 lot open)
```

## What gets logged

Every message produces an audit line in `sigbridge.log` and on stdout:

```
message 412 received: BUY EURUSD @ 1.0850 | SL 1.0800 | TP1 1.0900 | TP2 1.0950
message 412 parsed: BUY EURUSD market @1.0850 SL=1.0800 TP=[1.0900, 1.0950]
message 412 AUDIT [PLACED] BUY EURUSD lot=0.01 entry=1.0850 SL=1.08 TP=1.09 ticket=50123
message 413 REJECTED: no stop loss
```

## Safety rules built in

| Rule | Behaviour |
|---|---|
| Missing stop loss | Rejected, unless `ALLOW_MISSING_SL=true` |
| Ambiguous / conflicting fields | Rejected, never guessed |
| SL on wrong side of entry | Rejected |
| TP on wrong side of entry | Rejected |
| Symbol differs from broker ticker | Auto-resolved (suffix, case, alias, `SYMBOL_MAP`) |
| Symbol matches several tickers | Rejected unless `SYMBOL_SUFFIX`/`SYMBOL_MAP` decides |
| Symbol genuinely unavailable | Rejected |
| Signal volume over `MAX_LOT` | Capped, with a warning |
| Total open volume over `MAX_OPEN_LOTS` | Trimmed to fit, or skipped |
| Open exposure unreadable | Refused - fails closed, never assumes zero |
| Multiple TPs | Volume split evenly, each leg re-checked against the cap |
| Duplicate messages | Tracked in `.seen_ids`, never re-executed |
| Request budget | Hard rolling-24h cap, persisted across restarts |

The bot only ever acts on a message parsed from the configured channel. It has
no discretionary path — it will not open, modify, or close anything based on
news, analysis, or an instruction given at runtime.

## Rate limiting

Telegram is read with long-poll `getUpdates`, so one request covers
`POLL_SECONDS` of listening rather than firing continuously. At the default 20s
that is roughly 4,300 requests/day, plus a handful per order — well under the
10,000 cap. `RequestBudget` counts every Telegram and MT5 call in a persisted
rolling window, reserves the last 100 requests for order management so polling
can never starve an exit, and stops rather than exceeding `MAX_REQUESTS_24H`.
Errors back off exponentially (5s to 15min) instead of retrying tightly.

## Before going live

- Run on a **demo account** until the audit log matches your expectations.
- Confirm symbol resolution with `--resolve` for each instrument the channel
  trades. Automatic matching covers most brokers, but check the ones that
  matter to you rather than discovering a mismatch on a live signal.
- Check `DEFAULT_LOT`, `MAX_LOT` and `MAX_OPEN_LOTS` against your account
  size, and confirm the first few sizing/exposure lines in the log match what
  you expect by hand.
- Verify your broker's stop level (minimum SL/TP distance) — orders inside it
  are rejected by the server and will show up as `FAILED` audit lines.

## Layout

```
sigbridge/parser.py           parsing + validation (no side effects)
sigbridge/mt5_client.py       native + http backends, lot splitting
sigbridge/telegram_source.py  long-poll channel reader, offset persistence
sigbridge/budget.py           rolling 24h request cap, backoff
sigbridge/symbols.py          broker ticker resolution (pure, testable)
sigbridge/sizing.py           risk-based lot calculation (pure, testable)
sigbridge/config.py           env var loading + validation
sigbridge/main.py             poll -> parse -> execute loop
bridge_server.py              optional Windows-side REST bridge
tests/test_parser.py          18 parser/splitting cases, no network
tests/test_sizing.py          14 risk-sizing and fallback cases, no network
tests/test_exposure.py        12 exposure-cap and sizing-mode cases, no network
tests/test_symbols.py         37 symbol-resolution cases, no network
```
