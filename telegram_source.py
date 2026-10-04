"""Telegram Bot API long-poll source with explicit durable acknowledgement."""
from __future__ import annotations

import json
import logging
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


class TelegramSource:
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
        return f"@{me.get('username')} (id={me.get('id')})"

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
