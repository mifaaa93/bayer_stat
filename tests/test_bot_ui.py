from types import SimpleNamespace
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import inspect
import time
from threading import Event

import requests
from telebot.apihelper import ApiTelegramException

import bot
from db import bind_group, get_group, init_db, observed_topics, open_db


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


def test_trigger_word_matches_only_separate_word(monkeypatch):
    monkeypatch.setattr(bot, "identity", SimpleNamespace(username="Tg2HtmlBot", id=12))
    def message(text):
        return SimpleNamespace(text=text, caption=None, reply_to_message=None)
    for text in ("кит", "КИТ!", "кит: помоги", "привет, кит, покажи цифры"):
        assert bot.trigger_text(message(text))
    for text in ("китовий", "киты", "ракита", "_кит_", "покажи статистику", "/кит"):
        assert not bot.trigger_text(message(text))


def test_trigger_word_is_removed_before_ai_question():
    assert bot.TRIGGER_WORD.sub(" ", "статистика за сегодня кит") \
        .strip() == "статистика за сегодня"
    assert bot.TRIGGER_WORD.sub(" ", "кит статистика за вчера кит") \
        .strip() == "статистика за вчера"


def test_unknown_topic_title_is_not_presented_as_real_name():
    assert bot.topic_label("Тема #3", 3) == "Топик #3 (название не получено)"
    assert bot.topic_label("Performance", 3) == "Performance"


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
        bot.process_question({"message": message, "question": message.text, "thread_id": 1})
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


def test_telegram_429_uses_retry_after_instead_of_short_retry(monkeypatch):
    now = [100.0]
    sleeps = []
    attempts = []
    monkeypatch.setattr(bot, "_tg_not_before", {})
    monkeypatch.setattr(bot.time, "monotonic", lambda: now[0])
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    monkeypatch.setattr(bot.time, "sleep", sleep)
    def send():
        attempts.append(now[0])
        if len(attempts) == 1:
            raise ApiTelegramException(
                "sendRichMessage", None,
                {"error_code": 429, "description": "Too Many Requests: retry after 22",
                 "parameters": {"retry_after": 22}},
            )
        return "sent"
    assert bot.tg_call(send, attempts=2, delay=0.1) == "sent"
    assert attempts == [100.0, 123.0]
    assert sleeps == [23.0]


def test_rate_limit_reserves_group_slot(monkeypatch):
    now = [10.0]
    sleeps = []
    monkeypatch.setattr(bot, "_tg_not_before", {})
    monkeypatch.setattr(bot.time, "monotonic", lambda: now[0])
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    monkeypatch.setattr(bot.time, "sleep", sleep)
    def send_message(chat_id, text):
        return now[0]
    assert bot.tg_call(send_message, -100, "one") == 10.0
    assert bot.tg_call(send_message, -100, "two") == 13.0
    assert sleeps == [3.0]


