"""Telegram bot interface for Синкотека multi-agent office."""

import asyncio
import json
import logging
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path
from dotenv import load_dotenv

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

load_dotenv(override=True)

KNOWLEDGE_DIR = Path(__file__).parent.parent.parent / "data" / "knowledge"
PROMPTS_DIR = Path(__file__).parent / "config" / "prompts"


def _load_prompt(name: str, fallback: str = "") -> str:
    """Load prompt from config/prompts/<name>.md, fall back to hardcoded string."""
    path = PROMPTS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return fallback

# --- Owner guard ---

def _is_owner(update: Update) -> bool:
    owner_id = os.getenv("TELEGRAM_OWNER_ID", "")
    if not owner_id:
        return True  # no restriction if not set
    return str(update.effective_user.id) == owner_id


async def _deny(update: Update) -> None:
    await update.message.reply_text("⛔ Доступ запрещён.")


# --- Agent identity ---

AGENT_NAMES = {
    "license_manager": "Екатерина",
    "lawyer": "Ксюша",
    "accountant": "Марина",
    "content_manager": "Саша",
    "biz_dev": "Директор по развитию",
    "developer": "Разработчик",
}

AGENT_LABELS = {
    "license_manager": "📋 Екатерина (Лицензионный менеджер)",
    "lawyer": "⚖️ Ксюша (Юрист)",
    "accountant": "💰 Марина (Бухгалтер)",
    "biz_dev": "🚀 Директор по развитию",
    "developer": "💻 Разработчик",
}

AGENT_MEMORY_NAMES = {
    "ekaterina": "license_manager",
    "ekaterina": "license_manager",
    "sasha": "content_manager",
    "marina": "accountant",
    "ksusha": "lawyer",
    "license_manager": "license_manager",
    "lawyer": "lawyer",
    "accountant": "accountant",
    "biz_dev": "biz_dev",
}

# Canonical memory name per agent key
MEMORY_NAME_MAP = {
    "license_manager": "ekaterina",
    "lawyer": "ksusha",
    "accountant": "marina",
    "content_manager": "sasha",
    "biz_dev": "biz_dev",
    "developer": "developer",
}

# --- Per-chat state ---

# License manager dialogue history (in-memory)
LICENSE_SESSIONS: dict[int, list[dict]] = defaultdict(list)

# Teaching mode: chat_id → agent memory name ("ekaterina", "marina", etc.)
TEACH_SESSIONS: dict[int, str] = {}

# Sticky agent: /lawyer etc sets this; None = coordinator mode
ACTIVE_AGENT: dict[int, str] = {}

# Coordinator dialogue history
COORDINATOR_SESSIONS: dict[int, list[dict]] = defaultdict(list)

# Direct agent dialogue sessions (not CrewAI)
DIRECT_SESSIONS: dict[str, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))

# --- Logging ---

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# --- Voice transcription ---

