"""Configuration, loaded strictly from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict


from .symbols import parse_symbol_map


class ConfigError(RuntimeError):
    pass


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
    tg_bot_token: str
    tg_channel: str
    mt5_mode: str          # "native" | "http"
    mt5_login: str
    mt5_password: str
    mt5_server: str
    mt5_http_url: str
    symbol_map: Dict[str, str]
    symbol_suffix: str
    sizing_mode: str       # "fixed" | "risk"
    default_lot: float
    risk_percent: float
    fallback_lot: float
    max_lot: float
    max_open_lots: float
    poll_seconds: int
    max_requests_24h: int
    allow_missing_sl: bool
    dry_run: bool

    @classmethod
    def from_env(cls) -> "Config":
        mode = os.environ.get("MT5_MODE", "native").strip().lower()
        if mode not in {"native", "http"}:
            raise ConfigError("MT5_MODE must be 'native' or 'http'")
        cfg = cls(
            tg_bot_token=_req("TELEGRAM_BOT_TOKEN"),
            tg_channel=_req("TELEGRAM_CHANNEL_ID"),
            mt5_mode=mode,
            mt5_login=_req("MT5_LOGIN"),
            mt5_password=_req("MT5_PASSWORD"),
            mt5_server=_req("MT5_SERVER"),
            mt5_http_url=os.environ.get("MT5_HTTP_URL", "").strip(),
            symbol_map=_symbol_map(os.environ.get("SYMBOL_MAP", "")),
            symbol_suffix=os.environ.get("SYMBOL_SUFFIX", "").strip(),
            sizing_mode=os.environ.get("SIZING_MODE", "fixed").strip().lower(),
            default_lot=float(os.environ.get("DEFAULT_LOT", "0.01")),
            risk_percent=float(os.environ.get("RISK_PERCENT", "1.0")),
            fallback_lot=float(os.environ.get("FALLBACK_LOT", "0.01")),
            max_lot=float(os.environ.get("MAX_LOT", "1.0")),
            max_open_lots=float(os.environ.get("MAX_OPEN_LOTS", "1.0")),
            poll_seconds=int(os.environ.get("POLL_SECONDS", "20")),
            max_requests_24h=int(os.environ.get("MAX_REQUESTS_24H", "9000")),
            allow_missing_sl=_flag("ALLOW_MISSING_SL", False),
            dry_run=_flag("DRY_RUN", False),
        )
        if cfg.mt5_mode == "http" and not cfg.mt5_http_url:
            raise ConfigError("MT5_MODE=http requires MT5_HTTP_URL")
        if cfg.sizing_mode not in {"fixed", "risk"}:
            raise ConfigError("SIZING_MODE must be 'fixed' or 'risk'")
        if cfg.default_lot <= 0:
            raise ConfigError("DEFAULT_LOT must be > 0")
        if not (0 < cfg.risk_percent <= 100):
            raise ConfigError("RISK_PERCENT must be >0 and <=100")
        if cfg.risk_percent > 5:
            raise ConfigError(
                f"RISK_PERCENT={cfg.risk_percent} is above the 5% sanity ceiling; "
                "set it deliberately lower or raise the ceiling in config.py")
        if cfg.fallback_lot <= 0 or cfg.fallback_lot > cfg.max_lot:
            raise ConfigError("FALLBACK_LOT must be >0 and <= MAX_LOT")
        if cfg.default_lot > cfg.max_lot:
            raise ConfigError("DEFAULT_LOT must be <= MAX_LOT")
        if cfg.max_open_lots <= 0:
            raise ConfigError("MAX_OPEN_LOTS must be > 0")
        if cfg.max_open_lots < cfg.max_lot:
            raise ConfigError(
                f"MAX_OPEN_LOTS ({cfg.max_open_lots}) is below MAX_LOT ({cfg.max_lot}); "
                "a single signal could never fill")
        if cfg.poll_seconds < 10:
            raise ConfigError("POLL_SECONDS must be >= 10 to respect the request budget")
        return cfg
