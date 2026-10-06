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
        CREATE TABLE IF NOT EXISTS observed_topics (
            chat_id INTEGER NOT NULL,
            thread_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            last_seen_utc TEXT NOT NULL,
            PRIMARY KEY (chat_id, thread_id)
        );
        CREATE TABLE IF NOT EXISTS group_messages (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            thread_id INTEGER,
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            author TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            PRIMARY KEY (chat_id, message_id)
        );
        CREATE INDEX IF NOT EXISTS idx_group_messages_context
            ON group_messages(chat_id, thread_id, message_id);
        """
    )
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(group_bindings)")
    }
    if "topic_id" not in columns:
        conn.execute("ALTER TABLE group_bindings ADD COLUMN topic_id INTEGER")
    if "topic_title" not in columns:
        conn.execute(
            "ALTER TABLE group_bindings ADD COLUMN topic_title TEXT NOT NULL DEFAULT 'General — вся группа'"
        )
    if "buyer_scope" not in columns:
        conn.execute(
            "ALTER TABLE group_bindings ADD COLUMN buyer_scope TEXT NOT NULL DEFAULT 'single'"
        )
    if "is_forum" not in columns:
        conn.execute(
            "ALTER TABLE group_bindings ADD COLUMN is_forum INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute(
            """UPDATE group_bindings SET is_forum=1
               WHERE topic_id IS NOT NULL
                  OR chat_id IN (SELECT chat_id FROM observed_topics)"""
        )
    with conn:
        # Existing installations could have retained messages from other
        # topics before the group selected a specific topic.
        conn.execute(
            """DELETE FROM group_messages
               WHERE EXISTS (
                   SELECT 1 FROM group_bindings AS binding
                   WHERE binding.chat_id=group_messages.chat_id
                     AND binding.topic_id IS NOT NULL
                     AND COALESCE(group_messages.thread_id, 1)<>binding.topic_id
               )"""
        )
        # Superseded by group_messages; never used by the AI context anymore.
        conn.execute("DROP TABLE IF EXISTS chat_history")


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
               username: str | None, buyer_id: str, buyer_name: str,
               topic_id: int | None = None,
               topic_title: str = "General — вся группа",
               is_forum: bool | None = None,
               buyer_scope: str = "single") -> None:
    forum = bool(topic_id is not None) if is_forum is None else bool(is_forum)
    with conn:
        conn.execute(
            """
            INSERT INTO group_bindings(
                chat_id,title,username,buyer_id,buyer_name,added_at_utc,
                topic_id,topic_title,is_forum,buyer_scope
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title, username=excluded.username,
                buyer_id=excluded.buyer_id, buyer_name=excluded.buyer_name,
                topic_id=excluded.topic_id, topic_title=excluded.topic_title,
                is_forum=excluded.is_forum, buyer_scope=excluded.buyer_scope
            """,
            (chat_id, title, username, buyer_id, buyer_name,
             datetime.now(timezone.utc).isoformat(), topic_id, topic_title,
             int(forum), buyer_scope),
        )
        conn.execute("DELETE FROM group_messages WHERE chat_id=?", (chat_id,))


def update_group_topic(
    conn: sqlite3.Connection, chat_id: int, topic_id: int | None, topic_title: str
) -> None:
    with conn:
        conn.execute(
            "UPDATE group_bindings SET topic_id=?, topic_title=? WHERE chat_id=?",
            (topic_id, topic_title, chat_id),
        )
        conn.execute("DELETE FROM group_messages WHERE chat_id=?", (chat_id,))


def remove_group(conn: sqlite3.Connection, chat_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM group_bindings WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM group_messages WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM observed_topics WHERE chat_id=?", (chat_id,))


def clear_group_context(conn: sqlite3.Connection, chat_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM group_messages WHERE chat_id=?", (chat_id,))


def remember_topic(conn: sqlite3.Connection, chat_id: int, thread_id: int,
                   title: str | None = None) -> None:
    if thread_id <= 1:
        return
    with conn:
        conn.execute(
            """INSERT INTO observed_topics(chat_id,thread_id,title,last_seen_utc)
               VALUES(?,?,?,?) ON CONFLICT(chat_id,thread_id) DO UPDATE SET
               title=CASE WHEN excluded.title LIKE 'Тема #%' THEN observed_topics.title
                          ELSE excluded.title END,
               last_seen_utc=excluded.last_seen_utc""",
            (chat_id, thread_id, (title or f"Тема #{thread_id}")[:150],
             datetime.now(timezone.utc).isoformat()),
        )
        if title:
            conn.execute(
                "UPDATE group_bindings SET topic_title=? "
                "WHERE chat_id=? AND topic_id=?",
                (title[:150], chat_id, thread_id),
            )


def observed_topics(conn: sqlite3.Connection, chat_id: int) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT thread_id,title,last_seen_utc FROM observed_topics "
        "WHERE chat_id=? ORDER BY thread_id LIMIT 80", (chat_id,),
    )]


def forget_topic(conn: sqlite3.Connection, chat_id: int, thread_id: int) -> None:
    with conn:
        conn.execute(
            "DELETE FROM observed_topics WHERE chat_id=? AND thread_id=?",
            (chat_id, thread_id),
        )


def record_group_message(
    conn: sqlite3.Connection, chat_id: int, message_id: int,
    thread_id: int | None, role: str, author: str, content: str,
) -> None:
    if role not in ("user", "assistant"):
        raise ValueError("Unknown group message role")
    now = datetime.now(timezone.utc)
    with conn:
        binding = get_group(conn, chat_id)
        if binding is None or (
            binding["topic_id"] is not None
            and int(binding["topic_id"]) != (thread_id or 1)
        ):
            return
        conn.execute(
            """INSERT OR IGNORE INTO group_messages(
                   chat_id,message_id,thread_id,role,author,content,created_at_utc
               ) VALUES(?,?,?,?,?,?,?)""",
            (chat_id, message_id, thread_id, role, author[:80],
             content[:1200], now.isoformat()),
        )
        conn.execute(
            """DELETE FROM group_messages WHERE chat_id=? AND message_id NOT IN (
                SELECT message_id FROM group_messages WHERE chat_id=?
                ORDER BY message_id DESC LIMIT 5000
            )""",
            (chat_id, chat_id),
        )
        conn.execute(
            "DELETE FROM group_messages WHERE created_at_utc<?",
            ((now - timedelta(days=7)).isoformat(),),
        )


def telegram_author_line(author: str, content: str) -> str:
    """Mark a chat username so the model does not treat it as the buyer."""
    return f"Telegram-автор {author} (это не байер): {content}"


def load_group_context(
    conn: sqlite3.Connection, chat_id: int, topic_id: int | None,
    before_message_id: int, limit: int = 36,
) -> list[dict[str, str]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = conn.execute(
        """SELECT role,author,content FROM (
               SELECT message_id,role,author,content FROM group_messages
               WHERE chat_id=? AND created_at_utc>=?
                 AND (? IS NULL OR COALESCE(thread_id,1)=?)
                 AND (message_id<? OR role='assistant')
               ORDER BY message_id DESC LIMIT ?
           ) ORDER BY message_id""",
        (chat_id, cutoff, topic_id, topic_id, before_message_id, limit),
    ).fetchall()
    return [
        {
            "role": row["role"],
            "content": (
                telegram_author_line(row["author"], row["content"])
                if row["role"] == "user" else row["content"]
            ),
        }
        for row in rows
    ]
