"""Telegram bot interface for Синкотека multi-agent office."""

import asyncio
import httpx
import json
import logging
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Optional
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
    "license_manager": "Рико",
    "lawyer": "Ксюша",
    "accountant": "Марина",
    "content_manager": "Ковальски",
    "biz_dev": "Директор по развитию",
    "developer": "Разработчик",
}

AGENT_LABELS = {
    "license_manager": "📋 Рико (Лицензионный менеджер)",
    "content_manager": "🗃️ Ковальски (Контент-менеджер)",
    "lawyer": "⚖️ Ксюша (Юрист)",
    "accountant": "💰 Марина (Бухгалтер)",
    "biz_dev": "🚀 Директор по развитию",
    "developer": "💻 Разработчик",
}

AGENT_MEMORY_NAMES = {
    "rico": "license_manager",
    "рико": "license_manager",
    "kowalski": "content_manager",
    "ковальски": "content_manager",
    "marina": "accountant",
    "ksusha": "lawyer",
    "license_manager": "license_manager",
    "lawyer": "lawyer",
    "accountant": "accountant",
    "biz_dev": "biz_dev",
}

# Canonical memory name per agent key
MEMORY_NAME_MAP = {
    "license_manager": "rico",
    "lawyer": "ksusha",
    "accountant": "marina",
    "content_manager": "kowalski",
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

# After enrich completes, store {chat_id: {"after_id": int, "limit": int}}
# so Kowalski can offer to fix release dates on the same batch.
_PENDING_DATE_FIX: dict[int, dict] = {}

# Pending label scrape confirmation {chat_id: {"label_id": str, "label_name": str, "analysis": dict}}
_PENDING_LABEL_SCRAPE: dict[int, dict] = {}

# /parse_label sent without args — next message is the label name
_PENDING_LABEL_NAME: set[int] = set()

# Last entity query used for a successful catalog lookup (chat_id → query text).
# Set when catalog_ctx is non-empty; cleared after export. Prevents stale history
# scan from picking up wrong artist on "да, выгрузи" follow-ups.
_LAST_CATALOG_ENTITY: dict[int, str] = {}

# After Excel sent: {chat_id: {"xlsx_bytes": bytes, "filename": str, "subject": str}}
# Waiting for user to confirm email send.
_PENDING_EMAIL_EXPORT: dict[int, dict] = {}

# Coordinator dialogue history
COORDINATOR_SESSIONS: dict[int, list[dict]] = defaultdict(list)


def _persist_active_agent(chat_id: int, agent_name: str) -> None:
    """Save sticky agent to Supabase so it survives Railway restarts."""
    import httpx
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_KEY", "")
    if not base or not key:
        return
    try:
        httpx.post(
            f"{base}/rest/v1/agent_sessions",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
            json={
                "session_id": f"sticky_{chat_id}",
                "agent_name": agent_name,
                "messages": [],
                "task_context": {"sticky": True, "chat_id": chat_id},
            },
            timeout=5,
        )
    except Exception:
        pass


def _restore_active_agent(chat_id: int) -> str | None:
    """Restore sticky agent from Supabase after restart. Returns agent name or None."""
    import httpx
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_KEY", "")
    if not base or not key:
        return None
    try:
        resp = httpx.get(
            f"{base}/rest/v1/agent_sessions",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            params={"session_id": f"eq.sticky_{chat_id}", "limit": "1"},
            timeout=5,
        )
        resp.raise_for_status()
        rows = resp.json()
        if rows:
            return rows[0].get("agent_name")
    except Exception:
        pass
    return None


def _set_pending_label_name(chat_id: int) -> None:
    """Persist 'waiting for label name' state to Supabase."""
    import httpx
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_KEY", "")
    if not base or not key:
        return
    try:
        httpx.post(
            f"{base}/rest/v1/agent_sessions",
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"},
            json={"session_id": f"pending_label_{chat_id}", "agent_name": "content_manager",
                  "messages": [], "task_context": {"pending_label_name": True, "chat_id": chat_id}},
            timeout=5,
        )
    except Exception:
        pass


def _clear_pending_label_name(chat_id: int) -> None:
    """Remove 'waiting for label name' state from Supabase."""
    import httpx
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_KEY", "")
    if not base or not key:
        return
    try:
        httpx.delete(
            f"{base}/rest/v1/agent_sessions",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            params={"session_id": f"eq.pending_label_{chat_id}"},
            timeout=5,
        )
    except Exception:
        pass


def _check_pending_label_name(chat_id: int) -> bool:
    """Check if 'waiting for label name' state exists in Supabase."""
    import httpx
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_KEY", "")
    if not base or not key:
        return False
    try:
        resp = httpx.get(
            f"{base}/rest/v1/agent_sessions",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            params={"session_id": f"eq.pending_label_{chat_id}", "limit": "1"},
            timeout=5,
        )
        resp.raise_for_status()
        return bool(resp.json())
    except Exception:
        return False


def _clear_active_agent(chat_id: int) -> None:
    """Remove sticky agent from Supabase."""
    import httpx
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_KEY", "")
    if not base or not key:
        return
    try:
        httpx.delete(
            f"{base}/rest/v1/agent_sessions",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            params={"session_id": f"eq.sticky_{chat_id}"},
            timeout=5,
        )
    except Exception:
        pass

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


# --- License manager (Рико) dialogue ---

LICENSE_SYSTEM_PROMPT = _load_prompt("rico")


_SEARCH_STOP_WORDS = {
    "найди", "ищи", "поищи", "ищу", "покажи", "скажи", "поиск", "запрос",
    "контакты", "контакт", "контактов", "контакте", "контакту",
    "есть", "нас", "там", "пожалуйста", "мне", "нужны", "нужен", "нужна",
    "как", "ли", "по", "в", "на", "для", "из", "с", "и", "или", "а", "но",
    "что", "это", "тоже", "еще", "ещё",
    "лейбл", "лейблу", "лейбла", "лейблов", "правообладатель", "правообладателя",
    "издательство", "издательства", "издательстве", "у", "me", "дай",
    "find", "search", "contacts", "contact", "the", "a", "an", "of", "for",
}

# Extra noise words specific to track/song searches — these match random tracks
# (e.g. "трек" appears in many song titles) and pollute the OR filter.
_TRACK_SEARCH_NOISE = _SEARCH_STOP_WORDS | {
    "трек", "треку", "треке", "трека", "треков", "треки",
    "песня", "песни", "песне", "песню", "песен",
    "музыка", "музыки", "музыке", "музыку",
    "информация", "информации", "информацию",
    "правам", "правах", "права", "право", "правами",
    "лицензия", "лицензии", "лицензию", "лицензирование",
    "права", "right", "rights", "track", "song", "music", "info",
    "хочу", "хочется", "узнать", "знать", "дать", "дай",
    "нужно", "нужен", "нужна", "нужны",
    # group/band meta-words
    "группы", "группа", "группе", "группу", "группой", "группах", "group", "band",
    "называется", "зовётся", "зовется", "под", "названием", "артист", "артиста",
    "исполнитель", "исполнителя", "исполнителем",
    # catalog query words — appear in "what do you have / show me tracks"
    "какие", "какой", "какая", "какое", "каких", "которые", "который",
    "тебя", "тебе", "твои", "твой", "твоя", "тебя",
    "базе", "базу", "базы", "базой", "каталоге", "каталогу", "каталог", "каталога",
    "посмотри", "выведи", "покажи", "перечисли", "назови", "скажи",
    "знаешь", "знаете", "имеются", "имеется", "числится", "числятся",
    "доступны", "доступно", "доступен", "available",
    "все", "всё", "всех", "список", "списке", "списку",
    "твоей", "вашей", "вашем", "ваших", "вашу",
    # count/quantity question words — appear in "how many tracks do you have"
    "сколько", "много", "мало", "столько", "несколько", "хватает",
    "количество", "количества", "количестве", "число", "числе", "числа",
    "есть ли", "have", "how", "many", "much",
    # time/year context words — appear in "песни 1983 года"
    "год", "года", "году", "годов", "годом", "годах", "лет",
    # command words for search/selection (like "покажи", "выведи")
    "выбери", "найди", "поищи", "подбери", "отбери",
}

_RU_VOWELS = set("аеёиоуыэюя")
_RU_ENDINGS = set("аеёиоуыэюяйь")

# Russian→Latin transliteration for searching bands stored in Latin in DB
# (e.g. "НАутилуса" → stem "наутилус" → translit "nautilus" → matches "Nautilus Pompilius")
_RU_TRANSLIT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'j', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
    'ъ': '', 'ы': 'i', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
}

def _translit_ru(text: str) -> str:
    """Transliterate Russian Cyrillic to Latin. Used to search Latin-stored artist names."""
    result = []
    for c in text.lower():
        result.append(_RU_TRANSLIT.get(c, c))
    return ''.join(result)

def _is_cyrillic(text: str) -> bool:
    return any('Ѐ' <= c <= 'ӿ' for c in text)


def _stem_ru(term: str) -> list[str]:
    """Return term + stemmed variants by removing Russian inflection endings."""
    variants = [term]
    if len(term) < 4:
        return variants
    if term[-1] in _RU_ENDINGS:
        s1 = term[:-1]
        variants.append(s1)
        if len(s1) >= 4 and s1[-1] in _RU_ENDINGS:
            variants.append(s1[:-1])
    return variants


def _extract_year_range(query: str) -> tuple:
    """Extract (year_from, year_to) from query. Returns (None, None) if no years found."""
    import re
    # "1970-1974" or "1970–1974" or "1970—1974"
    m = re.search(r'\b((?:19|20)\d\d)\s*[–\-—]\s*((?:19|20)\d\d)\b', query)
    if m:
        return int(m.group(1)), int(m.group(2))
    # Single year: "за 1975 год" / "в 1975"
    m = re.search(r'\b((?:19|20)\d\d)\b', query)
    if m:
        y = int(m.group(1))
        return y, y
    return None, None


def _year_from_release_date(rd: str) -> int | None:
    """Extract 4-digit year from release_date string (handles '2015' and '23.04.2015 (2015)')."""
    import re
    if not rd:
        return None
    m = re.search(r'\b((?:19|20)\d\d)\b', rd)
    return int(m.group(1)) if m else None


_QUOTE_CHARS = ".,!?:;'\"()[]" + "".join(chr(c) for c in (
    0x00AB, 0x00BB, 0x201E, 0x201C, 0x201D, 0x2039, 0x203A, 0x2013, 0x2014
))
_LQ = chr(0x00AB)   # left guillemet
_RQ = chr(0x00BB)   # right guillemet
_LC = chr(0x201C)   # left curly double-quote
_RC = chr(0x201D)   # right curly double-quote


def _extract_quoted_strings(text: str) -> list[str]:
    """Extract text inside guillemets or double-quotes (explicit names)."""
    import re
    found = []
    for pat in (
        _LQ + r"([^" + _RQ + r"]+)" + _RQ,
        _LC + r"([^" + _RC + r"]+)" + _RC,
        r'"([^"]+)"',
    ):
        found.extend(re.findall(pat, text))
    return [s.strip().lower() for s in found if s.strip()]

def _extract_search_terms(text: str) -> list[str]:
    """Extract meaningful search terms, removing Russian/English stop words."""
    words = [w.strip(_QUOTE_CHARS).lower() for w in text.split()]
    return [w for w in words if len(w) > 1 and w not in _SEARCH_STOP_WORDS]