async def transcribe_voice_file(file_path: str) -> str:
    """Transcribe OGG/audio file using OpenAI Whisper. Returns empty string if unavailable."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return ""
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=api_key)
        with open(file_path, "rb") as f:
            transcript = await client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="ru",
            )
        return transcript.text.strip()
    except Exception as e:
        logger.warning(f"Whisper transcription failed: {e}")
        return ""


async def download_and_transcribe(update: Update) -> str | None:
    """Download voice/audio message and return transcribed text. None if failed."""
    voice = update.message.voice or update.message.audio
    if not voice:
        return None

    thinking = await update.message.reply_text("🎤 Распознаю голос…")
    try:
        tg_file = await voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)
        text = await transcribe_voice_file(tmp_path)
        Path(tmp_path).unlink(missing_ok=True)

        if not text:
            await thinking.edit_text(
                "Голосовые сообщения требуют OPENAI_API_KEY (Whisper).\n"
                "Добавь ключ в .env или напиши текстом."
            )
            return None

        await thinking.edit_text(f"🎤 {text}")
        return text
    except Exception as e:
        logger.exception("Voice download/transcription error")
        await thinking.edit_text(f"Ошибка распознавания: {e}")
        return None


# --- License manager (Екатерина) dialogue ---

LICENSE_SYSTEM_PROMPT = _load_prompt("ekaterina")


def run_license_dialogue(chat_id: int, user_message: str) -> dict:
    """Direct Anthropic API call with conversation history for Екатерина."""
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    history = LICENSE_SESSIONS[chat_id]
    history.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=LICENSE_SYSTEM_PROMPT,
        messages=history,
    )

    assistant_text = response.content[0].text
    history.append({"role": "assistant", "content": assistant_text})
    LICENSE_SESSIONS[chat_id] = history[-20:]

    # Try full text first, then extract first JSON object from anywhere in text
    for candidate in [
        re.sub(r"```json|```", "", assistant_text).strip(),
        *re.findall(r'\{[\s\S]*?\}(?=\s*$|\s*\n\s*[^{])', assistant_text),
        *(m.group() for m in [re.search(r'\{[\s\S]+\}', assistant_text)] if m),
    ]:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and "action" in parsed:
                return parsed
        except Exception:
            continue
    return {"action": "continue_dialogue", "reply_text": assistant_text}


def save_to_asana(full_text: str) -> str:
    """Create Asana task from license request draft. Returns task URL or error."""
    import httpx
    token = os.getenv("ASANA_TOKEN", "")
    project_id = os.getenv("ASANA_PROJECT_ID", "")
    workspace_id = os.getenv("ASANA_WORKSPACE_ID", "331121027676371")
    if not token:
        return "⚠️ ASANA_TOKEN не настроен"
    if not project_id and not workspace_id:
        return "⚠️ Укажи ASANA_PROJECT_ID или ASANA_WORKSPACE_ID в переменных Railway"

    lines = full_text.strip().splitlines()
    task_name = lines[0].replace("ПРОЕКТ ЗАДАЧИ:", "").strip().strip("«»") if lines else "Запрос лицензии"
    notes = "\n".join(lines[1:]).strip() if len(lines) > 1 else full_text

    import datetime
    data: dict = {
        "name": task_name,
        "notes": notes,
        "assignee": "denis@synclab.pro",
        "due_on": datetime.date.today().isoformat(),
    }
    if project_id:
        data["projects"] = [project_id]
    else:
        data["workspace"] = workspace_id

    try:
        resp = httpx.post(
            "https://app.asana.com/api/1.0/tasks",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"data": data},
            timeout=15,
        )
        resp.raise_for_status()
        task_id = resp.json()["data"]["gid"]
        link = f"https://app.asana.com/0/{project_id}/{task_id}" if project_id else f"https://app.asana.com/0/{workspace_id}/{task_id}"
        return f"✅ Задача в Asana 👍\n{link}"
    except httpx.HTTPStatusError as e:
        return f"❌ Ошибка Asana {e.response.status_code}: {e.response.text[:400]}"
    except Exception as e:
        return f"❌ Ошибка Asana: {e}"


# --- Agent routing ---

AGENT_KEYWORDS = {
    "license_manager": [
        "лицензи", "правообладател", "права", "синхронизац", "sync", "isrc",
        "iswc", "паблишер", "publisher", "рао", "mcps", "ascap", "bmi",
        "переговор", "трек", "каталог", "найди", "найти", "екатерина", "катя",
    ],
    "lawyer": [
        "договор", "контракт", "юрист", "юридич", "риск", "эксклюзив",
        "exclusive", "worldwide", "territory", "perpetuity", "sublicens",
        "гк рф", "нарушен", "претензи", "иск", "аудит", "ксюша", "ксения",
    ],
    "accountant": [
        "роялти", "royalt", "счёт", "счет", "акт", "ндс", "налог",
        "оплат", "доход", "расход", "бухгалтер", "финанс", "отчёт", "отчет",
        "платёж", "платеж", "выплат", "рассчита", "марина",
    ],
    "biz_dev": [
        "питч", "pitch", "supervisor", "бренд", "netflix", "реклам",
        "агентств", "партнёр", "партнер", "продвижени", "outreach",
        "холодное письмо", "новый клиент", "развити",
    ],
    "developer": [
        "база данных", "схема", "api", "интеграц", "ddex", "cwr",
        "метадан", "supabase", "postgresql", "sql", "автоматиз",
        "разработ", "скрипт", "импорт", "саша",
    ],
}

SLASH_MAP = {
    "license": "license_manager",
    "lawyer": "lawyer",
    "accountant": "accountant",
    "bizdev": "biz_dev",
    "dev": "developer",
}

# --- Direct agent prompts (no CrewAI, direct Anthropic call) ---
# Prompts loaded from src/syncoteca/config/prompts/<name>.md
# Edit those files directly — no code change needed.

_PROMPT_KEYS = {
    "accountant": "marina",
    "lawyer": "ksusha",
    "biz_dev": "biz_dev",
    "content_manager": "sasha",
    "developer": "developer",
}

DIRECT_PROMPTS: dict[str, str] = {
    agent: _load_prompt(fname)
    for agent, fname in _PROMPT_KEYS.items()
}

# Agents that use direct call (not CrewAI) in bot context
DIRECT_AGENTS = set(DIRECT_PROMPTS.keys())


def run_direct_agent(agent_name: str, chat_id: int, user_message: str) -> str:
    """Direct Anthropic API call without CrewAI overhead."""
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    system = DIRECT_PROMPTS.get(agent_name) or "Ты — помощник агентства Синкотека."
    history = DIRECT_SESSIONS[agent_name][chat_id]
    history.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        system=system,
        messages=history[-16:],
    )

    reply = response.content[0].text.strip()
    history.append({"role": "assistant", "content": reply})
    DIRECT_SESSIONS[agent_name][chat_id] = history[-20:]
    return reply


def classify_agent(text: str) -> str:
    lower = text.lower()
    scores = {agent: 0 for agent in AGENT_KEYWORDS}
    for agent, keywords in AGENT_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                scores[agent] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "license_manager"


def run_agent(agent_name: str, user_request: str) -> str:
    from .crew import SyncotecaCrew
    from crewai import Agent, Crew, Task, Process

    crew_instance = SyncotecaCrew()

    agent_map = {
        "license_manager": crew_instance.license_manager,
        "lawyer": crew_instance.lawyer,
        "accountant": crew_instance.accountant,
        "biz_dev": crew_instance.biz_dev,
        "developer": crew_instance.developer,
    }

    agent: Agent = agent_map[agent_name]()
    name = AGENT_NAMES.get(agent_name, agent_name)

    task = Task(
        description=(
            f"Запрос от Дениса (руководителя Синкотека):\n\n{user_request}\n\n"
            "Дай конкретный, практически применимый ответ на русском языке. "
            "Используй инструменты (synclab_db, database, search и др.) если нужны данные. "
            "Если нужна консультация с Денисом — явно укажи это в ответе. "
            "Будь краток — не более 600 слов."
        ),
        expected_output=(
            "Чёткий профессиональный ответ на запрос. "
            "Если нужны действия — пронумерованный список шагов."
        ),
        agent=agent,
    )

    crew = Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        max_rpm=15,
        verbose=False,
    )

    result = crew.kickoff()
    return str(result)


# --- Coordinator ---

_COORDINATOR_PROMPT_TEMPLATE = _load_prompt("coordinator")


def _get_coordinator_prompt() -> str:
    from datetime import date
    today = date.today().strftime("%Y-%m-%d")
    return _COORDINATOR_PROMPT_TEMPLATE.replace("{TODAY}", today)


def run_coordinator(chat_id: int, message: str) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    history = COORDINATOR_SESSIONS[chat_id]
    history.append({"role": "user", "content": message})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        system=_get_coordinator_prompt(),
        messages=history[-12:],
    )

    text = response.content[0].text.strip()
    history.append({"role": "assistant", "content": text})
    COORDINATOR_SESSIONS[chat_id] = history[-20:]

    try:
        clean = re.sub(r"```json|```", "", text).strip()
        return json.loads(clean)
    except Exception:
        return {"action": "reply", "text": text}


# --- Teaching mode ---

def _resolve_memory_name(raw: str) -> str:
    """Map user input to canonical agent_memory name."""
    mapping = {
        "екатерина": "ekaterina",
        "катя": "ekaterina",
        "ekaterina": "ekaterina",
        "license": "ekaterina",
        "license_manager": "ekaterina",
        "саша": "sasha",
        "sasha": "sasha",
        "content": "sasha",
        "марина": "marina",
        "marina": "marina",
        "accountant": "marina",
        "ксюша": "ksusha",
        "ксения": "ksusha",
        "ksusha": "ksusha",
        "lawyer": "ksusha",
    }
    return mapping.get(raw.lower(), raw.lower())


def _memory_display_name(mem_name: str) -> str:
    return {
        "ekaterina": "Екатерины",
        "sasha": "Саши",
        "marina": "Марины",
        "ksusha": "Ксюши",
    }.get(mem_name, mem_name)


def _knowledge_read(mem_name: str) -> list[dict]:
    path = KNOWLEDGE_DIR / f"{mem_name}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _knowledge_write(mem_name: str, entries: list[dict]) -> None:
    from datetime import datetime as _dt
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    path = KNOWLEDGE_DIR / f"{mem_name}.json"
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


async def _save_to_memory(mem_name: str, text: str, update: Update) -> None:
    """Save text to agent local knowledge JSON (data/knowledge/{mem_name}.json)."""
    from datetime import datetime as _dt
    ts = _dt.now().strftime("%Y-%m-%d %H:%M")
    entries = _knowledge_read(mem_name)
    entries.insert(0, {"ts": ts, "text": text})
    _knowledge_write(mem_name, entries)
    display = _memory_display_name(mem_name)
    await update.message.reply_text(
        f"✅ [{ts}] → {display}:\n\n{text[:400]}"
        + ("\n\n…" if len(text) > 400 else "")
    )


# --- Command handlers ---

WELCOME = """
👋 *Синкотека* — AI-офис музыкального агентства.

