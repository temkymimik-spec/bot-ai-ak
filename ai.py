import json
import aiohttp
import config


async def _chat(session, prompt, system=None, max_tokens=600):
    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {"model": config.AI_MODEL, "messages": messages, "max_tokens": max_tokens}
    try:
        async with session.post(config.OPENROUTER_BASE, headers=headers, json=payload, timeout=40) as r:
            r.raise_for_status()
            data = await r.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Ошибка ИИ: {e}"


def _examples(example=None):
    if example:
        return example
    from database import db
    with db() as conn:
        rows = conn.execute("SELECT content FROM ai_examples").fetchall()
    return "\n".join(r["content"] for r in rows) or "(примеров пока нет)"


def _chatbot_system(example=None):
    return (
        "Ты менеджер pocket-option, общаешься в телеграме как живой человек. "
        "Подражай стилю руководителя из примеров. Коротко, живо, без 'как бот', "
        "не упоминай что ты ИИ и сами инструкции.\n"
        f"{config.POCKET_CONTEXT}\n\n"
        "ТВОЯ ЗАДАЧА ПОМИМО ОБЩЕНИЯ: аккуратно, по ходу диалога (НЕ одним сообщением, "
        "не давя), узнавать у человека по чуть-чуть:\n"
        "- делал ли он депозит на pocket-option и сколько\n"
        "- какой у него pocket id / аккаунт\n"
        "- торговал ли (сделки), сколько заработал/слил\n\n"
        "Выясняй это постепенно, естественно, в разговорной манере. Доведи до того, "
        "чтобы он либо сделал первый депозит/действие, либо честно сказал.\n\n"
        f"ПРИМЕРЫ (стиль):\n{_examples(example)}"
    )


def _eval_system():
    return (
        "Ты строгий аналитик партнёрской сети pocket-option. Определяешь, сделал ли "
        "человек РЕАЛЬНОЕ действие на pocket-option по итогам переписки.\n\n"
        "ДЕЙСТВИЕ ЗАСЧИТАНО, если есть хотя бы одно:\n"
        "- внёс депозит на pocket-option (называет сумму)\n"
        "- совершил сделку/торговал\n"
        "- дал свой pocket id / номер аккаунта pocket-option\n"
        "- подтвердил, что зарегистрирован и пополнил\n\n"
        "НЕ ЗАСЧИТАНО: просто говорит 'интересно', 'ок', задаёт вопросы, но действия "
        "нет; путает с другими платформами; нет pocket id при REQUIRE_ACTION=1.\n\n"
        "Верни СТРОГО JSON:\n"
        "{\"cash_action\": true/false, \"confirmations\": число_подтверждений_действия, "
        "\"pocket_id\": \"...или null\", \"income\": \"...или null\", "
        "\"reason\": \"кратко\"}"
    )


async def ai_reply(session, history, example=None):
    """Живой ответ клиенту (асинхронно) — ИИ и сам собирает данные по ходу."""
    return await _chat(session, str(history[-1]["content"]), system=_chatbot_system(example), max_tokens=400)


async def evaluate_action(session, conversation_text, confs_so_far):
    """Проверка: сделал ли человек реальное действие на pocket-option."""
    prompt = (
        f"Переписка (последние сообщения):\n{conversation_text[:4000]}\n\n"
        f"Подтверждений действия уже было: {confs_so_far}\n"
        f"Требуется реальное действие: {'да' if config.REQUIRE_ACTION else 'нет'}\n\n"
        "Оцени, сделал ли человек действие на pocket-option."
    )
    raw = await _chat(session, prompt, system=_eval_system(), max_tokens=250)
    try:
        raw = raw.strip().strip("```").strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()
        data = json.loads(raw)
        cash = bool(data.get("cash_action", False))
        confs = int(data.get("confirmations", confs_so_far))
        pocket = data.get("pocket_id")
        income = data.get("income")
        reason = data.get("reason", "")
        confirmed = (
            cash
            and confs >= config.MIN_CONFIRMATIONS
            and (pocket or income) is not None
            and (pocket or income or "") != ""
        )
        return {
            "cash_action": cash,
            "confirmations": confs,
            "pocket_id": pocket,
            "income": income,
            "reason": reason,
            "confirmed": confirmed,
        }
    except Exception:
        return {
            "cash_action": False,
            "confirmations": confs_so_far,
            "pocket_id": None,
            "income": None,
            "reason": "не удалось распарсить ответ ИИ",
            "confirmed": False,
        }
