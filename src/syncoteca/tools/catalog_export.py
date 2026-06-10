"""Excel export of Supabase tracks catalog with natural-language filter parsing."""
import io
import os
import re

import httpx
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment


def _sb_headers() -> dict:
    key = os.getenv("SUPABASE_KEY", "")
    return {"apikey": key, "Authorization": f"Bearer {key}"}

def _sb_base() -> str:
    return os.getenv("SUPABASE_URL", "").rstrip("/")


# Short label aliases: when Denis says just "Мелодия" or "Зион", resolve to label search.
# Keys are lowercase. Values are the search term passed to label.ilike.*value*.
LABEL_ALIASES: dict[str, str] = {
    "мелодия": "Мелодия",
    "melody": "Мелодия",
    "зион": "Zion",
    "zion": "Zion",
    "джем": "ДЖЕМ",
    "auris": "Auris",
    "аурис": "Auris",
    "dnk": "DNK",
    "днк": "DNK",
    "adam": "Adam Music",
    "balt": "Balt",
    "golden sound": "Golden Sound",
}

# Words that signal "label context" — "фирме Мелодия", "компании X", "издательства X"
_LABEL_CONTEXT_RE = re.compile(
    r"(?:лейбл[аеыуой]?|label|фирм[аеыуой]|компани[яи]|издательств[аоеу]?|"
    r"правообладател[яьей]+|рекорд-лейбл[аеыуой]?)\s+([«»\w\s.,-]+?)(?:\s+(?:год|за|и|дай|скинь)|[.,!?]|$)",
    re.IGNORECASE,
)


def _strip_quotes(s: str) -> str:
    return s.strip("«»\"'").strip()


def parse_export_query(text: str) -> dict:
    """Extract filters from natural-language export request.

    Returns dict with optional keys: artist, label, year_from, year_to, genre
    Examples:
      "репертуар S.T.A.L.K.E.R." → {"artist": "S.T.A.L.K.E.R."}
      "треки за 1996 год"         → {"year_from": 1996, "year_to": 1996}
      "период 1975-1980"          → {"year_from": 1975, "year_to": 1980}
      "лейбл Мелодия"             → {"label": "Мелодия"}
      "по фирме «Мелодия»"        → {"label": "Мелодия"}
      "Мелодия"                   → {"label": "Мелодия"}  (via alias)
    """
    filters: dict = {}
    lower = text.lower()

    # Handle CLI-style flags: --label="X" / --artist="X" (user copied bot suggestion)
    m = re.search(r'--label=["\']?([^"\']+)["\']?', text, re.IGNORECASE)
    if m:
        filters["label"] = _strip_quotes(m.group(1).strip())
        return filters
    m = re.search(r'--artist=["\']?([^"\']+)["\']?', text, re.IGNORECASE)
    if m:
        filters["artist"] = _strip_quotes(m.group(1).strip())
        return filters

    # Year range: "1975-1980" / "1975–1980"
    m = re.search(r"\b((?:19|20)\d{2})\s*[-–—]\s*((?:19|20)\d{2})\b", text)
    if m:
        filters["year_from"] = int(m.group(1))
        filters["year_to"] = int(m.group(2))
        return filters  # year range overrides everything

    # Single year
    m = re.search(r"\b((?:19|20)\d{2})\b", text)
    if m:
        filters["year_from"] = int(m.group(1))
        filters["year_to"] = int(m.group(1))

    # Label: "лейбл X" / "label X" / "фирме X" / "компании X" / "издательства X"
    m = _LABEL_CONTEXT_RE.search(text)
    if m:
        filters["label"] = _strip_quotes(m.group(1).strip())

    # Artist: "репертуар X" / "исполнитель X" / "группа X" / "артист X"
    # Handles Russian inflection: группа/группе/группы/группой/группу
    if not filters:
        m = re.search(
            r"(?:репертуар|исполнител[ья]|групп[аеыуойи]?|артист[аеуыой]?|artist|band)\s+([«»\w\s.,-]+?)(?:\s*$)",
            text, re.IGNORECASE,
        )
        if m:
            artist = _strip_quotes(m.group(1).strip())
            # Strip trailing noise ("и дай мне Excel", etc.)
            artist = re.sub(r'\s+(?:и|дай|скинь|в|как|excel|файл|xlsx).*$', '', artist, flags=re.IGNORECASE).strip()
            if artist:
                filters["artist"] = artist

    # Alias lookup: single known label keyword (e.g. "Мелодия", "Зион")
    if not filters:
        text_clean = lower.strip(".,!?«» ")
        for alias_key, alias_val in LABEL_ALIASES.items():
            if alias_key in text_clean:
                filters["label"] = alias_val
                break

    # Fallback: bare words → artist only for very short clean queries
    if not filters:
        stop = {
            "выгрузи", "выгрузка", "выгрузку", "выгружай", "выгрузить",
            "экспорт", "экспортируй", "сделай", "дай", "покажи", "скинь",
            "треки", "трек", "треков", "трека", "треке",
            "музыку", "музыка", "репертуар", "исполнитель",
            "группа", "из", "базы", "за", "год", "года", "период", "все", "мне",
            "пожалуйста", "список", "по", "полный", "полное", "полностью",
            "файл", "да", "нет", "всё", "отлично", "хорошо", "ладно",
            "хочу", "нужно", "нужен", "нужны", "можешь", "можно",
            "дайте", "отчёт", "отчет", "и", "а", "но", "в", "на", "для",
            "excel", "xlsx", "полная", "весь", "всю", "фирме", "фирма",
            "компании", "компания", "лейбле", "лейбла",
            "давай", "давайте", "конечно", "окей", "ок", "угу", "ага",
            "посмотри", "проверь", "напомни", "есть", "ли", "у", "нас",
            "что", "где", "когда", "какой", "какие", "сколько",
            "тебя", "тебе", "тобой", "твои", "твой", "твоя", "твоей",
            "базе", "базу", "базы", "базой",
            "каталоге", "каталогу", "каталог", "каталога",
            "реестре", "реестра", "реестру",
            "знаешь", "знаете", "имеется", "имеются",
            "наш", "наша", "наше", "наши",
        }
        words = [_strip_quotes(w.strip(".,!?")) for w in text.split()
                 if w.lower().strip(".,!?«» ") not in stop]
        words = [w for w in words if w]
        if 1 <= len(words) <= 4:
            filters["artist"] = " ".join(words)

    return filters


