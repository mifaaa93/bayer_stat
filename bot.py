"""Telegram group analytics and a private-only buyer/group administration UI."""

from __future__ import annotations

import html
import logging
import math
import os
import re
import threading
import time
from contextlib import closing

import telebot
from telebot import types

import mysql_stats
from ai_analysis import Analyst
from db import (
    bind_group, clear_chat_history, count_groups, get_group, init_db,
    list_groups, load_chat_history, open_db, remove_group, save_chat_turn,
)
from settings import DATABASE, admin_ids


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("buyer-bot")
bot = telebot.TeleBot(os.environ["TELEGRAM_BOT_TOKEN"], parse_mode=None)
ADMINS = admin_ids()
pending: dict[int, dict] = {}
state_lock = threading.RLock()
chat_locks: dict[int, threading.Lock] = {}
identity = None


def admin_private(message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMINS
                and message.chat.type == "private")


def main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ Добавить группу", "📋 Список групп")
    return kb


def request_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton(
        text="📨 Выбрать группу",
        request_chat=types.KeyboardButtonRequestChat(
            request_id=1, chat_is_channel=False, request_title=True,
            request_username=True,
        ),
    ))
    kb.add("Отмена")
    return kb


def reset_pending(user_id: int) -> None:
    with state_lock:
        pending.pop(user_id, None)


def confirm_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("✅ Подтвердить", callback_data="confirm"),
           types.InlineKeyboardButton("❌ Отменить", callback_data="cancel"))
    return kb


def buyer_label(buyer: dict) -> str:
    return f"{buyer['name']} ({buyer['status'].strip().lower()})"


def show_buyers(chat_id: int, user_id: int, message_id: int) -> None:
    try:
        with mysql_stats.connection() as conn:
            options = mysql_stats.buyers(conn)
    except Exception:
        log.exception("Unable to load traffers")
        bot.edit_message_text(
            "Не удалось загрузить байеров из MySQL. Проверьте доступ и схему.",
            chat_id, message_id,
        )
        return
    if not options:
        bot.edit_message_text("В таблице traffers нет байеров.", chat_id, message_id)
        return
    with state_lock:
        if user_id not in pending:
            return
        pending[user_id]["buyers"] = {b["id"]: b for b in options}
        pending[user_id]["stage"] = "choose"
        pending[user_id]["message_id"] = message_id
        title = pending[user_id]["title"]
    kb = types.InlineKeyboardMarkup(row_width=1)
    for buyer in options[:80]:
        kb.add(types.InlineKeyboardButton(
            buyer_label(buyer)[:60], callback_data=f"pick:{buyer['id']}"
        ))
    kb.add(types.InlineKeyboardButton("❌ Отменить", callback_data="cancel"))
    bot.edit_message_text(
        f"Группа: {title}\nВыберите байера:",
        chat_id, message_id, reply_markup=kb,
    )


@bot.message_handler(commands=["start", "admin", "help"])
def start(message):
    if not admin_private(message):
        return
    bot.send_message(
        message.chat.id, "Панель управления группами.\n"
        "Добавление: выберите группу кнопкой Telegram, затем байера из traffers.\n"
        "Бот должен состоять в группе, чтобы отвечать на вопросы.",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["reset"])
def reset(message):
    if not admin_private(message):
        return
    with closing(open_db(DATABASE)) as conn:
        clear_chat_history(conn, message.chat.id)
    bot.send_message(message.chat.id, "Контекст этой переписки очищен.",
                     reply_markup=main_keyboard())


@bot.message_handler(content_types=["chat_shared"])
def shared_chat(message):
    if not admin_private(message):
        return
    shared = message.chat_shared
    with state_lock:
        state = pending.get(message.from_user.id)
        if not state or state.get("stage") != "request" or shared.request_id != 1:
            return
        state["chat_id"] = shared.chat_id
        state["title"] = shared.title or f"Группа {shared.chat_id}"
        state["username"] = shared.username
    # Telegram cannot edit a message carrying ReplyKeyboardMarkup. Restore
    # the ordinary admin keyboard in a separate message and keep this one
    # editable throughout buyer selection, confirmation and success.
    bot.send_message(message.chat.id, "Админ-меню:", reply_markup=main_keyboard())
    status = bot.send_message(
        message.chat.id,
        "Группа выбрана: " + (shared.title or str(shared.chat_id)) +
        "\nЗагружаю байеров…",
    )
    show_buyers(message.chat.id, message.from_user.id, status.message_id)


