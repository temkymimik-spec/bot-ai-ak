import asyncio
import logging
import random
import string
import datetime
import json
import aiohttp

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command, CommandObject
from aiogram.exceptions import TelegramBadRequest

import config
from database import db, init_db
from worker import Workers
import ai as ai_module

logging.basicConfig(level=logging.INFO)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()
workers = Workers(None)  # AIO-сессия подставится в on_startup
AIO = None  # aiohttp-сессия для ИИ


class AdminStates(StatesGroup):
    waiting_example = State()
    waiting_channel_id = State()


class UserStates(StatesGroup):
    waiting_wallet = State()


def is_admin(uid):
    return uid in config.ADMIN_IDS


def gen_code():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def rub(leads):
    return leads * config.LID_RATE


# ============================================================
# АДМИН-ПАНЕЛЬ
# ============================================================
async def admin_menu(m, extra=""):
    k = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎛 Аккаунты (сессии)", callback_data="a_sessions")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="a_stats")],
        [InlineKeyboardButton(text="🏆 Топ лидов по партнёрам", callback_data="a_top")],
        [InlineKeyboardButton(text="🔗 Все партнёрские ссылки", callback_data="a_links")],
        [InlineKeyboardButton(text="🧪 Капча-каналы", callback_data="a_channels")],
        [InlineKeyboardButton(text="🔎 Оценка чатов ИИ", callback_data="a_chats")],
        [InlineKeyboardButton(text="💰 Заявки на вывод", callback_data="a_wd")],
        [InlineKeyboardButton(text="🔄 Перезапуск воркеров", callback_data="a_restart")],
    ])
    await m.answer(
        "🛠 <b>Админ-панель</b>\n\n"
        "• Кидай <b>.session</b> — подключу аккаунт для ИИ\n"
        "• <b>/пример</b> — общий пример общения для ИИ\n"
        "• <b>/стиль &lt;имя_сессии&gt;</b> — свой стиль для конкретного аккаунта\n"
        "• <b>/канал ID</b> — добавить канал для капчи\n"
        f"{extra}",
        reply_markup=k, parse_mode="HTML",
    )


# ============================================================
# УПРАВЛЕНИЕ АККАУНТАМИ (СЕССИЯМИ)
# ============================================================
@dp.callback_query(F.data == "a_sessions")
async def a_sessions(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ нет доступа", show_alert=True)
        return
    with db() as conn:
        rows = conn.execute("SELECT * FROM sessions").fetchall()
    text = "🎛 <b>Аккаунты (сессии)</b>\n\n"
    kb = []
    for r in rows:
        online = "🟢" if r["name"] in workers.workers else "🔴"
        replies = r["daily_replies"] if r["daily_date"] == datetime.date.today().isoformat() else 0
        status = "✓ in сети" if online == "🟢" else ("⏸ пауза" if r["status"] == "paused" else "✓")
        text += f"{online} {r['name']} · @{r['username'] or '?'} · {status} · ответов {replies}/день\n"
        kb.append([InlineKeyboardButton(
            text=f"▪ {r['name']}",
            callback_data=f"session::{r['id']}",
        )])
    kb.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="a_sessions")])
    if not rows:
        text += "Пока нет сессий. Кидай .session файл."
    await cq.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
                               parse_mode="HTML")
    await cq.answer()


