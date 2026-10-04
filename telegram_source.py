"""Telegram channel reader using the Bot API long-poll getUpdates.

Long polling is used deliberately: one held-open request covers up to
`timeout` seconds, so a 20s poll costs ~4320 requests/day rather than the
tens of thousands a tight loop would burn.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterator, Tuple

from .budget import RequestBudget

log = logging.getLogger("sigbridge.telegram")
API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    pass


class TelegramSource:
    def __init__(self, cfg, budget: RequestBudget, offset_file: str = ".tg_offset"):
        self.cfg, self.budget = cfg, budget
        self.offset_file = Path(offset_file)
        self.offset = self._load_offset()

    def _load_offset(self) -> int:
        try:
            return int(self.offset_file.read_text().strip())
        except Exception:
            return 0

    def _save_offset(self) -> None:
        try:
            self.offset_file.write_text(str(self.offset))
        except Exception as e:
            log.warning("could not persist offset: %s", e)

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
        return (str(chat.get("id")) == str(self.cfg.tg_channel)
                or str(chat.get("username", "")).lower() == want)

    def poll(self) -> Iterator[Tuple[int, str]]:
        """Yield (message_id, text) for new messages in the target channel."""
        updates = self._call(
            "getUpdates",
            {"offset": self.offset, "timeout": self.cfg.poll_seconds,
             "allowed_updates": json.dumps(["channel_post", "message"])},
            timeout=self.cfg.poll_seconds + 20)
        for u in updates:
            self.offset = max(self.offset, u["update_id"] + 1)
            msg = u.get("channel_post") or u.get("message")
            if not msg:
                continue
            chat = msg.get("chat", {})
            if not self._matches_channel(chat):
                continue
            text = msg.get("text") or msg.get("caption")
            if not text:
                log.info("skipping non-text message %s", msg.get("message_id"))
                continue
            yield msg["message_id"], text
        self._save_offset()