def test_optional_edit_skips_busy_group_slot_without_waiting(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(bot, "_tg_not_before", {-100: 13.0})
    monkeypatch.setattr(bot.time, "monotonic", lambda: now[0])
    waits = []
    calls = []
    monkeypatch.setattr(bot.time, "sleep", lambda seconds: waits.append(seconds))

    def edit_message_text(text, chat_id, message_id):
        calls.append((text, chat_id, message_id))
        return True

    assert bot.tg_call(
        edit_message_text, "Данные получены", -100, 123,
        attempts=1, skip_if_busy=True,
    ) is None
    assert waits == [] and calls == []
    now[0] = 14.0
    assert bot.tg_call(
        edit_message_text, "Данные получены", -100, 123,
        attempts=1, skip_if_busy=True,
    ) is True
    assert len(calls) == 1


def test_group_handler_does_not_send_typing():
    assert "send_chat_action" not in inspect.getsource(bot.process_question)
    assert bot.bot.message_handlers[0]["function"] is bot.group_message


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
            assert kb.keyboard[0][0].text == "🌐 Общая статистика (все байеры)"
            assert kb.keyboard[1][0].text == "Buyer (работает)"
            assert "*" in bot.pending[42]["buyers"]
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
    monkeypatch.setattr(
        bot.bot, "get_chat",
        lambda chat_id: SimpleNamespace(id=chat_id, is_forum=False),
    )
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


def test_forum_topic_selection_uses_observed_topics(monkeypatch):
    with TemporaryDirectory() as folder:
        monkeypatch.setattr(bot, "DATABASE", str(Path(folder) / "bot.sqlite3"))
        monkeypatch.setattr(bot, "ADMINS", {42})
        with closing(open_db(bot.DATABASE)) as conn:
            init_db(conn)
            from db import remember_topic
            remember_topic(conn, -1001, 57, "Performance")
        edits = []
        monkeypatch.setattr(bot.bot, "get_chat", lambda _:
                            SimpleNamespace(id=-1001, is_forum=True))
        monkeypatch.setattr(bot.bot, "edit_message_text", lambda *args, **kwargs:
                            edits.append((args, kwargs)))
        monkeypatch.setattr(bot.bot, "answer_callback_query", lambda *_, **__: None)
        buyer_calls = []
        monkeypatch.setattr(bot, "show_buyers", lambda *args: buyer_calls.append(args))
        bot.pending[42] = {"stage": "request", "chat_id": -1001, "title": "Forum"}
        try:
            bot.show_topics(42, 42, 5)
            buttons = [
                button.text for row in edits[-1][1]["reply_markup"].keyboard
                for button in row
            ]
            assert bot.ALL_TOPICS in buttons
            assert "Performance" in buttons
            call = SimpleNamespace(
                data="topic:57", id="callback",
                from_user=SimpleNamespace(id=42),
                message=SimpleNamespace(
                    chat=SimpleNamespace(id=42, type="private"), message_id=5
                ),
            )
            bot.admin_callback(call)
            assert bot.pending[42]["topic_id"] == 57
            assert buyer_calls == [(42, 42, 5)]
        finally:
            bot.pending.pop(42, None)


def test_change_topic_keeps_buyer_and_shows_back_button(monkeypatch):
    with TemporaryDirectory() as folder:
        monkeypatch.setattr(bot, "DATABASE", str(Path(folder) / "bot.sqlite3"))
        monkeypatch.setattr(bot, "ADMINS", {42})
        with closing(open_db(bot.DATABASE)) as conn:
            init_db(conn)
            bind_group(conn, -1001, "Forum", None, "5", "Buyer", None, "General — вся группа")
            from db import remember_topic
            remember_topic(conn, -1001, 57, "Performance")
        monkeypatch.setattr(
            bot.bot, "get_chat",
            lambda _id: SimpleNamespace(id=-1001, is_forum=True),
        )
        edits = []
        monkeypatch.setattr(
            bot.bot, "edit_message_text",
            lambda *args, **kwargs: edits.append((args, kwargs)),
        )
        monkeypatch.setattr(bot.bot, "answer_callback_query", lambda *_args, **_kw: None)
        call = SimpleNamespace(
            data="change_topic:-1001:3", id="callback",
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=42, type="private"), message_id=10
            ),
        )
        bot.admin_callback(call)
        assert bot.pending[42]["mode"] == "change_topic"
        buttons = [
            button.text
            for row in edits[-1][1]["reply_markup"].keyboard
            for button in row
        ]
        assert "⬅️ Назад" in buttons
        assert "❌ Отменить" not in buttons
        # Selecting topic updates only topic, not buyer.
        call.data = "topic:57"
        bot.admin_callback(call)
        with closing(open_db(bot.DATABASE)) as conn:
            group = get_group(conn, -1001)
            assert group["buyer_id"] == "5"
            assert group["topic_id"] == 57
        bot.pending.pop(42, None)


def test_change_buyer_has_back_at_picker_and_confirmation(monkeypatch):
    class MySQL:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass
    monkeypatch.setattr(bot, "ADMINS", {42})
    monkeypatch.setattr(bot.mysql_stats, "connection", lambda: MySQL())
    monkeypatch.setattr(
        bot.mysql_stats, "buyers",
        lambda _: [{"id": "5", "name": "Buyer", "status": "Работает"}],
    )
    monkeypatch.setattr(bot.bot, "answer_callback_query", lambda *_, **__: None)
    edits = []
    monkeypatch.setattr(
        bot.bot, "edit_message_text",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    cards = []
    monkeypatch.setattr(bot, "group_card", lambda *args: cards.append(args))
    bot.pending[42] = {
        "stage": "choose", "mode": "change_buyer", "chat_id": -1001,
        "title": "Group", "topic_title": bot.ALL_TOPICS,
        "back_group_id": -1001, "back_page": 2,
    }
    call = SimpleNamespace(
        data="", id="callback", from_user=SimpleNamespace(id=42),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=42, type="private"), message_id=5
        ),
    )
    try:
        bot.show_buyers(42, 42, 5)
        picker_buttons = [
            button.text for row in edits[-1][1]["reply_markup"].keyboard
            for button in row
        ]
        assert "⬅️ Назад" in picker_buttons
        assert "❌ Отменить" not in picker_buttons
        call.data = "pick:5"
        bot.admin_callback(call)
        confirm_buttons = [
            button.text for row in edits[-1][1]["reply_markup"].keyboard
            for button in row
        ]
        assert "⬅️ Назад" in confirm_buttons
        assert "❌ Отменить" not in confirm_buttons
        call.data = "buyer_confirm_back"
        bot.admin_callback(call)
        assert bot.pending[42]["stage"] == "choose"
        call.data = "buyer_back:-1001:2"
        bot.admin_callback(call)
        assert cards == [(42, -1001, 2, 5)]
    finally:
        bot.pending.pop(42, None)