@dp.callback_query(F.data.startswith("session::"))
async def session_detail(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ нет доступа", show_alert=True)
        return
    sid = int(cq.data.split("::")[1])
    with db() as conn:
        r = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        led = conn.execute("SELECT COUNT(*) c FROM leads WHERE ai_username=? AND credited=1",
                           (r["username"],)).fetchone()["c"]
        chats = conn.execute("SELECT COUNT(*) c FROM conversations WHERE ai_username=?",
                             (r["username"],)).fetchone()["c"]
    online = r["name"] in workers.workers
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶ Включить", callback_data=f"session_on::{sid}")],
        [InlineKeyboardButton(text="⏸ Пауза", callback_data=f"session_pause::{sid}")],
        [InlineKeyboardButton(text="🖊 Стиль аккаунта", callback_data=f"session_style::{sid}")],
        [InlineKeyboardButton(text="💬 Чаты аккаунта", callback_data=f"session_chats::{sid}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"session_del::{sid}")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="a_sessions")],
    ])
    await cq.message.edit_text(
        f"🎛 <b>{r['name']}</b>\n"
        f"@{r['username'] or '?'} · {'🟢 онлайн' if online else '🔴 офлайн'}\n"
        f"Статус: {r['status']}\n"
        f"📈 Лидов начислено с этого аккаунта: {led}\n"
        f"💬 Чатов ведётся: {chats}\n"
        f"🖊 Стиль: {'задан' if r['example'] else 'общий (глобальный пример)'}",
        reply_markup=kb, parse_mode="HTML",
    )
    await cq.answer()


@dp.callback_query(F.data.startswith("session_on::"))
async def session_on(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ нет доступа", show_alert=True); return
    sid = int(cq.data.split("::")[1])
    with db() as conn:
        row = conn.execute("SELECT name FROM sessions WHERE id=?", (sid,)).fetchone()
        conn.execute("UPDATE sessions SET status='active' WHERE id=?", (sid,))
    await workers.start_one(row["name"])
    await cq.answer("✅ Аккаунт включён")
    await session_detail(cq)


@dp.callback_query(F.data.startswith("session_pause::"))
async def session_pause(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ нет доступа", show_alert=True); return
    sid = int(cq.data.split("::")[1])
    with db() as conn:
        row = conn.execute("SELECT name FROM sessions WHERE id=?", (sid,)).fetchone()
        conn.execute("UPDATE sessions SET status='paused' WHERE id=?", (sid,))
    await workers.stop_one(row["name"])
    await cq.answer("⏸ Аккаунт на паузе")
    await session_detail(cq)


@dp.callback_query(F.data.startswith("session_del::"))
async def session_del(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ нет доступа", show_alert=True); return
    sid = int(cq.data.split("::")[1])
    with db() as conn:
        row = conn.execute("SELECT name FROM sessions WHERE id=?", (sid,)).fetchone()
        conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
    await workers.stop_one(row["name"])
    try:
        import os
        os.remove(f"{config.SESSION_DIR}/{row['name']}")
    except Exception:
        pass
    await cq.answer("🗑 Аккаунт удалён")
    await a_sessions(cq)


@dp.callback_query(F.data.startswith("session_style::"))
async def session_style(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ нет доступа", show_alert=True); return
    sid = int(cq.data.split("::")[1])
    with db() as conn:
        row = conn.execute("SELECT name FROM sessions WHERE id=?", (sid,)).fetchone()
    await state.update_data(session_id=sid)
    await state.set_state(AdminStates.waiting_example)
    await cq.message.answer(
        f"✍️ <b>Свой стиль для {row['name']}</b>\n"
        f"Отправь текст примера общения. Только этот аккаунт будет ему следовать.\n"
        f"Для отмены: /отмена",
        parse_mode="HTML",
    )
    await cq.answer()


@dp.message(Command("стиль"))
async def cmd_style(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    args = m.text.split(maxsplit=1)
    if len(args) < 2:
        await m.answer("Формат: /стиль <имя_сессии>\nНапример: /стиль myacc.session")
        return
    with db() as conn:
        row = conn.execute("SELECT id FROM sessions WHERE name=?", (args[1],)).fetchone()
    if not row:
        await m.answer("❌ Сессия не найдена.")
        return
    await state.update_data(session_id=row["id"])
    await state.set_state(AdminStates.waiting_example)
    await m.answer(f"✍️ Отправь пример стиля для {args[1]}:")


@dp.callback_query(F.data.startswith("session_chats::"))
async def session_chats(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ нет доступа", show_alert=True); return
    sid = int(cq.data.split("::")[1])
    with db() as conn:
        r = conn.execute("SELECT username FROM sessions WHERE id=?", (sid,)).fetchone()
        rows = conn.execute(
            "SELECT telegram_id FROM conversations WHERE ai_username=? ORDER BY updated_at DESC LIMIT 15",
            (r["username"],),
        ).fetchall()
    kb = []
    for row in rows:
        kb.append([InlineKeyboardButton(
            text=row["telegram_id"], callback_data=f"ev::{row['telegram_id']}"
        )])
    kb.append([InlineKeyboardButton(text="◀ Назад", callback_data=f"session::{sid}")])
    await cq.message.edit_text(
        f"💬 <b>Чаты аккаунта @{r['username']}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML",
    )
    await cq.answer()


@dp.message(Command("отмена"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.")


@dp.message(Command("start"))
async def cmd_start(m: Message, command: CommandObject):
    code = command.args
    uid = m.from_user.id
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (id, username, created_at) VALUES (?,?,?)",
            (uid, m.from_user.username, int(datetime.datetime.now().timestamp())),
        )
    if is_admin(uid):
        await admin_menu(m)
        return

    if code:
        await handle_partner_link(m, code)
    else:
        await user_menu(m)


@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if is_admin(m.from_user.id):
        await admin_menu(m)


@dp.message(F.document)
async def upload_session(m: Message):
    if not is_admin(m.from_user.id):
        return
    doc = m.document
    if not doc.file_name.endswith(".session"):
        await m.answer("Нужен файл .session")
        return
    path = f"{config.SESSION_DIR}/{doc.file_name}"
    await bot.download(doc, destination=path)
    with db() as conn:
        conn.execute(
            "INSERT INTO sessions (name, session_path, created_at) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET session_path=?, status='active'",
            (doc.file_name, path, int(datetime.datetime.now().timestamp()), path),
        )
    await m.answer(f"✅ Сессия {doc.file_name} сохранена. Подключаю…")
    try:
        await workers.start_one(doc.file_name)
        await m.answer("✅ Аккаунт подключён и теперь греет лидов.")
    except Exception as e:
        await m.answer(f"❌ Ошибка: {e}")


@dp.message(F.text, AdminStates.waiting_example)
async def save_example(m: Message, state: FSMContext):
    data = await state.get_data()
    sid = data.get("session_id")
    with db() as conn:
        if sid:
            # стиль конкретного аккаунта
            conn.execute("UPDATE sessions SET example=? WHERE id=?", (m.text, sid))
            await m.answer("✅ Стиль этого аккаунта обновлён. ИИ на нём будет так общаться.")
        else:
            # глобальный пример
            conn.execute("INSERT INTO ai_examples (content) VALUES (?)", (m.text,))
            await m.answer("✅ Пример сохранён. ИИ будет подражать (если у аккаунта нет своего стиля).")
    await state.clear()


@dp.message(Command("пример"))
async def cmd_example(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    await state.set_state(AdminStates.waiting_example)
    await m.answer("Отправь текст примера общения (стиль для ИИ):")


@dp.message(Command("канал"))
async def cmd_add_channel(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    args = m.text.split()
    if len(args) > 1:
        cid = args[1]
        try:
            with db() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO captcha_channels (channel_id, username, title) VALUES (?,?,?)",
                    (int(cid), args[2] if len(args) > 2 else f"id{cid}", cid),
                )
            await m.answer(f"✅ Канал {cid} добавлен в капчу ({config.CAPTCHA_CHANNELS_REQUIRED} требуются: пока "
                           f"{_channel_count()} в пуле).")
        except Exception as e:
            await m.answer(f"❌ {e}")
    else:
        await m.answer("Формат: /канал <channel_id> [@username]\n\n"
                       "Пул каналов (бот-админ сможет проверить подписку):\n" + _channels_list())


def _channel_count():
    with db() as conn:
        return conn.execute("SELECT COUNT(*) c FROM captcha_channels").fetchone()["c"]


def _channels_list():
    with db() as conn:
        rows = conn.execute("SELECT channel_id, username, title FROM captcha_channels").fetchall()
    return "\n".join(f"• {r['channel_id']} ({r['title']})" for r in rows) or "(пусто)"


# ============================================================
# ПОЛЬЗОВАТЕЛЬ (партнёр)
# ============================================================
async def user_menu(m, extra=""):
    uid = m.from_user.id
    with db() as conn:
        row = conn.execute("SELECT leads FROM users WHERE id=?", (uid,)).fetchone()
    leads = row["leads"] if row else 0
    k = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Мои партнёрские ссылки", callback_data="u_links")],
        [InlineKeyboardButton(text="💰 Заказать вывод", callback_data="u_wd")],
        [InlineKeyboardButton(text="📈 Статистика", callback_data="u_stats")],
    ])
    await m.answer(
        f"👋 Привет, партнёр!\n\n💎 Лидов: <b>{leads}</b> (≈{rub(leads)} руб)\n\n"
        f"Создай ссылку → лей людей → они проходят капчу, ИИ их греет.\n"
        f"ИИ сам выяснит действие и начислит тебе лида, ничего кидать не нужно.",
        reply_markup=k, parse_mode="HTML",
    )


# ------ партнёрские ссылки ------
@dp.callback_query(F.data == "u_links")
async def u_links(cq: CallbackQuery):
    uid = cq.from_user.id
    with db() as conn:
        rows = conn.execute(
            "SELECT l.*, s.username as ai FROM links l LEFT JOIN sessions s "
            "ON l.session_id=s.id WHERE l.user_id=?", (uid,)
        ).fetchall()
        sessions = conn.execute("SELECT id, name, username FROM sessions WHERE status='active'").fetchall()
    text = "🔗 <b>Твои ссылки</b>\n\n"
    if rows:
        for r in rows:
            text += f"• <code>{r['code']}</code> → @{r['ai'] or '?'} (кликов {r['clicks']})\n"
        text += f"\nПример: <code>https://t.me/{_botme()}?start={rows[0]['code']}</code>\n"
    else:
        text += "Пока нет. Создай ниже.\n"
    kb_rows = []
    for s in sessions:
        kb_rows.append([InlineKeyboardButton(
            text=f"Создать на аккаунт @{s['username'] or s['name']}",
            callback_data=f"mk_link::{s['id']}",
        )])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await cq.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await cq.answer()


def _botme():
    return bot if hasattr(bot, "username") and bot.username else "<ваш_бот>"


@dp.callback_query(F.data.startswith("mk_link::"))
async def mk_link(cq: CallbackQuery):
    sid = int(cq.data.split("::")[1])
    code = gen_code()
    with db() as conn:
        conn.execute("INSERT INTO links (user_id, session_id, code, created_at) VALUES (?,?,?,?)",
                     (cq.from_user.id, sid, code, int(datetime.datetime.now().timestamp())))
    await cq.message.answer(
        f"✅ Ссылка создана!\n\n"
        f"<code>https://t.me/<ваш_бот>?start={code}</code>\n\n"
        f"Лей по ней людей: они пройдут капчу и попадут к ИИ.",
        parse_mode="HTML",
    )
    await cq.answer()


# ============================================================
# ФЛОУ ЛИДА ПО ПАРТНЁРСКОЙ ССЫЛКЕ + КАПЧА
# ============================================================
async def handle_partner_link(m: Message, code: str):
    uid = m.from_user.id
    with db() as conn:
        link = conn.execute(
            "SELECT l.*, s.username as ai FROM links l LEFT JOIN sessions s "
            "ON l.session_id=s.id WHERE l.code=?", (code,)
        ).fetchone()

    if not link:
        await m.answer("❌ Ссылка не найдена.")
        return

    with db() as conn:
        conn.execute("UPDATE links SET clicks=clicks+1 WHERE id=?", (link["id"],))

    # есть ли подключённый ИИ-аккаунт (по сессии, на которую ведёт ссылка)
    ai_username = link["ai"]
    if not ai_username or (link["session_id"] not in workers.workers):
        await m.answer("⏳ ИИ-аккаунт для этой ссылки пока не подключён. Зайди позже.")
        return

    # создаём/обновляем лида, привязываем к партнёру
    with db() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE telegram_id=?", (str(uid),)).fetchone()
        if not lead:
            conn.execute(
                "INSERT INTO leads (partner_id, link_id, telegram_id, username, ai_username, "
                "status, captcha_passed, created_at, updated_at) "
                "VALUES (?,?,?,?,?,'fresh',0,?,?)",
                (link["user_id"], link["id"], str(uid), m.from_user.username, ai_username,
                 int(datetime.datetime.now().timestamp()), int(datetime.datetime.now().timestamp())),
            )
            captcha_passed = False
        else:
            # повторный заход — если капчу уже прошёл, отдаём сразу
            captcha_passed = bool(lead["captcha_passed"])
            conn.execute(
                "UPDATE leads SET ai_username=? WHERE id=?", (ai_username, lead["id"])
            )

    if captcha_passed:
        await give_ai_contact(m, ai_username)
        return

    # ----- КАПЧА -----
    with db() as conn:
        channels = conn.execute("SELECT * FROM captcha_channels").fetchall()
    if len(channels) < config.CAPTCHA_CHANNELS_REQUIRED:
        await m.answer("🛠 Защита настраивается, зайди чуть позже.")
        return

    channels = channels[:config.CAPTCHA_CHANNELS_REQUIRED]
    kb_rows = []
    for ch in channels:
        uname = ch["username"]
        t = uname if uname.startswith("@") else f"@{uname}"
        kb_rows.append([InlineKeyboardButton(
            text=f"📢 Подписаться: {ch['title'] or t}", url=f"https://t.me/{uname.lstrip('@')}"
        )])
    kb_rows.append([InlineKeyboardButton(text="✅ Я подписался — проверить", callback_data="captcha_check")])
    # запомним выбранные каналы для этого лида
    with db() as conn:
        conn.execute("UPDATE leads SET comment=?, captcha_passed=0, status='fresh' WHERE telegram_id=?",
                     (json.dumps([c["channel_id"] for c in channels]), str(uid)))
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await m.answer(
        "🔒 <b>Подтверди, что ты не робот</b>\n\nПодпишись на эти каналы, чтобы получить доступ к наставнику:",
        reply_markup=kb, parse_mode="HTML",
    )


@dp.callback_query(F.data == "captcha_check")
async def captcha_check(cq: CallbackQuery):
    uid = cq.from_user.id
    lead_comment = None
    with db() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE telegram_id=?", (str(uid),)).fetchone()
        if lead:
            lead_comment = lead["comment"]
        channels = conn.execute("SELECT * FROM captcha_channels").fetchall()
    if not lead or not lead_comment:
        await cq.answer("Начни с партнёрской ссылки")
        return
    required = json.loads(lead_comment)
    subscribed = True
    for channel_id in required:
        ch = next((c for c in channels if c["channel_id"] == channel_id), None)
        if ch is None:
            continue
        try:
            if ch["username"] and ch["username"].startswith("@"):
                member = await bot.get_chat_member(channel_id, uid)
                if member.status in ("left", "kicked"):
                    subscribed = False
            # для username-каналов нужна проверка прав — делаем через get_chat_member по id
            else:
                member = await bot.get_chat_member(channel_id, uid)
                if member.status in ("left", "kicked"):
                    subscribed = False
        except TelegramBadRequest:
            # бот не в канале — не можем проверить, считаем ок (админ сам решит)
            pass
        except Exception:
            pass

    if subscribed:
        with db() as conn:
            conn.execute("UPDATE leads SET captcha_passed=1, status='handed', updated_at=? "
                         "WHERE telegram_id=?",
                         (int(datetime.datetime.now().timestamp()), str(uid)))
        ai = lead["ai_username"]
        await cq.message.answer("✅ Проверка пройдена!")
        if ai:
            await cq.message.answer(
                f"🎓 Вот твой наставник: <b>@{ai}</b>\n\n"
                f"Напиши ему, он поможет с заработком на pocket-option.",
                parse_mode="HTML",
            )
        else:
            await cq.message.answer("⏳ Наставник ещё не подключён.")
    else:
        await cq.message.answer("❌ Ты подписался не на все каналы. Проверь и жми снова.")
    await cq.answer()


async def give_ai_contact(m: Message, ai_username: str):
    await m.answer(
        f"🎓 Твой наставник: <b>@{ai_username}</b>\n\n"
        f"Напиши ему — расскажет как зарабатывать на pocket-option.",
        parse_mode="HTML",
    )


# ============================================================
# ВЫВОД
# ============================================================
@dp.callback_query(F.data == "u_wd")
async def u_wd(cq: CallbackQuery, state: FSMContext):
    uid = cq.from_user.id
    now = datetime.datetime.now()
    with db() as conn:
        row = conn.execute("SELECT leads FROM users WHERE id=?", (uid,)).fetchone()
    leads = row["leads"]
    if now.weekday() not in (5, 6):
        await cq.message.answer("⏳ Вывод только с <b>субботы по воскресенье</b>.", parse_mode="HTML")
        await cq.answer()
        return
    if leads < config.MIN_WITHDRAW_LIDS:
        await cq.message.answer(f"❌ Минимум {config.MIN_WITHDRAW_LIDS} лидов ({rub(config.MIN_WITHDRAW_LIDS)} руб). У тебя {leads}.")
        await cq.answer()
        return
    await state.set_state(UserStates.waiting_wallet)
    await cq.message.answer(f"💰 У тебя {leads} лидов = {rub(leads)} руб.\nПришли номер кошелька/карты для вывода:")
    await cq.answer()


@dp.message(UserStates.waiting_wallet)
async def do_withdraw(m: Message, state: FSMContext):
    uid = m.from_user.id
    now = int(datetime.datetime.now().timestamp())
    with db() as conn:
        row = conn.execute("SELECT leads FROM users WHERE id=?", (uid,)).fetchone()
        conn.execute(
            "INSERT INTO withdrawals (user_id, lead_count, amount_rub, wallet, created_at) "
            "VALUES (?,?,?,?,?)",
            (uid, row["leads"], rub(row["leads"]), m.text, now),
        )
        conn.execute("UPDATE users SET leads=0 WHERE id=?", (uid,))
    await m.answer("✅ Заявка на вывод оформлена. Админ свяжется с тобой.")
    await state.clear()


@dp.callback_query(F.data == "u_stats")
async def u_stats(cq: CallbackQuery):
    uid = cq.from_user.id
    with db() as conn:
        row = conn.execute("SELECT leads FROM users WHERE id=?", (uid,)).fetchone()
        cnt = conn.execute("SELECT COUNT(*) c FROM leads WHERE partner_id=?", (uid,)).fetchone()["c"]
        q = conn.execute("SELECT COUNT(*) c FROM leads WHERE partner_id=? AND status='qualified'", (uid,)).fetchone()["c"]
    await cq.message.answer(
        f"📈 <b>Твоя статистика</b>\n"
        f"💎 Начислено лидов: {row['leads']} ({rub(row['leads'])} руб)\n"
        f"🔁 Всего по твоим ссылкам пришло людей: {cnt}\n"
        f"✅ Из них подтверждено ИИ: {q}",
        parse_mode="HTML",
    )
    await cq.answer()


# ============================================================
# АДМИН-КОЛБЭКИ
# ============================================================
@dp.callback_query(F.data == "a_stats")
async def a_stats(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ нет доступа", show_alert=True); return
    with db() as conn:
        partners = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        leads_total = conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
        handed = conn.execute("SELECT COUNT(*) c FROM leads WHERE status='handed'").fetchone()["c"]
        qualified = conn.execute("SELECT COUNT(*) c FROM leads WHERE status='qualified'").fetchone()["c"]
        rejected = conn.execute("SELECT COUNT(*) c FROM leads WHERE status='rejected'").fetchone()["c"]
        earnings = conn.execute("SELECT COALESCE(SUM(leads),0) s FROM users").fetchone()["s"]
        sessions = conn.execute("SELECT COUNT(*) c FROM sessions WHERE status='active'").fetchone()["c"]
    await cq.message.edit_text(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Партнёров: {partners}\n"
        f"🔌 Активных аккаунтов ИИ: {sessions}\n"
        f"🟢 Всего лидов в базе: {leads_total}\n"
        f"✋ Выдано наставнику: {handed}\n"
        f"✅ Подтверждено ИИ (лиды): {qualified}\n"
        f"❌ Отсеяно: {rejected}\n"
        f"💎 Начислено лидов партнёрам: {earnings} ({rub(earnings)} руб)",
        parse_mode="HTML",
    )
    await cq.answer()


@dp.callback_query(F.data == "a_top")
async def a_top(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ нет доступа", show_alert=True); return
    with db() as conn:
        rows = conn.execute(
            "SELECT username, leads FROM users WHERE leads>0 ORDER BY leads DESC LIMIT 10"
        ).fetchall()
    text = "🏆 <b>Топ партнёров по лидам</b>\n\n" if rows else "🏆 Топ партнёров по лидам\n\n(пусто)"
    for i, r in enumerate(rows, 1):
        text += f"{i}. @{r['username'] or r['id']} — {r['leads']} ({rub(r['leads'])} руб)\n"
    await cq.message.edit_text(text, parse_mode="HTML")
    await cq.answer()


@dp.callback_query(F.data == "a_links")
async def a_links(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ нет доступа", show_alert=True); return
    with db() as conn:
        rows = conn.execute(
            "SELECT l.*, u.username as pu, s.username as ai FROM links l "
            "JOIN users u ON l.user_id=u.id LEFT JOIN sessions s ON l.session_id=s.id"
        ).fetchall()
    text = "🔗 <b>Все ссылки</b>\n\n" if rows else "🔗 Все ссылки\n\n(пусто)"
    for r in rows:
        text += f"• <code>{r['code']}</code> → @{r['pu'] or r['user_id']} → @{r['ai'] or '?'} (кликов {r['clicks']})\n"
    await cq.message.edit_text(text, parse_mode="HTML")
    await cq.answer()


@dp.callback_query(F.data == "a_channels")
async def a_channels(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ нет доступа", show_alert=True); return
    await cq.message.edit_text(
        f"🧪 <b>Капча-каналы</b> (нужно {config.CAPTCHA_CHANNELS_REQUIRED} в пуле)\n\n"
        f"{_channels_list()}\n\n"
        f"Добавить: /канал <id> [@user]\n"
        f"Бота добавь в эти каналы админом — тогда подписку проверим автоматически.",
        parse_mode="HTML",
    )
    await cq.answer()


@dp.callback_query(F.data == "a_chats")
async def a_chats(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ нет доступа", show_alert=True); return
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT telegram_id FROM conversations ORDER BY updated_at DESC LIMIT 20"
        ).fetchall()
    text = "🔎 <b>Чаты для оценки ИИ</b>\n\n" if rows else "Чатов нет."
    kb = []
    for r in rows:
        kb.append([InlineKeyboardButton(text=r["telegram_id"], callback_data=f"ev::{r['telegram_id']}")])
    await cq.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cq.answer()


@dp.callback_query(F.data.startswith("ev::"))
async def eval_one(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ нет доступа", show_alert=True); return
    tg = cq.data.split("::")[1]
    with db() as conn:
        conv = conn.execute(
            "SELECT messages FROM conversations WHERE telegram_id=? ORDER BY updated_at DESC LIMIT 1",
            (tg,),
        ).fetchone()
        lead = conn.execute("SELECT * FROM leads WHERE telegram_id=?", (tg,)).fetchone()
    txt = "\n".join(f"{x['role']}: {x['content'][:150]}" for x in json.loads(conv["messages"])[-8:])
    res = await ai_module.evaluate_action(AIO, txt, lead["confirmations"] if lead else 0)
    await cq.message.answer(
        f"🔎 <b>{tg}</b>\n\n{txt}\n\n"
        f"Действие на PO: {'ДА' if res['cash_action'] else 'нет'} · подтверждений: {res['confirmations']}\n"
        f"Pocket id: {res['pocket_id'] or '—'} · доход: {res['income'] or '—'}\n"
        f"Лид: {'ЗАСЧИТАН' if res['confirmed'] else 'нет'}\n"
        f"Причина: {res['reason'][:200]}",
        parse_mode="HTML",
    )
    await cq.answer()


@dp.callback_query(F.data == "a_wd")
async def a_wd(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ нет доступа", show_alert=True); return
    with db() as conn:
        rows = conn.execute("SELECT * FROM withdrawals WHERE status='pending'").fetchall()
    text = "💰 <b>Заявки на вывод</b>\n\n" if rows else "💰 Заявок нет."
    for r in rows:
        text += f"• id{r['id']} — {r['user_id']} — {r['lead_count']} л = {r['amount_rub']} руб — {r['wallet']}\n"
    await cq.message.edit_text(text, parse_mode="HTML")
    await cq.answer()


@dp.callback_query(F.data == "a_restart")
async def a_restart(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ нет доступа", show_alert=True); return
    await workers.restart()
    await cq.message.answer("🔄 Воркеры перезапущены.")
    await cq.answer()


# ============================================================
# ЗАПУСК
# ============================================================
async def on_startup():
    await bot.delete_webhook(drop_pending_updates=True)
    init_db()
    global AIO
    AIO = aiohttp.ClientSession(loop=asyncio.get_running_loop())
    workers.aio = AIO
    bot.username = (await bot.me()).username
    await workers.start_all()


async def on_shutdown():
    if AIO:
        await AIO.close()
    await workers.stop_all()


if __name__ == "__main__":
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    asyncio.run(dp.start_polling(bot))
