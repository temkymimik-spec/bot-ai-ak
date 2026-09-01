import os
from pathlib import Path

_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")

SESSION_DIR = os.path.join(os.path.dirname(__file__), "sessions")
os.makedirs(SESSION_DIR, exist_ok=True)

OPENROUTER_BASE = "https://openrouter.ai/api/v1/chat/completions"
AI_MODEL = os.getenv("AI_MODEL", "openai/gpt-4o-mini")

# Экономика
LID_RATE = 250
MIN_WITHDRAW_LIDS = 4

# Жёсткий скоринг
SCORE_CONFIRM_THRESHOLD = 0.75      # минимальный средний скор, чтобы назвать лидом
MIN_CONFIRMATIONS = 2              # минимум независимых подтверждений интереса
REQUIRE_PROOF = True               # лид без proof не засчитывается

# Асинхронность / лимиты аккаунта
DAILY_REPLY_LIMIT = 300            # ответов ИИ с одного аккаунта в день (защита от бана)
RATE_LIMIT_DELAY = 0.7             # секунды между исходящими сообщениями с аккаунта
WARM_UP_MSGS = 5                   # начальных сообщений текстом, потом медленнее (тепло акка)

# Капча
CAPTCHA_CHANNELS_REQUIRED = 3      # сколько каналов надо подписаться

# ИИ-грев на pocket-option
POCKET_CONTEXT = (
    "Люди приходят по партнёрской схеме зарабатывать на pocket-option. "
    "Ты менеджер, греешь клиентов к торговле. Тащи их к первому депозиту "
    "и к выполнению стратегий, мягко, без обещаний гарантий."
)
