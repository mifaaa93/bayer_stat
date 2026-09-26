from contextlib import closing
from tempfile import TemporaryDirectory
from pathlib import Path

from db import (
    bind_group, clear_chat_history, count_groups, get_group, init_db,
    list_groups, load_chat_history, open_db, remove_group, save_chat_turn,
)


def test_group_crud_history_and_pagination():
    with TemporaryDirectory() as folder:
        with closing(open_db(str(Path(folder) / "bot.sqlite3"))) as conn:
            init_db(conn)
            assert set(row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )) == {"group_bindings", "chat_history", "sqlite_sequence"}
            for i in range(23):
                bind_group(conn, -1000 - i, f"Group {i}", None, "5", "Buyer")
            assert count_groups(conn) == 23
            assert len(list_groups(conn, 0)) == 10
            assert len(list_groups(conn, 1)) == 10
            assert len(list_groups(conn, 2)) == 3
            save_chat_turn(conn, -1000, "первый", "ответ")
            save_chat_turn(conn, -1001, "другой чат", "другой ответ")
            assert len(load_chat_history(conn, -1000)) == 2
            bind_group(conn, -1000, "New title", "newname", "7", "Other buyer")
            assert get_group(conn, -1000)["buyer_id"] == "7"
            assert not load_chat_history(conn, -1000)
            assert len(load_chat_history(conn, -1001)) == 2
            remove_group(conn, -1001)
            assert get_group(conn, -1001) is None
            assert not load_chat_history(conn, -1001)


def test_chat_context_is_bounded_per_chat():
    with TemporaryDirectory() as folder:
        with closing(open_db(str(Path(folder) / "bot.sqlite3"))) as conn:
            init_db(conn)
            for i in range(8):
                save_chat_turn(conn, 1, f"q{i}", f"a{i}")
            assert len(load_chat_history(conn, 1)) == 12
            assert load_chat_history(conn, 1)[0]["content"] == "q2"
            clear_chat_history(conn, 1)
            assert load_chat_history(conn, 1) == []