def search_contacts_by_labels(label_names: list[str]) -> str:
    """Search contacts by label name (owner_type). Returns formatted contacts or ''."""
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_KEY", "")
    if not base or not key or not label_names:
        return ""
    try:
        conditions = []
        for label in label_names[:5]:
            clean = label.replace("*", "").replace("(", "").replace(")", "").replace(",", "").replace("\x00", "").strip()
            # Split multi-word labels into per-word conditions — PostgREST OR filter
            # doesn't handle spaces in ilike values reliably ("ПМИ / ПЕРВОЕ МУЗЫКАЛЬНОЕ"
            # won't match "Первое музыкальное" as a single multi-word slug).
            for word in clean.split():
                w = word.strip("/–-")
                if len(w) >= 4:
                    conditions.append(f"owner_type.ilike.*{w}*")
        # deduplicate while preserving order
        seen: set[str] = set()
        uniq_conds = [c for c in conditions if not (c in seen or seen.add(c))]  # type: ignore[func-returns-value]
        if not uniq_conds:
            return ""
        or_filter = f"({','.join(uniq_conds[:20])})"
        resp = httpx.get(
            f"{base}/rest/v1/contacts",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            params={"or": or_filter, "limit": "15"},
            timeout=10,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return ""
        lines = ["[КОНТАКТЫ ПРАВООБЛАДАТЕЛЕЙ (заполни поля Правообладатель и Контакт из этих данных):"]
        for r in rows:
            first = r.get("first_name") or ""
            last = r.get("last_name") or ""
            name = f"{first} {last}".strip()
            owner = r.get("owner_type") or ""
            email = r.get("email") or ""
            role = r.get("adittional_info") or ""
            parts = [f"• {owner}"]
            if name:
                parts.append(f"— {name}")
            if role:
                parts.append(f"({role})")
            if email:
                parts.append(f"| {email}")
            lines.append(" ".join(parts))
        lines.append("]")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Contacts by label search error: {e}")
        return ""


def search_supabase_tracks(query: str) -> str:
    """Search tracks table by title/artist → extract label → look up label contacts.
    Returns combined track info + rights holder contacts or ''."""
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_KEY", "")
    if not base or not key:
        return ""
    try:
        # Priority 1: extract explicitly quoted strings «...» — these are exact names.
        # Quoted strings bypass noise filtering entirely: no false positives from
        # conversational words like "тебя", "каталоге" filling the DB limit.
        quoted_terms = _extract_quoted_strings(query)
        terms = quoted_terms
        if not terms:
            # Priority 2: noise-filtered word extraction
            words = [w.strip(_QUOTE_CHARS).lower() for w in query.split()]
            terms = [w for w in words if len(w) > 1 and w not in _TRACK_SEARCH_NOISE]
        if not terms:
            terms = _extract_search_terms(query) or [query.strip()]
        logger.info(f"Track search terms: {terms} (from: {query[:80]})")

        # Build ilike OR conditions. Add phrase condition first (all terms joined) so
        # "Агата Кристи" matches the exact artist field before per-word fallbacks.
        def _clean(s: str) -> str:
            # Strip PostgREST structural chars: comma splits OR conditions, dot separates
            # col.operator.value, parens wrap groups, * is ilike wildcard, null byte is unsafe.
            out = s.replace("(", "").replace(")", "").replace("'", "").replace("*", "")
            out = out.replace(",", "").replace(".", " ").replace("\x00", "")
            return out.strip()

        conditions = []
        clean_terms = [_clean(t) for t in terms[:8] if len(_clean(t)) >= 2]
        if not clean_terms:
            return ""

        # Year range extraction before building conditions so year tokens can be excluded
        # from text search (avoids "1983" matching in title/album/label fields).
        year_from, year_to = _extract_year_range(query) if not quoted_terms else (None, None)

        # Strip pure 4-digit year tokens from text search terms — handled via release_date filter
        non_year_terms = [t for t in clean_terms if not re.match(r'^\d{4}$', t)]
        year_only_search = year_from is not None and not non_year_terms
        # Use non_year_terms for text conditions when possible; fall back to all clean_terms
        # only if no year was detected (avoids "1983" matching random titles/albums)
        text_terms = non_year_terms if year_from is not None else clean_terms

        if year_only_search:
            # Year-only query (e.g. "песни 1983 года") — query release_date directly.
            # Searching year in text fields returns false matches (titles, album names, etc.)
            if year_from == year_to or year_to is None:
                year_conds = [f"release_date.ilike.*{year_from}*"]
            elif (year_to - year_from) <= 20:
                year_conds = [f"release_date.ilike.*{y}*" for y in range(year_from, year_to + 1)]
            else:
                # Wide range: build one condition per decade prefix
                decades = sorted({str(y)[:3] for y in range(year_from, year_to + 1)})
                year_conds = [f"release_date.ilike.*{d}*" for d in decades]
            or_filter = f"({','.join(year_conds)})"
            skip_year_postfilter = True  # DB already filtered — no Python pass needed
        else:
            skip_year_postfilter = False
            if not text_terms:
                return ""
            # Priority: full-phrase match on artist/title (catches "Агата Кристи" as one unit)
            if len(text_terms) > 1:
                phrase = " ".join(text_terms)
                conditions.append(f"artist.ilike.*{phrase}*")
                conditions.append(f"title.ilike.*{phrase}*")
                # Transliterated phrase for Latin-stored bands (e.g. "наутилус помпилиус")
                tphrase = _translit_ru(phrase)
                if tphrase != phrase:
                    conditions.append(f"artist.ilike.*{tphrase}*")

            # Per-term fallback. Label column only for non-quoted terms (quoted = explicit track
            # title, not label name — avoids «Мелодия» flooding with Фирма Мелодия tracks).
            search_label = not bool(quoted_terms)
            for t in text_terms:
                cols = ("title", "artist", "album", "label") if search_label else ("title", "artist", "album")
                for col in cols:
                    conditions.append(f"{col}.ilike.*{t}*")
                # Transliterated variants for Russian terms referencing Latin-stored artists
                # e.g. "наутилуса" → stem "наутилус" → translit "nautilus" → matches "Nautilus Pompilius"
                if _is_cyrillic(t):
                    for stem in _stem_ru(t):
                        tlit = _translit_ru(stem)
                        if len(tlit) >= 4 and tlit != stem:
                            conditions.append(f"artist.ilike.*{tlit}*")
                            conditions.append(f"title.ilike.*{tlit}*")
                            if search_label:
                                conditions.append(f"label.ilike.*{tlit}*")

            or_filter = f"({','.join(conditions)})"

        logger.info(f"Track ilike filter: {or_filter[:200]}")

        resp = httpx.get(
            f"{base}/rest/v1/tracks",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            params={
                "or": or_filter,
                "select": "title,artist,album,label,lyrics_author,music_author,link,release_date",
                "limit": "200",
            },
            timeout=10,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return ""

        # Post-filter by year range if detected in query (skipped for year-only searches
        # where release_date was already filtered at the DB level)
        if year_from is not None and not skip_year_postfilter:
            filtered = []
            for r in rows:
                y = _year_from_release_date(r.get("release_date") or "")
                if y is not None and year_from <= y <= (year_to or year_from):
                    filtered.append(r)
            if filtered:
                rows = filtered
            # If filter yields nothing (bad data), return all rows with a note
        year_note = f", период {year_from}–{year_to}" if year_from and year_to and year_from != year_to else (f", {year_from} г." if year_from else "")

        labels_found: set[str] = set()
        count_note = f" (показаны первые {len(rows)}, может быть больше)" if len(rows) >= 200 else f" (всего найдено: {len(rows)})"
        lines = [f"[ТРЕКИ ИЗ БАЗЫ SYNC LAB{count_note}{year_note}:"]
        # Large result sets (catalog view): compact format to keep context manageable.
        # Detailed fields (link, authors) only for small results where user needs them.
        detailed = len(rows) <= 30
        for r in rows:
            title = r.get("title") or ""
            artist = r.get("artist") or ""
            label = r.get("label") or ""
            release_date = r.get("release_date") or ""
            parts = [f"• «{title}»"]
            if artist:
                parts.append(f"— {artist}")
            if label:
                parts.append(f"| Лейбл: {label}")
                labels_found.add(label)
            yr = _year_from_release_date(release_date)
            if yr:
                parts.append(f"| Год: {yr}")
            if detailed:
                lyrics_author = r.get("lyrics_author") or ""
                music_author = r.get("music_author") or ""
                link = r.get("link") or ""
                if lyrics_author:
                    parts.append(f"| Автор текста: {lyrics_author}")
                if music_author:
                    parts.append(f"| Автор музыки: {music_author}")
                if link:
                    parts.append(f"| Ссылка: {link}")
            lines.append(" ".join(parts))
        lines.append("]")
        track_ctx = "\n".join(lines)

        # Auto-lookup contacts for found labels
        if labels_found:
            contacts_ctx = search_contacts_by_labels(list(labels_found))
            if contacts_ctx:
                return track_ctx + "\n\n" + contacts_ctx
        return track_ctx
    except Exception as e:
        logger.warning(f"Supabase tracks search error: {e}")
        return ""


def search_supabase_catalog(query: str) -> str:
    """Kowalski-specific catalog search: tracks + authors + labels + genre/tag fields.

    Extends base track search to include music_author, lyrics_author, genre_1 columns
    so Kowalski can answer "find tracks by composer X" or "tracks tagged jazz".
    """
    import httpx
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_KEY", "")
    if not base or not key:
        return ""
    try:
        quoted_terms = _extract_quoted_strings(query)
        terms = quoted_terms
        if not terms:
            words = [w.strip(_QUOTE_CHARS).lower() for w in query.split()]
            terms = [w for w in words if len(w) > 1 and w not in _TRACK_SEARCH_NOISE]
        if not terms:
            terms = _extract_search_terms(query) or [query.strip()]

        def _clean(s: str) -> str:
            out = s.replace("(", "").replace(")", "").replace("'", "").replace("*", "")
            out = out.replace(",", "").replace(".", " ").replace("\x00", "")
            return out.strip()

        clean_terms = [_clean(t) for t in terms[:8] if len(_clean(t)) >= 2]
        if not clean_terms:
            return ""

        year_from, year_to = _extract_year_range(query) if not quoted_terms else (None, None)
        non_year_terms = [t for t in clean_terms if not re.match(r'^\d{4}$', t)]
        text_terms = non_year_terms if year_from is not None else clean_terms

        conditions = []
        if text_terms:
            if len(text_terms) > 1:
                phrase = " ".join(text_terms)
                for col in ("artist", "title", "music_author", "lyrics_author"):
                    conditions.append(f"{col}.ilike.*{phrase}*")
                tphrase = _translit_ru(phrase)
                if tphrase != phrase:
                    conditions.append(f"artist.ilike.*{tphrase}*")

            all_cols = ("title", "artist", "album", "label", "music_author", "lyrics_author", "genre_1")
            for t in text_terms:
                for col in all_cols:
                    conditions.append(f"{col}.ilike.*{t}*")
                if _is_cyrillic(t):
                    for stem in _stem_ru(t):
                        tlit = _translit_ru(stem)
                        if len(tlit) >= 4 and tlit != stem:
                            conditions.append(f"artist.ilike.*{tlit}*")
                            conditions.append(f"music_author.ilike.*{tlit}*")

        if not conditions and year_from is None:
            return ""

        if year_from is not None and not conditions:
            if year_from == year_to or year_to is None:
                or_filter = f"(release_date.ilike.*{year_from}*)"
            else:
                year_conds = [f"release_date.ilike.*{y}*" for y in range(year_from, min(year_to + 1, year_from + 21))]
                or_filter = f"({','.join(year_conds)})"
        else:
            or_filter = f"({','.join(conditions)})"

        # COUNT query — get total matching rows without fetching all data
        count_resp = httpx.get(
            f"{base}/rest/v1/tracks",
            headers={"apikey": key, "Authorization": f"Bearer {key}", "Prefer": "count=exact"},
            params={"or": or_filter, "select": "id", "limit": "1"},
            timeout=15,
        )
        total_count = 0
        if count_resp.is_success:
            cr = count_resp.headers.get("Content-Range", "")
            m_cr = re.search(r'/(\d+)', cr)
            if m_cr:
                total_count = int(m_cr.group(1))

        resp = httpx.get(
            f"{base}/rest/v1/tracks",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            params={
                "or": or_filter,
                "select": "title,artist,album,label,lyrics_author,music_author,genre_1,link,release_date",
                "limit": "50",
            },
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return ""

        if year_from is not None:
            # Always apply Python year post-filter: fixes ilike false positives
            # e.g. release_date="2004-2005" matches *2005* but first year = 2004.
            # Also: if text-term search returned unrelated years, discard them.
            yt_s = year_to or year_from
            rows = [
                r for r in rows
                if (lambda y: y is not None and year_from <= y <= yt_s)(
                    _year_from_release_date(r.get("release_date") or "")
                )
            ]
            if not rows:
                return ""

        year_note = f", {year_from}–{year_to}" if year_from and year_to and year_from != year_to else (f", {year_from}" if year_from else "")
        display_rows = rows[:20]
        if total_count > len(display_rows):
            count_note = f" (всего в базе: {total_count} треков, показаны первые {len(display_rows)})"
        else:
            count_note = f" ({len(display_rows)} треков)"
        lines = [f"[КАТАЛОГ SYNC LAB{count_note}{year_note}:"]
        detailed = len(display_rows) <= 20
        for r in display_rows:
            parts = [f"• «{r.get('title') or ''}»", f"— {r.get('artist') or '?'}"]
            if r.get("label"):
                parts.append(f"| Лейбл: {r['label']}")
            yr = _year_from_release_date(r.get("release_date") or "")
            if yr:
                parts.append(f"| {yr}")
            if r.get("genre_1"):
                parts.append(f"| Жанр: {r['genre_1']}")
            if detailed:
                if r.get("music_author"):
                    parts.append(f"| Авт. музыки: {r['music_author']}")
                if r.get("lyrics_author"):
                    parts.append(f"| Авт. слов: {r['lyrics_author']}")
                if r.get("link"):
                    parts.append(f"| {r['link']}")
            lines.append(" ".join(parts))
        lines.append("]")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Catalog search error: {e}")
        return ""


def search_supabase_contacts(query: str) -> str:
    """Search Supabase contacts by individual terms (ilike per word). Returns formatted context or ''."""
    import httpx
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_KEY", "")
    if not base or not key:
        return ""
    try:
        terms = _extract_search_terms(query)
        if not terms:
            terms = [query.strip()]

        # Expand each term with Russian stem variants to handle inflection
        all_terms: list[str] = []
        for term in terms[:6]:
            all_terms.extend(_stem_ru(term))

        # Build OR filter: each term/variant searched across all relevant columns
        conditions = []
        for term in all_terms[:18]:
            t = term.replace("*", "").replace("(", "").replace(")", "").replace(",", "").replace(".", " ").replace("\x00", "").strip()
            for col in ("owner_type", "first_name", "last_name", "email", "adittional_info"):
                conditions.append(f"{col}.ilike.*{t}*")

        or_filter = f"({','.join(conditions)})"
        resp = httpx.get(
            f"{base}/rest/v1/contacts",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            params={"or": or_filter, "limit": "15"},
            timeout=10,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return ""

        lines = ["[КОНТАКТЫ ИЗ БАЗЫ ДАННЫХ SYNC LAB (используй эти данные, не придумывай):"]
        seen_owners: set[str] = set()
        for r in rows:
            first = r.get("first_name") or ""
            last = r.get("last_name") or ""
            name = f"{first} {last}".strip()
            owner = r.get("owner_type") or ""
            email = r.get("email") or ""
            role = r.get("adittional_info") or ""
            parts = [f"• {owner}"]
            if name:
                parts.append(f"— {name}")
            if role:
                parts.append(f"({role})")
            if email:
                parts.append(f"| {email}")
            lines.append(" ".join(parts))
            seen_owners.add(owner)
        lines.append("]")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Supabase search error: {e}")
        return ""


def search_asana_contacts(query: str) -> str:
    """Search Asana workspace tasks for rights-holder deal history. Returns formatted context or ''."""
    import httpx
    token = os.getenv("ASANA_TOKEN", "")
    workspace_id = os.getenv("ASANA_WORKSPACE_ID", "331121027676371")
    if not token:
        return ""
    try:
        terms = _extract_search_terms(query)
        asana_query = " ".join(terms) if terms else query.strip()
        resp = httpx.get(
            f"https://app.asana.com/api/1.0/workspaces/{workspace_id}/tasks/search",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "text": asana_query,
                "opt_fields": "name,notes,permalink_url",
                "limit": 5,
            },
            timeout=10,
        )
        resp.raise_for_status()
        tasks = resp.json().get("data", [])
        if not tasks:
            return ""
        lines = ["[ИСТОРИЯ СДЕЛОК ИЗ ASANA (используй эти данные):"]
        for t in tasks:
            lines.append(f"• {t.get('name', '—')}")
            if notes := (t.get("notes") or "").strip():
                lines.append(f"  {notes[:500]}")
        lines.append("]")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Asana search error: {e}")
        return ""


_INJECT_BLOCK_RE = re.compile(
    r'\[(?:ТРЕКИ ИЗ БАЗЫ[^\]]{0,120}|КОНТАКТЫ ИЗ БАЗЫ[^\]]{0,120}|'
    r'КОНТАКТЫ ПРАВООБЛАДАТЕЛЕЙ[^\]]{0,120}|ИСТОРИЯ СДЕЛОК[^\]]{0,120}):'
    r'[\s\S]*?\n\]',
    re.MULTILINE,
)

def _compress_old_message(msg: dict) -> dict:
    """Strip injected data blocks from user messages older than the last 2 turns.
    Keeps the actual user question; replaces data blocks with a short placeholder.
    Reduces token cost for long sessions without losing conversation continuity."""
    if msg["role"] != "user":
        return msg
    compressed = _INJECT_BLOCK_RE.sub("[данные из базы]", msg["content"])
    if compressed == msg["content"]:
        return msg
    return {**msg, "content": compressed}


def run_license_dialogue(chat_id: int, user_message: str) -> dict:
    """Direct Anthropic API call with conversation history for Рико."""
    import anthropic
    import datetime

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    today_prefix = f"[СЕГОДНЯ: {datetime.date.today().isoformat()}]\n"
    history = LICENSE_SESSIONS[chat_id]
    history.append({"role": "user", "content": today_prefix + user_message})

    # Compress injected data blocks in all messages except the last 4 (2 turns).
    # This keeps full context for the current work while cutting token cost of
    # older turns — Rico still sees the full conversation flow, just not stale
    # bulk track/contact data from previous searches.
    keep_full = 4
    if len(history) > keep_full:
        messages_to_send = (
            [_compress_old_message(m) for m in history[:-keep_full]]
            + history[-keep_full:]
        )
    else:
        messages_to_send = history

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=LICENSE_SYSTEM_PROMPT,
        messages=messages_to_send,
    )

    assistant_text = response.content[0].text
    history.append({"role": "assistant", "content": assistant_text})
    LICENSE_SESSIONS[chat_id] = history[-60:]  # 30 turns; compressed history is small

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


def save_to_asana(
    full_text: str,
    task_name: str = "",
    assignee: str = "denis@synclab.pro",
    due_on: str = "",
) -> str:
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
    if not task_name:
        task_name = lines[0].replace("ПРОЕКТ ЗАДАЧИ:", "").strip().strip("«»") if lines else "Запрос лицензии"
    notes = "\n".join(lines[1:]).strip() if len(lines) > 1 else full_text

    import datetime
    if not due_on:
        due_on = datetime.date.today().isoformat()

    data: dict = {
        "name": task_name,
        "notes": notes,
        "assignee": assignee or "denis@synclab.pro",
        "due_on": due_on,
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


def find_asana_task_by_name(name: str) -> list[dict]:
    """Search Asana tasks by name fragment. Returns list of {gid, name, due_on, assignee}."""
    token = os.getenv("ASANA_TOKEN", "")
    workspace_id = os.getenv("ASANA_WORKSPACE_ID", "331121027676371")
    if not token:
        return []
    try:
        resp = httpx.get(
            f"https://app.asana.com/api/1.0/workspaces/{workspace_id}/tasks/search",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "text": name,
                "completed": "false",
                "opt_fields": "gid,name,due_on,assignee.name",
                "limit": "10",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception:
        return []


def update_asana_task_due(task_gid: str, new_due: str) -> str:
    """Update due_on for an existing Asana task. new_due must be YYYY-MM-DD."""
    token = os.getenv("ASANA_TOKEN", "")
    if not token:
        return "⚠️ ASANA_TOKEN не настроен"
    try:
        resp = httpx.put(
            f"https://app.asana.com/api/1.0/tasks/{task_gid}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"data": {"due_on": new_due}},
            timeout=10,
        )
        resp.raise_for_status()
        task_name = resp.json()["data"].get("name", task_gid)
        link = f"https://app.asana.com/0/0/{task_gid}"
        return f"✅ Задача «{task_name}» перенесена на {new_due}\n{link}"
    except httpx.HTTPStatusError as e:
        return f"❌ Ошибка Asana {e.response.status_code}: {e.response.text[:300]}"
    except Exception as e:
        return f"❌ Ошибка: {e}"


# --- Morning briefing ---

_DAY_NAMES_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_DAY_NAMES_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
_MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def _briefing_date_bounds(date_range: str) -> tuple[str, str]:
    """Return (after_exclusive, before_exclusive) ISO date strings for Asana query.
    Empty string means no bound."""
    import datetime
    today = datetime.date.today()
    d = datetime.timedelta

    if date_range == "today":
        return ("", (today + d(days=1)).isoformat())
    if date_range == "tomorrow":
        return (today.isoformat(), (today + d(days=2)).isoformat())
    if date_range == "this_week":
        # from today through Sunday of current week
        end = today + d(days=(6 - today.weekday()) + 1)
        return ((today - d(days=1)).isoformat(), end.isoformat())
    if date_range == "next_week":
        days_to_monday = (7 - today.weekday()) % 7 or 7
        next_mon = today + d(days=days_to_monday)
        next_sun_plus1 = next_mon + d(days=7)
        return ((next_mon - d(days=1)).isoformat(), next_sun_plus1.isoformat())
    # fallback: today
    return ("", (today + d(days=1)).isoformat())


_asana_me_gid_cache: str | None = None
_asana_user_gid_cache: dict[str, str] = {}

# Known team emails — override with ASANA_EMAIL_ALEXANDRA / ASANA_EMAIL_EKATERINA in Railway
_TEAM_EMAILS = {
    "alexandra": "alexa.sp@yandex.ru",
    "ekaterina": "kate@synclab.pro",
}


def _get_asana_me_gid() -> str | None:
    global _asana_me_gid_cache
    if _asana_me_gid_cache:
        return _asana_me_gid_cache
    token = os.getenv("ASANA_TOKEN", "")
    if not token:
        return None
    try:
        resp = httpx.get(
            "https://app.asana.com/api/1.0/users/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        _asana_me_gid_cache = resp.json()["data"]["gid"]
        return _asana_me_gid_cache
    except Exception:
        return None


def _get_asana_user_gid(person_key: str) -> str | None:
    """Lookup Asana GID by email for a team member. Cached per process."""
    if person_key in _asana_user_gid_cache:
        return _asana_user_gid_cache[person_key]
    token = os.getenv("ASANA_TOKEN", "")
    workspace_id = os.getenv("ASANA_WORKSPACE_ID", "331121027676371")
    if not token:
        return None
    env_key = f"ASANA_EMAIL_{person_key.upper()}"
    email = os.getenv(env_key, _TEAM_EMAILS.get(person_key, "")).lower()
    if not email:
        return None
    try:
        resp = httpx.get(
            f"https://app.asana.com/api/1.0/workspaces/{workspace_id}/users",
            headers={"Authorization": f"Bearer {token}"},
            params={"opt_fields": "gid,name,email"},
            timeout=10,
        )
        resp.raise_for_status()
        for user in resp.json().get("data", []):
            if (user.get("email") or "").lower() == email:
                _asana_user_gid_cache[person_key] = user["gid"]
                logger.info(f"Asana GID for {person_key}: {user['gid']} ({user.get('name')})")
                return user["gid"]
    except Exception as e:
        logger.warning(f"Asana user GID lookup failed for {person_key}: {e}")
    return None


# Confirmed team GIDs (from /asana_debug). Override with ASANA_GID_ALEXANDRA / ASANA_GID_EKATERINA
_TEAM_GIDS = {
    "alexandra": "1201138547007400",  # Alexandra Guseva (alexa.sp@yandex.ru)
    "ekaterina": "911206717671832",   # Kate Timashova (kate@synclab.pro)
}


def fetch_asana_briefing(date_range: str = "today", filter_person: str | None = None) -> dict:
    """Fetch Asana tasks for given date_range via workspace search + assignee.any GID filter."""
    import datetime
    token = os.getenv("ASANA_TOKEN", "")
    workspace_id = os.getenv("ASANA_WORKSPACE_ID", "331121027676371")
    if not token:
        return {"error": "ASANA_TOKEN не настроен"}

    after, before = _briefing_date_bounds(date_range)

    try:
        headers = {"Authorization": f"Bearer {token}"}
        params: dict = {
            "opt_fields": "name,due_on,completed,assignee.name,permalink_url",
            "completed": "false",
            "limit": "100",
        }
        if before:
            params["due_on.before"] = before
        if after:
            params["due_on.after"] = after

        # Resolve GID for server-side assignee filter
        if filter_person == "me":
            gid = _get_asana_me_gid()
        elif filter_person in ("alexandra", "ekaterina"):
            env_key = f"ASANA_GID_{filter_person.upper()}"
            gid = os.getenv(env_key) or _TEAM_GIDS.get(filter_person) or _get_asana_user_gid(filter_person)
        else:
            gid = None

        if gid:
            params["assignee.any"] = gid
            logger.info(f"Briefing: assignee.any={gid} for filter_person={filter_person}")

        resp = httpx.get(
            f"https://app.asana.com/api/1.0/workspaces/{workspace_id}/tasks/search",
            headers=headers,
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        tasks = resp.json().get("data", [])
        logger.info(f"Briefing: {len(tasks)} tasks returned from workspace search (filter={filter_person}, gid={gid})")

        today_str = datetime.date.today().isoformat()

        if date_range == "today":
            today_tasks = [t for t in tasks if t.get("due_on") == today_str]
            overdue_tasks = [t for t in tasks if t.get("due_on") and t.get("due_on") < today_str]
            return {"today": today_tasks, "overdue": overdue_tasks, "date_range": date_range,
                    "filter_person": filter_person}
        else:
            return {"tasks": tasks, "date_range": date_range, "filter_person": filter_person}

    except Exception as e:
        return {"error": str(e)}


def parse_briefing_intent(text: str) -> dict:
    """Extract date_range and filter_person from natural language."""
    lower = text.lower()

    # Date range
    if any(w in lower for w in ("следующ", "будущ")) and "недел" in lower:
        date_range = "next_week"
    elif "недел" in lower:
        date_range = "this_week"
    elif "завтра" in lower:
        date_range = "tomorrow"
    else:
        date_range = "today"

    # Person filter — order matters: check specific names before "me"
    _kate_words = (
        "екатерин", "катер", "катя", "кати",          # RU
        "kate", "katya", "katy", "katie", "ekaterina", # EN
    )
    _alex_words = (
        "александр", "александр", "саш", "алекс", "саня",  # RU
        "alex", "sasha", "alexander", "alexandra", "alexa", "sash",  # EN
    )
    if any(w in lower for w in _kate_words):
        filter_person = "ekaterina"
    elif any(w in lower for w in _alex_words):
        filter_person = "alexandra"
    elif any(w in lower for w in ("мои", "моих", "у меня", "только мои", "мои задачи", "my", "mine", "дениса", "денис")):
        filter_person = "me"
    else:
        filter_person = None  # all people

    return {"date_range": date_range, "filter_person": filter_person}


def format_morning_briefing(data: dict, date_range: str = "today") -> str:
    import datetime
    from collections import defaultdict

    today = datetime.date.today()
    day_name = _DAY_NAMES_FULL[today.weekday()]
    date_str = f"{today.day} {_MONTHS_RU[today.month - 1]} {today.year}"

    if "error" in data:
        return f"☀️ Доброе утро!\n\n⚠️ Не удалось получить задачи из Asana: {data['error']}"

    dr = data.get("date_range", date_range)

    _RANGE_LABELS = {
        "today": f"☀️ Утренний брифинг — {day_name}, {date_str}",
        "tomorrow": "📅 Задачи на завтра",
        "this_week": "📅 Задачи на эту неделю",
        "next_week": "📅 Задачи на следующую неделю",
    }
    header = _RANGE_LABELS.get(dr, f"☀️ Брифинг — {date_str}")

    # --- Today view: split today / overdue, grouped by person ---
    if dr == "today":
        today_tasks = [t for t in data.get("today", []) if (t.get("assignee") or {}).get("name")]
        overdue_tasks = [t for t in data.get("overdue", []) if (t.get("assignee") or {}).get("name")]
        total = len(today_tasks) + len(overdue_tasks)

        lines = [f"{header}\n"]
        if total == 0:
            lines.append("✅ Задач на сегодня нет. Хороший день!")
            return "\n".join(lines)

        by_person: dict[str, dict] = defaultdict(lambda: {"today": [], "overdue": []})
        for t in today_tasks:
            person = (t.get("assignee") or {}).get("name", "") or "Без исполнителя"
            by_person[person]["today"].append(t["name"])
        for t in overdue_tasks:
            person = (t.get("assignee") or {}).get("name", "") or "Без исполнителя"
            due = t.get("due_on", "")
            label = f"{t['name']} (до {due})" if due else t["name"]
            by_person[person]["overdue"].append(label)

        for person, buckets in by_person.items():
            cnt = len(buckets["today"]) + len(buckets["overdue"])
            lines.append(f"👤 {person} — {cnt} задач")
            if buckets["today"]:
                lines.append("  📋 Сегодня:")
                for task in buckets["today"]:
                    lines.append(f"    • {task}")
            if buckets["overdue"]:
                lines.append("  🔴 Просрочено:")
                for task in buckets["overdue"]:
                    lines.append(f"    • {task}")
            lines.append("")

        lines.append(f"Всего активных: {total}")
        return "\n".join(lines)

    # --- Week / tomorrow view: grouped by person, date shown per task ---
    tasks = [t for t in data.get("tasks", []) if (t.get("assignee") or {}).get("name")]
    total = len(tasks)
    lines = [f"{header}\n"]

    if total == 0:
        lines.append("✅ Задач нет.")
        return "\n".join(lines)

    by_person2: dict[str, list] = defaultdict(list)
    for t in tasks:
        person = (t.get("assignee") or {}).get("name", "") or "Без исполнителя"
        due = t.get("due_on", "")
        due_label = ""
        if due:
            d = datetime.date.fromisoformat(due)
            due_label = f" [{_DAY_NAMES_RU[d.weekday()]} {d.day} {_MONTHS_RU[d.month-1]}]"
        by_person2[person].append(f"{t['name']}{due_label}")

    for person, task_list in by_person2.items():
        lines.append(f"👤 {person} — {len(task_list)} задач")
        for task in task_list:
            lines.append(f"  • {task}")
        lines.append("")

    lines.append(f"Всего: {total}")
    return "\n".join(lines)


async def morning_briefing_job(context) -> None:
    owner_id = os.getenv("TELEGRAM_OWNER_ID", "")
    if not owner_id:
        return
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, fetch_asana_briefing, "today", None)
    text = format_morning_briefing(data, "today")
    await context.bot.send_message(chat_id=int(owner_id), text=text)


# --- Agent routing ---

AGENT_KEYWORDS = {
    "license_manager": [
        "лицензи", "правообладател", "права", "синхронизац", "sync", "isrc",
        "iswc", "паблишер", "publisher", "рао", "mcps", "ascap", "bmi",
        "переговор", "трек", "каталог", "найди", "найти", "рико", "rico",
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
    "content_manager": [
        "метадан", "bpm", "каталог", "тег", "isrc", "iswc", "ddex", "cwr",
        "ковальски", "kowalski", "контент",
    ],
    "developer": [
        "база данных", "схема", "api", "интеграц", "supabase", "postgresql",
        "sql", "автоматиз", "разработ", "скрипт", "импорт",
    ],
}

SLASH_MAP = {
    "license": "license_manager",
    "lawyer": "lawyer",
    "accountant": "accountant",
    "bizdev": "biz_dev",
    "dev": "developer",
    "kowalski": "content_manager",
}

# --- Direct agent prompts (no CrewAI, direct Anthropic call) ---
# Prompts loaded from src/syncoteca/config/prompts/<name>.md
# Edit those files directly — no code change needed.

_PROMPT_KEYS = {
    "accountant": "marina",
    "lawyer": "ksusha",
    "biz_dev": "biz_dev",
    "content_manager": "kowalski",
    "developer": "developer",
}

DIRECT_PROMPTS: dict[str, str] = {
    agent: _load_prompt(fname)
    for agent, fname in _PROMPT_KEYS.items()
}

# Agents that use direct call (not CrewAI) in bot context
DIRECT_AGENTS = set(DIRECT_PROMPTS.keys())

# Accountant needs Sonnet for reliable financial arithmetic
_DIRECT_AGENT_MODELS: dict[str, str] = {
    "accountant": "claude-sonnet-4-6",
    "content_manager": "claude-sonnet-4-6",
}
_DEFAULT_DIRECT_MODEL = "claude-haiku-4-5-20251001"


_CATALOG_BLOCK_RE = re.compile(r'\[КАТАЛОГ SYNC LAB[^\n]*\n.*?\n\]', re.DOTALL)


def _strip_old_catalog_blocks(messages: list[dict]) -> list[dict]:
    """For content_manager: strip [КАТАЛОГ SYNC LAB...] from all but the last user message.

    Prevents multiple injected slices from confusing the LLM with different track counts.
    """
    result = []
    last_user_idx = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=-1)
    for i, msg in enumerate(messages):
        if msg["role"] == "user" and i < last_user_idx:
            clean = _CATALOG_BLOCK_RE.sub("", msg["content"]).strip()
            result.append({"role": "user", "content": clean or msg["content"]})
        else:
            result.append(msg)
    return result


def run_direct_agent(agent_name: str, chat_id: int, user_message: str) -> str:
    """Direct Anthropic API call without CrewAI overhead."""
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    system = DIRECT_PROMPTS.get(agent_name) or "Ты — помощник агентства Синкотека."
    history = DIRECT_SESSIONS[agent_name][chat_id]
    history.append({"role": "user", "content": user_message})
    model = _DIRECT_AGENT_MODELS.get(agent_name, _DEFAULT_DIRECT_MODEL)

    # Kowalski keeps a longer context window (30 turns)
    ctx_window = 60 if agent_name == "content_manager" else 16

    hist_slice = history[-ctx_window:]
    if agent_name == "content_manager":
        hist_slice = _strip_old_catalog_blocks(hist_slice)

    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=system,
        messages=hist_slice,
    )

    reply = response.content[0].text.strip()
    history.append({"role": "assistant", "content": reply})
    keep = 60 if agent_name == "content_manager" else 20
    DIRECT_SESSIONS[agent_name][chat_id] = history[-keep:]
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
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=_get_coordinator_prompt(),
        messages=history[-12:],
    )

    text = response.content[0].text.strip()
    history.append({"role": "assistant", "content": text})
    COORDINATOR_SESSIONS[chat_id] = history[-20:]

    try:
        clean = re.sub(r"```json|```", "", text).strip()
        # Haiku sometimes outputs multiple JSON objects; extract all, pick the best
        import re as _re
        objects = []
        depth = 0
        start = None
        for i, ch in enumerate(clean):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}" and depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        objects.append(json.loads(clean[start:i+1]))
                    except Exception:
                        pass
        if not objects:
            return {"action": "reply", "text": text}
        # Prefer action-bearing objects over plain replies
        priority = {"asana_task": 0, "calendar": 1, "search": 2, "route": 3, "reply": 4}
        objects.sort(key=lambda o: priority.get(o.get("action", "reply"), 5))
        return objects[0]
    except Exception:
        return {"action": "reply", "text": text}


# --- Teaching mode ---

def _resolve_memory_name(raw: str) -> str:
    """Map user input to canonical agent_memory name."""
    mapping = {
        "рико": "rico",
        "rico": "rico",
        "license": "rico",
        "license_manager": "rico",
        "ковальски": "kowalski",
        "kowalski": "kowalski",
        "content": "kowalski",
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
        "rico": "Рико",
        "kowalski": "Ковальски",
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
/license — Рико (лицензии, права)
/lawyer — Ксюша (договоры, юрист)
/accountant — Марина (роялти, бухгалтерия)
/bizdev — Директор по развитию
/dev — Разработчик
/stop — вернуться к координатору

*Обучение агентов:*
/know марина НДС с 2026: 22%
/teach рико — режим обучения (все сообщения → знания агента)
/teach_stop — выйти из режима обучения
/memory рико — показать знания агента
"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return await _deny(update)
    chat_id = update.effective_chat.id
    ACTIVE_AGENT.pop(chat_id, None)
    _clear_active_agent(chat_id)
    TEACH_SESSIONS.pop(chat_id, None)
    LICENSE_SESSIONS[chat_id] = []
    _PENDING_LABEL_NAME.discard(chat_id)
    _PENDING_LABEL_SCRAPE.pop(chat_id, None)
    _PENDING_EMAIL_EXPORT.pop(chat_id, None)
    _LAST_CATALOG_ENTITY.pop(chat_id, None)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _clear_pending_label_name, chat_id)
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
    _persist_active_agent(chat_id, agent_name)
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
    raw = args[0] if args else "rico"
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
    raw = args[0] if args else "rico"
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
            "Агенты: рико · ксюша · марина · ковальски · biz_dev · developer",
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
    thinking_msg = await update.message.reply_text("📋 Рико думает…")

    try:
        loop = asyncio.get_event_loop()

        # Search tracks→labels→contacts + contacts directly + Asana + web in parallel
        from syncoteca.tools.tavily_search_tool import TavilySearchTool
        _tavily = TavilySearchTool()
        tracks_ctx, sb_ctx, asana_ctx, web_ctx = await asyncio.gather(
            loop.run_in_executor(None, search_supabase_tracks, user_request),
            loop.run_in_executor(None, search_supabase_contacts, user_request),
            loop.run_in_executor(None, search_asana_contacts, user_request),
            loop.run_in_executor(None, _tavily._run, user_request),
        )
        if web_ctx:
            web_ctx = f"[ВЕБ-ПОИСК (используй для контекста о бренде/треке/исполнителе):\n{web_ctx}\n]"
        # tracks_ctx already includes label contacts — put first so Rico sees it first
        db_context = "\n\n".join(c for c in [tracks_ctx, sb_ctx, asana_ctx, web_ctx] if c)
        enriched = f"{db_context}\n\n{user_request}" if db_context else user_request

        result = await loop.run_in_executor(
            None, run_license_dialogue, chat_id, enriched
        )

        action = result.get("action", "continue_dialogue")
        reply_text = result.get("reply_text", "")

        _SEND_CHOICE = "\n\n—\nКуда отправить?\n📧 «в почту» — только email\n📋 «в асану» — только Asana\n📧📋 «в оба» — email + Asana"
        _MAX = 4000

        async def _edit_split(msg, text: str) -> None:
            """Edit msg with first 4000 chars, send overflow as new messages."""
            chunks = [text[i:i + _MAX] for i in range(0, len(text), _MAX)]
            await msg.edit_text(chunks[0])
            for chunk in chunks[1:]:
                await update.message.reply_text(chunk)

        if action == "save_to_asana":
            full_text = result.get("full_text", reply_text)
            subject = result.get("subject", "")
            assignee = result.get("assignee_email", "denis@synclab.pro")
            due_on = result.get("due_on", "")
            await thinking_msg.edit_text(f"📋 Сохраняю в Asana: {subject or 'задача'}…")
            asana_result = await loop.run_in_executor(
                None, save_to_asana, full_text, subject, assignee, due_on
            )
            await update.message.reply_text(asana_result)
        elif action == "send_email":
            to = result.get("to", "")
            subject = result.get("subject", "Запрос лицензии")
            body = result.get("body", reply_text)
            await thinking_msg.edit_text(f"📧 Отправляю письмо на {to}…")
            from syncoteca.tools.email_tool import EmailDraftTool
            mailer = EmailDraftTool()
            mail_result = await loop.run_in_executor(
                None, lambda: mailer._run(to=to, subject=subject, body=body, send=True)
            )
            await update.message.reply_text(mail_result)
        elif action == "send_both":
            to = result.get("to", "")
            subject = result.get("subject", "Запрос лицензии")
            body = result.get("body", reply_text)
            full_text = result.get("full_text", body)
            assignee = result.get("assignee_email", "denis@synclab.pro")
            due_on = result.get("due_on", "")
            await thinking_msg.edit_text(f"📧 Отправляю письмо на {to} и сохраняю в Asana…")
            from syncoteca.tools.email_tool import EmailDraftTool
            mailer = EmailDraftTool()
            mail_result = await loop.run_in_executor(
                None, lambda: mailer._run(to=to, subject=subject, body=body, send=True)
            )
            asana_result = await loop.run_in_executor(
                None, save_to_asana, full_text, subject, assignee, due_on
            )
            await update.message.reply_text(f"{mail_result}\n\n{asana_result}")
        elif action == "draft_ready":
            full = reply_text + _SEND_CHOICE
            if len(full) <= _MAX:
                await thinking_msg.edit_text(full)
            else:
                chunks = [reply_text[i:i + _MAX] for i in range(0, len(reply_text), _MAX)]
                await thinking_msg.edit_text(chunks[0])
                for chunk in chunks[1:]:
                    await update.message.reply_text(chunk)
                await update.message.reply_text(_SEND_CHOICE)
        else:
            await _edit_split(thinking_msg, reply_text)

    except Exception as e:
        logger.exception("License dialogue error")
        await thinking_msg.edit_text(f"Ошибка: {e}")


def _rika_catalog_redirect(text: str) -> bool:
    """Return True when Rico should hand off to Kowalski: catalog/repertoire list requests."""
    # Export/tool intents always go to Kowalski
    if _kowalski_detect_intent(text):
        return True
    lower = text.lower()
    # Catalog filter signals: looking for a SET of tracks, not a single rights lookup
    catalog_signals = (
        "все треки", "все песни", "все записи",
        "репертуар",
        "список треков", "список песен",
        "треки группы", "песни группы",
        "треки артиста", "песни артиста",
        "треки исполнителя", "песни исполнителя",
        "найди треки", "найди песни", "покажи треки", "покажи песни",
        "треки жанра", "песни жанра", "по жанру",
        "треки лейбла", "все треки лейбла",
        "по автору", "автора слов", "автора музыки", "автор слов", "автор музыки",
        "сколько треков у", "сколько песен у",
    )
    return any(sig in lower for sig in catalog_signals)


def _kowalski_detect_intent(text: str) -> str | None:
    """Detect catalog tool intent from natural language. Returns tool name or None."""
    lower = text.lower()
    if any(w in lower for w in (
        "выгрузи", "выгрузим", "выгрузите", "выгрузка", "выгружай", "выгрузить", "выгрузку", "выгружать",
        "весь каталог", "полный каталог", "всю базу",
        "экспорт", "экспортируй", "сделай excel", "сформируй excel",
        "excel по", "excel для", "скачать список", "дай список треков",
        "дай файл", "пришли файл", "скинь файл", "сделай файл",
        "полный список", "полную выгрузку", "сделай выгрузку",
        "сделай отчёт", "сделай отчет", "дай отчёт",
        "в табличку", "в таблицу", "табличку", "таблицу",
    )):
        return "export"
    # "делай/сделай/повтори репертуар [за X]" — export without explicit "выгрузи"
    import re as _re
    if _re.search(r'(?:делай|сделай|повтори|дай|покажи|сформируй)\s+(?:ещё\s+раз\s+)?репертуар', lower):
        return "export"
    if any(w in lower for w in (
        "проверь даты", "обнови даты", "discogs", "дата дискогс",
        "треки без даты", "проверить даты", "fix_dates", "fix dates",
        "по датам", "проверь по дат", "обнови по дат", "сверь дат",
        "проверка дат", "уточни дат", "дата релиз",
        "даты релизов", "даты релиза", "дату релиза",
        "проверь на дат", "сверь на дат", "проверить на дат",
        "корректност", "корректные дат", "проверь корректн",
        "проверь релизы", "сверь релизы", "верифицируй релизы",
        "проверь все релизы", "сверь все релизы", "верифицируй все релизы",
        "релизы артиста", "релизы лейбла", "релизы исполнител",
    )):
        return "fix_dates"
    if any(w in lower for w in (
        "аномали", "неполные метаданн", "проверь каталог", "аудит каталог",
        "треки без автора", "треки без исполнитель", "check_catalog",
        "нет автора", "нет исполнитель", "нет лейбла", "пустые поля",
    )):
        return "check_catalog"
    if any(w in lower for w in (
        "export_anomalies", "выгрузи аномали", "excel аномали", "список аномали",
    )):
        return "export_anomalies"
    if "последн" in lower and any(w in lower for w in ("трек", "добав", "загруз", "по id")):
        return "recent"
    if any(w in lower for w in (
        "спарси лейбл", "парсинг лейбла", "загрузи каталог лейбла", "парсинг каталога",
        "скрапинг лейбла", "скрапить лейбл", "скрапить каталог",
        "загрузи лейбл", "скачай лейбл", "скачай каталог лейбла",
        "спарсить лейбл", "спарсить каталог", "запусти парсинг",
    )):
        return "scrape_label"
    if any(w in lower for w in (
        "стоп парсинг", "остановить парсинг", "останови парсинг", "отмена парсинга",
        "прерви парсинг", "отменить парсинг", "стоп скрапинг", "stop parsing",
        "стоп скрапер", "остановить скрапинг",
    )):
        return "stop_scrape"
    if any(w in lower for w in (
        "скип", "скипни", "пропусти", "пропустить", "skip album", "пропусти альбом",
        "пропустить альбом", "не нужен этот", "не нужно это",
    )):
        return "skip_album"
    # Status queries about enrichment must NOT trigger a new enrich run
    if re.search(r'\b(закончил|завершил|уже\s+закончил|ты\s+закончил|готово)\b', lower) and \
       any(w in lower for w in ("обогащени", "обогат", "обогащ", "enrich")):
        return None
    if any(w in lower for w in (
        "обнови базу", "обогати треки", "обогати базу", "заполни пустые", "пустые треки",
        "обработай треки", "треки без данных", "запусти обогащение", "обновить базу",
        "обогащение треков", "enrich", "заполни метаданные", "обнови метаданные",
        "yandex music обогащение", "обогати через яндекс", "обогати через yandex",
        "заполни пустые поля", "треки без метаданных",
        # Short stems — resilient to voice typos ("обоготи", "обогатить" etc.)
        "обогат", "обогащ", "обогот",
    )):
        return "enrich"
    return None


_ARTIST_FILLER = {
    "да", "нет", "всё", "все", "всех", "отлично", "хорошо", "ладно", "пожалуйста",
    "полный", "полное", "полностью", "список", "списке", "по", "и", "а", "но",
    "excel", "xlsx", "файл", "отчёт", "отчет",
    "выгрузку", "выгрузи", "выгрузим", "выгрузите", "выгружаем", "выгружаете", "выгружать",
    "дай", "скинь", "покажи", "мне", "нам", "тебе", "там",
    "давай", "давайте", "конечно", "окей", "ок", "угу", "ага",
    "хочу", "хочешь", "нужно", "нужен", "нужны", "можешь", "можно",
    "посмотри", "покажи", "проверь", "скажи", "напомни",
    # pronouns and database-context words that appear in search queries
    "тебя", "тебе", "тобой", "твои", "твой", "твоя", "твоей",
    "вашей", "вашем", "ваших", "вашу", "наш", "наша", "наше", "наши",
    "базе", "базу", "базы", "базой", "базе",
    "каталоге", "каталогу", "каталог", "каталога",
    "реестре", "реестра", "реестру",
    "есть", "ли", "у", "нас", "что", "где", "когда",
    "какой", "какая", "какое", "какие", "каких",
    "сколько", "много", "мало",
    "инфу", "информацию", "информации", "информация",
    "данные", "данных", "данным", "данного",
    "подбери", "подобрать",
    "песни", "песня", "песню",
    "знаешь", "знаете", "имеется", "имеются", "числится",
    # personal pronouns
    "ты", "вы", "он", "она", "они", "я", "мы",
    # demonstrative pronouns ("по этой группе", "по той группе")
    "этот", "эта", "это", "эти", "этой", "этого", "этому", "этим", "этих", "эту",
    "тот", "та", "то", "те", "той", "того", "тому", "тем", "тех", "ту",
    "данный", "данная", "данное", "данной", "данного", "данному", "данных",
    # noun forms that trail "по этой группе"
    "группе", "группу", "группой",
    # vision/perception verbs
    "видишь", "видите", "вижу", "видит", "видно",
    "посмотрю", "посмотри", "посмотрим", "посмотрите", "посмотреть",
    "взгляну", "взгляни", "гляну", "загляну",
    "пришли", "пришлю",
    "сделай", "сделаю", "сделал", "сделает",
    # catalog context
    "реестре", "реестра", "реестру", "нашем", "нашей",
    # pronoun oblique cases ("что есть по ней")
    "ней", "нём", "ним", "ними", "них",
    "нам", "нами", "вам", "вами", "вас",
    "им", "ими", "их",
    "ему", "её", "его",
}


def _export_filters_are_clean(filters: dict) -> bool:
    """Return True if parsed filters look like a genuine artist/year/label (not conversational noise)."""
    if filters.get("year_from") or filters.get("label") or filters.get("genre") or filters.get("date_added") or filters.get("release_day") or filters.get("music_authors"):
        return True
    artist = filters.get("artist", "")
    if not artist:
        return False
    words = [w.lower().strip(".,!?-") for w in artist.split() if w.strip()]
    if not words or len(words) > 5:
        return False
    filler_count = sum(1 for w in words if w in _ARTIST_FILLER)
    return filler_count == 0


def _kowalski_resolve_export_query(text: str, chat_id: int) -> str:
    """Resolve what to export: use text if it has a clean artist/year/label, else look in history."""
    from syncoteca.tools.catalog_export import parse_export_query
    filters = parse_export_query(text)
    if _export_filters_are_clean(filters):
        return text  # text already has clean signal

    # Long messages with no entity = new unrelated request (e.g. "из чарта выгрузку")
    # Don't pull stale entity from history — let it fail gracefully.
    if len(text.split()) > 10:
        return text

    # Fast path: use the last catalog lookup query (set when catalog_ctx was non-empty).
    # Stored as parsed filters dict to avoid re-parsing noise like "инфу ASTI".
    cached = _LAST_CATALOG_ENTITY.get(chat_id)
    if cached:
        if isinstance(cached, dict) and _export_filters_are_clean(cached):
            # Reconstruct a clean minimal query from the stored filters
            artist = cached.get("artist")
            if artist:
                logger.info(f"Export subject from _LAST_CATALOG_ENTITY dict: {artist!r}")
                return artist
            label = cached.get("label")
            if label:
                return f"лейбл {label}"
            year_from = cached.get("year_from")
            if year_from:
                year_to = cached.get("year_to", year_from)
                if year_to and year_to != year_from:
                    return f"{year_from}-{year_to}"
                return str(year_from)
            date_added = cached.get("date_added")
            if date_added:
                return date_added  # ISO YYYY-MM-DD, re-parsed by parse_export_query
            release_day = cached.get("release_day")
            if release_day:
                return f"релиз {release_day}"
        elif isinstance(cached, str):
            cached_filters = parse_export_query(cached)
            if _export_filters_are_clean(cached_filters):
                logger.info(f"Export subject from _LAST_CATALOG_ENTITY str: {cached!r}")
                return cached

    # Short affirmation ("да, выгружай") — scan session history for last mentioned entity
    history = DIRECT_SESSIONS["content_manager"].get(chat_id, [])
    for msg in reversed(history[-30:]):
        content = msg.get("content", "")
        role = msg.get("role", "")
        # Strip injected catalog blocks and assistant formatting
        clean = re.sub(r'\[КАТАЛОГ SYNC LAB.*?\]', '', content, flags=re.DOTALL)
        clean = re.sub(r'\[Выгрузка:.*?\]', '', clean, flags=re.DOTALL).strip()
        if not clean:
            continue
        # For assistant messages: extract entity mentions like "треков Любэ", "репертуар X"
        if role == "assistant":
            # Look for "N треков X" / "репертуар X" / "треки X" patterns in assistant text
            m = re.search(r'треков?\s+([\w\s.,-]+?)(?:\s*[|\n(]|\s*$)', clean, re.IGNORECASE)
            if m:
                candidate_text = m.group(1).strip()
                cand = parse_export_query(candidate_text)
                if _export_filters_are_clean(cand):
                    logger.info(f"Export subject from assistant msg: '{candidate_text}'")
                    return candidate_text
            continue
        # User messages: strip and parse
        candidate = parse_export_query(clean)
        if _export_filters_are_clean(candidate):
            logger.info(f"Export subject from user history: {candidate}")
            return clean
    return text  # last resort


def _build_label_scrape_prompt(
    label_id: str,
    label_name: str,
    analysis: Optional[dict],
    sublabels: list,
) -> tuple[str, dict]:
    """Build confirmation message + pending dict for label scrape."""
    album_info = ""
    if analysis:
        total = analysis.get("album_count", 0)
        eta_s = analysis.get("eta_seconds", 0)
        eta_h = eta_s // 3600
        eta_m = (eta_s % 3600) // 60
        eta_str = f"~{eta_h}ч {eta_m}мин" if eta_h else f"~{eta_m} мин"
        album_info = f" — {total} альбомов (~{eta_str})"

    if sublabels:
        sub_names = [s["name"] for s in sublabels]
        shown = sub_names[:10]
        more = len(sub_names) - 10
        subs_preview = ", ".join(shown) + (f" ...и ещё {more}" if more > 0 else "")
        msg = (
            f"🗃️ Ковальски: лейбл «{label_name}»{album_info}\n\n"
            f"📂 У него {len(sublabels)} саблейблов:\n{subs_preview}\n\n"
            f"Что парсить?\n"
            f"1️⃣ Только «{label_name}»\n"
            f"2️⃣ «{label_name}» + все {len(sublabels)} саблейблов\n"
            f"3️⃣ Только саблейблы (без основного)\n"
            f"Или назови конкретный саблейбл"
        )
        pending = {"stage": "sublabel_choice", "label_id": label_id, "label_name": label_name,
                   "analysis": analysis, "sublabels": sublabels}
    else:
        msg = (
            f"🗃️ Ковальски: лейбл «{label_name}»{album_info}\n\n"
            f"Запускать парсинг? (да / нет)"
        )
        pending = {"stage": "confirm", "label_id": label_id, "label_name": label_name,
                   "analysis": analysis, "sublabels": []}
    return msg, pending


async def _send_excel_by_email(update: Update, query: str, subject: str, to_override: Optional[str] = None) -> None:
    """Re-fetch Excel from Supabase and send via Resend API (primary) or SMTP (fallback)."""
    import base64
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email.mime.text import MIMEText
    from email import encoders

    owner_email = to_override or os.getenv("OWNER_EMAIL", "denis@synclab.pro")
    resend_key = os.getenv("RESEND_API_KEY", "")
    smtp_host = os.getenv("EMAIL_SMTP_HOST", "")
    smtp_user = os.getenv("EMAIL_SMTP_USER", "")
    smtp_pass = os.getenv("EMAIL_SMTP_PASS", "")
    smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "587"))

    if not resend_key and not (smtp_host and smtp_user and smtp_pass):
        await update.message.reply_text("❌ Ни RESEND_API_KEY, ни SMTP не настроены в Railway env.")
        return

    thinking = await update.message.reply_text("📧 Формирую файл для отправки…")
    try:
        from syncoteca.tools.catalog_export import export_catalog
        loop = asyncio.get_event_loop()
        xlsx_bytes, filename, count, _ = await loop.run_in_executor(None, export_catalog, query)

        email_subject = f"Синкотека — {subject} ({count} треков)"
        email_body = f"Выгрузка каталога: {subject}\n{count} треков."

        if resend_key:
            def _resend_send():
                import httpx as _httpx
                return _httpx.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
                    json={
                        "from": smtp_user or "noreply@synclab.pro",
                        "to": [owner_email],
                        "subject": email_subject,
                        "text": email_body,
                        "attachments": [{"filename": filename, "content": base64.b64encode(xlsx_bytes).decode()}],
                    },
                    timeout=30,
                )
            r = await loop.run_in_executor(None, _resend_send)
            r.raise_for_status()
        else:
            msg = MIMEMultipart()
            msg["From"] = smtp_user
            msg["To"] = owner_email
            msg["Subject"] = email_subject
            msg.attach(MIMEText(email_body, "plain", "utf-8"))
            part = MIMEBase("application", "octet-stream")
            part.set_payload(xlsx_bytes)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
            msg.attach(part)

            def _smtp_send():
                with smtplib.SMTP(smtp_host, smtp_port) as s:
                    s.starttls()
                    s.login(smtp_user, smtp_pass)
                    s.sendmail(smtp_user, owner_email, msg.as_string())

            await loop.run_in_executor(None, _smtp_send)

        await thinking.edit_text(f"✅ Отправлено на {owner_email} ({count} треков).")
    except Exception as e:
        await thinking.edit_text(f"❌ Ошибка отправки письма: {e}")


