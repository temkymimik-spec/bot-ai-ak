import asyncio
import json
import datetime
import aiohttp
from telethon import TelegramClient
from telethon.events import NewMessage
import config
import ai as ai_module
from database import db


class AccountWorker:
    """Один TelegramClient + очередь исходящих сообщений (rate-limit) + ИИ-грев."""

    def __init__(self, session_name):
        self.session_name = session_name
        self.username = None
        self.client = None
        self._queue = asyncio.Queue()
        self._sender_task = None

    # -------- старт --------
    async def start(self):
        self.client = TelegramClient(
            f"{config.SESSION_DIR}/{self.session_name}.session",
            config.API_ID,
            config.API_HASH,
        )
        await self.client.start()
        me = await self.client.get_me()
        self.username = me.username
        # обновляем @username в БД
        with db() as conn:
            conn.execute(
                "UPDATE sessions SET username=? WHERE name=?", (self.username, self.session_name)
            )
        self.client.add_event_handler(self.on_message, NewMessage())
        self._sender_task = asyncio.create_task(self._sender())
        print(f"[worker] {self.session_name} запущен (@{self.username})")

    async def stop(self):
        if self._sender_task:
            self._sender_task.cancel()
        if self.client:
            await self.client.disconnect()

    # -------- исходящая очередь (rate-limit на аккаунт) --------
    async def _sender(self):
        while True:
            chat, text = await self._queue.get()
            try:
                await self.client.send_message(chat, text)
                with db() as conn:
                    conn.execute(
                        "UPDATE sessions SET daily_replies=daily_replies+1, daily_date=? "
                        "WHERE name=?",
                        (datetime.date.today().isoformat(), self.session_name),
                    )
                # тепло аккаунта: первые N ответов быстрые, потом с задержкой
                if self._queue.qsize() > 0:
                    await asyncio.sleep(config.RATE_LIMIT_DELAY)
            except Exception as e:
                print(f"[worker] ошибка send {self.session_name}: {e}")
            finally:
                self._queue.task_done()

    def _check_daily_limit(self):
        with db() as conn:
            row = conn.execute(
                "SELECT daily_replies, daily_date FROM sessions WHERE name=?", (self.session_name,)
            ).fetchone()
        today = datetime.date.today().isoformat()
        if not row or row["daily_date"] != today:
            return 0
        return row["daily_replies"]

    # -------- входящее сообщение от лида --------
    async def on_message(self, event):
        chat = await event.get_chat()
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False):
            return
        tg_id = str(sender.id)

        # учёт лида в базе (помечаем, что аккаунт его греет)
        self._ensure_lead(tg_id, sender.username)

        # если аккаунт вышел за дневной лимит ответов — молчим (не спалим)
        if self._check_daily_limit() >= config.DAILY_REPLY_LIMIT:
            return

        history = self._load_history(tg_id)
        history.append({"role": "user", "content": event.raw_text})
        history = history[-40:]

        # генерация ответа ИИ идёт асинхронно, а отправка — через очередь аккаунта
        async with aiohttp.ClientSession(loop=asyncio.get_running_loop()) as session:
            reply = await ai_module.ai_reply(session, history)
        history.append({"role": "assistant", "content": reply})
        self._save_history(self.username, tg_id, history)

        await self._queue.put((chat, reply))

    def _ensure_lead(self, tg_id, username):
        with db() as conn:
            row = conn.execute("SELECT id FROM leads WHERE telegram_id=?", (tg_id,)).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO leads (telegram_id, username, ai_username, status, captcha_passed, "
                    "created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
                    (tg_id, username, self.username, "handed",
                     int(datetime.datetime.now().timestamp()),
                     int(datetime.datetime.now().timestamp())),
                )
            else:
                conn.execute(
                    "UPDATE leads SET username=?, ai_username=?, updated_at=? WHERE id=?",
                    (username, self.username, int(datetime.datetime.now().timestamp()), row["id"]),
                )

    # -------- история --------
    def _load_history(self, tg_id):
        with db() as conn:
            row = conn.execute(
                "SELECT messages FROM conversations WHERE ai_username=? AND telegram_id=?",
                (self.username, tg_id),
            ).fetchone()
        if row:
            return json.loads(row["messages"])
        return [{"role": "assistant", "content": "Привет 👋 👀"}]

    @staticmethod
    def _save_history(ai_username, tg_id, messages):
        with db() as conn:
            conn.execute(
                "INSERT INTO conversations (ai_username, telegram_id, messages, updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(ai_username, telegram_id) "
                "DO UPDATE SET messages=?, updated_at=?",
                (
                    ai_username,
                    tg_id,
                    json.dumps(messages, ensure_ascii=False),
                    int(datetime.datetime.now().timestamp()),
                    json.dumps(messages, ensure_ascii=False),
                    int(datetime.datetime.now().timestamp()),
                ),
            )


class Workers:
    def __init__(self):
        self.account = None  # один аккаунт на всех (упростим). Можно расширить до dict.

    async def start_all(self):
        with db() as conn:
            rows = conn.execute("SELECT name FROM sessions WHERE status='active'").fetchall()
        for row in rows:
            try:
                w = AccountWorker(row["name"])
                await w.start()
                self.account = w
            except Exception as e:
                print(f"[worker] ошибка {row['name']}: {e}")

    async def restart(self):
        if self.account:
            await self.account.stop()
        self.account = None
        await self.start_all()

    def get_active_username(self):
        return self.account.username if self.account else None
