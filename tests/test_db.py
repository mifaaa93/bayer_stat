from contextlib import closing
from tempfile import TemporaryDirectory
from pathlib import Path

from db import (
    bind_group, clear_group_context, count_groups, forget_topic, get_group, init_db,
    list_groups, load_group_context, observed_topics, open_db,
    record_group_message, remember_topic, remove_group,
)


def test_group_crud_history_and_pagination():
    with TemporaryDirectory() as folder:
        with closing(open_db(str(Path(folder) / "bot.sqlite3"))) as conn:
            init_db(conn)
            assert set(row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )) == {"group_bindings", "group_messages", "observed_topics"}
            for i in range(23):
                bind_group(conn, -1000 - i, f"Group {i}", None, "5", "Buyer")
            assert count_groups(conn) == 23
            assert len(list_groups(conn, 0)) == 10
            assert len(list_groups(conn, 1)) == 10
            assert len(list_groups(conn, 2)) == 3
            record_group_message(conn, -1000, 1, None, "user", "Alice", "первый")
            record_group_message(conn, -1001, 1, None, "user", "Bob", "другой чат")
            assert len(load_group_context(conn, -1000, None, 100)) == 1
            bind_group(conn, -1000, "New title", "newname", "7", "Other buyer")
            assert get_group(conn, -1000)["buyer_id"] == "7"
            assert not load_group_context(conn, -1000, None, 100)
            assert len(load_group_context(conn, -1001, None, 100)) == 1
            remove_group(conn, -1001)
            assert get_group(conn, -1001) is None
            assert not load_group_context(conn, -1001, None, 100)


def test_chat_context_is_bounded_per_chat():
    with TemporaryDirectory() as folder:
        with closing(open_db(str(Path(folder) / "bot.sqlite3"))) as conn:
            init_db(conn)
            bind_group(conn, -1000, "Group", None, "5", "Buyer")
            for i in range(45):
                record_group_message(conn, -1000, i, 1, "user", "Alice", f"q{i}")
            history = load_group_context(conn, -1000, None, 100)
            assert len(history) == 36
            assert history[0]["content"] == "Alice: q9"
            clear_group_context(conn, -1000)
            assert load_group_context(conn, -1000, None, 100) == []


def test_topic_scoped_context_and_delayed_answer():
    with TemporaryDirectory() as folder:
        with closing(open_db(str(Path(folder) / "bot.sqlite3"))) as conn:
            init_db(conn)
            bind_group(conn, -100, "Forum", None, "5", "Buyer", 10, "Ads")
            remember_topic(conn, -100, 10, "Ads")
            remember_topic(conn, -100, 20, "Other")
            record_group_message(conn, -100, 1, 10, "user", "Alice", "План")
            record_group_message(conn, -100, 2, 20, "user", "Bob", "Другой топик")
            record_group_message(conn, -100, 3, 10, "user", "Alice", "Вопрос 2")
            record_group_message(conn, -100, 4, 10, "user", "Alice", "Будущий вопрос")
            # Reply to the earlier question may be posted after question 2.
            record_group_message(conn, -100, 5, 10, "assistant", "bot", "Ответ 1")
            record_group_message(conn, -100, 1, 10, "user", "Mallory", "Дубль")
            scoped = load_group_context(conn, -100, 10, before_message_id=3)
            assert scoped == [
                {"role": "user", "content": "Alice: План"},
                {"role": "assistant", "content": "Ответ 1"},
            ]
            whole_group = load_group_context(conn, -100, None, before_message_id=3)
            assert [item["content"] for item in whole_group] == [
                "Alice: План", "Ответ 1",
            ]
            assert conn.execute(
                "SELECT COUNT(*) FROM group_messages WHERE thread_id=20"
            ).fetchone()[0] == 0
            forget_topic(conn, -100, 20)
            assert [topic["thread_id"] for topic in observed_topics(conn, -100)] == [10]
            remove_group(conn, -100)
            assert not observed_topics(conn, -100)
            assert not load_group_context(conn, -100, None, before_message_id=100)


def test_existing_groups_gain_nullable_topic_on_migration():
    with TemporaryDirectory() as folder:
        with closing(open_db(str(Path(folder) / "bot.sqlite3"))) as conn:
            conn.execute(
                """CREATE TABLE group_bindings(
                    chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL,
                    username TEXT, buyer_id TEXT NOT NULL,
                    buyer_name TEXT NOT NULL, added_at_utc TEXT NOT NULL
                )"""
            )
            conn.execute(
                "INSERT INTO group_bindings VALUES (-100, 'Old', NULL, '5', 'Buyer', '2026-09-26')"
            )
            conn.commit()
            init_db(conn)
            assert get_group(conn, -100)["topic_id"] is None
            assert get_group(conn, -100)["topic_title"] == "General — вся группа"


def test_cleanup_drops_legacy_table_and_removes_only_off_topic_context():
    with TemporaryDirectory() as folder:
        with closing(open_db(str(Path(folder) / "bot.sqlite3"))) as conn:
            init_db(conn)
            bind_group(conn, -100, "Forum", None, "5", "Buyer", 10, "Ads")
            bind_group(conn, -200, "All topics", None, "5", "Buyer")
            remember_topic(conn, -100, 20, "Other")
            conn.execute("CREATE TABLE chat_history (id INTEGER PRIMARY KEY, content TEXT)")
            # Simulate records from the old implementation.
            for chat_id, mid, thread_id in (
                (-100, 1, 10), (-100, 2, 20), (-100, 3, None), (-200, 1, 20)
            ):
                conn.execute(
                    """INSERT INTO group_messages VALUES
                       (?, ?, ?, 'user', 'Alice', 'message', '2026-09-27T00:00:00+00:00')""",
                    (chat_id, mid, thread_id),
                )
            conn.commit()
            init_db(conn)
            init_db(conn)  # Migration is safe to repeat.
            assert conn.execute(
                "SELECT name FROM sqlite_master WHERE name='chat_history'"
            ).fetchone() is None
            assert [tuple(row) for row in conn.execute(
                "SELECT chat_id,message_id FROM group_messages ORDER BY chat_id,message_id"
            )] == [(-200, 1), (-100, 1)]
            assert observed_topics(conn, -100)[0]["thread_id"] == 20
            assert count_groups(conn) == 2
