"""
Import "Музыкальный редактор" (Денис Шарко) into SQLite with FTS5 full-text search.
Splits by chapters via PDF page-break markers (\x0c).
Usage: python scripts/import_book.py
"""

import re
import sqlite3
from pathlib import Path

PDF_PATH  = Path("/Users/synclabpro/Desktop/Projects/syncoteca-office/музыкальный редактор.pdf")
TEXT_PATH = Path(__file__).parent.parent / "data" / "book_muzredaktor.txt"
DB_PATH   = Path(__file__).parent.parent / "data" / "syncoteca.db"

BOOK_META = {
    "title":  "Музыкальный редактор: практическое руководство по sync licensing",
    "author": "Денис Шарко",
}

# Chapter heading patterns — match first line of a page
CHAPTER_HEADING_RE = re.compile(
    r"^("
    r"ПРОЛОГ|"
    r"ОТ АВТОРА|"
    r"С чего все началось|"
    r"Зачем я делюсь опытом|"
    r"Слова благодарности|"
    r"ПЕРВАЯ ЧАСТЬ|"
    r"ВТОРАЯ ЧАСТЬ|"
    r"ТРЕТЬЯ ЧАСТЬ|"
    r"ЗАКЛЮЧИТЕЛЬНАЯ ЧАСТЬ|"
    r"ЗАКЛЮЧЕНИЕ|"
    r"ГЛАВА\s+\d+\."
    r")",
    re.IGNORECASE,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS book_chapters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_title  TEXT NOT NULL,
    author      TEXT NOT NULL,
    chapter     TEXT NOT NULL,
    content     TEXT NOT NULL,
    char_count  INTEGER
);

CREATE VIRTUAL TABLE IF NOT EXISTS book_fts
USING fts5(chapter, content, content=book_chapters, content_rowid=id);

CREATE TRIGGER IF NOT EXISTS book_chapters_ai AFTER INSERT ON book_chapters BEGIN
    INSERT INTO book_fts(rowid, chapter, content) VALUES (new.id, new.chapter, new.content);
END;
"""

CHUNK_SIZE    = 3000
CHUNK_OVERLAP = 200


def clean_page(page: str) -> str:
    lines = []
    for line in page.split('\n'):
        # Drop lines with many garbage/replacement chars (TOC dot leaders)
        garbage = sum(1 for c in line if ord(c) > 0xFFFD or c == '�')
        if garbage >= 3:
            continue
        # Drop TOC dot-leader lines: "Title ........ 22"
        if re.search(r'\.{4,}\s*\d+\s*$', line):
            continue
        # Drop standalone page numbers
        if re.fullmatch(r'\s*\d{1,3}\s*', line):
            continue
        # Drop running headers (evenly spaced UPPERCASE, like "г Л А В А   1")
        if len(line) > 10 and re.search(r'([А-ЯЁA-Z]\s){5,}', line):
            continue
        lines.append(line)
    result = '\n'.join(lines)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()


def split_chapters(raw_text: str) -> list[tuple[str, str]]:
    """Split book by PDF page breaks (\x0c), group pages into chapters."""
    pages = raw_text.split('\x0c')
    chapters: list[tuple[str, list[str]]] = []
    current_title = "Вступление / Оглавление"
    current_pages: list[str] = []

    for page in pages:
        clean = clean_page(page)
        if not clean:
            continue
        # Get first non-empty line
        first_line = next((l.strip() for l in clean.split('\n') if l.strip()), "")
        m = CHAPTER_HEADING_RE.match(first_line)
        if m:
            if current_pages:
                chapters.append((current_title, current_pages))
            current_title = first_line[:100]
            current_pages = [clean]
        else:
            current_pages.append(clean)

    if current_pages:
        chapters.append((current_title, current_pages))

    return [
        (title, '\n\n'.join(pages))
        for title, pages in chapters
        if len('\n\n'.join(pages)) > 200
    ]


def chunk_content(title: str, content: str) -> list[tuple[str, str]]:
    if len(content) <= CHUNK_SIZE:
        return [(title, content)]
    chunks, start, part = [], 0, 1
    while start < len(content):
        end = min(start + CHUNK_SIZE, len(content))
        if end < len(content):
            nl = content.rfind('\n\n', start, end)
            if nl > start + CHUNK_SIZE // 2:
                end = nl
        chunks.append((f"{title} [часть {part}]", content[start:end].strip()))
        start = end - CHUNK_OVERLAP
        part += 1
    return chunks


def run() -> None:
    print(f"\n=== Импорт книги «Музыкальный редактор» ===")
    print(f"БД: {DB_PATH}\n")

    if not DB_PATH.exists():
        print("БД не найдена. Run import_sql.py first.")
        return

    if TEXT_PATH.exists():
        print(f"Читаю кэш: {TEXT_PATH}")
        raw_text = TEXT_PATH.read_text(encoding="utf-8")
    elif PDF_PATH.exists():
        from pdfminer.high_level import extract_text
        print(f"Читаю PDF: {PDF_PATH}")
        raw_text = extract_text(str(PDF_PATH))
        TEXT_PATH.write_text(raw_text, encoding="utf-8")
    else:
        print("PDF и кэш не найдены.")
        return

    print(f"Текст: {len(raw_text):,} символов\n")

    print("Разбиваю на главы...")
    chapters = split_chapters(raw_text)
    print(f"Найдено разделов: {len(chapters)}")
    for t, c in chapters:
        print(f"  [{len(c):>6} chars] {t[:70]}")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    conn.execute("DROP TABLE IF EXISTS book_fts")
    conn.execute("DROP TRIGGER IF EXISTS book_chapters_ai")
    conn.execute("DROP TABLE IF EXISTS book_chapters")
    conn.executescript(SCHEMA)
    conn.commit()

    print("\nЗаписываю в SQLite + FTS5...")
    total = 0
    for title, content in chapters:
        for chunk_title, chunk_text in chunk_content(title, content):
            conn.execute(
                "INSERT INTO book_chapters (book_title, author, chapter, content, char_count) "
                "VALUES (?, ?, ?, ?, ?)",
                (BOOK_META["title"], BOOK_META["author"], chunk_title, chunk_text, len(chunk_text)),
            )
            total += 1
    conn.commit()

    fts_count = conn.execute("SELECT COUNT(*) FROM book_fts").fetchone()[0]
    print(f"Чанков сохранено:  {total}")
    print(f"FTS-индекс:        {fts_count}")

    print("\n--- Тест: 'роялти' ---")
    for r in conn.execute(
        "SELECT chapter, snippet(book_fts, 1, '**', '**', '...', 30) FROM book_fts WHERE book_fts MATCH 'роялти' LIMIT 3"
    ):
        print(f"  [{r[0][:40]}] {r[1][:120]}")

    print("\n--- Тест: 'правообладатель' ---")
    for r in conn.execute(
        "SELECT chapter, snippet(book_fts, 1, '**', '**', '...', 30) FROM book_fts WHERE book_fts MATCH 'правообладатель' LIMIT 3"
    ):
        print(f"  [{r[0][:40]}] {r[1][:120]}")

    print("\n--- Тест: 'договор' ---")
    for r in conn.execute(
        "SELECT chapter, snippet(book_fts, 1, '**', '**', '...', 30) FROM book_fts WHERE book_fts MATCH 'договор' LIMIT 3"
    ):
        print(f"  [{r[0][:40]}] {r[1][:120]}")

    conn.close()
    print(f"\nГотово.")


if __name__ == "__main__":
    run()
