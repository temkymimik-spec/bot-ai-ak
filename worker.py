import asyncio
import json
import datetime
from telethon import TelegramClient
from telethon.events import NewMessage
import config
import ai as ai_module
from database import db


# Магический префикс, чтобы бот (бот-аккаунт) и воркеры не путали «кто это».
# Воркерам нечего делать с сообщениями от бота — пропускаем bot=True.

class AccountWorker:
    """Один TelegramClient + очередь исходящих (rate-limit) + ИИ-грев и авто-начисление лидов."""

    EVAL_EVERY = 4  # проверять лида каждые N сообщений

    def __init__(self, session_name, aio_session):
        self.session_name = session_name
        self.username = None
        self.example = None          # собственный стиль аккаунта
        self.client = None
        self.aio = aio_session
        self._queue = asyncio.Queue()
        self._sender_task = None

    # ---------- старт / стоп ----------
    async def start(self):
        with db() as conn:
            row = conn.execute(
                "SELECT example, status FROM sessions WHERE name=?", (self.session_name,)
            ).fetchone()
        if not row or row["status"] != "active":
            return
        self.example = row["example"]

        self.client = TelegramClient(
            f"{config.SESSION_DIR}/{self.session_name}.session",
            config.API_ID, config.API_HASH,
        )
        await self.client.start()
        me = await self.client.get_me()
        self.username = me.username
        with db() as conn:
            conn.execute("UPDATE sessions SET username=? WHERE name=?",
                         (self.username, self.session_name))
        self.client.add_event_handler(self.on_message, NewMessage())
        self._sender_task = asyncio.create_task(self._sender())
        print(f"[worker] {self.session_name} запущен (@{self.username})")

    async def stop(self):
        if self._sender_task:
            self._sender_task.cancel()
        if self.client:
            await self.client.disconnect()

    # ---------- исходящая очередь (rate-limit на аккаунт) ----------
    async def _sender(self):
        while True:
            chat, text = await self._queue.get()
            try:
                await self.client.send_message(chat, text)
                with db() as conn:
                    conn.execute(
                        "UPDATE sessions SET daily_replies=daily_replies+1, daily_date=? WHERE name=?",
                        (datetime.date.today().isoformat(), self.session_name),
                    )
                if self._queue.qsize():
                    await asyncio.sleep(config.RATE_LIMIT_DELAY)
            except Exception as e:
                print(f"[worker] send err {self.session_name}: {e}")
            finally:
                self._queue.task_done()

    def _daily_replies(self):
        with db() as conn:
            row = conn.execute(
                "SELECT daily_replies, daily_date FROM sessions WHERE name=?", (self.session_name,)
            ).fetchone()
        if not row or row["daily_date"] != datetime.date.today().isoformat():
            return 0
        return row["daily_replies"]

    def _load_example(self):
        """Подтянуть актуальный стиль аккаунта (если админ его менял)."""
        with db() as conn:
            row = conn.execute("SELECT example FROM sessions WHERE name=?", (self.session_name,)).fetchone()
        if row and row["example"]:
            self.example = row["example"]

    # ---------- входящее от лида ----------
    async def on_message(self, event):
        chat = await event.get_chat()
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False):
            return
        tg_id = str(sender.id)
        if self._daily_replies() >= config.DAILY_REPLY_LIMIT:
            return

        self._load_example()
        hist = self._load_history(tg_id)
        hist.append({"role": "user", "content": event.raw_text})
        hist = hist[-40:]

        reply = await ai_module.ai_reply(self.aio, hist, self.example)
        hist.append({"role": "assistant", "content": reply})
        self._save_history(tg_id, hist)

        await self._queue.put((chat, reply))
        self._ensure_lead(tg_id, sender.username)

        # авто-сбор действия: оцениваем раз в N сообщений и начисляем лида
        if len(hist) % self.EVAL_EVERY == 0:
            await self._maybe_credit(tg_id, hist)

    def _ensure_lead(self, tg_id, username):
        with db() as conn:
            row = conn.execute("SELECT id FROM leads WHERE telegram_id=?", (tg_id,)).fetchone()
            now = int(datetime.datetime.now().timestamp())
            if not row:
                conn.execute(
                    "INSERT INTO leads (telegram_id, username, ai_username, status, captcha_passed, "
                    "created_at, updated_at) VALUES (?,?,?,'handed',1,?,?)",
                    (tg_id, username, self.username, now, now),
                )

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
                (ai_username, tg_id, json.dumps(messages, ensure_ascii=False),
                 int(datetime.datetime.now().timestamp()),
                 json.dumps(messages, ensure_ascii=False),
                 int(datetime.datetime.now().timestamp())),
            )

    # ---------- авто-начисление лида ----------
    async def _maybe_credit(self, tg_id, history):
        with db() as conn:
            lead = conn.execute("SELECT * FROM leads WHERE telegram_id=?", (tg_id,)).fetchone()
        if not lead or lead["credited"]:
            return
        txt = "\n".join(f"{x['role']}: {x['content'][:200]}" for x in history[-12:])
        res = await ai_module.evaluate_action(self.aio, txt, lead["confirmations"])

        with db() as conn:
            lead = conn.execute("SELECT * FROM leads WHERE telegram_id=?", (tg_id,)).fetchone()
            if lead["credited"]:
                return
            conn.execute(
                "UPDATE leads SET confirmations=?, pocket_id=?, income=?, "
                "action_done=?, status=?, updated_at=? WHERE id=?",
                (res["confirmations"], res["pocket_id"], res["income"],
                 int(res["cash_action"]),
                 "qualified" if res["confirmed"] else "pending",
                 int(datetime.datetime.now().timestamp()), lead["id"]),
            )

        if res["confirmed"] and lead["partner_id"]:
            with db() as conn:
                conn.execute("UPDATE users SET leads=leads+1 WHERE id=?", (lead["partner_id"],))
                conn.execute("UPDATE leads SET credited=1 WHERE id=?", (lead["id"],))
            print(f"[worker] ЛИД начислен: {lead['partner_id']} ← {tg_id} "
                  f"(pocket={res.get('pocket_id')}, доход={res.get('income')})")


class Workers:
    """Менеджер нескольких аккаунтов (сессий)."""

    def __init__(self, aio_session):
        self.aio = aio_session
        self.workers = {}

    async def start_all(self):
        await self.stop_all()
        with db() as conn:
            rows = conn.execute("SELECT name FROM sessions WHERE status='active'").fetchall()
        for row in rows:
            await self.start_one(row["name"])

    async def start_one(self, name):
        try:
            w = AccountWorker(name, self.aio)
            await w.start()
            if w.client:
                self.workers[name] = w
        except Exception as e:
            print(f"[worker] ошибка {name}: {e}")

    async def stop_one(self, name):
        w = self.workers.pop(name, None)
        if w:
            await w.stop()

    async def stop_all(self):
        for name in list(self.workers.keys()):
            await self.stop_one(name)

    async def restart(self):
        await self.start_all()

    def get_active_usernames(self):
        return {n: w.username for n, w in self.workers.items() if w.username}

    def username_of(self, name):
        w = self.workers.get(name)
        return w.username if w else None
