"""
Import RAO registry xlsx (322,689 works) into SQLite.
Usage: python scripts/import_rao.py
"""

import sqlite3
import time
from pathlib import Path

RAO_XLSX = Path("/Users/synclabpro/Desktop/Projects/syncoteca-office/rao_result_322,689.xlsx")
DB_PATH  = Path(__file__).parent.parent / "data" / "syncoteca.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS rao_registry (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    genre           TEXT,
    lyrics_author   TEXT,
    composer        TEXT,
    other_authors   TEXT,
    found_via       TEXT
);

CREATE INDEX IF NOT EXISTS idx_rao_title    ON rao_registry(title);
CREATE INDEX IF NOT EXISTS idx_rao_composer ON rao_registry(composer);
CREATE INDEX IF NOT EXISTS idx_rao_lyrics   ON rao_registry(lyrics_author);
CREATE INDEX IF NOT EXISTS idx_rao_genre    ON rao_registry(genre);
"""

BATCH = 5000


def clean(val) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s if s and s.lower() != "none" else None


def run() -> None:
    import pandas as pd

    if not RAO_XLSX.exists():
        print(f"File not found: {RAO_XLSX}")
        return

    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}. Run import_sql.py first.")
        return

    print(f"\n=== Импорт реестра РАО ===")
    print(f"Файл: {RAO_XLSX} ({RAO_XLSX.stat().st_size // 1_048_576} MB)")
    print(f"БД:   {DB_PATH}\n")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-128000")

    # Drop and recreate rao table for clean import
    conn.execute("DROP TABLE IF EXISTS rao_registry")
    conn.executescript(SCHEMA)
    conn.commit()
    print("Схема создана.")

    print("Читаю xlsx... (может занять 1–2 минуты)")
    t0 = time.time()

    df = pd.read_excel(
        RAO_XLSX,
        sheet_name="Реестр РАО",
        engine="openpyxl",
        dtype=str,
        keep_default_na=False,
    )
    df.columns = ["title", "genre", "lyrics_author", "composer", "other_authors", "found_via"]

    read_time = time.time() - t0
    print(f"Прочитано {len(df):,} строк за {read_time:.1f} с\n")

    # Normalize: replace empty strings and "None" strings with None
    def norm(s):
        s = str(s).strip() if s else ""
        return s if s and s.lower() != "none" else None

    print("Импортирую в SQLite батчами...")
    total = 0
    t1 = time.time()

    for start in range(0, len(df), BATCH):
        chunk = df.iloc[start:start + BATCH]
        rows = [
            (
                norm(r.title),
                norm(r.genre),
                norm(r.lyrics_author),
                norm(r.composer),
                norm(r.other_authors),
                norm(r.found_via),
            )
            for r in chunk.itertuples()
            if norm(r.title)  # skip rows with no title
        ]
        conn.executemany(
            "INSERT INTO rao_registry (title, genre, lyrics_author, composer, other_authors, found_via) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        total += len(rows)
        pct = (start + len(chunk)) / len(df) * 100
        elapsed = time.time() - t1
        print(f"  {pct:5.1f}%  {total:,} rows  ({elapsed:.0f}s)", end="\r")

    print(f"\n\nИмпортировано: {total:,} записей за {time.time()-t1:.1f} с")

    # Stats
    genres = conn.execute(
        "SELECT genre, COUNT(*) c FROM rao_registry GROUP BY genre ORDER BY c DESC LIMIT 10"
    ).fetchall()
    print("\nТоп жанров:")
    for g, c in genres:
        print(f"  {g or 'н/у':<50} {c:>6}")

    total_db = conn.execute("SELECT COUNT(*) FROM rao_registry").fetchone()[0]
    print(f"\nВсего в rao_registry: {total_db:,}")
    conn.close()
    print(f"\nГотово. БД: {DB_PATH}")


if __name__ == "__main__":
    run()
