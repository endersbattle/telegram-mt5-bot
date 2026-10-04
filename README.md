# Telegram → MT5 bridge

A strict Telegram signal copier for MetaTrader 5 with conservative execution
safety. Ambiguous or unverifiable signals are rejected rather than guessed.

## Telegram modes

The hardened branch supports two Telegram input modes:

- `TELEGRAM_MODE=user` — log in as your own Telegram account with Telethon.
  No BotFather bot or group-admin permission is required. Your account only
  needs normal access to the signal group/channel.
- `TELEGRAM_MODE=bot` — legacy Bot API mode using `TELEGRAM_BOT_TOKEN`.

User mode is the recommended setup when you cannot add a bot to the group.

Create `.env` beside the Python files:

```text
TELEGRAM_MODE=user
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=replace-me
TELEGRAM_PHONE=+441234567890
TELEGRAM_SESSION=.telegram_user

# Public group:
TELEGRAM_CHANNEL_ID=@groupusername

# Or, for private groups, use the exact group title or numeric dialog id:
# TELEGRAM_CHANNEL_ID=My Signal Group
```

The code loads `.env` automatically.

On the first run, Telethon will ask for the Telegram login code sent to your
account and, if enabled, your Telegram two-step-verification password. The
resulting `.session` file is a login credential and is intentionally ignored
by Git. Do not share it.

On first initialization, user mode records the newest existing message as its
cursor. Older chat history is skipped so historical trade signals are not
executed. After that, only newer message IDs are processed.

## Install

From the project folder:

```powershell
python -m pip install -r requirements.txt
```

Then configure the remaining MT5 values in `.env`.

## MT5 native Windows mode

```text
MT5_MODE=native
MT5_LOGIN=...
MT5_PASSWORD=...
MT5_SERVER=...
```

## Safe first test

Keep these values:

```text
DRY_RUN=true
ALLOW_LIVE_TRADING=false
ALLOW_REAL_ACCOUNT=false
```

Run the package from its parent folder. If your project folder is
`C:\Users\benpe\Desktop\icmarketsbot`:

```powershell
cd C:\Users\benpe\Desktop
python -m icmarketsbot.main --check
```

The first user-mode check may prompt for your Telegram login code. After a
successful login, the local session is reused automatically.

Then perform one safe poll:

```powershell
python -m icmarketsbot.main --once
```

With `DRY_RUN=true`, orders are validated and logged but are not sent.

## Execution safety

This branch uses durable SQLite state and per-leg idempotency. A signal and each
TP leg are persisted before broker submission. If the process or HTTP link dies
during submission, the order is recorded as `UNKNOWN` and reconciled against
MT5 before any retry; it is never blindly sent twice.

Other fail-closed controls include:

- `DRY_RUN=true` by default.
- Live orders additionally require `ALLOW_LIVE_TRADING=true`.
- Real MT5 accounts additionally require `ALLOW_REAL_ACCOUNT=true`.
- Stop loss is mandatory.
- Edited/replayed Telegram messages are not treated as fresh trade signals.
- Stale signals are rejected with `MAX_SIGNAL_AGE_SECONDS`.
- Market sizing uses the current executable side.
- Risk sizing uses account equity and MT5 `order_calc_profit`.
- Broker volume/stop/filling rules are checked before submission.
- `order_check` runs before `order_send`.
- Ambiguous MT5 outcomes are reconciled rather than blindly retried.
- Open lots, monetary risk, daily equity loss and drawdown are capped.
- Request-budget persistence is atomic.

## Security

Never commit:

- `.env`
- Telegram `.session` files
- MT5 passwords
- Telegram API hashes or bot tokens

If any credentials were previously committed to a public Git repository,
rotate them; replacing the latest file does not erase Git history.