async def _run_kowalski_tool(update: Update, intent: str, text: str) -> None:
    """Execute a Kowalski catalog tool triggered by natural language."""
    import io
    loop = asyncio.get_event_loop()
    chat_id = update.effective_chat.id

    if intent == "export":
        thinking = await update.message.reply_text("🗃️ Ковальски: формирую Excel…")
        try:
            from syncoteca.tools.catalog_export import export_catalog, parse_export_query, build_export_caption
            export_query = _kowalski_resolve_export_query(text, chat_id)
            logger.info(f"Kowalski export query resolved: {export_query!r}")
            xlsx_bytes, filename, count, tracks = await loop.run_in_executor(None, export_catalog, export_query)

            filters = parse_export_query(export_query)
            yf = filters.get("year_from")
            yt = filters.get("year_to", yf)
            year_subject = f"{yf}-{yt}" if yf and yt and yf != yt else (str(yf) if yf else None)
            _da = filters.get("date_added")
            _rd = filters.get("release_day")
            date_subject = (f"добавлено {_da}" if _da else None) or (f"релиз {_rd}" if _rd else None)
            subject = filters.get("artist") or filters.get("label") or year_subject or date_subject or export_query[:40]
            history = DIRECT_SESSIONS["content_manager"][chat_id]
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": f"[Выгрузка: {subject} — {count} треков → {filename}]"})
            DIRECT_SESSIONS["content_manager"][chat_id] = history[-60:]

            _LAST_CATALOG_ENTITY.pop(chat_id, None)

            if count == 0:
                await thinking.edit_text(
                    f"🗃️ Ковальски: по запросу «{subject}» треков не найдено.\n"
                    "Уточни: название исполнителя, год или лейбл."
                )
                return
            caption = build_export_caption(tracks, subject)
            if count > 3000:
                await thinking.edit_text(f"🗃️ Ковальски: найдено {count} треков, генерирую файл…")
            await thinking.delete()
            try:
                await update.message.reply_document(
                    document=io.BytesIO(xlsx_bytes),
                    filename=filename,
                    caption=caption,
                    read_timeout=60,
                    write_timeout=60,
                )
            except Exception as tg_err:
                logger.warning(f"Telegram file upload timeout (file likely delivered): {tg_err}")
            # Offer email after sending — store query (not bytes) to survive redeploy
            owner_email = os.getenv("OWNER_EMAIL", "denis@synclab.pro")
            _PENDING_EMAIL_EXPORT[chat_id] = {"query": export_query, "subject": subject}
            await update.message.reply_text(f"📧 Отправить на {owner_email}?")
        except Exception as e:
            await thinking.edit_text(f"❌ Ошибка экспорта: {e}")

    elif intent == "recent":
        m = re.search(r'\b(\d{1,3})\b', text)
        limit = min(int(m.group(1)), 200) if m else 50
        thinking = await update.message.reply_text(f"🗃️ Ковальски: ищу последние {limit} треков…")
        try:
            from syncoteca.tools.catalog_export import fetch_recent_tracks, build_excel, build_export_caption
            tracks = await loop.run_in_executor(None, fetch_recent_tracks, limit)
            if not tracks:
                await thinking.edit_text("🗃️ Ковальски: треков не найдено.")
                return
            lines = [f"🗃️ Последние {len(tracks)} треков (по ID):"]
            for i, t in enumerate(tracks[:15], 1):
                a = t.get("artist") or "?"
                tit = t.get("title") or "?"
                lines.append(f"{i}. {a} — {tit}")
            if len(tracks) > 15:
                lines.append(f"…ещё {len(tracks) - 15} в Excel.")
            await thinking.edit_text("\n".join(lines))
            xlsx = await loop.run_in_executor(None, build_excel, tracks, f"Последние {len(tracks)} треков")
            caption = build_export_caption(tracks, f"Последние {len(tracks)} треков")
            await update.message.reply_document(
                document=io.BytesIO(xlsx),
                filename=f"SYNCLAB_recent_{len(tracks)}.xlsx",
                caption=caption,
                read_timeout=60,
                write_timeout=60,
            )
        except Exception as e:
            await thinking.edit_text(f"❌ Ошибка: {e}")

    elif intent == "fix_dates":
        import re as _re
        from datetime import date as _date, timedelta as _td
        _tl = text.lower()

        # Label filter: "по лейблу X" / "лейбла X" / "релизы лейбла X"
        _label_fix: Optional[str] = None
        _lm = re.search(
            r"(?:по\s+лейблу?|лейбл(?:а|е|у|ом|ях|ам)?\s+|релизы\s+лейбл\w*\s+)[«\"]?([\w\s\-\.]+?)[»\"]?"
            r"(?=\s*(?:[.,!?]|$|\b(?:и|с|по|за|от|до|треки|даты)\b))",
            text, re.IGNORECASE,
        )
        if _lm:
            _label_fix = _lm.group(1).strip()

        # Artist filter: "по артисту X" / "репертуар X" / "релизы X" / "по датам релиза X"
        _artist_fix: Optional[str] = None
        if not _label_fix:
            _am = re.search(
                r"(?:по\s+артисту?|по\s+исполнител[юя]|по\s+групп\w+|по\s+коллектив\w*|артист(?:а|у|ом|е)?\s+|исполнител[яю]\s+|групп[ауе]?\s+|репертуар[еу]?\s+|релизы\s+(?!лейбл)|по\s+дат(?:ам?|е|ах|ами)?\s+релиз\w*\s+)[«\"]?([\w\s\-\.]+?)[»\"]?"
                r"(?=\s*(?:[.,!?]|$|\b(?:и|с|по|за|от|до|треки|даты|лейбл)\b))",
                text, re.IGNORECASE,
            )
            if _am:
                _artist_fix = _am.group(1).strip()
            else:
                # Fallback 1: "по датам Name" — bare name after date trigger (skip "релиза/релизов/etc")
                _dm_artist = re.search(r"по\s+дат(?:ам?|е|ах|ами)?\s+(?:релиз\w*\s+)?([\w\s\-\.]{2,40})", text, re.IGNORECASE)
                if _dm_artist:
                    _artist_fix = _dm_artist.group(1).strip().rstrip(".,!?")
                else:
                    # Fallback 2: quoted name in message
                    _qm = re.search(r'[«"\']([\w\s\-\.]{2,40})[»"\']', text)
                    if _qm:
                        _artist_fix = _qm.group(1).strip()

        # Date filter: "за сегодня" / "за вчера" / "за YYYY-MM-DD"
        _date_fix: Optional[str] = None
        if "сегодня" in _tl or "today" in _tl:
            _date_fix = _date.today().isoformat()
        elif "вчера" in _tl or "yesterday" in _tl:
            _date_fix = (_date.today() - _td(days=1)).isoformat()
        else:
            _dm = _re.search(r'\b(20\d{2}-\d{2}-\d{2})\b', text)
            if _dm:
                _date_fix = _dm.group(1)

        _has_filter = _label_fix or _artist_fix or _date_fix
        m = _re.search(r'\b(\d{1,4})\b', text)
        limit = min(int(m.group(1)), 50000) if m else (5000 if _has_filter else 5000)
        only_null = "all" not in _tl and "все" not in _tl and "корректн" not in _tl and "последн" not in _tl and not _has_filter

        from syncoteca.tools.date_fixer import run_date_fix
        asyncio.create_task(run_date_fix(
            chat_id, update.get_bot(), limit=limit, only_null=only_null,
            label=_label_fix, artist=_artist_fix, date_from=_date_fix,
        ))
        if _artist_fix:
            _scope_desc = f"артист «{_artist_fix}»"
        elif _label_fix:
            _scope_desc = f"лейбл «{_label_fix}»"
        elif _date_fix:
            _scope_desc = "сегодняшние треки"
        else:
            _scope_desc = "только без даты" if only_null else "все треки"
        reply = (
            f"🗃️ Ковальски: запускаю проверку дат Discogs\n"
            f"Область: {_scope_desc} | Лимит: {limit}\n"
            f"Займёт ~{limit // 60 + 1} мин."
        )
        await update.message.reply_text(reply)
        history = DIRECT_SESSIONS["content_manager"][chat_id]
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": reply})
        DIRECT_SESSIONS["content_manager"][chat_id] = history[-60:]

    elif intent == "check_catalog":
        thinking = await update.message.reply_text("🔍 Ковальски: сканирую каталог на аномалии…")
        try:
            from syncoteca.tools.catalog_audit import run_audit
            _, report = await loop.run_in_executor(None, run_audit)
            await thinking.edit_text(report)
        except Exception as e:
            await thinking.edit_text(f"❌ Ошибка аудита: {e}")

    elif intent == "export_anomalies":
        thinking = await update.message.reply_text("🔍 Ковальски: формирую Excel с аномалиями…")
        try:
            from syncoteca.tools.catalog_audit import fetch_anomalies, export_anomalies_excel
            tracks = await loop.run_in_executor(None, fetch_anomalies)
            if not tracks:
                await thinking.edit_text("✅ Ковальски: аномалий нет.")
                return
            xlsx_bytes = await loop.run_in_executor(None, export_anomalies_excel, tracks)
            await thinking.edit_text(f"🔍 Найдено {len(tracks)} аномалий, отправляю…")
            await update.message.reply_document(
                document=io.BytesIO(xlsx_bytes),
                filename="SYNCLAB_anomalies.xlsx",
                caption=f"🔍 SYNC LAB — {len(tracks)} треков с неполными метаданными",
            )
            await thinking.delete()
        except Exception as e:
            await thinking.edit_text(f"❌ Ошибка: {e}")

    elif intent == "enrich":
        import re as _re
        # Artist filter — multiple patterns:
        # 1. "обогати/обогащением/обогащение... [артиста] X"
        # 2. "по артисту/исполнителю [- ] X"
        # 3. Quoted name «X» / "X" / 'X' (including Unicode curved quotes)
        _enrich_artist: Optional[str] = None
        _name_re = r"([\w\s\-\.]{2,40}?)"
        _stop = r"(?=\s*(?:[.,!?]|$|\b(?:и|с|по|за|от|до|треки|базу|через|яндекс)\b))"

        for _pat in (
            # "обогати/обогащение/обогащением [артиста/по артисту] X"
            r"(?:обогати|обогатить|обогащени\w*)\s+(?:(?:по\s+)?(?:артист\w+|исполнител\w+|групп\w+|коллектив\w*)\s*[-—]?\s*)?" + _name_re + _stop,
            # "займись обогащением по Артисту X" / "по артисту - X"
            r"(?:по\s+)?(?:артист\w+|исполнител\w+|групп\w+|коллектив\w*)\s*[-—]?\s*" + _name_re + _stop,
        ):
            _ea = re.search(_pat, text, re.IGNORECASE)
            if _ea:
                _cand = _ea.group(1).strip().strip("-—").strip()
                # Reject if it's a generic word
                if _cand.lower() not in {"информации", "информацию", "данных", "данные", "треков", "базы"}:
                    _enrich_artist = _cand
                    break

        if not _enrich_artist:
            # Quoted name fallback -- use chr() to avoid Unicode literals in source
            _oq = chr(0x00AB) + chr(0x201C) + chr(0x2018) + chr(0x22) + chr(0x27)
            _cq = chr(0x00BB) + chr(0x201D) + chr(0x2019) + chr(0x22) + chr(0x27)
            _qpat = chr(0x5B) + _oq + chr(0x5D) + r"([\w\s\-\.]{2,40})" + chr(0x5B) + _cq + chr(0x5D)
            _qea = re.search(_qpat, text)
            if _qea:
                _enrich_artist = _qea.group(1).strip()

        m = _re.search(r'\b(\d{1,4})\b', text)
        limit = min(int(m.group(1)), 2000) if m else (2000 if _enrich_artist else 1000)
        thinking = await update.message.reply_text("🗃️ Ковальски: пошёл посмотрю что есть для работы…")
        try:
            from syncoteca.tools.yandex_enricher import count_empty_tracks
            total_pending = await loop.run_in_executor(None, count_empty_tracks)
        except Exception:
            total_pending = "?"
        if str(total_pending) == "0" and not _enrich_artist:
            reply = "🗃️ Ковальски: а у нас всё в базе хорошо — пустых треков нет."
            await thinking.edit_text(reply)
            history = DIRECT_SESSIONS["content_manager"][chat_id]
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": reply})
            DIRECT_SESSIONS["content_manager"][chat_id] = history[-60:]
            return
        scope_note = f" «{_enrich_artist}»" if _enrich_artist else ""
        _tp = int(total_pending) if str(total_pending).isdigit() else 0
        eta = f"~{_tp * 4 // 60} мин" if _tp > 0 else "несколько минут"
        reply = (
            f"🗃️ Ковальски: запускаю обогащение{scope_note}.\n"
            f"Займёт {eta}. Иду работать."
        )
        await thinking.edit_text(reply)
        history = DIRECT_SESSIONS["content_manager"][chat_id]
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": reply})
        DIRECT_SESSIONS["content_manager"][chat_id] = history[-60:]
        asyncio.create_task(_run_enrich_task(chat_id, update.get_bot(), limit, artist=_enrich_artist))

    elif intent == "scrape_label":
        # Extract label name from text — strip trigger phrases
        label_query = re.sub(
            r'(спарси\s+лейбл|парсинг\s+лейбла?|загрузи\s+каталог\s+лейбла?|загрузи\s+лейбл|'
            r'скачай\s+каталог\s+лейбла?|скачай\s+лейбл|спарсить\s+лейбл|спарсить\s+каталог|'
            r'скрапинг\s+лейбла?|скрапить\s+(лейбл|каталог)|запусти\s+парсинг\s+(лейбла?|каталога)?)',
            '', text, flags=re.IGNORECASE,
        ).strip().strip('.,!?')
        if not label_query:
            await update.message.reply_text("🗃️ Ковальски: укажи название лейбла. Например: «спарси лейбл Sony Music»")
            return
        thinking = await update.message.reply_text(f"🗃️ Ковальски: ищу лейбл «{label_query}» в базе…")
        try:
            from syncoteca.tools.label_scraper import find_label_in_db, analyze_label, find_sublabels, is_running
            if is_running():
                await thinking.edit_text("⚠️ Ковальски: парсинг уже запущен. Сначала останови: «стоп парсинг».")
                return
            found = await loop.run_in_executor(None, find_label_in_db, label_query)
            if not found:
                await thinking.edit_text(
                    f"🗃️ Ковальски: лейбл «{label_query}» не найден в таблице labels.\n"
                    f"Проверь название или добавь лейбл в базу."
                )
                return
            label_id, label_name = found
            await thinking.edit_text(f"🗃️ Ковальски: нашёл «{label_name}». Анализирую каталог…")
            try:
                analysis, sublabels = await asyncio.wait_for(
                    asyncio.gather(
                        loop.run_in_executor(None, analyze_label, label_id),
                        loop.run_in_executor(None, find_sublabels, label_name),
                    ),
                    timeout=40.0,
                )
            except asyncio.TimeoutError:
                analysis, sublabels = None, []
            except Exception:
                analysis, sublabels = None, []

            msg, pending = _build_label_scrape_prompt(label_id, label_name, analysis, sublabels)
            _PENDING_LABEL_SCRAPE[chat_id] = pending
            await thinking.edit_text(msg)
        except Exception as e:
            await thinking.edit_text(f"❌ Ошибка поиска лейбла: {e}")

    elif intent == "stop_scrape":
        from syncoteca.tools.label_scraper import cancel_scrape, is_running
        if is_running():
            cancel_scrape()
            await update.message.reply_text("🛑 Ковальски: отправляю сигнал остановки. Парсинг завершится после текущего альбома.")
        else:
            await update.message.reply_text("🗃️ Ковальски: парсинг сейчас не запущен.")
        _PENDING_LABEL_SCRAPE.pop(chat_id, None)

    elif intent == "skip_album":
        from syncoteca.tools.label_scraper import skip_current_album, is_running
        if is_running():
            skip_current_album()
            await update.message.reply_text("⏭ Ковальски: текущий альбом пропущен, перехожу к следующему.")
        else:
            await update.message.reply_text("🗃️ Ковальски: парсинг сейчас не запущен.")