def group_list(chat_id: int, page: int = 0, edit_message_id=None):
    with closing(open_db(DATABASE)) as conn:
        total = count_groups(conn)
        pages = max(1, math.ceil(total / 10))
        page = min(max(0, page), pages - 1)
        groups = list_groups(conn, page)
    kb = types.InlineKeyboardMarkup(row_width=1)
    for group in groups:
        kb.add(types.InlineKeyboardButton(
            group["title"][:50], callback_data=f"view:{group['chat_id']}:{page}"
        ))
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("⬅️", callback_data=f"page:{page - 1}"))
    if page + 1 < pages:
        nav.append(types.InlineKeyboardButton("➡️", callback_data=f"page:{page + 1}"))
    if nav:
        kb.row(*nav)
    text = f"Группы: {total}. Страница {page + 1}/{pages}."
    if edit_message_id:
        bot.edit_message_text(text, chat_id, edit_message_id, reply_markup=kb)
    else:
        bot.send_message(chat_id, text, reply_markup=kb)


def group_card(chat_id: int, group_id: int, page: int, message_id: int):
    with closing(open_db(DATABASE)) as conn:
        group = get_group(conn, group_id)
    if not group:
        group_list(chat_id, page, message_id)
        return
    try:
        with mysql_stats.connection() as mysql:
            current = mysql_stats.buyer(mysql, group["buyer_id"])
    except Exception:
        log.warning("Could not refresh buyer status in group card", exc_info=True)
        current = None
    buyer_text = buyer_label(current) if current else f"{group['buyer_name']} (статус неизвестен)"
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton(
        "🔁 Сменить байера", callback_data=f"change:{group_id}:{page}"
    ))
    kb.row(types.InlineKeyboardButton(
        "🗑 Удалить группу", callback_data=f"delete:{group_id}:{page}"
    ))
    kb.row(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"page:{page}"))
    text = (
        f"Группа: {html.escape(group['title'])}\n"
        f"ID: <code>{group['chat_id']}</code>\n"
        f"Байер: {html.escape(buyer_text)} "
        f"(<code>{html.escape(group['buyer_id'])}</code>)"
    )
    bot.edit_message_text(text, chat_id, message_id,
                          reply_markup=kb, parse_mode="HTML")


@bot.message_handler(content_types=["text"], func=admin_private)
def admin_text(message):
    text = (message.text or "").strip()
    if text == "➕ Добавить группу":
        with state_lock:
            pending[message.from_user.id] = {"stage": "request"}
        bot.send_message(
            message.chat.id,
            "Нажмите «Выбрать группу». Telegram передаст ID группы и её название. "
            "Для закрытой группы ссылка-приглашение этим методом не передаётся.",
            reply_markup=request_keyboard(),
        )
    elif text == "📋 Список групп":
        reset_pending(message.from_user.id)
        group_list(message.chat.id)
    elif text == "Отмена":
        reset_pending(message.from_user.id)
        bot.send_message(message.chat.id, "Отменено.", reply_markup=main_keyboard())
    else:
        bot.send_message(message.chat.id, "Выберите действие кнопкой меню.",
                         reply_markup=main_keyboard())


