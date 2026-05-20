"""
Import PostgreSQL dump files into local SQLite database.
Usage: python scripts/import_sql.py
"""

import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "data" / "syncoteca.db"

SOURCES = {
    "contracts":      Path("/Users/synclabpro/Desktop/Projects/syncoteca-office/contracts_rows.sql"),
    "musical_works":  Path("/Users/synclabpro/Desktop/Projects/syncoteca-office/musical_works_rows.sql"),
    "tracks":         Path("/Users/synclabpro/Desktop/Projects/syncoteca-office/tracks_rows_yandex_music.sql"),
    "labels":         Path("/Users/synclabpro/Desktop/Projects/syncoteca-office/labels_rows.sql"),
    "contacts":       Path("/Users/synclabpro/Desktop/Projects/syncoteca-office/contacts_rows.sql"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
    id                  INTEGER PRIMARY KEY,
    created_at          TEXT,
    licensee_name       TEXT,
    project_name        TEXT,
    project_type        TEXT,
    license_term_start  TEXT,
    license_term_end    TEXT,
    territory           TEXT,
    media_channels      TEXT,
    original_filename   TEXT,
    licensor_name       TEXT,
    license_cost        TEXT
);

CREATE TABLE IF NOT EXISTS musical_works (
    id                INTEGER PRIMARY KEY,
    created_at        TEXT,
    contract_id       INTEGER,
    title             TEXT,
    performer         TEXT,
    lyrics_authors    TEXT,
    music_authors     TEXT,
    usage_details     TEXT,
    promo_details     TEXT,
    incontext_details TEXT,
    cost              TEXT,
    FOREIGN KEY (contract_id) REFERENCES contracts(id)
);

CREATE TABLE IF NOT EXISTS tracks (
    id                      INTEGER PRIMARY KEY,
    title                   TEXT,
    artist                  TEXT,
    album                   TEXT,
    duration                TEXT,
    release_date            TEXT,
    label                   TEXT,
    link                    TEXT,
    lyrics_author           TEXT,
    music_author            TEXT,
    created_at              TEXT,
    genre_1                 TEXT,
    genre_2                 TEXT,
    genre_3                 TEXT,
    genre_4                 TEXT,
    genre_5                 TEXT,
    parent_link             TEXT,
    source_type             TEXT,
    yandex_id               TEXT,
    album_processed         INTEGER,
    chart_position          INTEGER,
    music_author_canonical  TEXT,
    lyrics_author_canonical TEXT,
    author_check_status     TEXT
);

CREATE TABLE IF NOT EXISTS labels (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    active      INTEGER,
    parent      TEXT,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY,
    owner_type      TEXT,
    email           TEXT,
    created_at      TEXT,
    additional_info TEXT,
    black_list      INTEGER DEFAULT 0,
    first_name      TEXT,
    last_name       TEXT,
    patronymic      TEXT
);

CREATE INDEX IF NOT EXISTS idx_works_contract   ON musical_works(contract_id);
CREATE INDEX IF NOT EXISTS idx_contracts_licensee ON contracts(licensee_name);
CREATE INDEX IF NOT EXISTS idx_contracts_type   ON contracts(project_type);
CREATE INDEX IF NOT EXISTS idx_works_title      ON musical_works(title);
CREATE INDEX IF NOT EXISTS idx_works_performer  ON musical_works(performer);
CREATE INDEX IF NOT EXISTS idx_tracks_artist    ON tracks(artist);
CREATE INDEX IF NOT EXISTS idx_tracks_title     ON tracks(title);
CREATE INDEX IF NOT EXISTS idx_tracks_label     ON tracks(label);
CREATE INDEX IF NOT EXISTS idx_tracks_genre     ON tracks(genre_1, genre_2, genre_3);
CREATE INDEX IF NOT EXISTS idx_tracks_author    ON tracks(music_author_canonical);
CREATE INDEX IF NOT EXISTS idx_contacts_email   ON contacts(email);
CREATE INDEX IF NOT EXISTS idx_contacts_owner   ON contacts(owner_type);
CREATE INDEX IF NOT EXISTS idx_labels_name      ON labels(name);
"""


def preprocess(sql_text: str) -> str:
    """Adapt PostgreSQL INSERT syntax to SQLite."""
    # Remove schema prefix: "public"."table" -> "table"
    sql_text = re.sub(r'"public"\."(\w+)"', r'"\1"', sql_text)

    # Normalize column names
    sql_text = sql_text.replace('"Usage_details"',    '"usage_details"')
    sql_text = sql_text.replace('"Promo_details"',    '"promo_details"')
    sql_text = sql_text.replace('"InContext_details"', '"incontext_details"')
    sql_text = sql_text.replace('"adittional_info"',  '"additional_info"')  # typo in source
    sql_text = sql_text.replace('"black_List"',       '"black_list"')

    # SQLite has no boolean literals — replace bare true/false with 1/0
    # Only outside of string literals (safe: these appear as column values, not in strings)
    sql_text = re.sub(r'\btrue\b',  '1', sql_text)
    sql_text = re.sub(r'\bfalse\b', '0', sql_text)

    return sql_text


def import_file(conn: sqlite3.Connection, table: str, sql_path: Path) -> int:
    print(f"  Reading {sql_path.name} ({sql_path.stat().st_size // 1024} KB)...")
    raw = sql_path.read_text(encoding="utf-8")
    sql = preprocess(raw).strip().rstrip(";")

    if not sql.upper().startswith("INSERT"):
        print(f"  WARNING: no INSERT in {sql_path.name}")
        return 0

    try:
        conn.execute(sql)
        conn.commit()
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error as e:
        print(f"  ERROR [{table}]: {e}")
        print(f"  SQL preview: {sql[:400]}")
        raise


def run() -> None:
    print(f"\n=== Синкотека — Import SQL to SQLite ===")
    print(f"DB: {DB_PATH}\n")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
        print("Old DB removed — fresh import.\n")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA cache_size=-64000")   # 64 MB cache for large tracks import

    print("Creating schema...")
    conn.executescript(SCHEMA)

    results = {}
    for table, path in SOURCES.items():
        if not path.exists():
            print(f"  SKIP {table}: file not found at {path}")
            results[table] = 0
            continue
        print(f"\nImporting {table}...")
        n = import_file(conn, table, path)
        results[table] = n
        print(f"  ✓ {n} rows")

    print("\n" + "=" * 40)
    print("=== ИТОГ ===")
    total = 0
    for table, n in results.items():
        print(f"  {table:<20} {n:>5} записей")
        total += n
    print(f"  {'ИТОГО':<20} {total:>5}")

    # Samples
    print("\n--- Контракты (3 примера) ---")
    for r in conn.execute("SELECT id, licensee_name, project_name, project_type FROM contracts ORDER BY id DESC LIMIT 3"):
        print(f"  #{r[0]} | {r[1]} | {r[2]} | {r[3]}")

    print("\n--- Треки с Яндекс.Музыки (3 примера) ---")
    for r in conn.execute("SELECT id, title, artist, label, genre_1 FROM tracks LIMIT 3"):
        print(f"  #{r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]}")

    print("\n--- Лейблы (3 примера) ---")
    for r in conn.execute("SELECT id, name, active, parent FROM labels LIMIT 3"):
        print(f"  {r[0]} | {r[1]} | active={r[2]} | parent={r[3]}")

    print("\n--- Контакты (3 примера) ---")
    for r in conn.execute("SELECT id, first_name, last_name, email, owner_type FROM contacts LIMIT 3"):
        print(f"  #{r[0]} | {r[1]} {r[2]} | {r[3]} | {r[4]}")

    conn.close()
    print(f"\nБД сохранена: {DB_PATH}")


if __name__ == "__main__":
    run()