async def _run_label_scrape_task(chat_id: int, bot, label_id: str, label_name: str, prefix: str = "") -> None:
    from syncoteca.tools.label_scraper import scrape_label
    import asyncio as _asyncio
    import time as _time

    STATUS_INTERVAL_S = 120  # send status summary every 2 min

    loop = _asyncio.get_event_loop()
    progress_msg = await bot.send_message(chat_id, f"⏳ {prefix}Парсинг «{label_name}»: получаю список альбомов…")
    _last_edit = [0.0]
    _last_status = [_time.monotonic()]
    _counters = [{"added": 0, "skipped": 0}]

    def _progress(done: int, total: int, info: str) -> None:
        now = _time.monotonic()
        # Edit progress message on every album (each album already takes 5s+, safe for Telegram)
        if now - _last_edit[0] >= 4.5:
            _last_edit[0] = now
            pct = int(done / total * 100) if total else 0
            text = (
                f"⏳ {prefix}«{label_name}»: [{done}/{total}] {pct}%\n"
                f"✅ {info}"
            )
            fut = _asyncio.run_coroutine_threadsafe(progress_msg.edit_text(text), loop)
            try:
                fut.result(timeout=5)
            except Exception:
                pass
        # Send status summary every 2 minutes as new message
        if now - _last_status[0] >= STATUS_INTERVAL_S:
            _last_status[0] = now
            pct = int(done / total * 100) if total else 0
            remaining = total - done
            eta_min = int(remaining * 6.5 / 60)
            status_text = (
                f"📊 «{label_name}»: [{done}/{total}] {pct}%\n"
                f"⏱ Осталось ~{eta_min} мин\n"
                f"Последний: {info}\n"
                f"«стоп парсинг» — для остановки"
            )
            fut2 = _asyncio.run_coroutine_threadsafe(bot.send_message(chat_id, status_text), loop)
            try:
                fut2.result(timeout=10)
            except Exception:
                pass

    try:
        result = await loop.run_in_executor(None, lambda: scrape_label(label_id, progress_cb=_progress))
    except Exception as e:
        await bot.send_message(chat_id, f"❌ Ковальски: ошибка парсинга лейбла — {e}")
        return

    try:
        await progress_msg.delete()
    except Exception:
        pass

    if result.get("error") == "already_running":
        await bot.send_message(chat_id, "⚠️ Ковальски: парсинг уже запущен, дождись завершения.")
        return

    cancelled = result.get("cancelled", False)
    final_label = result.get("label_name") or label_name
    heading = (
        f"🛑 Ковальски: парсинг «{final_label}» остановлен."
        if cancelled else
        f"✅ Ковальски: парсинг «{final_label}» завершён."
    )
    from datetime import date as _date
    today_str = _date.today().strftime("%d.%m.%Y")
    hashtag = "#" + re.sub(r"[^\w]", "_", final_label).strip("_")
    lines = [
        heading,
        f"📅 Дата: {today_str}",
        f"Альбомов обработано: {result.get('albums_done', 0)} из {result.get('albums_total', 0)}.",
        f"✅ Добавлено треков: {result.get('added', 0)}",
    ]
    if result.get("skipped"):
        lines.append(f"⏭ Пропущено (уже есть): {result['skipped']}")
    if result.get("albums_skipped"):
        lines.append(f"⏭ Альбомов пропущено вручную: {result['albums_skipped']}")
    if result.get("errors"):
        lines.append(f"❌ Ошибок: {result['errors']}")
    if cancelled:
        lines.append("💡 Для продолжения: «спарси лейбл» снова — начнёт с того места.")
    lines.append(f"\n{hashtag} #парсинг_лейбла")
    await bot.send_message(chat_id, "\n".join(lines))

    # Auto-chain: enrich → discogs date fix for newly added tracks
    newly_added = result.get("added", 0)
    if not cancelled and newly_added > 0:
        from datetime import date as _date2
        today_iso = _date2.today().isoformat()  # YYYY-MM-DD
        chain_limit = min(newly_added + 500, 10000)
        await bot.send_message(
            chat_id,
            f"🔗 Ковальски: запускаю автоцепочку для {newly_added} новых треков\n"
            f"1/2 — Яндекс обогащение (метаданные)…"
        )
        await _run_enrich_task(chat_id, bot, limit=chain_limit, date_from=today_iso)
        await bot.send_message(chat_id, "🔗 2/2 — Discogs проверка дат…")
        from syncoteca.tools.date_fixer import run_date_fix as _rdf
        await _rdf(chat_id, bot, limit=chain_limit, only_null=False, date_from=today_iso)