По умолчанию работает *Координатор* — он сам поймёт кому передать задачу.

*Прямой доступ к агенту:*
/license — Екатерина (лицензии, права)
/lawyer — Ксюша (договоры, юрист)
/accountant — Марина (роялти, бухгалтерия)
/bizdev — Директор по развитию
/dev — Разработчик
/stop — вернуться к координатору

*Обучение агентов:*
/know марина НДС с 2026: 22%
/teach екатерина — режим обучения (все сообщения → знания агента)
/teach_stop — выйти из режима обучения
/memory екатерина — показать знания агента
"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, parse_mode="Markdown")


async def handle_slash_agent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cmd = update.message.text.split()[0].lstrip("/").lower()
    agent_name = SLASH_MAP.get(cmd)
    if not agent_name:
        await update.message.reply_text("Неизвестная команда.")
        return

    chat_id = update.effective_chat.id
    TEACH_SESSIONS.pop(chat_id, None)
    ACTIVE_AGENT[chat_id] = agent_name
    # Clear Ekaterina's history when switching to another agent
    if agent_name != "license_manager":
        LICENSE_SESSIONS[chat_id] = []

    args = update.message.text.split(maxsplit=1)
    user_request = args[1] if len(args) > 1 else ""

    if agent_name == "license_manager":
        if not user_request:
            LICENSE_SESSIONS[chat_id] = []
            label = AGENT_LABELS["license_manager"]
            await update.message.reply_text(
                f"{label}\n\nГотова. Расскажи — какую песню и для какого проекта нужно лицензировать?"
            )
            return
        await _dispatch_license(update, user_request)
    else:
        label = AGENT_LABELS.get(agent_name, agent_name)
        if not user_request:
            await update.message.reply_text(
                f"{label}\n\nСлушаю. Пиши задачу — отвечу."
            )
            return
        await _dispatch(update, agent_name, user_request)


