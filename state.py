"""Durable execution state for crash-safe Telegram -> MT5 processing."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class Leg:
    chat_id: int
    message_id: int
    leg_index: int
    idempotency_key: str
    symbol: str
    direction: str
    order_kind: str
    volume: float
    entry: Optional[float]
    sl: float
    tp: Optional[float]
    status: str
    ticket: Optional[int]
    detail: str


class StateStore:
    """SQLite WAL state store.

    Signal identity is (chat_id, message_id); each TP leg has its own durable
    idempotency key.  Writes are committed before external side effects.
    """

    def __init__(self, path: str = ".trader_state.sqlite3"):
        self.path = path
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS signals(
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    update_id INTEGER NOT NULL,
                    message_date INTEGER,
                    text_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(chat_id, message_id)
                )
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS legs(
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    leg_index INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    order_kind TEXT NOT NULL,
                    volume REAL NOT NULL,
                    entry REAL,
                    sl REAL NOT NULL,
                    tp REAL,
                    status TEXT NOT NULL,
                    ticket INTEGER,
                    detail TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(chat_id, message_id, leg_index)
                )
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS equity_state(
                    day TEXT PRIMARY KEY,
                    start_equity REAL NOT NULL,
                    high_equity REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)

    @staticmethod
    def key(chat_id: int, message_id: int, leg_index: int) -> str:
        raw = f"{chat_id}:{message_id}:{leg_index}".encode()
        return hashlib.sha256(raw).hexdigest()[:32]

    def get_meta_int(self, key: str, default: int = 0) -> int:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return int(row["value"]) if row else int(default)

    def set_meta_int(self, key: str, value: int) -> None:
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(int(value))),
            )

    def get_offset(self) -> int:
        row = self.db.execute(
            "SELECT value FROM meta WHERE key='telegram_bot_offset'"
        ).fetchone()
        if row:
            return int(row["value"])
        # Backward-compatible migration from the first hardening revision.
        legacy = self.db.execute(
            "SELECT value FROM meta WHERE key='telegram_offset'"
        ).fetchone()
        return int(legacy["value"]) if legacy else 0

    def set_offset(self, offset: int) -> None:
        self.set_meta_int("telegram_bot_offset", offset)

    def begin_signal(
        self,
        chat_id: int,
        message_id: int,
        update_id: int,
        message_date: Optional[int],
        text: str,
    ) -> str:
        now = time.time()
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        with self._lock, self.db:
            row = self.db.execute(
                "SELECT status,text_hash FROM signals WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            ).fetchone()
            if row:
                if row["text_hash"] != digest:
                    raise RuntimeError("message content changed for an existing signal identity")
                return str(row["status"])
            self.db.execute(
                "INSERT INTO signals(chat_id,message_id,update_id,message_date,text_hash,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'RECEIVED',?,?)",
                (chat_id, message_id, update_id, message_date, digest, now, now),
            )
        return "RECEIVED"

    def set_signal_status(self, chat_id: int, message_id: int, status: str, detail: str = "") -> None:
        with self._lock, self.db:
            self.db.execute(
                "UPDATE signals SET status=?,detail=?,updated_at=? WHERE chat_id=? AND message_id=?",
                (status, detail[:1000], time.time(), chat_id, message_id),
            )

    def upsert_leg(
        self, chat_id: int, message_id: int, leg_index: int, symbol: str,
        direction: str, order_kind: str, volume: float, entry: Optional[float],
        sl: float, tp: Optional[float],
    ) -> Leg:
        key = self.key(chat_id, message_id, leg_index)
        now = time.time()
        with self._lock, self.db:
            self.db.execute(
                """INSERT INTO legs(
                       chat_id,message_id,leg_index,idempotency_key,symbol,direction,order_kind,
                       volume,entry,sl,tp,status,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'VALIDATED',?)
                   ON CONFLICT(chat_id,message_id,leg_index) DO NOTHING""",
                (chat_id, message_id, leg_index, key, symbol, direction, order_kind,
                 float(volume), entry, float(sl), tp, now),
            )
        return self.get_leg(chat_id, message_id, leg_index)

    def get_leg(self, chat_id: int, message_id: int, leg_index: int) -> Leg:
        row = self.db.execute(
            "SELECT * FROM legs WHERE chat_id=? AND message_id=? AND leg_index=?",
            (chat_id, message_id, leg_index),
        ).fetchone()
        if not row:
            raise KeyError((chat_id, message_id, leg_index))
        return Leg(
            row["chat_id"], row["message_id"], row["leg_index"], row["idempotency_key"],
            row["symbol"], row["direction"], row["order_kind"], row["volume"],
            row["entry"], row["sl"], row["tp"], row["status"], row["ticket"], row["detail"],
        )

    def set_leg_status(
        self, chat_id: int, message_id: int, leg_index: int,
        status: str, ticket: Optional[int] = None, detail: str = "",
    ) -> None:
        with self._lock, self.db:
            self.db.execute(
                "UPDATE legs SET status=?,ticket=COALESCE(?,ticket),detail=?,updated_at=? "
                "WHERE chat_id=? AND message_id=? AND leg_index=?",
                (status, ticket, detail[:1000], time.time(), chat_id, message_id, leg_index),
            )

    def unsettled_legs(self) -> Iterable[Leg]:
        rows = self.db.execute(
            "SELECT * FROM legs WHERE status IN ('SUBMITTING','UNKNOWN') ORDER BY updated_at"
        ).fetchall()
        for row in rows:
            yield Leg(
                row["chat_id"], row["message_id"], row["leg_index"], row["idempotency_key"],
                row["symbol"], row["direction"], row["order_kind"], row["volume"],
                row["entry"], row["sl"], row["tp"], row["status"], row["ticket"], row["detail"],
            )

    def update_equity(self, day: str, equity: float) -> tuple[float, float]:
        now = time.time()
        with self._lock, self.db:
            row = self.db.execute(
                "SELECT start_equity,high_equity FROM equity_state WHERE day=?", (day,)
            ).fetchone()
            if not row:
                self.db.execute(
                    "INSERT INTO equity_state(day,start_equity,high_equity,updated_at) VALUES(?,?,?,?)",
                    (day, equity, equity, now),
                )
                return equity, equity
            start = float(row["start_equity"])
            high = max(float(row["high_equity"]), equity)
            self.db.execute(
                "UPDATE equity_state SET high_equity=?,updated_at=? WHERE day=?",
                (high, now, day),
            )
            return start, high