def test_admin_can_name_topic_when_bot_never_saw_its_creation(monkeypatch):
    with TemporaryDirectory() as folder:
        monkeypatch.setattr(bot, "DATABASE", str(Path(folder) / "bot.sqlite3"))
        monkeypatch.setattr(bot, "ADMINS", {42})
        with closing(open_db(bot.DATABASE)) as conn:
            init_db(conn)
            bind_group(conn, -1001, "Forum", None, "5", "Buyer", 57, "Тема #57")
        edits = []
        cards = []
        monkeypatch.setattr(bot.bot, "edit_message_text",
                            lambda *args, **kwargs: edits.append((args, kwargs)))
        monkeypatch.setattr(bot.bot, "answer_callback_query", lambda *_, **__: None)
        monkeypatch.setattr(bot, "group_card", lambda *args: cards.append(args))
        call = SimpleNamespace(
            data="rename_topic:-1001:0", id="callback",
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=42, type="private"), message_id=5
            ),
        )
        try:
            bot.admin_callback(call)
            assert bot.pending[42]["stage"] == "rename_topic"
            bot.admin_text(SimpleNamespace(
                text="Реальное название", from_user=SimpleNamespace(id=42),
                chat=SimpleNamespace(id=42, type="private"),
            ))
            with closing(open_db(bot.DATABASE)) as conn:
                assert get_group(conn, -1001)["topic_title"] == "Реальное название"
                assert observed_topics(conn, -1001)[0]["title"] == "Реальное название"
            assert cards == [(42, -1001, 0, 5)]
        finally:
            bot.pending.pop(42, None)


def test_messages_are_recorded_but_only_trigger_in_selected_topic(monkeypatch):
    with TemporaryDirectory() as folder:
        monkeypatch.setattr(bot, "DATABASE", str(Path(folder) / "bot.sqlite3"))
        monkeypatch.setattr(bot, "identity", SimpleNamespace(username="Tg2HtmlBot", id=12))
        with closing(open_db(bot.DATABASE)) as conn:
            init_db(conn)
            bind_group(conn, -100, "Forum", None, "5", "Buyer", 57, "Ads")
        queued = []
        monkeypatch.setattr(bot, "enqueue_question",
                            lambda message, question, group: queued.append((message.message_id, question)))
        def message(message_id, thread_id, text):
            return SimpleNamespace(
                chat=SimpleNamespace(id=-100, type="supergroup"),
                message_id=message_id, message_thread_id=thread_id,
                content_type="text", text=text, caption=None,
                from_user=SimpleNamespace(id=7, first_name="Alice",
                                          last_name="", username="alice"),
                reply_to_message=None, forum_topic_created=None,
                forum_topic_edited=None, forum_topic_deleted=None,
            )
        bot.group_message(message(1, 57, "обычный разговор"))
        bot.group_message(message(2, 57, "китовий"))
        bot.group_message(message(3, 58, "кит, привет"))
        bot.group_message(message(4, 57, "кит, цифры за вчера"))
        assert queued == [(4, "кит, цифры за вчера")]
        with closing(open_db(bot.DATABASE)) as conn:
            assert [r[0] for r in conn.execute(
                "SELECT message_id FROM group_messages WHERE chat_id=-100 ORDER BY message_id"
            )] == [1, 2, 4]
            assert {r["thread_id"] for r in observed_topics(conn, -100)} == {57, 58}