def fetch_tracks(filters: dict, limit: int = 2000) -> list[dict]:
    """Query Supabase tracks with the given filters. Returns list of track dicts."""
    params: dict = {
        "select": "id,title,artist,album,label,music_author,lyrics_author,release_date,genre_1,link",
        "order": "artist.asc,release_date.asc",
        "limit": str(limit),
    }

    conditions = []

    if filters.get("artist"):
        a = filters["artist"].replace("*", "").replace("(", "").replace(")", "")
        conditions.append(f"artist.ilike.*{a}*")

    if filters.get("label"):
        lb = filters["label"].replace("*", "")
        conditions.append(f"label.ilike.*{lb}*")

    if filters.get("genre"):
        g = filters["genre"].replace("*", "")
        conditions.append(f"genre_1.ilike.*{g}*")

    if conditions:
        params["or"] = f"({','.join(conditions)})"

    r = httpx.get(f"{_sb_base()}/rest/v1/tracks", headers=_sb_headers(), params=params, timeout=15)
    r.raise_for_status()
    rows = r.json()

    # Post-filter by year if specified
    if filters.get("year_from") is not None:
        yf = filters["year_from"]
        yt = filters.get("year_to", yf)
        filtered = []
        for row in rows:
            rd = row.get("release_date") or ""
            m = re.search(r"\b((?:19|20)\d{2})\b", str(rd))
            if m and yf <= int(m.group(1)) <= yt:
                filtered.append(row)
        return filtered

    return rows


def build_excel(tracks: list[dict], title: str = "Каталог SYNC LAB") -> bytes:
    """Build an .xlsx workbook from track list and return as bytes."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Треки"

    # Header style
    header_fill = PatternFill(start_color="1F2D3D", end_color="1F2D3D", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=11)

    columns = [
        ("№", 5),
        ("Исполнитель", 30),
        ("Название", 40),
        ("Год", 8),
        ("Лейбл", 25),
        ("Альбом", 30),
        ("Автор музыки", 25),
        ("Автор текста", 25),
        ("Жанр", 15),
        ("Ссылка", 40),
    ]

    for col_idx, (col_name, col_width) in enumerate(columns, 1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[cell.column_letter].width = col_width

    ws.row_dimensions[1].height = 20

    def extract_year(s):
        if not s:
            return ""
        m = re.search(r"\b((?:19|20)\d{2})\b", str(s))
        return m.group(1) if m else str(s)

    for i, t in enumerate(tracks, 1):
        row = [
            i,
            t.get("artist") or "",
            t.get("title") or "",
            extract_year(t.get("release_date")),
            t.get("label") or "",
            t.get("album") or "",
            t.get("music_author") or "",
            t.get("lyrics_author") or "",
            t.get("genre_1") or "",
            t.get("link") or "",
        ]
        for col_idx, value in enumerate(row, 1):
            ws.cell(row=i + 1, column=col_idx, value=value)

    # Freeze header row
    ws.freeze_panes = "A2"

    # Add a summary sheet
    ws_info = wb.create_sheet("Инфо")
    ws_info["A1"] = "Выгрузка"
    ws_info["B1"] = title
    ws_info["A2"] = "Треков"
    ws_info["B2"] = len(tracks)
    ws_info["A3"] = "Источник"
    ws_info["B3"] = "SYNC LAB / Supabase"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_catalog(query_text: str) -> tuple[bytes, str, int]:
    """Parse query, fetch tracks, build Excel.

    Returns (xlsx_bytes, filename, track_count).
    """
    filters = parse_export_query(query_text)
    tracks = fetch_tracks(filters)

    # Build a human-readable filename
    parts = []
    if filters.get("artist"):
        safe = re.sub(r"[^a-zA-Zа-яёА-ЯЁ0-9 ]", "", filters["artist"])[:30].strip()
        parts.append(safe)
    if filters.get("label"):
        parts.append(filters["label"][:15])
    if filters.get("year_from"):
        yf = filters["year_from"]
        yt = filters.get("year_to", yf)
        parts.append(str(yf) if yf == yt else f"{yf}-{yt}")
    suffix = "_".join(parts).replace(" ", "_") or "catalog"
    filename = f"SYNCLAB_{suffix}.xlsx"

    title_str = " | ".join(str(v) for v in filters.values() if v) or "Полный каталог"
    xlsx_bytes = build_excel(tracks, title=f"SYNC LAB — {title_str}")
    return xlsx_bytes, filename, len(tracks)