async def _run_multi_label_scrape_task(chat_id: int, bot, labels: list[tuple[str, str]]) -> None:
    """Scrape multiple labels sequentially, sending a summary per label + grand total."""
    grand_added = grand_skipped = grand_errors = 0
    for idx, (label_id, label_name) in enumerate(labels, 1):
        prefix = f"[{idx}/{len(labels)}] " if len(labels) > 1 else ""
        await _run_label_scrape_task(chat_id, bot, label_id, label_name, prefix=prefix)
        # _run_label_scrape_task sends its own summary — collect totals if multi
    if len(labels) > 1:
        await bot.send_message(chat_id, f"✅ Ковальски: все {len(labels)} лейблов обработаны.")


async def _run_fix_dates_task(chat_id: int, bot, limit: int, only_null: bool) -> None:
    from syncoteca.tools.date_fixer import run_date_fix
    await run_date_fix(chat_id, bot, limit=limit, only_null=only_null)


async def _run_enrich_task(
    chat_id: int,
    bot,
    limit: int,
    date_from: Optional[str] = None,
    auto_discogs: bool = False,
    artist: Optional[str] = None,
) -> None:
    from syncoteca.tools.yandex_enricher import enrich_batch
    import asyncio as _asyncio
    import time as _time

    loop = _asyncio.get_event_loop()

    scope_label = ""
    if artist:
        scope_label = f" «{artist}»"
    elif date_from:
        scope_label = f" за {date_from}"
    progress_msg = await bot.send_message(chat_id, f"⏳ Начинаю обогащение{scope_label}…")
    _last_edit = [0.0]

    def _progress(done: int, total: int, info: str) -> None:
        now = _time.monotonic()
        if now - _last_edit[0] < 3.5:
            return
        _last_edit[0] = now
        text = f"⏳ Обогащение{scope_label}: {done}/{total}\n✅ {info}"
        fut = _asyncio.run_coroutine_threadsafe(progress_msg.edit_text(text), loop)
        try:
            fut.result(timeout=5)
        except Exception:
            pass

    try:
        result = await loop.run_in_executor(
            None,
            lambda: enrich_batch(limit=limit, progress_cb=_progress, date_from=date_from, artist=artist),
        )
    except Exception as e:
        await bot.send_message(chat_id, f"❌ Ковальски: ошибка обогащения — {e}")
        return

    ok = result.get("ok", 0)
    skipped = result.get("skipped", 0)
    errors = result.get("errors", 0)
    total = result.get("total", 0)
    min_id = result.get("min_id", 0)

    try:
        await progress_msg.delete()
    except Exception:
        pass

    lines = [f"🗃️ Ковальски: обогащение{scope_label} завершено.", f"Обработано: {total} треков."]
    if ok:
        lines.append(f"✅ Успешно: {ok}")
    if skipped:
        lines.append(f"⏭ Пропущено (нет ссылки): {skipped}")
    if errors:
        lines.append(f"❌ Ошибок: {errors}")

    if ok > 0 and not auto_discogs:
        date_limit = ok + 20
        lines.append(
            f"\n❓ Проверить даты релизов по этим трекам через Discogs?\n"
            f"Яндекс Музыка выставил даты — Discogs сверит и найдёт более ранние если есть.\n"
            f"(~{date_limit} треков, id > {max(0, min_id - 1)} — скажи «да»)"
        )
        _PENDING_DATE_FIX[chat_id] = {"after_id": max(0, min_id - 1), "limit": date_limit, "only_null": False}

    report = "\n".join(lines)
    await bot.send_message(chat_id, report)

    if ok > 0 and auto_discogs:
        date_limit = min(ok + 50, 1000)
        await bot.send_message(
            chat_id,
            f"🔍 Ковальски: автоматически запускаю проверку дат Discogs для {date_limit} треков…"
        )
        from syncoteca.tools.date_fixer import run_date_fix
        await run_date_fix(chat_id, bot, limit=date_limit, only_null=False, after_id=max(0, min_id - 1))


