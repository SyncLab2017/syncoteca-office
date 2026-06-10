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
    elif any(w in lower for w in ("мои", "моих", "у меня", "мне", "только мои", "my", "mine")):
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
}
_DEFAULT_DIRECT_MODEL = "claude-haiku-4-5-20251001"


def run_direct_agent(agent_name: str, chat_id: int, user_message: str) -> str:
    """Direct Anthropic API call without CrewAI overhead."""
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    system = DIRECT_PROMPTS.get(agent_name) or "Ты — помощник агентства Синкотека."
    history = DIRECT_SESSIONS[agent_name][chat_id]
    history.append({"role": "user", "content": user_message})
    model = _DIRECT_AGENT_MODELS.get(agent_name, _DEFAULT_DIRECT_MODEL)

    response = client.messages.create(
        model=model,
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
    "внеси", "внести", "добавь",
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
            await thinking_msg.edit_text("📋 Создаю задачу в Asana…")
            asana_result = await loop.run_in_executor(
                None, save_to_asana, notes, title
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
        BotCommand("start", "Главное меню"),
        BotCommand("stop", "Вернуться к координатору"),
        BotCommand("license", "→ Рико (лицензии, права)"),
        BotCommand("lawyer", "→ Ксюша (договоры, юрист)"),
        BotCommand("accountant", "→ Марина (роялти, бухгалтерия)"),
        BotCommand("bizdev", "→ Директор по развитию"),
        BotCommand("dev", "→ Разработчик"),
        BotCommand("know", "Записать знание: /know марина НДС 22%"),
        BotCommand("teach", "Режим обучения: /teach рико"),
        BotCommand("teach_stop", "Завершить режим обучения"),
        BotCommand("memory", "Показать знания: /memory рико"),
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