@bot.callback_query_handler(func=lambda call: True)
def admin_callback(call):
    if not call.from_user or call.from_user.id not in ADMINS or call.message.chat.type != "private":
        bot.answer_callback_query(call.id, "Только для администратора в личном чате")
        return
    data = call.data or ""
    uid, chat_id, mid = call.from_user.id, call.message.chat.id, call.message.message_id
    try:
        if data == "cancel":
            with state_lock:
                state = pending.get(uid)
                if not state or state.get("message_id") != mid:
                    raise ValueError("Выбор устарел. Начните заново.")
            reset_pending(uid)
            bot.edit_message_text("Отменено.", chat_id, mid)
        elif data.startswith("pick:"):
            buyer_id = data[5:]
            with state_lock:
                state = pending.get(uid)
                if (not state or state.get("stage") != "choose"
                        or state.get("message_id") != mid
                        or buyer_id not in state["buyers"]):
                    raise ValueError("Выбор устарел. Начните заново.")
                state["selected"] = buyer_id
                state["stage"] = "confirm"
                chosen, title = state["buyers"][buyer_id], state["title"]
            bot.edit_message_text(
                f"Группа: {title}\nБайер: {buyer_label(chosen)}\nПодтвердить привязку?",
                chat_id, mid, reply_markup=confirm_keyboard(),
            )
        elif data == "confirm":
            with state_lock:
                state = pending.get(uid)
                if (not state or state.get("stage") != "confirm"
                        or state.get("message_id") != mid):
                    raise ValueError("Подтверждение устарело. Начните заново.")
                selected = state["selected"]
            with mysql_stats.connection() as mysql:
                current = mysql_stats.buyer(mysql, selected)
            if not current:
                raise ValueError("Байер больше не найден в MySQL.")
            # get_chat also checks that the bot can access the selected group.
            try:
                chat = bot.get_chat(state["chat_id"])
                membership = bot.get_chat_member(chat.id, bot.get_me().id)
            except Exception as exc:
                raise ValueError("Добавьте бота в выбранную группу и повторите подтверждение.") from exc
            if chat.type not in ("group", "supergroup"):
                raise ValueError("Можно добавить только группу, не канал.")
            if membership.status in ("left", "kicked"):
                raise ValueError("Бот должен состоять в выбранной группе.")
            with closing(open_db(DATABASE)) as conn:
                bind_group(conn, chat.id, chat.title or state["title"],
                           chat.username or state.get("username"), current["id"], current["name"])
            reset_pending(uid)
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(
                "📋 Посмотреть все группы", callback_data="page:0",
            ))
            bot.edit_message_text(
                "Группа привязана к байеру " + buyer_label(current) + ".",
                chat_id, mid, reply_markup=kb,
            )
        elif data.startswith("page:"):
            group_list(chat_id, int(data.split(":")[1]), mid)
        elif data.startswith("view:"):
            _, group_id, page = data.split(":")
            group_card(chat_id, int(group_id), int(page), mid)
        elif data.startswith("change:"):
            _, group_id, page = data.split(":")
            with closing(open_db(DATABASE)) as conn:
                group = get_group(conn, int(group_id))
            if not group:
                raise ValueError("Группа уже удалена.")
            with state_lock:
                pending[uid] = {
                    "stage": "choose", "chat_id": int(group_id), "title": group["title"],
                    "username": group["username"],
                }
            show_buyers(chat_id, uid, mid)
        elif data.startswith("delete:"):
            _, group_id, page = data.split(":")
            kb = types.InlineKeyboardMarkup()
            kb.row(types.InlineKeyboardButton(
                "🗑 Да, удалить", callback_data=f"remove:{group_id}:{page}"
            ), types.InlineKeyboardButton("Отмена", callback_data=f"view:{group_id}:{page}"))
            bot.edit_message_text("Удалить привязку группы и её историю чата?",
                                  chat_id, mid, reply_markup=kb)
        elif data.startswith("remove:"):
            _, group_id, page = data.split(":")
            with closing(open_db(DATABASE)) as conn:
                remove_group(conn, int(group_id))
            group_list(chat_id, int(page), mid)
        bot.answer_callback_query(call.id)
    except (ValueError, RuntimeError) as exc:
        bot.answer_callback_query(call.id, str(exc)[:180], show_alert=True)
    except Exception:
        log.exception("Admin callback failed")
        bot.answer_callback_query(call.id, "Ошибка доступа к базе или Telegram", show_alert=True)


