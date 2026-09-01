import sqlite3
from contextlib import contextmanager

DB_PATH = "bot.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript(
            """
            -- Подключенные Telegram-аккаунты (Telethon .session) с ИИ
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                session_path TEXT,
                username TEXT,              -- @юзернейм аккаунта (выдаётся лиду)
                status TEXT DEFAULT 'active',
                daily_replies INTEGER DEFAULT 0,
                daily_date TEXT,
                created_at INTEGER
            );

            -- Партнёры (пользователи бота)
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                leads INTEGER DEFAULT 0,
                created_at INTEGER
            );

            -- Индивидуальные партнёрские ссылки
            CREATE TABLE IF NOT EXISTS links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                session_id INTEGER,
                code TEXT UNIQUE,
                clicks INTEGER DEFAULT 0,
                created_at INTEGER
            );

            -- Каналы для капчи (суб/гарант)
            CREATE TABLE IF NOT EXISTS captcha_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER UNIQUE,
                username TEXT,
                title TEXT
            );

            -- Лиды (каждый входящий через бота/капчу)
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                partner_id INTEGER,          -- партнёр, чья ссылка
                link_id INTEGER,
                telegram_id TEXT,            -- id чела в боте
                username TEXT,
                ai_username TEXT,            -- какой аккаунт ИИ выдан
                status TEXT DEFAULT 'fresh', -- fresh / handed / qualified / rejected / ghost
                score REAL DEFAULT 0,        -- жёсткий скоринг 0..1
                confirmations INTEGER DEFAULT 0,
                proof TEXT,
                income TEXT,
                pocket_id TEXT,
                captcha_passed INTEGER DEFAULT 0,
                created_at INTEGER,
                updated_at INTEGER
            );

            -- Примеры общения для ИИ
            CREATE TABLE IF NOT EXISTS ai_examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT
            );

            -- Истории диалогов ИИ с лидами (по аккаунту)
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ai_username TEXT,
                telegram_id TEXT,
                messages TEXT,
                updated_at INTEGER,
                UNIQUE(ai_username, telegram_id)
            );

            -- Заявки на вывод
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                lead_count INTEGER,
                amount_rub INTEGER,
                status TEXT DEFAULT 'pending',
                wallet TEXT,
                created_at INTEGER
            );
            """
        )