async def _dispatch(update: Update, agent_name: str, user_request: str) -> None:
    from . import events as ev

    # Kowalski: intercept catalog tool intents before LLM call
    if agent_name == "content_manager":
        # Check for pending date-fix followup ("да" after enrich completion)
        chat_id_early = update.effective_chat.id
        if chat_id_early in _PENDING_DATE_FIX:
            lower_req = user_request.lower().strip(".,!? ")
            _yes_words = {"да", "конечно", "да проверь", "проверь", "проверяй", "давай", "ок", "окей", "угу", "ага"}
            if lower_req in _yes_words or lower_req.startswith("да"):
                params = _PENDING_DATE_FIX.pop(chat_id_early)
                from syncoteca.tools.date_fixer import run_date_fix
                asyncio.create_task(
                    run_date_fix(
                        chat_id_early,
                        update.get_bot(),
                        limit=params["limit"],
                        only_null=params.get("only_null", False),
                        after_id=params["after_id"],
                    )
                )
                return
            else:
                _PENDING_DATE_FIX.pop(chat_id_early, None)

        # /parse_label sent without args — treat next message as label name
        # Check both in-memory (fast) and Supabase (survives Railway restart)
        _is_pending_label = chat_id_early in _PENDING_LABEL_NAME
        if not _is_pending_label:
            loop_early = asyncio.get_event_loop()
            _is_pending_label = await loop_early.run_in_executor(None, _check_pending_label_name, chat_id_early)
        if _is_pending_label:
            _PENDING_LABEL_NAME.discard(chat_id_early)
            loop_early = asyncio.get_event_loop()
            await loop_early.run_in_executor(None, _clear_pending_label_name, chat_id_early)
            label_query = user_request.strip().strip('«»"\'')
            await _run_kowalski_tool(update, "scrape_label", f"спарси лейбл {label_query}")
            return

        # Pending label-scrape confirmation
        if chat_id_early in _PENDING_LABEL_SCRAPE:
            pending = _PENDING_LABEL_SCRAPE[chat_id_early]
            lower_req = user_request.lower().strip(".,!? ")
            _no_words = {"нет", "не надо", "отмена", "стоп", "cancel", "no", "отменить", "не сейчас"}
            if lower_req in _no_words or any(w in lower_req for w in ("не надо", "не сейчас", "отмен")):
                _PENDING_LABEL_SCRAPE.pop(chat_id_early, None)
                await update.message.reply_text("🗃️ Ковальски: парсинг отменён.")
                return

            stage = pending.get("stage", "confirm")
            sublabels = pending.get("sublabels", [])

            if stage == "sublabel_choice":
                # Parse user choice: 1 / 2 / 3 / sublabel name
                labels_to_scrape: list[tuple[str, str]] = []
                main_entry = (pending["label_id"], pending["label_name"])
                sub_entries = [(s["id"], s["name"]) for s in sublabels]

                if lower_req in ("1", "только основной", "только главный", "основной"):
                    labels_to_scrape = [main_entry]
                elif lower_req in ("2", "все", "всё", "все вместе", "оба", "всех", "все саблейблы", "с саблейблами"):
                    labels_to_scrape = [main_entry] + sub_entries
                elif lower_req in ("3", "только саблейблы", "только дочерние", "без основного"):
                    labels_to_scrape = sub_entries
                else:
                    # Try to match sublabel by name
                    matched = [s for s in sublabels if lower_req in s["name"].lower()]
                    if matched:
                        labels_to_scrape = [(s["id"], s["name"]) for s in matched]
                    elif lower_req in ("да", "конечно", "давай", "ок", "окей", "поехали", "запускай"):
                        labels_to_scrape = [main_entry] + sub_entries  # "да" = all
                    else:
                        # Not recognized — keep pending, re-ask
                        subs_list = "\n".join(f"  • {s['name']}" for s in sublabels[:15])
                        if len(sublabels) > 15:
                            subs_list += f"\n  ...и ещё {len(sublabels) - 15}"
                        await update.message.reply_text(
                            f"🗃️ Не понял выбор. Ответь:\n"
                            f"1 — только «{pending['label_name']}»\n"
                            f"2 — основной + все {len(sublabels)} саблейблов\n"
                            f"3 — только саблейблы\n"
                            f"Или назови название саблейбла:\n{subs_list}"
                        )
                        return
            else:
                # Simple да/нет
                _yes_words = {"да", "конечно", "давай", "ок", "окей", "угу", "ага", "yes", "подтверждаю", "запускай", "поехали"}
                if lower_req not in _yes_words and not lower_req.startswith("да"):
                    return  # not a yes — fall through to LLM
                labels_to_scrape = [(pending["label_id"], pending["label_name"])]

            _PENDING_LABEL_SCRAPE.pop(chat_id_early, None)
            total_labels = len(labels_to_scrape)
            names_preview = ", ".join(n for _, n in labels_to_scrape[:3])
            if total_labels > 3:
                names_preview += f" ...и ещё {total_labels - 3}"
            await update.message.reply_text(
                f"🗃️ Ковальски: запускаю парсинг {total_labels} лейбл(а/ов): {names_preview}\n"
                f"Прогресс — каждые 30 минут. Для остановки: «стоп парсинг»."
            )
            asyncio.create_task(_run_multi_label_scrape_task(
                chat_id_early, update.get_bot(), labels_to_scrape,
            ))
            return

        # Email confirmation: pending after Excel was sent
        _email_affirmations = {
            "да", "ок", "окей", "да!", "yes", "конечно", "давай", "отправляй", "отправь", "send",
            "вышли", "высылай", "валяй", "действуй", "шли", "пошли", "хорошо", "отправь-ка",
            "давай отправляй", "давай вышли", "пусть идёт", "пусть идет",
        }
        _email_send_substrings = ("почт", "email", "отправ", "направ", "вышл", "высыл", "валяй", "действуй")
        if chat_id_early in _PENDING_EMAIL_EXPORT:
            _lower_req = user_request.lower().strip(".,!? ")
            # Check exact match, OR any send-keyword anywhere in message, OR email address present
            _has_custom_email = re.search(r'\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b', user_request, re.IGNORECASE)
            _is_confirm = (
                _lower_req in _email_affirmations
                or any(w in _lower_req for w in _email_send_substrings)
                or _has_custom_email
                # "да, вышли" / "да пошли" — "да" as first word
                or _lower_req.split(",")[0].strip() in _email_affirmations
                or _lower_req.split()[0] in _email_affirmations
            )
            if _is_confirm:
                _ep = _PENDING_EMAIL_EXPORT.pop(chat_id_early)
                _custom_to = _has_custom_email.group(0) if _has_custom_email else None
                await _send_excel_by_email(update, _ep["query"], _ep["subject"], to_override=_custom_to)
                return
            elif _lower_req in {"нет", "не надо", "не нужно", "no", "cancel", "отмена"}:
                _PENDING_EMAIL_EXPORT.pop(chat_id_early, None)
                await update.message.reply_text("🗃️ Ковальски: окей, без почты.")
                return

        # Affirmation after catalog summary → trigger export with cached entity
        _export_affirmations = {"да", "ок", "окей", "да!", "yes", "конечно", "давай", "хорошо", "выгрузи", "выгружай", "выгрузку", "файл", "excel", "экселька"}
        if chat_id_early in _LAST_CATALOG_ENTITY:
            _lower_req = user_request.lower().strip(".,!? ")
            if _lower_req in _export_affirmations or any(w in _lower_req for w in ("выгруз", "экспорт", "excel", "файл")):
                await _run_kowalski_tool(update, "export", user_request)
                return

        intent = _kowalski_detect_intent(user_request)
        if intent:
            await _run_kowalski_tool(update, intent, user_request)
            return

    label = AGENT_LABELS.get(agent_name, agent_name)
    mem_name = MEMORY_NAME_MAP.get(agent_name, agent_name)
    thinking_msg = await update.message.reply_text(f"{label}…")

    ev.emit(mem_name, "task_start", user_request[:120], status="thinking")

    try:
        loop = asyncio.get_event_loop()
        chat_id = update.effective_chat.id

        if agent_name == "content_manager":
            # Skip catalog search for pure conversational affirmations ("да", "ок", "угу")
            # — they have no search terms, would return irrelevant tracks and confuse LLM.
            _affirmations = {"да", "нет", "ок", "окей", "угу", "ага", "хорошо", "понял", "ладно", "хорошо"}
            _is_affirmation = (
                len(user_request.split()) <= 2
                and all(w.lower().strip(".,!?!") in _affirmations for w in user_request.split())
            )
            if _is_affirmation:
                catalog_ctx = ""
            else:
                # Only search catalog if there's a meaningful entity (artist/label/year/genre).
                # Without a clear entity, random word matches corrupt LLM context.
                from syncoteca.tools.catalog_export import parse_export_query as _pq
                _entity_filters = _pq(user_request)
                if _export_filters_are_clean(_entity_filters):
                    # If we have a clean artist filter, use fetch_tracks for accurate search
                    # (handles genitive case — "Николая Носкова" → "Николай Носков")
                    if _entity_filters.get("artist") or _entity_filters.get("year_from"):
                        from syncoteca.tools.catalog_export import fetch_tracks as _ft
                        _direct_rows = await loop.run_in_executor(None, lambda: _ft(_entity_filters, limit=2000))
                        if _direct_rows:
                            _total_direct = len(_direct_rows)
                            _yf = _entity_filters.get("year_from")
                            _yt = _entity_filters.get("year_to", _yf)
                            _yr_note = f" {_yf}\u2013{_yt}" if _yf and _yt and _yf != _yt else (f" {_yf}" if _yf else "")
                            _lines = [f"[КАТАЛОГ SYNC LAB — всего {_total_direct} треков{_yr_note}. Данные для ответа, не упоминай показано N треков:"]
                            # For year-only queries: show artist breakdown
                            if _yf and not _entity_filters.get("artist"):
                                from collections import Counter
                                _ac = Counter(_dr.get("artist") or "?" for _dr in _direct_rows)
                                _lines.append(f"Артистов: {len(_ac)}")
                                for _art, _cnt in _ac.most_common(20):
                                    _lines.append(f"  {_art}: {_cnt} тр.")
                                if len(_ac) > 20:
                                    _lines.append(f"  ... и ещё {len(_ac)-20} артистов")
                            else:
                                from collections import Counter
                                # Year breakdown across ALL rows for accurate stats
                                _yc = Counter()
                                for _dr in _direct_rows:
                                    _rd = _dr.get("release_date") or ""
                                    import re as _re
                                    _ym = _re.search(r"\b(\d{4})\b", str(_rd))
                                    if _ym: _yc[_ym.group(1)] += 1
                                if _yc:
                                    _lines.append(f"По годам:")
                                    for _yr, _yc_n in sorted(_yc.items()):
                                        _lines.append(f"  {_yr} — {{_yc_n}} тр.")
                                    _no_date = _total_direct - sum(_yc.values())
                                    if _no_date > 0:
                                        _lines.append(f"  Без даты: {{_no_date}} тр.")
                                # First 20 tracks with details
                                for _dr in _direct_rows[:20]:
                                    _dp = [f"• {_dr.get('title') or '?'}", f"— {_dr.get('artist') or '?'}"]
                                    if _dr.get("label"): _dp.append(f"| Лейбл: {_dr['label']}")
                                    if _dr.get("release_date"): _dp.append(f"| {_dr['release_date']}")
                                    _lines.append(" ".join(_dp))
                            _lines.append("]")
                            catalog_ctx = "\n".join(_lines)
                        else:
                            _a_note = _entity_filters.get("artist", "")
                            _yf_note = _entity_filters.get("year_from", "")
                            if _a_note:
                                catalog_ctx = f"[КАТАЛОГ SYNC LAB: поиск по артисту '{_a_note}' — 0 треков найдено.]"
                            elif _yf_note:
                                catalog_ctx = f"[КАТАЛОГ SYNC LAB: за период {_yf_note} — 0 треков найдено.]"
                            else:
                                catalog_ctx = ""
                        if catalog_ctx:
                            _LAST_CATALOG_ENTITY[chat_id] = dict(_entity_filters)
                            # New catalog data invalidates any pending email from a previous export
                            _PENDING_EMAIL_EXPORT.pop(chat_id, None)
                    else:
                        catalog_ctx = await loop.run_in_executor(None, search_supabase_catalog, user_request)
                        if catalog_ctx:
                            _LAST_CATALOG_ENTITY[chat_id] = dict(_entity_filters)
                            _PENDING_EMAIL_EXPORT.pop(chat_id, None)
                else:
                    catalog_ctx = ""
            enriched = f"{catalog_ctx}\n\n{user_request}" if catalog_ctx else user_request
            result = await loop.run_in_executor(
                None, run_direct_agent, agent_name, chat_id, enriched
            )
            # If Claude signals an export but no actual export was triggered,
            # intercept and run the real export so the file is actually delivered.
            _EXP_SIGNALS = ("Выгружаю", "Формирую выгрузку", "файл придёт", "файл будет")
            _r_str = str(result)
            if any(_s in _r_str for _s in _EXP_SIGNALS):
                from syncoteca.tools.catalog_export import parse_export_query as _pqe, export_catalog as _ece
                _re_filters = _pqe(_r_str)
                # Fallback: if Claude's text alone has no clean entity, use cached entity
                _re_query = _r_str
                if not _export_filters_are_clean(_re_filters):
                    _cached_ent = _LAST_CATALOG_ENTITY.get(chat_id)
                    if isinstance(_cached_ent, dict) and _export_filters_are_clean(_cached_ent):
                        _re_art = _cached_ent.get("artist", "")
                        _re_yf = _cached_ent.get("year_from")
                        _re_query = _re_art or (str(_re_yf) if _re_yf else "")
                        _re_filters = _cached_ent
                if _export_filters_are_clean(_re_filters) and _re_query:
                    try:
                        import io as _io_exp
                        _re_bytes, _re_fname, _re_cnt, _ = await loop.run_in_executor(None, _ece, _re_query)
                        if _re_cnt > 0:
                            await update.message.reply_document(
                                document=_io_exp.BytesIO(_re_bytes),
                                filename=_re_fname,
                                caption=f"Отправлено: {_re_fname} ({_re_cnt} треков)",
                            )
                            _exp_subj = _re_filters.get("artist") or str(_re_filters.get("year_from", ""))
                            DIRECT_SESSIONS["content_manager"][chat_id].append({"role": "assistant", "content": f"[Выгрузка: {_exp_subj} — {_re_cnt} треков → {_re_fname}]"})
                            _LAST_CATALOG_ENTITY.pop(chat_id, None)
                    except Exception as _re_exc:
                        logger.warning(f"Auto-export after Claude signal failed: {_re_exc}")
        elif agent_name in DIRECT_AGENTS:
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

    # Restore sticky agent from Supabase after Railway restart
    if chat_id not in ACTIVE_AGENT:
        loop = asyncio.get_event_loop()
        restored = await loop.run_in_executor(None, _restore_active_agent, chat_id)
        if restored:
            ACTIVE_AGENT[chat_id] = restored

    # Sticky agent takes priority over license session history
    if chat_id in ACTIVE_AGENT:
        agent_name = ACTIVE_AGENT[chat_id]
        if agent_name == "license_manager":
            if _rika_catalog_redirect(text):
                ACTIVE_AGENT[chat_id] = "content_manager"
                _persist_active_agent(chat_id, "content_manager")
                await update.message.reply_text("🔄 Передаю Ковальски — он разберётся с каталогом.")
                await _dispatch(update, "content_manager", text)
            else:
                await _dispatch_license(update, text)
        else:
            await _dispatch(update, agent_name, text)
        return

    if LICENSE_SESSIONS.get(chat_id):
        await _dispatch_license(update, text)
        return

    await _dispatch_coordinator(update, text)


