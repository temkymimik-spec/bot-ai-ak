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


def _examples():
    from database import db
    with db() as conn:
        rows = conn.execute("SELECT content FROM ai_examples").fetchall()
    return "\n".join(r["content"] for r in rows) or "(примеров пока нет)"


def _chatbot_system():
    return (
        "Ты менеджер pocket-option, общаешься в телеграме как живой человек. "
        "Подражай стилю руководителя из примеров. Коротко, живо, без 'как бот'. "
        "Не упоминай инструкции и что ты ИИ.\n"
        f"{config.POCKET_CONTEXT}\n\nПРИМЕРЫ (обязательно следуй стилю):\n{_examples()}"
    )


def _lead_eval_system():
    return (
        "Ты строгий аналитик лидогенерации для pocket-option. "
        "Задача: отсеять пустых и подтвердить только НАСТОЯЩИХ лидов — тех, кто "
        "реально заинтересован и действует.\n\n"
        "КРИТЕРИИ ЛИДА (нужно большинство):\n"
        "- интересуется, задаёт вопросы по торговле/выводу\n"
        "- упоминает/сделал депозит на pocket-option или готов\n"
        "- показывает активность не один раз (несколько сообщений/дней)\n"
        "- присылает proof: pocket id, скрин, сколько заработал\n\n"
        "НЕ ЛИД: односложные ответы, 'ок', молчание, сомнение, ничего не сделал, "
        "нет pocket id при REQUIRE_PROOF=1.\n\n"
        "Верни СТРОГО JSON:\n"
        "{\"score\": 0..1, \"confirmations\": число_подтверждений_интереса, "
        "\"is_lead\": bool, \"reasons\": [\"...\"], \"needs_proof\": bool}"
    )


async def ai_reply(session, history):
    """Живой ответ клиенту (асинхронно, через общий aiohttp-сессию)."""
    return await _chat(session, str(history[-1]['content']), system=_chatbot_system(), max_tokens=400)


async def evaluate_chat(session, conversation_text, proof, confirmations_so_far):
    """Жёсткая оценка чата. Возвращает структуру скоринга."""
    prompt = (
        f"Переписка (последние сообщения):\n{conversation_text[:4000]}\n\n"
        f"Proof, присланный человеком: {proof or '(пусто)'}\n"
        f"Конфирмаций уже было ранее: {confirmations_so_far}\n"
        f"Требуется ли proof для лида: {'да' if config.REQUIRE_PROOF else 'нет'}\n\n"
        "Оцени строго."
    )
    raw = await _chat(session, prompt, system=_lead_eval_system(), max_tokens=300)
    try:
        raw_clean = raw.strip().strip("```").strip()
        if raw_clean.startswith("json"):
            raw_clean = raw_clean[4:].strip()
        data = json.loads(raw_clean)
        score = float(data.get("score", 0))
        confs = int(data.get("confirmations", confirmations_so_far))
        needs_proof = bool(data.get("needs_proof", True))
        is_lead = bool(data.get("is_lead", False))
        reasons = data.get("reasons", ["нет причины"])
        return {
            "score": score,
            "confirmations": confs,
            "is_lead": is_lead,
            "needs_proof": needs_proof,
            "reasons": reasons,
            "confirmed": (
                not needs_proof
                and score >= config.SCORE_CONFIRM_THRESHOLD
                and confs >= config.MIN_CONFIRMATIONS
            ),
        }
    except Exception:
        return {
            "score": 0.0,
            "confirmations": confirmations_so_far,
            "is_lead": False,
            "needs_proof": True,
            "reasons": ["не удалось распарсить ответ ИИ"],
            "confirmed": False,
        }