async def handle_teach(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enter teaching mode for an agent."""
    if not _is_owner(update):
        return await _deny(update)
    args = context.args
    raw = args[0] if args else "ekaterina"
    mem_name = _resolve_memory_name(raw)
    display = _memory_display_name(mem_name)

    chat_id = update.effective_chat.id
    TEACH_SESSIONS[chat_id] = mem_name
    ACTIVE_AGENT.pop(chat_id, None)
    await update.message.reply_text(
        f"🎓 Режим обучения {display} активен.\n\n"
        "Отправляй голосовые или текстовые сообщения — "
        "всё сохранится в долгосрочную память агента в Supabase.\n\n"
        "Примеры того, что можно передать:\n"
        "— «Малышко Евгений — представитель Пахмутовой, пишем через e.malyshko@gmail.com»\n"
        "— «С СОЮЗ МЬЮЗИК не работаем, только через Осипов и Партнеры»\n"
        "— «Для Мелодии минимальный flat fee — 200 000 руб»\n\n"
        "/teach_stop — выйти из режима обучения"
    )


async def handle_teach_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mem_name = TEACH_SESSIONS.pop(update.effective_chat.id, None)
    if mem_name:
        display = _memory_display_name(mem_name)
        await update.message.reply_text(f"✅ Режим обучения {display} завершён.")
    else:
        await update.message.reply_text("Режим обучения не был активен.")


async def handle_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show agent knowledge (/memory <agent>)."""
    if not _is_owner(update):
        return await _deny(update)
    args = context.args
    raw = args[0] if args else "ekaterina"
    mem_name = _resolve_memory_name(raw)
    display = _memory_display_name(mem_name)
    entries = _knowledge_read(mem_name)
    if not entries:
        await update.message.reply_text(f"📭 Знаний для {display} пока нет.")
        return
    lines = [f"[{e.get('ts','—')}] {e.get('text','')}" for e in entries[:15]]
    text = f"📚 Знания {display} ({len(entries)} записей):\n\n" + "\n\n".join(lines)
    MAX = 4000
    for i in range(0, len(text), MAX):
        await update.message.reply_text(text[i:i + MAX])


async def handle_memory_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Directly add a knowledge entry: /remember <agent> <text>."""
    if not _is_owner(update):
        return await _deny(update)
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Использование: /remember <агент> <текст>\nПример: /remember marina НДС с 2026: 22%")
        return
    raw = args[0]
    text = " ".join(args[1:])
    mem_name = _resolve_memory_name(raw)
    await _save_to_memory(mem_name, text, update)


async def handle_know(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/know <agent> <text> — one-line knowledge injection."""
    if not _is_owner(update):
        return await _deny(update)
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        lines = [
            "Использование: /know <агент> <текст>",
            "",
            "Агенты: екатерина · ксюша · марина · саша · biz_dev · developer",
            "",
            "Пример: /know marina НДС внутренний рынок с 2026: 22%",
        ]
        await update.message.reply_text("\n".join(lines))
        return
    mem_name = _resolve_memory_name(parts[1])
    await _save_to_memory(mem_name, parts[2], update)


# --- Message routing ---

def _clean_for_telegram(text: str) -> str:
    """Strip markdown that Telegram can't parse."""
    text = re.sub(r"\|.*\|", "", text)
    text = re.sub(r"[-|]{3,}", "", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"(?<!\n)\*(?!\s)", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def _dispatch_license(update: Update, user_request: str) -> None:
    chat_id = update.effective_chat.id
    thinking_msg = await update.message.reply_text("📋 Екатерина думает…")

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, run_license_dialogue, chat_id, user_request
        )

        action = result.get("action", "continue_dialogue")
        reply_text = result.get("reply_text", "")

        if action == "save_to_asana":
            full_text = result.get("full_text", reply_text)
            await thinking_msg.edit_text(f"{full_text}\n\nСохраняю в Asana…")
            asana_result = await loop.run_in_executor(None, save_to_asana, full_text)
            LICENSE_SESSIONS[chat_id] = []
            await update.message.reply_text(asana_result)
        elif action == "send_email":
            to = result.get("to", "")
            subject = result.get("subject", "Запрос лицензии")
            body = result.get("body", reply_text)
            full_text = result.get("full_text", body)
            await thinking_msg.edit_text(f"📧 Отправляю письмо на {to}…")
            from syncoteca.tools.email_tool import EmailDraftTool
            mailer = EmailDraftTool()
            mail_result = await loop.run_in_executor(
                None, lambda: mailer._run(to=to, subject=subject, body=body, send=True)
            )
            LICENSE_SESSIONS[chat_id] = []
            await update.message.reply_text(mail_result)
        elif action == "send_both":
            to = result.get("to", "")
            subject = result.get("subject", "Запрос лицензии")
            body = result.get("body", reply_text)
            full_text = result.get("full_text", body)
            await thinking_msg.edit_text(f"📧 Отправляю письмо на {to} и сохраняю в Asana…")
            from syncoteca.tools.email_tool import EmailDraftTool
            mailer = EmailDraftTool()
            mail_result = await loop.run_in_executor(
                None, lambda: mailer._run(to=to, subject=subject, body=body, send=True)
            )
            asana_result = await loop.run_in_executor(None, save_to_asana, full_text)
            LICENSE_SESSIONS[chat_id] = []
            await update.message.reply_text(f"{mail_result}\n\n{asana_result}")
        elif action == "draft_ready":
            await thinking_msg.edit_text(
                f"{reply_text}\n\n—\nКуда отправить?\n📧 «в почту» — только email\n📋 «в асану» — только Asana\n📧📋 «в оба» — email + Asana"
            )
        else:
            await thinking_msg.edit_text(reply_text)

    except Exception as e:
        logger.exception("License dialogue error")
        await thinking_msg.edit_text(f"Ошибка: {e}")


async def _dispatch(update: Update, agent_name: str, user_request: str) -> None:
    from . import events as ev

    label = AGENT_LABELS.get(agent_name, agent_name)
    mem_name = MEMORY_NAME_MAP.get(agent_name, agent_name)
    thinking_msg = await update.message.reply_text(f"{label}…")

    ev.emit(mem_name, "task_start", user_request[:120], status="thinking")

    try:
        loop = asyncio.get_event_loop()
        chat_id = update.effective_chat.id

        if agent_name in DIRECT_AGENTS:
            result = await loop.run_in_executor(
                None, run_direct_agent, agent_name, chat_id, user_request
            )
        else:
            result = await loop.run_in_executor(None, run_agent, agent_name, user_request)

        ev.emit(mem_name, "task_done", str(result)[:120], status="idle")

        text = f"{label}\n\n{_clean_for_telegram(str(result))}"
        MAX = 4000
        parts = [text[i:i + MAX] for i in range(0, len(text), MAX)]
        await thinking_msg.edit_text(parts[0])
        for part in parts[1:]:
            await update.message.reply_text(part)

    except Exception as e:
        ev.emit(mem_name, "error", str(e)[:120], status="idle")
        err_str = str(e)
        if "529" in err_str or "overloaded" in err_str.lower():
            await thinking_msg.edit_text(
                "⏳ Anthropic API перегружен (529). Подожди 1–2 минуты и повтори запрос."
            )
        else:
            logger.exception("Agent error")
            await thinking_msg.edit_text(f"❌ Ошибка: {err_str[:300]}")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages: transcribe → teach or dispatch."""
    chat_id = update.effective_chat.id
    text = await download_and_transcribe(update)
    if not text:
        return

    if chat_id in TEACH_SESSIONS:
        await _save_to_memory(TEACH_SESSIONS[chat_id], text, update)
        return

    # Sticky agent takes priority over license session history
    if chat_id in ACTIVE_AGENT:
        agent_name = ACTIVE_AGENT[chat_id]
        if agent_name == "license_manager":
            await _dispatch_license(update, text)
        else:
            await _dispatch(update, agent_name, text)
        return

    if LICENSE_SESSIONS.get(chat_id):
        await _dispatch_license(update, text)
        return

    await _dispatch_coordinator(update, text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return await _deny(update)
    text = update.message.text.strip()
    if not text:
        return

    chat_id = update.effective_chat.id

    if chat_id in TEACH_SESSIONS:
        await _save_to_memory(TEACH_SESSIONS[chat_id], text, update)
        return

    # Sticky agent takes priority over license session history
    if chat_id in ACTIVE_AGENT:
        agent_name = ACTIVE_AGENT[chat_id]
        if agent_name == "license_manager":
            await _dispatch_license(update, text)
        else:
            await _dispatch(update, agent_name, text)
        return

    if LICENSE_SESSIONS.get(chat_id):
        await _dispatch_license(update, text)
        return

    # Default: Coordinator routes the message
    await _dispatch_coordinator(update, text)


async def _dispatch_coordinator(update: Update, text: str) -> None:
    chat_id = update.effective_chat.id
    thinking_msg = await update.message.reply_text("🎯 Рядовой…")
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, run_coordinator, chat_id, text)
        action = result.get("action", "reply")

        if action == "route":
            agent_name = result.get("agent", "license_manager")
            task = result.get("task", text)
            label = AGENT_LABELS.get(agent_name, agent_name)
            await thinking_msg.edit_text(f"🔀 → {label}")
            if agent_name == "license_manager":
                LICENSE_SESSIONS[chat_id] = []
                await _dispatch_license(update, task)
            else:
                await _dispatch(update, agent_name, task)
        elif action == "calendar":
            await thinking_msg.edit_text("📅 Создаю встречу…")
            from syncoteca.tools.google_calendar_tool import GoogleCalendarTool
            cal = GoogleCalendarTool()
            cal_result = await loop.run_in_executor(
                None,
                lambda: cal._run(
                    title=result.get("title", "Встреча"),
                    date=result.get("date", ""),
                    time=result.get("time", "10:00"),
                    duration_minutes=int(result.get("duration_minutes", 60)),
                    description=result.get("description", ""),
                    attendees=result.get("attendees", []),
                ),
            )
            await thinking_msg.edit_text(f"🎯 Рядовой:\n\n{cal_result}")
        elif action == "search":
            query = result.get("query", text)
            await thinking_msg.edit_text(f"🔍 Ищу: {query}…")
            from syncoteca.tools.web_search_tool import WebSearchTool
            searcher = WebSearchTool()
            search_results = await loop.run_in_executor(None, lambda: searcher._run(query))
            followup = f"Результаты поиска по запросу «{query}»:\n\n{search_results}\n\nОтветь кратко на вопрос пользователя: {text}"
            final = await loop.run_in_executor(None, run_coordinator, chat_id, followup)
            reply = final.get("text", search_results)
            await thinking_msg.edit_text(f"🎯 Рядовой:\n\n{reply}")
        else:
            reply = result.get("text", "…")
            await thinking_msg.edit_text(f"🎯 Рядовой:\n\n{reply}")
    except Exception as e:
        logger.exception("Coordinator error")
        await thinking_msg.edit_text(f"Ошибка координатора: {e}")


async def handle_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stop — clear sticky agent, return to coordinator."""
    if not _is_owner(update):
        return await _deny(update)
    chat_id = update.effective_chat.id
    ACTIVE_AGENT.pop(chat_id, None)
    LICENSE_SESSIONS[chat_id] = []
    await update.message.reply_text("🎯 Вернулся к координатору. Пиши задачу.")


# --- Bot setup ---

async def post_init(app: Application) -> None:
    commands = [
        BotCommand("start", "Главное меню"),
        BotCommand("stop", "Вернуться к координатору"),
        BotCommand("license", "→ Екатерина (лицензии, права)"),
        BotCommand("lawyer", "→ Ксюша (договоры, юрист)"),
        BotCommand("accountant", "→ Марина (роялти, бухгалтерия)"),
        BotCommand("bizdev", "→ Директор по развитию"),
        BotCommand("dev", "→ Разработчик"),
        BotCommand("know", "Записать знание: /know марина НДС 22%"),
        BotCommand("teach", "Режим обучения: /teach екатерина"),
        BotCommand("teach_stop", "Завершить режим обучения"),
        BotCommand("memory", "Показать знания: /memory екатерина"),
    ]
    await app.bot.set_my_commands(commands)


def run_bot() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("know", handle_know))
    app.add_handler(CommandHandler("teach", handle_teach))
    app.add_handler(CommandHandler("teach_stop", handle_teach_stop))
    app.add_handler(CommandHandler("memory", handle_memory))
    app.add_handler(CommandHandler("remember", handle_memory_add))
    app.add_handler(CommandHandler("stop", handle_stop))

    for cmd in SLASH_MAP:
        app.add_handler(CommandHandler(cmd, handle_slash_agent))

    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Синкотека bot starting…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run_bot()