def _wrap_forwarded(update) -> str | None:
    """Return text content of a forwarded message, wrapped with a marker, or None."""
    msg = update.message
    if not msg:
        return None
    is_forwarded = (
        getattr(msg, "forward_origin", None) is not None
        or getattr(msg, "forward_from", None) is not None
        or getattr(msg, "forward_from_chat", None) is not None
        or getattr(msg, "forward_sender_name", None) is not None
    )
    if not is_forwarded:
        return None
    raw = (msg.text or msg.caption or "").strip()
    if not raw:
        return None
    sender = ""
    origin = getattr(msg, "forward_origin", None)
    if origin:
        # PTB v20: MessageOriginUser, MessageOriginChannel, MessageOriginChat, MessageOriginHiddenUser
        if hasattr(origin, "sender_user") and origin.sender_user:
            u = origin.sender_user
            sender = f" от {u.first_name or ''} {u.last_name or ''}".strip()
        elif hasattr(origin, "sender_user_name") and origin.sender_user_name:
            sender = f" от {origin.sender_user_name}"
        elif hasattr(origin, "chat") and origin.chat:
            sender = f" из канала «{origin.chat.title or ''}»"
    return f"[ПЕРЕСЛАННОЕ СООБЩЕНИЕ{sender} (Денис переслал как данные для обработки):\n{raw}\n]"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return await _deny(update)

    # Detect forwarded messages before plain text extraction
    forwarded_text = _wrap_forwarded(update)
    if forwarded_text:
        text = forwarded_text
    else:
        raw = (update.message.text or "").strip()
        if not raw:
            return
        text = raw

    chat_id = update.effective_chat.id

    if chat_id in TEACH_SESSIONS:
        await _save_to_memory(TEACH_SESSIONS[chat_id], text, update)
        return

    # Restore sticky agent from Supabase after Railway restart
    if chat_id not in ACTIVE_AGENT:
        loop = asyncio.get_event_loop()
        restored = await loop.run_in_executor(None, _restore_active_agent, chat_id)
        if restored:
            ACTIVE_AGENT[chat_id] = restored

    # Sticky agent takes priority over license session history
    if chat_id in ACTIVE_AGENT:
        agent_name = ACTIVE_AGENT[chat_id]
        if agent_name == "license_manager":
            if _rika_catalog_redirect(text):
                ACTIVE_AGENT[chat_id] = "content_manager"
                _persist_active_agent(chat_id, "content_manager")
                await update.message.reply_text("🔄 Передаю Ковальски — он разберётся с каталогом.")
                await _dispatch(update, "content_manager", text)
            else:
                await _dispatch_license(update, text)
        else:
            await _dispatch(update, agent_name, text)
        return

    if LICENSE_SESSIONS.get(chat_id):
        await _dispatch_license(update, text)
        return

    # Default: Coordinator routes the message
    await _dispatch_coordinator(update, text)


_BRIEFING_KEYWORDS = {
    "задачи", "задач", "задача", "задание", "задания", "заданий",
    "asana", "асана", "планы", "план", "расписание", "список дел",
    "что сегодня", "что у меня", "что надо", "что нужно сделать",
    "брифинг", "briefing", "дела",
}
# Verbs that signal task *creation* — exclude from briefing detection
_TASK_CREATION_VERBS = (
    "поставь", "поставить", "создай", "создать", "добавь", "добавить",
    "занеси", "занести", "запиши", "записать", "сделай", "сделать",
    "напомни", "напомнить", "зафиксируй", "зафиксировать",
    "внеси", "внести", "отправь", "отправить", "отправьте",
    "зашли", "пошли", "скинь",
)


_RESCHEDULE_VERBS = (
    "сдвинь", "сдвини", "сдвинуть", "перенеси", "перенести", "перенесите",
    "reschedule", "move", "postpone",
)
_WEEKDAY_MAP = {
    "понедельник": 0, "понедельника": 0,
    "вторник": 1, "вторника": 1,
    "среду": 2, "среда": 2, "среды": 2,
    "четверг": 3, "четверга": 3,
    "пятницу": 4, "пятница": 4, "пятницы": 4,
    "субботу": 5, "суббота": 5, "субботы": 5,
    "воскресенье": 6, "воскресенья": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_MONTH_MAP = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def _is_reschedule_intent(text: str) -> bool:
    lower = text.lower()
    return any(v in lower for v in _RESCHEDULE_VERBS) and "задач" in lower


_TASK_NAME_NOISE = {
    "мне", "мне,", "пожалуйста", "прошу", "давай", "пожалуйста,", "прошу,", "давай,",
    "под", "названием", "название", "именем", "имени", "с", "по",
}
_TASK_PREFIX_STRIP = (
    "под названием ", "с названием ", "под именем ", "по имени ", "название ",
)


def _parse_new_due(date_str: str) -> str | None:
    """Parse natural language date like 'завтра', 'пятницу', '10 июня' → YYYY-MM-DD."""
    import datetime
    today = datetime.date.today()
    # Strip punctuation and extra words like "день", "дня" that follow the date keyword
    s = date_str.strip().rstrip(".,!?;:").lower()

    # "завтра" / "завтрашний день" / "завтрашнего дня"
    if s == "завтра" or s.startswith("завтра") or "tomorrow" in s:
        return (today + datetime.timedelta(days=1)).isoformat()
    # "послезавтра" / "послезавтрашний день"
    if s.startswith("послезавтра") or "day after tomorrow" in s:
        return (today + datetime.timedelta(days=2)).isoformat()
    if s.startswith("сегодня") or s == "today":
        return today.isoformat()

    # "через N дней"
    m = re.search(r'через\s+(\d+)\s+дн', s)
    if m:
        return (today + datetime.timedelta(days=int(m.group(1)))).isoformat()

    # Weekday name → next occurrence
    for word, wd in _WEEKDAY_MAP.items():
        if word in s:
            days_ahead = (wd - today.weekday()) % 7 or 7
            return (today + datetime.timedelta(days=days_ahead)).isoformat()

    # "10 июня" / "5 мая"
    m = re.search(r'(\d{1,2})\s+(' + '|'.join(_MONTH_MAP) + r')', s)
    if m:
        day, month_word = int(m.group(1)), m.group(2)
        month = _MONTH_MAP[month_word]
        year = today.year if month >= today.month else today.year + 1
        try:
            return datetime.date(year, month, day).isoformat()
        except ValueError:
            return None

    # ISO date fallback
    m = re.search(r'\d{4}-\d{2}-\d{2}', s)
    if m:
        return m.group()

    return None


def parse_reschedule_intent(text: str) -> dict:
    """Extract task_name and new_due from 'Сдвинь задачу X на Y'."""
    lower = text.lower()

    # Find the verb position
    verb_end = 0
    for v in _RESCHEDULE_VERBS:
        idx = lower.find(v)
        if idx >= 0:
            verb_end = max(verb_end, idx + len(v))

    # Find "задач*" keyword after the verb
    task_kw_idx = lower.find("задач", verb_end)
    if task_kw_idx < 0:
        return {"task_name": "", "new_due": ""}

    # Skip past the full "задач*" word to its end
    word_rest = lower[task_kw_idx:]
    word_boundary = re.search(r'\s', word_rest)
    after_kw = task_kw_idx + (word_boundary.start() if word_boundary else len(word_rest))

    remaining_orig = text[after_kw:].strip()
    remaining_lower = remaining_orig.lower()

    # Strip "под названием" / "с названием" / "под именем" prefixes
    for prefix in _TASK_PREFIX_STRIP:
        if remaining_lower.startswith(prefix):
            remaining_orig = remaining_orig[len(prefix):].strip()
            remaining_lower = remaining_orig.lower()
            break

    # Check for quoted task name (guillemets, curly quotes, ASCII quotes)
    quoted = _extract_quoted_strings(remaining_orig)
    if quoted:
        task_name = quoted[0]
        # Find date after the closing quote character
        last_q = max(
            remaining_orig.rfind('"'),
            remaining_orig.rfind(_RQ),
            remaining_orig.rfind(_RC),
        )
        after_quote = remaining_orig[last_q + 1:].strip() if last_q >= 0 else ""
        date_part = re.sub(r'^на\s+', '', after_quote, flags=re.IGNORECASE).strip()
    else:
        # Split on " на " preposition before the date
        na_match = re.search(r'\s+на\s+', remaining_orig, re.IGNORECASE)
        if na_match:
            task_name = remaining_orig[:na_match.start()].strip()
            date_part = remaining_orig[na_match.end():].strip()
        else:
            task_name = remaining_orig.strip()
            date_part = ""

    # Strip noise words from task_name (filler, prefix fragments)
    task_words = [
        w for w in task_name.split()
        if w.lower().rstrip(",.!?") not in _TASK_NAME_NOISE
    ]
    task_name = " ".join(task_words).strip()

    new_due = _parse_new_due(date_part) if date_part else ""
    return {"task_name": task_name, "new_due": new_due}


def _is_briefing_intent(text: str) -> bool:
    lower = text.lower()
    # Task creation commands ("поставь задачу", "создай задачу в асане") look like
    # briefing due to "задач*" keyword — exclude them explicitly.
    if any(v in lower for v in _TASK_CREATION_VERBS) and "задач" in lower:
        return False
    has_tasks = any(w in lower for w in _BRIEFING_KEYWORDS)
    # Also trigger on date/person scope words even without an explicit task keyword
    scope_words = (
        "завтра", "следующ", "будущ", "недел",
        "екатерин", "катер", "катя", "кати",
        "александр", "саш", "алекс", "саня",
        "kate", "katya", "katy", "katie", "ekaterina",
        "alex", "sasha", "alexander", "alexandra", "alexa",
    )
    has_scope = any(w in lower for w in scope_words)
    return has_tasks or (has_scope and any(w in lower for w in ("задач", "дел", "план")))



async def _dispatch_coordinator(update: Update, text: str) -> None:
    chat_id = update.effective_chat.id
    thinking_msg = await update.message.reply_text("🎯 Рядовой…")
    try:
        loop = asyncio.get_event_loop()

        # Reschedule intent — check BEFORE briefing (both contain "задач" + date words)
        if _is_reschedule_intent(text):
            intent = parse_reschedule_intent(text)
            task_name = intent["task_name"]
            new_due = intent["new_due"]
            if not task_name or not new_due:
                await thinking_msg.edit_text(
                    "🎯 Рядовой:\n\n⚠️ Не понял. Скажи: «Сдвинь задачу [название] на [день]»."
                )
                return
            await thinking_msg.edit_text(f"🔍 Ищу задачу «{task_name}»…")
            found = await loop.run_in_executor(None, find_asana_task_by_name, task_name)
            if not found:
                await thinking_msg.edit_text(
                    f"🎯 Рядовой:\n\n❌ Задача «{task_name}» не найдена в Asana."
                )
                return
            if len(found) == 1:
                import datetime
                actual_name = found[0].get("name", task_name)
                gid = found[0]["gid"]
                upd_ok = await loop.run_in_executor(None, update_asana_task_due, gid, new_due)
                try:
                    new_date = datetime.date.fromisoformat(new_due)
                    today = datetime.date.today()
                    delta = (new_date - today).days
                    # Human-readable date label
                    day_word = _DAY_NAMES_FULL[new_date.weekday()].lower()
                    date_ru = f"{new_date.day} {_MONTHS_RU[new_date.month - 1]}"
                    if delta == 0:
                        when = f"сегодня, {date_ru} ({day_word})"
                    elif delta == 1:
                        when = f"завтра, {date_ru} ({day_word})"
                    elif delta == 2:
                        when = f"послезавтра, {date_ru} ({day_word})"
                    else:
                        when = f"{day_word}, {date_ru}"
                    if upd_ok.startswith("✅"):
                        confirm = f"✅ Задачу «{actual_name}» сдвинул на {when}."
                    else:
                        confirm = upd_ok  # error text from API
                    await thinking_msg.edit_text(f"🎯 Рядовой:\n\n{confirm}")
                except Exception:
                    await thinking_msg.edit_text(f"🎯 Рядовой:\n\n{upd_ok}")
                return
            # Multiple matches
            names = "\n".join(
                f"• {t['name']} (срок: {t.get('due_on') or 'не задан'})" for t in found[:5]
            )
            await thinking_msg.edit_text(
                f"🎯 Рядовой:\n\nНашёл несколько задач — уточни название:\n{names}"
            )
            return

        # Briefing intent: pull Asana directly, skip LLM routing
        if _is_briefing_intent(text):
            intent = parse_briefing_intent(text)
            data = await loop.run_in_executor(
                None, fetch_asana_briefing, intent["date_range"], intent["filter_person"]
            )
            reply = format_morning_briefing(data, intent["date_range"])
            await thinking_msg.edit_text(reply)
            return
        result = await loop.run_in_executor(None, run_coordinator, chat_id, text)
        action = result.get("action", "reply")

        if action == "route":
            agent_name = result.get("agent", "license_manager")
            task = result.get("task", text)
            label = AGENT_LABELS.get(agent_name, agent_name)
            await thinking_msg.edit_text(f"🔀 → {label}")
            if agent_name == "license_manager":
                ACTIVE_AGENT[chat_id] = "license_manager"
                _persist_active_agent(chat_id, "license_manager")
                LICENSE_SESSIONS[chat_id] = []
                await _dispatch_license(update, task)
            else:
                await _dispatch(update, agent_name, task)
        elif action == "calendar":
            await thinking_msg.edit_text("📅 Создаю встречу…")
            from syncoteca.tools.google_calendar_tool import GoogleCalendarTool
            # Extract emails ONLY from the user's raw text — never trust LLM-generated attendees
            # (LLM hallucinates emails for company names, causing 400 Invalid attendee email)
            _email_pat = re.compile(r'[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+\.[a-zA-Z]{2,}')
            safe_attendees = _email_pat.findall(text)
            cal = GoogleCalendarTool()
            cal_result = await loop.run_in_executor(
                None,
                lambda: cal._run(
                    title=result.get("title", "Встреча"),
                    date=result.get("date", ""),
                    time=result.get("time", "10:00"),
                    duration_minutes=int(result.get("duration_minutes", 60)),
                    description=result.get("description", ""),
                    attendees=safe_attendees,
                ),
            )
            await thinking_msg.edit_text(f"🎯 Рядовой:\n\n{cal_result}")
        elif action == "reschedule_task":
            task_name = result.get("task_name", "")
            new_due = result.get("new_due", "")
            if not task_name or not new_due:
                await thinking_msg.edit_text("🎯 Рядовой:\n\n⚠️ Не понял — укажи название задачи и новую дату.")
            else:
                await thinking_msg.edit_text(f"🔍 Ищу задачу «{task_name}»…")
                found = await loop.run_in_executor(None, find_asana_task_by_name, task_name)
                if not found:
                    await thinking_msg.edit_text(f"🎯 Рядовой:\n\n❌ Задача «{task_name}» не найдена в Asana.")
                elif len(found) == 1:
                    gid = found[0]["gid"]
                    upd_result = await loop.run_in_executor(None, update_asana_task_due, gid, new_due)
                    await thinking_msg.edit_text(f"🎯 Рядовой:\n\n{upd_result}")
                else:
                    # Multiple matches — show list, ask to clarify
                    names = "\n".join(
                        f"• {t['name']} (срок: {t.get('due_on') or 'не задан'})" for t in found[:5]
                    )
                    await thinking_msg.edit_text(
                        f"🎯 Рядовой:\n\nНашёл несколько задач, уточни:\n{names}\n\nСкажи точнее название."
                    )
        elif action == "asana_task":
            title = result.get("title", text[:80])
            notes = result.get("notes", text)
            due_on = result.get("due_on", "")
            await thinking_msg.edit_text("📋 Создаю задачу в Asana…")
            asana_result = await loop.run_in_executor(
                None, save_to_asana, notes, title, "denis@synclab.pro", due_on
            )
            await thinking_msg.edit_text(f"🎯 Рядовой:\n\n{asana_result}")
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


async def handle_asana_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/asana_debug — diagnose Asana API connectivity and project access."""
    if not _is_owner(update):
        return await _deny(update)
    thinking = await update.message.reply_text("🔍 Диагностика Asana…")
    token = os.getenv("ASANA_TOKEN", "")
    workspace_id = os.getenv("ASANA_WORKSPACE_ID", "331121027676371")
    lines = []

    if not token:
        await thinking.edit_text("❌ ASANA_TOKEN не задан в Railway")
        return

    # 1. Current user
    try:
        r = httpx.get("https://app.asana.com/api/1.0/users/me",
                      headers={"Authorization": f"Bearer {token}"}, timeout=10)
        me = r.json().get("data", {})
        lines.append(f"✅ Токен OK: {me.get('name')} ({me.get('email')})")
    except Exception as e:
        lines.append(f"❌ /users/me: {e}")

    # 2. Workspace users — find Alexandra and Kate
    try:
        r = httpx.get(f"https://app.asana.com/api/1.0/workspaces/{workspace_id}/users",
                      headers={"Authorization": f"Bearer {token}"},
                      params={"opt_fields": "gid,name,email"}, timeout=10)
        r.raise_for_status()
        users = r.json().get("data", [])
        lines.append(f"✅ Пользователей в workspace: {len(users)}")
        for u in users:
            if any(k in (u.get("email") or "").lower() for k in ("alexa", "kate")):
                lines.append(f"   👤 {u.get('name')} | {u.get('email')} | gid={u.get('gid')}")
    except Exception as e:
        lines.append(f"❌ /workspace/users: {e}")

    # 3. Direct project fetch
    import datetime
    today = datetime.date.today().isoformat()
    for label, pid in [("Alexandra", "1201138547007410"), ("Kate", "911206717671835")]:
        try:
            r = httpx.get(f"https://app.asana.com/api/1.0/projects/{pid}/tasks",
                          headers={"Authorization": f"Bearer {token}"},
                          params={"opt_fields": "name,due_on,completed", "limit": "10"}, timeout=10)
            if r.status_code != 200:
                lines.append(f"❌ {label} project {pid}: HTTP {r.status_code}")
            else:
                tasks = r.json().get("data", [])
                today_cnt = sum(1 for t in tasks if t.get("due_on") == today)
                lines.append(f"✅ {label} project: {len(tasks)} задач (первые 10), сегодня={today_cnt}")
                for t in tasks[:3]:
                    lines.append(f"   • {t.get('name','?')[:50]} | due={t.get('due_on')} | done={t.get('completed')}")
        except Exception as e:
            lines.append(f"❌ {label} project fetch: {e}")

    await thinking.edit_text("\n".join(lines))


async def handle_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/briefing [tomorrow|week|nextweek] — request briefing on demand."""
    if not _is_owner(update):
        return await _deny(update)
    arg = (context.args[0] if context.args else "").lower()
    dr_map = {"tomorrow": "tomorrow", "week": "this_week", "nextweek": "next_week"}
    date_range = dr_map.get(arg, "today")
    thinking = await update.message.reply_text("📋 Запрашиваю задачи из Asana…")
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, fetch_asana_briefing, date_range, None)
    text = format_morning_briefing(data, date_range)
    await thinking.edit_text(text)