def test_new_questions_are_queued_in_order(monkeypatch):
    chat_id = -500
    first_started, release, finished = Event(), Event(), Event()
    calls = []
    def process(request):
        calls.append(request["question"])
        if request["question"] == "первый":
            first_started.set()
            assert release.wait(3)
        else:
            finished.set()
    monkeypatch.setattr(bot, "process_question", process)
    bot.group_queues.pop(chat_id, None)
    first = SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=1)
    second = SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=2)
    try:
        bot.enqueue_question(first, "первый")
        assert first_started.wait(2)
        bot.enqueue_question(second, "второй")
        assert len(bot.group_queues[chat_id]["items"]) == 1
        release.set()
        assert finished.wait(3)
        deadline = time.monotonic() + 3
        while bot.group_queues[chat_id]["running"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls == ["первый", "второй"]
    finally:
        release.set()
        bot.group_queues.pop(chat_id, None)


def test_accumulated_questions_form_one_followup_batch_and_later_questions_wait(monkeypatch):
    chat_id = -707
    monkeypatch.setattr(bot, "identity", SimpleNamespace(username="Tg2HtmlBot", id=12))
    first_started, release_first = Event(), Event()
    batch_started, release_batch = Event(), Event()
    last_done = Event()
    calls = []

    def process(request):
        calls.append(request)
        batch = request["batch_requests"]
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(3)
        elif len(calls) == 2:
            assert [item["question"] for item in batch] == [
                "кит вчера", "кит позавчера", "кит бюджет"
            ]
            prompt = bot.batch_question(request)
            assert "кит" not in prompt
            assert all(author in prompt for author in ("alice", "bob"))
            assert prompt.index("вчера") < prompt.index("позавчера") < prompt.index("бюджет")
            batch_started.set()
            assert release_batch.wait(3)
        else:
            last_done.set()

    def message(mid, text, author="alice", thread=57):
        return SimpleNamespace(
            message_id=mid, message_thread_id=thread,
            chat=SimpleNamespace(id=chat_id),
            from_user=SimpleNamespace(id=mid, username=author),
        )

    monkeypatch.setattr(bot, "process_question", process)
    bot.group_queues.pop(chat_id, None)
    binding = {"buyer_id": "5", "topic_id": 57}
    try:
        bot.enqueue_question(message(1, "кит первый"), "кит первый", binding)
        assert first_started.wait(2)
        bot.enqueue_question(message(2, "кит вчера"), "кит вчера", binding)
        bot.enqueue_question(message(3, "кит позавчера", "bob"), "кит позавчера", binding)
        bot.enqueue_question(message(4, "кит бюджет"), "кит бюджет", binding)
        release_first.set()
        assert batch_started.wait(3)
        bot.enqueue_question(message(5, "кит потом"), "кит потом", binding)
        release_batch.set()
        assert last_done.wait(3)
        assert [len(call["batch_requests"]) for call in calls] == [1, 3, 1]
    finally:
        release_first.set()
        release_batch.set()
        deadline = time.monotonic() + 3
        while bot.group_queues.get(chat_id, {}).get("running") and time.monotonic() < deadline:
            time.sleep(0.01)
        bot.group_queues.pop(chat_id, None)


def test_batches_do_not_mix_topics_or_buyer_scopes(monkeypatch):
    chat_id = -808
    waiting, release, finished = Event(), Event(), Event()
    calls = []

    def process(request):
        calls.append(request)
        if len(calls) == 1:
            waiting.set()
            assert release.wait(3)
        if len(calls) == 4:
            finished.set()

    def message(mid, thread):
        return SimpleNamespace(
            message_id=mid, message_thread_id=thread,
            chat=SimpleNamespace(id=chat_id),
            from_user=SimpleNamespace(id=mid, username="alice"),
        )

    monkeypatch.setattr(bot, "process_question", process)
    bot.group_queues.pop(chat_id, None)
    try:
        bot.enqueue_question(message(1, 57), "первый", {"buyer_id": "5", "topic_id": None})
        assert waiting.wait(2)
        bot.enqueue_question(message(2, 57), "второй", {"buyer_id": "5", "topic_id": None})
        bot.enqueue_question(message(3, 58), "третий", {"buyer_id": "5", "topic_id": None})
        bot.enqueue_question(message(4, 58), "четвёртый", {"buyer_id": "6", "topic_id": None})
        release.set()
        assert finished.wait(3)
        assert [len(call["batch_requests"]) for call in calls] == [1, 1, 1, 1]
    finally:
        release.set()
        deadline = time.monotonic() + 3
        while bot.group_queues.get(chat_id, {}).get("running") and time.monotonic() < deadline:
            time.sleep(0.01)
        bot.group_queues.pop(chat_id, None)


def test_large_pending_batch_is_split_without_losing_questions(monkeypatch):
    chat_id = -818
    started, release, finished = Event(), Event(), Event()
    batches = []
    def process(request):
        batches.append([item["question"] for item in request["batch_requests"]])
        if len(batches) == 1:
            started.set()
            assert release.wait(3)
        elif len(batches) == 3:
            finished.set()
    monkeypatch.setattr(bot, "process_question", process)
    bot.group_queues.pop(chat_id, None)
    binding = {"buyer_id": "5", "topic_id": 57}
    def message(mid):
        return SimpleNamespace(
            chat=SimpleNamespace(id=chat_id), message_id=mid,
            message_thread_id=57, from_user=SimpleNamespace(id=mid, username="alice")
        )
    questions = [str(i) + "x" * 3999 for i in range(3)]
    try:
        bot.enqueue_question(message(1), "первый", binding)
        assert started.wait(2)
        for mid, question in enumerate(questions, 2):
            bot.enqueue_question(message(mid), question, binding)
        release.set()
        assert finished.wait(3)
        assert [len(batch) for batch in batches] == [1, 2, 1]
        assert [q for batch in batches[1:] for q in batch] == questions
    finally:
        release.set()
        deadline = time.monotonic() + 3
        while bot.group_queues.get(chat_id, {}).get("running") and time.monotonic() < deadline:
            time.sleep(0.01)
        bot.group_queues.pop(chat_id, None)


def test_followup_queued_during_answer_gets_previous_answer_in_context(monkeypatch):
    chat_id = -909
    first_started, release, second_done = Event(), Event(), Event()
    contexts = []

    class FakeAnalyst:
        def __init__(self, *_args, **_kwargs):
            pass

        def answer_stream(self, question, on_text, on_status, history,
                          after_tool_batch=None):
            contexts.append((question, history))
            if question == "вчера":
                first_started.set()
                assert release.wait(3)
            else:
                second_done.set()
            return "Ответ 1" if question == "вчера" else "Ответ 2"

    with TemporaryDirectory() as folder:
        monkeypatch.setattr(bot, "DATABASE", str(Path(folder) / "bot.sqlite3"))
        monkeypatch.setattr(bot, "identity", SimpleNamespace(username="Tg2HtmlBot", id=12))
        monkeypatch.setattr(bot, "Analyst", FakeAnalyst)
        with closing(open_db(bot.DATABASE)) as conn:
            init_db(conn)
            bind_group(conn, chat_id, "Group", None, "5", "Buyer")
        sent = []
        monkeypatch.setattr(bot.bot, "reply_to",
                            lambda *_args, **_kwargs: SimpleNamespace(message_id=200))
        monkeypatch.setattr(bot.bot, "edit_message_text", lambda *_args, **_kw: None)
        monkeypatch.setattr(bot.bot, "delete_message", lambda *_args, **_kw: None)
        monkeypatch.setattr(
            bot.bot, "send_rich_message",
            lambda *_args, **_kwargs: (
                sent.append(True) or SimpleNamespace(message_id=300 + len(sent))
            ),
        )
        bot.group_queues.pop(chat_id, None)

        def message(mid, text):
            return SimpleNamespace(
                chat=SimpleNamespace(id=chat_id, type="supergroup"),
                message_id=mid, message_thread_id=None,
                content_type="text", text=text, caption=None,
                from_user=SimpleNamespace(id=9, username="alice"),
                reply_to_message=None, forum_topic_created=None,
                forum_topic_edited=None, forum_topic_deleted=None,
            )
        try:
            bot.group_message(message(1, "кит вчера"))
            assert first_started.wait(3)
            bot.group_message(message(2, "кит позавчера"))
            release.set()
            assert second_done.wait(3)
            # The second task starts only after the first answer is saved.
            assert contexts[0][0] == "вчера"
            assert contexts[1][0] == "позавчера"
            assert any(item["role"] == "assistant" and item["content"] == "Ответ 1"
                       for item in contexts[1][1])
        finally:
            release.set()
            deadline = time.monotonic() + 3
            while bot.group_queues.get(chat_id, {}).get("running") and time.monotonic() < deadline:
                time.sleep(0.01)
            bot.group_queues.pop(chat_id, None)
