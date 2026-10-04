"""Configuration loaded strictly from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from .symbols import parse_symbol_map


class ConfigError(RuntimeError):
    pass


def _load_local_env() -> None:
    """Load a simple KEY=VALUE .env without overriding real environment vars."""
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().with_name(".env")]
    seen = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        for raw in resolved.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)


def _req(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise ConfigError(f"required environment variable {name} is not set")
    return v


def _symbol_map(raw: str) -> dict:
    try:
        return parse_symbol_map(raw)
    except ValueError as e:
        raise ConfigError(str(e)) from e


def _flag(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    telegram_mode: str
    tg_bot_token: str
    tg_api_id: int
    tg_api_hash: str
    tg_phone: str
    tg_session: str
    tg_channel: str

    mt5_mode: str
    mt5_login: str
    mt5_password: str
    mt5_server: str
    mt5_http_url: str
    bridge_secret: str

    symbol_map: Dict[str, str]
    symbol_suffix: str
    sizing_mode: str
    default_lot: float
    risk_percent: float
    max_lot: float
    max_open_lots: float
    max_open_risk_pct: float
    poll_seconds: int
    max_requests_24h: int
    dry_run: bool
    allow_live_trading: bool
    allow_real_account: bool
    state_db: str
    max_signal_age_seconds: int
    max_reference_deviation_pct: float
    max_daily_equity_loss_pct: float
    max_equity_drawdown_pct: float
    deviation_points: int
    magic: int

    @classmethod
    def from_env(cls) -> "Config":
        _load_local_env()

        telegram_mode = os.environ.get("TELEGRAM_MODE", "user").strip().lower()
        if telegram_mode not in {"user", "bot"}:
            raise ConfigError("TELEGRAM_MODE must be 'user' or 'bot'")

        tg_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        tg_api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
        tg_phone = os.environ.get("TELEGRAM_PHONE", "").strip()
        tg_session = os.environ.get(
            "TELEGRAM_SESSION",
            str(Path(__file__).resolve().with_name(".telegram_user")),
        ).strip()
        tg_api_id = 0

        if telegram_mode == "bot":
            if not tg_bot_token:
                raise ConfigError("TELEGRAM_MODE=bot requires TELEGRAM_BOT_TOKEN")
        else:
            raw_api_id = _req("TELEGRAM_API_ID")
            try:
                tg_api_id = int(raw_api_id)
            except ValueError as e:
                raise ConfigError("TELEGRAM_API_ID must be an integer") from e
            tg_api_hash = tg_api_hash or _req("TELEGRAM_API_HASH")
            tg_phone = tg_phone or _req("TELEGRAM_PHONE")
            if not tg_session:
                raise ConfigError("TELEGRAM_SESSION must not be empty")

        mode = os.environ.get("MT5_MODE", "native").strip().lower()
        if mode not in {"native", "http"}:
            raise ConfigError("MT5_MODE must be 'native' or 'http'")

        login = os.environ.get("MT5_LOGIN", "").strip()
        password = os.environ.get("MT5_PASSWORD", "").strip()
        server = os.environ.get("MT5_SERVER", "").strip()
        http_url = os.environ.get("MT5_HTTP_URL", "").strip()
        bridge_secret = os.environ.get("BRIDGE_SECRET", "").strip()

        if mode == "native":
            login = login or _req("MT5_LOGIN")
            password = password or _req("MT5_PASSWORD")
            server = server or _req("MT5_SERVER")
        else:
            if not http_url:
                raise ConfigError("MT5_MODE=http requires MT5_HTTP_URL")
            if not bridge_secret:
                raise ConfigError("MT5_MODE=http requires BRIDGE_SECRET")

        cfg = cls(
            telegram_mode=telegram_mode,
            tg_bot_token=tg_bot_token,
            tg_api_id=tg_api_id,
            tg_api_hash=tg_api_hash,
            tg_phone=tg_phone,
            tg_session=tg_session,
            tg_channel=_req("TELEGRAM_CHANNEL_ID"),
            mt5_mode=mode,
            mt5_login=login,
            mt5_password=password,
            mt5_server=server,
            mt5_http_url=http_url,
            bridge_secret=bridge_secret,
            symbol_map=_symbol_map(os.environ.get("SYMBOL_MAP", "")),
            symbol_suffix=os.environ.get("SYMBOL_SUFFIX", "").strip(),
            sizing_mode=os.environ.get("SIZING_MODE", "fixed").strip().lower(),
            default_lot=float(os.environ.get("DEFAULT_LOT", "0.01")),
            risk_percent=float(os.environ.get("RISK_PERCENT", "1.0")),
            max_lot=float(os.environ.get("MAX_LOT", "1.0")),
            max_open_lots=float(os.environ.get("MAX_OPEN_LOTS", "1.0")),
            max_open_risk_pct=float(os.environ.get("MAX_OPEN_RISK_PCT", "5.0")),
            poll_seconds=int(os.environ.get("POLL_SECONDS", "20")),
            max_requests_24h=int(os.environ.get("MAX_REQUESTS_24H", "9000")),
            dry_run=_flag("DRY_RUN", True),
            allow_live_trading=_flag("ALLOW_LIVE_TRADING", False),
            allow_real_account=_flag("ALLOW_REAL_ACCOUNT", False),
            state_db=os.environ.get("STATE_DB", ".trader_state.sqlite3").strip(),
            max_signal_age_seconds=int(os.environ.get("MAX_SIGNAL_AGE_SECONDS", "300")),
            max_reference_deviation_pct=float(os.environ.get("MAX_REFERENCE_DEVIATION_PCT", "0.25")),
            max_daily_equity_loss_pct=float(os.environ.get("MAX_DAILY_EQUITY_LOSS_PCT", "3.0")),
            max_equity_drawdown_pct=float(os.environ.get("MAX_EQUITY_DRAWDOWN_PCT", "5.0")),
            deviation_points=int(os.environ.get("DEVIATION_POINTS", "20")),
            magic=int(os.environ.get("MT5_MAGIC", "770077")),
        )

        if cfg.sizing_mode not in {"fixed", "risk"}:
            raise ConfigError("SIZING_MODE must be 'fixed' or 'risk'")
        if cfg.default_lot <= 0 or cfg.max_lot <= 0:
            raise ConfigError("DEFAULT_LOT and MAX_LOT must be > 0")
        if cfg.default_lot > cfg.max_lot:
            raise ConfigError("DEFAULT_LOT must be <= MAX_LOT")
        if not (0 < cfg.risk_percent <= 5):
            raise ConfigError("RISK_PERCENT must be > 0 and <= 5")
        if cfg.max_open_lots <= 0 or cfg.max_open_lots < cfg.max_lot:
            raise ConfigError("MAX_OPEN_LOTS must be >= MAX_LOT and > 0")
        if cfg.max_open_risk_pct <= 0:
            raise ConfigError("MAX_OPEN_RISK_PCT must be > 0")
        if cfg.poll_seconds < 10:
            raise ConfigError("POLL_SECONDS must be >= 10")
        if cfg.max_signal_age_seconds < 0:
            raise ConfigError("MAX_SIGNAL_AGE_SECONDS must be >= 0")
        if cfg.max_reference_deviation_pct < 0:
            raise ConfigError("MAX_REFERENCE_DEVIATION_PCT must be >= 0")
        if cfg.max_daily_equity_loss_pct <= 0 or cfg.max_equity_drawdown_pct <= 0:
            raise ConfigError("equity loss/drawdown limits must be > 0")
        if cfg.deviation_points < 0:
            raise ConfigError("DEVIATION_POINTS must be >= 0")
        if not cfg.dry_run and not cfg.allow_live_trading:
            raise ConfigError(
                "live trading is blocked: set ALLOW_LIVE_TRADING=true deliberately, "
                "or leave DRY_RUN=true"
            )
        return cfg