async def handle_fix_dates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/fix_dates [limit] [all] — Kowalski verifies release dates via Discogs."""
    if not _is_owner(update):
        return await _deny(update)

    from syncoteca.tools.date_fixer import run_date_fix

    args = context.args or []
    limit = 5000
    only_null = True
    for a in args:
        if a.isdigit():
            limit = min(int(a), 50000)
        elif a.lower() == "all":
            only_null = False

    chat_id = update.effective_chat.id
    asyncio.create_task(run_date_fix(chat_id, context.bot, limit=limit, only_null=only_null))


async def handle_verify_dates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/verify_dates [limit] [after_id] — Re-check tracks with existing dates against Discogs."""
    if not _is_owner(update):
        return await _deny(update)

    from syncoteca.tools.date_fixer import run_date_fix

    args = context.args or []
    limit = 5000
    after_id = 0
    _limit_set = False
    for a in args:
        if a.isdigit():
            if not _limit_set:
                limit = min(int(a), 50000)
                _limit_set = True
            else:
                after_id = int(a)

    chat_id = update.effective_chat.id
    after_note = f" (id > {after_id})" if after_id else ""
    await update.message.reply_text(
        f"🗃️ Ковальски: запускаю перепроверку дат через Discogs.\n"
        f"Режим: все треки включая уже с датой — ищу более ранние.{after_note}\n"
        f"Лимит: {limit} | Займёт ~{limit // 60 + 1} мин."
    )
    asyncio.create_task(run_date_fix(chat_id, context.bot, limit=limit, only_null=False, after_id=after_id))


async def handle_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/export <query> — Kowalski exports catalog to Excel."""
    if not _is_owner(update):
        return await _deny(update)

    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text(
            "🗃️ Ковальски: укажи фильтр.\n"
            "Примеры:\n"
            "  /export S.T.A.L.K.E.R.\n"
            "  /export 1996\n"
            "  /export 1975-1980\n"
            "  /export лейбл Мелодия\n"
            "  /export Земфира"
        )
        return

    thinking = await update.message.reply_text("🗃️ Ковальски: формирую Excel…")
    loop = asyncio.get_event_loop()

    try:
        from syncoteca.tools.catalog_export import export_catalog, build_export_caption
        xlsx_bytes, filename, count, tracks = await loop.run_in_executor(None, export_catalog, query)

        if count == 0:
            await thinking.edit_text(f"🗃️ Ковальски: по запросу «{query}» треков не найдено.")
            return

        caption = build_export_caption(tracks, query)
        await thinking.edit_text(f"🗃️ Ковальски: найдено {count} треков, отправляю файл…")
        import io
        await update.message.reply_document(
            document=io.BytesIO(xlsx_bytes),
            filename=filename,
            caption=caption,
            read_timeout=120,
            write_timeout=120,
        )
        await thinking.delete()
    except Exception as e:
        logger.exception("Export error")
        await thinking.edit_text(f"❌ Ошибка экспорта: {e}")


async def handle_check_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/check_catalog — Kowalski scans for tracks with link but missing metadata."""
    if not _is_owner(update):
        return await _deny(update)

    thinking = await update.message.reply_text("🔍 Ковальски: сканирую каталог на аномалии…")
    loop = asyncio.get_event_loop()

    try:
        from syncoteca.tools.catalog_audit import run_audit
        tracks, report = await loop.run_in_executor(None, run_audit)
        await thinking.edit_text(report)
    except Exception as e:
        logger.exception("Catalog audit error")
        await thinking.edit_text(f"❌ Ошибка аудита: {e}")


async def handle_export_anomalies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/export_anomalies — Kowalski exports tracks with incomplete metadata to Excel."""
    if not _is_owner(update):
        return await _deny(update)

    thinking = await update.message.reply_text("🔍 Ковальски: формирую Excel с аномалиями…")
    loop = asyncio.get_event_loop()

    try:
        from syncoteca.tools.catalog_audit import fetch_anomalies, export_anomalies_excel
        import io

        tracks = await loop.run_in_executor(None, fetch_anomalies)

        if not tracks:
            await thinking.edit_text("✅ Ковальски: аномалий нет — все треки со ссылками заполнены.")
            return

        xlsx_bytes = await loop.run_in_executor(None, export_anomalies_excel, tracks)
        await thinking.edit_text(f"🔍 Найдено {len(tracks)} аномалий, отправляю файл…")
        await update.message.reply_document(
            document=io.BytesIO(xlsx_bytes),
            filename="SYNCLAB_anomalies.xlsx",
            caption=f"🔍 SYNC LAB — {len(tracks)} треков с неполными метаданными",
        )
        await thinking.delete()
    except Exception as e:
        logger.exception("Export anomalies error")
        await thinking.edit_text(f"❌ Ошибка: {e}")


async def handle_enrich(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/enrich [limit|today|yesterday|YYYY-MM-DD] — Kowalski enriches empty tracks via Yandex Music."""
    if not _is_owner(update):
        return await _deny(update)

    from datetime import date as _date, timedelta as _timedelta
    import re as _re

    args_str = " ".join(context.args) if context.args else ""
    args_lower = args_str.lower()

    # Artist filter: /enrich Носков or /enrich artist="Носков"
    artist_filter: Optional[str] = None
    _am = _re.search(r'artist=["\']?([^"\']+)["\']?', args_str, _re.IGNORECASE)
    if _am:
        artist_filter = _am.group(1).strip()
    elif not any(c.isdigit() for c in args_str) and args_str.strip() and args_lower not in ("today", "yesterday", "сегодня", "вчера"):
        # Plain text args with no digits = artist name
        artist_filter = args_str.strip()

    # Date filter detection
    date_from: Optional[str] = None
    auto_discogs = False
    if not artist_filter:
        if "today" in args_lower or "сегодня" in args_lower:
            date_from = _date.today().isoformat()
            auto_discogs = True
        elif "yesterday" in args_lower or "вчера" in args_lower:
            date_from = (_date.today() - _timedelta(days=1)).isoformat()
            auto_discogs = True
        else:
            m_date = _re.search(r'\b(20\d{2}-\d{2}-\d{2})\b', args_str)
            if m_date:
                date_from = m_date.group(1)
                auto_discogs = True

    _has_filter = artist_filter or date_from
    m_limit = _re.search(r'\b(\d{1,4})\b', args_str)
    limit = min(int(m_limit.group(1)), 2000) if m_limit and not _has_filter else (2000 if _has_filter else 250)

    chat_id = update.effective_chat.id
    ACTIVE_AGENT[chat_id] = "content_manager"
    _persist_active_agent(chat_id, "content_manager")

    loop = asyncio.get_event_loop()
    thinking = await update.message.reply_text("🗃️ Ковальски: пошёл посмотрю что есть для работы…")
    try:
        from syncoteca.tools.yandex_enricher import count_empty_tracks
        total_pending = await loop.run_in_executor(None, count_empty_tracks)
    except Exception:
        total_pending = "?"

    if str(total_pending) == "0" and not _has_filter:
        await thinking.edit_text("🗃️ Ковальски: а у нас всё в базе хорошо — пустых треков нет.")
        return

    scope_note = f" «{artist_filter}»" if artist_filter else (f" за {date_from}" if date_from else "")
    discogs_note = " + проверка Discogs автоматом" if auto_discogs else ""
    eta_src = total_pending if not _has_filter else "?"
    eta = f"~{int(eta_src) * 4 // 60} мин" if str(eta_src).isdigit() and int(eta_src) > 0 else "несколько минут"
    reply = (
        f"🗃️ Ковальски: запускаю обогащение{scope_note}.\n"
        f"Займёт {eta}{discogs_note}. Иду работать."
    )
    await thinking.edit_text(reply)
    asyncio.create_task(_run_enrich_task(
        chat_id, context.bot, limit,
        date_from=date_from, auto_discogs=auto_discogs, artist=artist_filter,
    ))


async def handle_parse_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/parse_label <name> — Kowalski analyzes + scrapes full Yandex Music label catalog."""
    if not _is_owner(update):
        return await _deny(update)
    label_query = " ".join(context.args).strip() if context.args else ""
    chat_id = update.effective_chat.id
    loop = asyncio.get_event_loop()
    ACTIVE_AGENT[chat_id] = "content_manager"
    _persist_active_agent(chat_id, "content_manager")
    if not label_query:
        _PENDING_LABEL_NAME.add(chat_id)
        await loop.run_in_executor(None, _set_pending_label_name, chat_id)
        await update.message.reply_text("🗃️ Ковальски: напиши название лейбла:")
        return
    thinking = await update.message.reply_text(f"🗃️ Ковальски: ищу лейбл «{label_query}» в базе…")
    try:
        from syncoteca.tools.label_scraper import find_label_in_db, analyze_label, find_sublabels, is_running
        if is_running():
            await thinking.edit_text("⚠️ Ковальски: парсинг уже запущен. Сначала останови: /stop_label_parse")
            return
        found = await loop.run_in_executor(None, find_label_in_db, label_query)
        if not found:
            await thinking.edit_text(
                f"🗃️ Ковальски: лейбл «{label_query}» не найден в таблице labels.\n"
                f"Проверь название или добавь лейбл в базу."
            )
            return
        label_id, label_name = found
        await thinking.edit_text(f"🗃️ Ковальски: нашёл «{label_name}». Анализирую каталог…")
        try:
            analysis, sublabels = await asyncio.wait_for(
                asyncio.gather(
                    loop.run_in_executor(None, analyze_label, label_id),
                    loop.run_in_executor(None, find_sublabels, label_name),
                ),
                timeout=40.0,
            )
        except (asyncio.TimeoutError, Exception):
            analysis, sublabels = None, []

        msg, pending = _build_label_scrape_prompt(label_id, label_name, analysis, sublabels)
        _PENDING_LABEL_SCRAPE[chat_id] = pending
        await thinking.edit_text(msg)
    except Exception as e:
        await thinking.edit_text(f"❌ Ошибка: {e}")


async def handle_stop_label_parse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stop_label_parse — Stop the running label catalog scrape."""
    if not _is_owner(update):
        return await _deny(update)
    from syncoteca.tools.label_scraper import cancel_scrape, is_running
    chat_id = update.effective_chat.id
    _PENDING_LABEL_SCRAPE.pop(chat_id, None)
    if is_running():
        cancel_scrape()
        await update.message.reply_text("🛑 Ковальски: сигнал остановки отправлен. Парсинг завершится после текущего альбома.")
    else:
        await update.message.reply_text("🗃️ Ковальски: парсинг сейчас не запущен.")


async def handle_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stop — clear sticky agent, teach mode, return to coordinator."""
    if not _is_owner(update):
        return await _deny(update)
    chat_id = update.effective_chat.id
    ACTIVE_AGENT.pop(chat_id, None)
    _clear_active_agent(chat_id)
    TEACH_SESSIONS.pop(chat_id, None)
    LICENSE_SESSIONS[chat_id] = []
    await update.message.reply_text("🎯 Вернулся к координатору. Пиши задачу.")


# --- Bot setup ---

async def post_init(app: Application) -> None:
    commands = [
        BotCommand("start", "Координатор — начать сначала"),
        BotCommand("license", "Рико (лицензии, права)"),
        BotCommand("kowalski", "Ковальски (контент, каталог, метаданные)"),
        BotCommand("enrich", "Ковальски: обогащение треков (Яндекс Музыка)"),
        BotCommand("parse_label", "Ковальски: парсинг каталога лейбла"),
        BotCommand("stop_label_parse", "Ковальски: остановить парсинг лейбла"),
        BotCommand("verify_dates", "Ковальски: перепроверить даты (Discogs)"),
        BotCommand("lawyer", "Ксюша (договоры, юрист)"),
        BotCommand("accountant", "Марина (роялти, бухгалтерия)"),
        BotCommand("bizdev", "Директор по развитию"),
        BotCommand("dev", "Разработчик"),
        BotCommand("know", "Знания: запомнить — /know рико текст"),
        BotCommand("memory", "Знания: показать — /memory рико"),
        BotCommand("teach_stop", "Знания: остановить режим обучения"),
        BotCommand("briefing", "Брифинг задач Asana на сегодня"),
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
    app.add_handler(CommandHandler("briefing", handle_briefing))
    app.add_handler(CommandHandler("fix_dates", handle_fix_dates))
    app.add_handler(CommandHandler("verify_dates", handle_verify_dates))
    app.add_handler(CommandHandler("enrich", handle_enrich))
    app.add_handler(CommandHandler("parse_label", handle_parse_label))
    app.add_handler(CommandHandler("stop_label_parse", handle_stop_label_parse))
    app.add_handler(CommandHandler("export", handle_export))
    app.add_handler(CommandHandler("check_catalog", handle_check_catalog))
    app.add_handler(CommandHandler("export_anomalies", handle_export_anomalies))
    app.add_handler(CommandHandler("asana_debug", handle_asana_debug))

    for cmd in SLASH_MAP:
        app.add_handler(CommandHandler(cmd, handle_slash_agent))

    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Morning briefing: every day at 09:00 Moscow time
    from zoneinfo import ZoneInfo
    from datetime import time as dt_time
    moscow_9am = dt_time(9, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    app.job_queue.run_daily(morning_briefing_job, time=moscow_9am)

    logger.info("Синкотека bot starting…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run_bot()
