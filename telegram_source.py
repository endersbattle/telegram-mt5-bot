"""Telegram source supporting Bot API and logged-in user-account mode."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Iterator, Optional

from .budget import RequestBudget

log = logging.getLogger("sigbridge.telegram")
API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramUpdate:
    update_id: int
    chat_id: int
    message_id: int
    message_date: Optional[int]
    text: str
    edited: bool = False


class _BotTelegramSource:
    def __init__(self, cfg, budget: RequestBudget, state):
        self.cfg, self.budget, self.state = cfg, budget, state
        self.offset = state.get_offset()

    def _call(self, method: str, params: dict, timeout: int = 60):
        url = API.format(token=self.cfg.tg_bot_token, method=method)
        data = urllib.parse.urlencode(params).encode()
        self.budget.spend(1)
        try:
            with urllib.request.urlopen(url, data=data, timeout=timeout) as r:
                body = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise TelegramError(f"{method} HTTP {e.code}: {e.read()[:200]!r}") from e
        except Exception as e:
            raise TelegramError(f"{method} failed: {e}") from e
        if not body.get("ok"):
            raise TelegramError(f"{method} returned not-ok: {body}")
        return body["result"]

    def verify(self) -> str:
        me = self._call("getMe", {})
        return f"bot @{me.get('username')} (id={me.get('id')})"

    def _matches_channel(self, chat: dict) -> bool:
        want = str(self.cfg.tg_channel).lstrip("@").lower()
        return (
            str(chat.get("id")) == str(self.cfg.tg_channel)
            or str(chat.get("username", "")).lower() == want
        )

    def poll(self) -> Iterator[TelegramUpdate]:
        updates = self._call(
            "getUpdates",
            {
                "offset": self.offset,
                "timeout": self.cfg.poll_seconds,
                "allowed_updates": json.dumps(
                    ["channel_post", "edited_channel_post", "message", "edited_message"]
                ),
            },
            timeout=self.cfg.poll_seconds + 20,
        )
        for u in updates:
            uid = int(u["update_id"])
            edited = "edited_channel_post" in u or "edited_message" in u
            msg = (
                u.get("channel_post")
                or u.get("edited_channel_post")
                or u.get("message")
                or u.get("edited_message")
            )
            if not msg:
                self.ack(uid)
                continue
            chat = msg.get("chat", {})
            if not self._matches_channel(chat):
                self.ack(uid)
                continue
            text = msg.get("text") or msg.get("caption")
            if not text:
                log.info("skipping non-text message %s", msg.get("message_id"))
                self.ack(uid)
                continue
            yield TelegramUpdate(
                update_id=uid,
                chat_id=int(chat.get("id", 0)),
                message_id=int(msg["message_id"]),
                message_date=int(msg.get("date")) if msg.get("date") is not None else None,
                text=text,
                edited=edited,
            )

    def ack(self, update_id: int) -> None:
        new_offset = int(update_id) + 1
        if new_offset > self.offset:
            self.state.set_offset(new_offset)
            self.offset = new_offset


class _UserTelegramSource:
    """Poll a chat as the signed-in Telegram user via Telethon.

    Cursor semantics intentionally use message_id, not Telegram update ids.
    Edits keep the same message id, so already-acknowledged edits are not
    re-executed as fresh trade signals.
    """

    CURSOR_KEY = "telegram_user_last_message_id"

    def __init__(self, cfg, budget: RequestBudget, state):
        self.cfg, self.budget, self.state = cfg, budget, state
        try:
            from telethon.sync import TelegramClient
        except ImportError as e:
            raise TelegramError(
                "TELEGRAM_MODE=user requires Telethon; run: python -m pip install -r requirements.txt"
            ) from e

        self.client = TelegramClient(
            cfg.tg_session,
            cfg.tg_api_id,
            cfg.tg_api_hash,
        )
        try:
            # First run is interactive: Telethon prompts for the login code and,
            # if enabled on the account, the 2FA password. The resulting
            # .session file is reused on future starts.
            self.client.start(phone=cfg.tg_phone)
        except Exception as e:
            raise TelegramError(f"Telegram user login failed: {e}") from e

        try:
            self.budget.spend(1)
            self.entity = self.client.get_entity(self._channel_ref(cfg.tg_channel))
        except Exception as e:
            raise TelegramError(
                f"cannot access Telegram chat {cfg.tg_channel!r} with this account: {e}"
            ) from e

        self.chat_id = int(getattr(self.entity, "id", 0))
        self.cursor = state.get_meta_int(self.CURSOR_KEY, 0)

        # Safety: the first time user mode is configured, establish the cursor
        # at the latest existing message so old signal history is never replayed.
        if self.cursor <= 0:
            self.budget.spend(1)
            latest = self.client.get_messages(self.entity, limit=1)
            if latest:
                self.cursor = int(latest[0].id)
                self.state.set_meta_int(self.CURSOR_KEY, self.cursor)
                log.warning(
                    "Telegram user mode initialized at message %s; older chat history will not execute",
                    self.cursor,
                )

    @staticmethod
    def _channel_ref(raw: str):
        value = str(raw).strip()
        if value.startswith("@"):
            return value
        try:
            return int(value)
        except ValueError:
            return value

    def verify(self) -> str:
        self.budget.spend(1)
        me = self.client.get_me()
        username = getattr(me, "username", None)
        identity = f"@{username}" if username else str(getattr(me, "id", "unknown"))
        title = (
            getattr(self.entity, "title", None)
            or getattr(self.entity, "username", None)
            or str(self.chat_id)
        )
        return f"user {identity}; chat={title} (id={self.chat_id})"

    def poll(self) -> Iterator[TelegramUpdate]:
        self.budget.spend(1)
        rows = self.client.get_messages(
            self.entity,
            min_id=self.cursor,
            limit=100,
            reverse=True,
        )
        yielded = False
        for msg in rows:
            mid = int(msg.id)
            if mid <= self.cursor:
                continue
            text = (getattr(msg, "message", None) or "").strip()
            if not text:
                self.ack(mid)
                continue
            dt = getattr(msg, "date", None)
            ts = int(dt.timestamp()) if dt is not None else None
            yielded = True
            yield TelegramUpdate(
                update_id=mid,
                chat_id=self.chat_id,
                message_id=mid,
                message_date=ts,
                text=text,
                edited=False,
            )

        # Telethon history polling returns immediately. Preserve roughly the
        # same cadence as Bot API long polling when there was nothing new.
        if not yielded:
            time.sleep(self.cfg.poll_seconds)

    def ack(self, update_id: int) -> None:
        mid = int(update_id)
        if mid > self.cursor:
            self.state.set_meta_int(self.CURSOR_KEY, mid)
            self.cursor = mid


class TelegramSource:
    """Facade selecting TELEGRAM_MODE=user or TELEGRAM_MODE=bot."""

    def __init__(self, cfg, budget: RequestBudget, state):
        impl = _UserTelegramSource if cfg.telegram_mode == "user" else _BotTelegramSource
        self._impl = impl(cfg, budget, state)

    def verify(self) -> str:
        return self._impl.verify()

    def poll(self) -> Iterator[TelegramUpdate]:
        return self._impl.poll()

    def ack(self, update_id: int) -> None:
        self._impl.ack(update_id)