def is_addressed(message) -> bool:
    global identity
    if identity is None:
        identity = bot.get_me()
    username = (identity.username or "").casefold()
    text = (message.text or "")
    # No commands in groups: only a regular @mention or reply to the bot.
    if text.lstrip().startswith("/"):
        return False
    return (bool(username and re.search(rf"@{re.escape(username)}\b", text, re.IGNORECASE))
            or bool(message.reply_to_message and message.reply_to_message.from_user
                    and message.reply_to_message.from_user.id == identity.id))


@bot.message_handler(content_types=["text"], func=lambda m: m.chat.type in ("group", "supergroup"))
def group_question(message):
    if not is_addressed(message):
        return
    log.info("Received group query chat_id=%s", message.chat.id)
    with closing(open_db(DATABASE)) as conn:
        group = get_group(conn, message.chat.id)
        history = load_chat_history(conn, message.chat.id) if group else []
    if not group:
        return
    lock = chat_locks.setdefault(message.chat.id, threading.Lock())
    if not lock.acquire(blocking=False):
        bot.reply_to(message, "Ещё обрабатываю предыдущий вопрос. Подождите.")
        return
    status = None
    answer = None
    try:
        question = re.sub(
            rf"@{re.escape(identity.username)}\b",
            "",
            message.text or "",
            flags=re.IGNORECASE,
        )
        question = question.strip()
        if not question:
            return
        status = bot.reply_to(message, "Анализирую вопрос…")
        analyst = Analyst(
            group["buyer_id"], group["buyer_name"], os.environ["OPENAI_API_KEY"],
            os.environ["OPENAI_MODEL"], os.getenv("OPENAI_BASE_URL", "https://ru.cheapvibecode.ru/v1"),
        )
        last_update = 0.0
        current_status = ""
        stage = "Анализирую вопрос"
        streaming = False
        done = threading.Event()
        progress_guard = threading.RLock()

        def progress(text, force=False):
            nonlocal last_update, current_status
            with progress_guard:
                if done.is_set():
                    return
                text = text[:3800]
                now = time.monotonic()
                if text == current_status or (not force and now-last_update < 1.2):
                    return
                try:
                    bot.edit_message_text(text, message.chat.id, status.message_id)
                    current_status, last_update = text, now
                except Exception:
                    log.debug("Progress update failed", exc_info=True)

        def on_status(label):
            nonlocal stage
            stage = label
            progress(label + "…", True)

        def on_text(text):
            nonlocal streaming
            streaming = True
            progress(text)

        def heartbeat():
            started = time.monotonic()
            while not done.wait(12):
                if streaming:
                    return
                progress(f"{stage} ({int(time.monotonic()-started)} с)…", True)

        threading.Thread(target=heartbeat, daemon=True).start()
        answer = analyst.answer_stream(question, on_text=on_text,
                                       on_status=on_status, history=history)
        done.set()
        try:
            bot.send_rich_message(
                message.chat.id, types.InputRichMessage(markdown=answer),
                reply_parameters=types.ReplyParameters(message_id=message.message_id),
            )
        except Exception:
            bot.reply_to(message, answer, parse_mode=None)
        with closing(open_db(DATABASE)) as conn:
            save_chat_turn(conn, message.chat.id, question, answer)
    except Exception:
        log.exception("Question processing failed")
        bot.reply_to(message, "Не получилось обработать запрос. Попробуйте позже.")
    finally:
        if "done" in locals():
            done.set()
        if status:
            try:
                bot.delete_message(message.chat.id, status.message_id)
            except Exception:
                log.debug("Could not remove status", exc_info=True)
        lock.release()


def main():
    with closing(open_db(DATABASE)) as conn:
        init_db(conn)
    log.info("Starting Telegram analytics bot")
    bot.infinity_polling(skip_pending=True, allowed_updates=["message", "callback_query"],
                         timeout=30, long_polling_timeout=30)


if __name__ == "__main__":
    main()
