"""SQLite storage for Telegram group bindings and short chat history only."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def open_db(path: str) -> sqlite3.Connection:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS group_bindings (
            chat_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            username TEXT,
            buyer_id TEXT NOT NULL,
            buyer_name TEXT NOT NULL,
            added_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chat_history_chat ON chat_history(chat_id, id);
        """
    )


def get_group(conn: sqlite3.Connection, chat_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM group_bindings WHERE chat_id=?", (chat_id,)).fetchone()
    return dict(row) if row else None


def count_groups(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM group_bindings").fetchone()[0]


def list_groups(conn: sqlite3.Connection, page: int, page_size: int = 10) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM group_bindings ORDER BY added_at_utc DESC, chat_id DESC "
            "LIMIT ? OFFSET ?",
            (page_size, max(0, page) * page_size),
        )
    ]


def bind_group(conn: sqlite3.Connection, chat_id: int, title: str,
               username: str | None, buyer_id: str, buyer_name: str) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO group_bindings(chat_id,title,username,buyer_id,buyer_name,added_at_utc)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title, username=excluded.username,
                buyer_id=excluded.buyer_id, buyer_name=excluded.buyer_name
            """,
            (chat_id, title, username, buyer_id, buyer_name,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.execute("DELETE FROM chat_history WHERE chat_id=?", (chat_id,))


def remove_group(conn: sqlite3.Connection, chat_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM group_bindings WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM chat_history WHERE chat_id=?", (chat_id,))


def load_chat_history(conn: sqlite3.Connection, chat_id: int, limit: int = 12) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = conn.execute(
        "SELECT role,content FROM (SELECT id,role,content FROM chat_history "
        "WHERE chat_id=? AND created_at_utc>=? ORDER BY id DESC LIMIT ?) ORDER BY id",
        (chat_id, cutoff, limit),
    )
    return [dict(row) for row in rows]


def save_chat_turn(conn: sqlite3.Connection, chat_id: int, question: str, answer: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.executemany(
            "INSERT INTO chat_history(chat_id,role,content,created_at_utc) VALUES(?,?,?,?)",
            [(chat_id, "user", question[:4000], now),
             (chat_id, "assistant", answer[:3800], now)],
        )
        conn.execute(
            "DELETE FROM chat_history WHERE chat_id=? AND id NOT IN "
            "(SELECT id FROM chat_history WHERE chat_id=? ORDER BY id DESC LIMIT 12)",
            (chat_id, chat_id),
        )
        conn.execute(
            "DELETE FROM chat_history WHERE created_at_utc<?",
            ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),),
        )


def clear_chat_history(conn: sqlite3.Connection, chat_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM chat_history WHERE chat_id=?", (chat_id,))
