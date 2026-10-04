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
    """Poll one Telegram chat using the signed-in user's account."""

    CURSOR_KEY = "telegram_user_last_message_id"

    def __init__(self, cfg, budget: RequestBudget, state):
        self.cfg, self.budget, self.state = cfg, budget, state
        try:
            from telethon.sync import TelegramClient
        except ImportError as e:
            raise TelegramError(
                "TELEGRAM_MODE=user requires Telethon; run: python -m pip install -r requirements.txt"
            ) from e

        self.client = TelegramClient(cfg.tg_session, cfg.tg_api_id, cfg.tg_api_hash)
        try:
            self.client.start(phone=cfg.tg_phone)
        except Exception as e:
            raise TelegramError(f"Telegram user login failed: {e}") from e

        try:
            self.budget.spend(1)
            self.entity = self.client.get_entity(self._channel_ref(cfg.tg_channel))
        except Exception:
            # Private groups often have no public username. Fall back to the
            # user's own dialog list and match exact title or numeric dialog id.
            wanted = str(cfg.tg_channel).strip()
            wanted_name = wanted.lstrip("@").lower()
            found = None
            self.budget.spend(1)
            for dialog in self.client.iter_dialogs():
                title = str(getattr(dialog, "name", "") or "")
                username = str(getattr(dialog.entity, "username", "") or "")
                if (
                    str(getattr(dialog, "id", "")) == wanted
                    or title.lower() == wanted.lower()
                    or username.lower() == wanted_name
                ):
                    found = dialog.entity
                    break
            if found is None:
                raise TelegramError(
                    f"cannot find Telegram chat {cfg.tg_channel!r} in this account's dialogs"
                )
            self.entity = found

        self.chat_id = int(getattr(self.entity, "id", 0))
        self.cursor = state.get_meta_int(self.CURSOR_KEY, 0)

        # First user-mode startup establishes a cursor at the newest existing
        # message. This prevents old signal history from being replayed.
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

        if not yielded:
            time.sleep(self.cfg.poll_seconds)

    def ack(self, update_id: int) -> None:
        mid = int(update_id)
        if mid > self.cursor:
            self.state.set_meta_int(self.CURSOR_KEY, mid)
            self.cursor = mid


class TelegramSource:
    def __init__(self, cfg, budget: RequestBudget, state):
        impl = _UserTelegramSource if cfg.telegram_mode == "user" else _BotTelegramSource
        self._impl = impl(cfg, budget, state)

    def verify(self) -> str:
        return self._impl.verify()

    def poll(self) -> Iterator[TelegramUpdate]:
        return self._impl.poll()

    def ack(self, update_id: int) -> None:
        self._impl.ack(update_id)
