from types import SimpleNamespace
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import inspect

import requests

import bot
from db import get_group, init_db, open_db


def test_reply_menu_and_request_chat_button():
    menu = bot.main_keyboard()
    assert [button["text"] for row in menu.keyboard for button in row] == [
        "➕ Добавить группу", "📋 Список групп"
    ]
    request = bot.request_keyboard()
    selector = request.keyboard[0][0]
    assert selector["request_chat"]["chat_is_channel"] is False
    assert selector["request_chat"]["request_id"] == 1
    assert selector["request_chat"]["request_title"] is True


def test_admin_ui_is_private_only(monkeypatch):
    monkeypatch.setattr(bot, "ADMINS", {42})
    private = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(type="private"),
    )
    group = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(type="supergroup"),
    )
    other = SimpleNamespace(
        from_user=SimpleNamespace(id=99),
        chat=SimpleNamespace(type="private"),
    )
    assert bot.admin_private(private)
    assert not bot.admin_private(group)
    assert not bot.admin_private(other)


def test_only_group_mention_or_reply(monkeypatch):
    monkeypatch.setattr(bot, "identity", SimpleNamespace(username="Tg2HtmlBot", id=12))
    def message(text, reply_to=None):
        reply = (SimpleNamespace(from_user=SimpleNamespace(id=reply_to))
                 if reply_to is not None else None)
        return SimpleNamespace(text=text, reply_to_message=reply)
    assert bot.is_addressed(message("@Tg2HtmlBot общая статистика"))
    assert bot.is_addressed(message("@Tg2HtmlBot"))
    assert bot.is_addressed(message("сколько стартов?", reply_to=12))
    assert not bot.is_addressed(message("сколько стартов?"))
    assert not bot.is_addressed(message("сколько стартов?", reply_to=99))
    assert not bot.is_addressed(message("@Tg2HtmlBotFake статистика"))
    assert not bot.is_addressed(message("/stats@Tg2HtmlBot общая статистика"))
    assert not bot.is_addressed(message("/stats@AnotherBot статистика"))
    assert not bot.is_addressed(message("/stats общая статистика"))


def test_bare_mention_gets_help_reply(monkeypatch):
    monkeypatch.setattr(bot, "identity", SimpleNamespace(username="Tg2HtmlBot", id=12))
    replies = []

    def fake_reply(message, text, **kwargs):
        replies.append(text)
        return SimpleNamespace(message_id=1)

    with TemporaryDirectory() as folder:
        db_path = str(Path(folder) / "bot.sqlite3")
        monkeypatch.setattr(bot, "DATABASE", db_path)
        with closing(open_db(db_path)) as conn:
            init_db(conn)
            from db import bind_group
            bind_group(conn, -100, "Group", None, "5", "Buyer")
        monkeypatch.setattr(bot.bot, "reply_to", fake_reply)
        message = SimpleNamespace(
            text="@Tg2HtmlBot",
            chat=SimpleNamespace(id=-100, type="supergroup"),
            from_user=SimpleNamespace(id=7),
            message_id=50,
            reply_to_message=None,
        )
        bot.group_question(message)
    assert replies == [bot.EMPTY_MENTION_REPLY]


def test_tg_call_retries_connection_errors(monkeypatch):
    monkeypatch.setattr(bot.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.ConnectionError("reset")
        return "ok"

    assert bot.tg_call(flaky, attempts=3, delay=0.01) == "ok"
    assert calls["n"] == 3


def test_tg_call_does_not_retry_logic_errors():
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise ValueError("bad")

    try:
        bot.tg_call(boom, attempts=3, delay=0.01)
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert calls["n"] == 1


def test_group_handler_does_not_send_typing():
    assert "send_chat_action" not in inspect.getsource(bot.group_question)


def test_picker_confirmation_edits_same_message(monkeypatch):
    class MySQL:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    with TemporaryDirectory() as folder:
        monkeypatch.setattr(bot, "DATABASE", str(Path(folder) / "bot.sqlite3"))
        with closing(open_db(bot.DATABASE)) as conn:
            init_db(conn)
        monkeypatch.setattr(bot, "ADMINS", {42})
        monkeypatch.setattr(bot.mysql_stats, "connection", lambda: MySQL())
        monkeypatch.setattr(
            bot.mysql_stats, "buyers",
            lambda _: [{"id": "5", "name": "Buyer", "status": "Работает"}],
        )
        monkeypatch.setattr(
            bot.mysql_stats, "buyer",
            lambda _, buyer_id: {"id": buyer_id, "name": "Buyer", "status": "Работает"},
        )
        monkeypatch.setattr(bot.bot, "get_chat", lambda chat_id: SimpleNamespace(
            id=chat_id, title="Group", type="supergroup", username=None,
        ))
        monkeypatch.setattr(bot.bot, "get_me", lambda: SimpleNamespace(id=12))
        monkeypatch.setattr(bot.bot, "get_chat_member", lambda *_: SimpleNamespace(status="member"))
        monkeypatch.setattr(bot.bot, "answer_callback_query", lambda *_args, **_kw: None)
        calls = []
        monkeypatch.setattr(bot.bot, "edit_message_text",
                            lambda text, chat_id, message_id, **kw:
                            calls.append((text, chat_id, message_id, kw)))
        bot.pending[42] = {"stage": "request", "chat_id": -1001, "title": "Group"}
        try:
            bot.show_buyers(42, 42, 10)
            assert calls[-1][2] == 10
            kb = calls[-1][3]["reply_markup"]
            assert kb.keyboard[0][0].text == "Buyer (работает)"
            for data in ("pick:5", "confirm"):
                call = SimpleNamespace(
                    data=data, id="callback",
                    from_user=SimpleNamespace(id=42),
                    message=SimpleNamespace(chat=SimpleNamespace(id=42, type="private"),
                                            message_id=10),
                )
                bot.admin_callback(call)
                assert calls[-1][2] == 10
            assert "Посмотреть все группы" in calls[-1][3]["reply_markup"].keyboard[0][0].text
            with closing(open_db(bot.DATABASE)) as conn:
                assert get_group(conn, -1001)["buyer_id"] == "5"
        finally:
            bot.pending.pop(42, None)


def test_shared_chat_uses_separate_menu_and_editable_picker(monkeypatch):
    monkeypatch.setattr(bot, "ADMINS", {42})
    bot.pending[42] = {"stage": "request"}
    sent = []
    monkeypatch.setattr(
        bot.bot, "send_message",
        lambda chat_id, text, **kwargs: (
            sent.append((chat_id, text, kwargs))
            or SimpleNamespace(message_id=len(sent))
        ),
    )
    show_calls = []
    monkeypatch.setattr(bot, "show_buyers", lambda *args: show_calls.append(args))
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(id=42, type="private"),
        chat_shared=SimpleNamespace(
            request_id=1, chat_id=-1001, title="Test group", username=None
        ),
    )
    try:
        bot.shared_chat(message)
        assert "reply_markup" in sent[0][2]  # Persistent reply menu.
        assert "reply_markup" not in sent[1][2]  # Must remain editable.
        assert show_calls == [(42, 42, 2)]
    finally:
        bot.pending.pop(42, None)
