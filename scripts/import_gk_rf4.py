"""Import ГК РФ Часть 4 PDF into SQLite with FTS5, split by article."""
import re
import sqlite3
from pathlib import Path
from pdfminer.high_level import extract_text

PDF_PATH = Path("/Users/synclabpro/Desktop/Projects/syncoteca-office/GK_RF_4.pdf")
DB_PATH = Path("data/syncoteca.db")
CACHE = Path("data/gk_rf4.txt")

# Extract text (cache to avoid re-running)
if not CACHE.exists():
    print("Extracting PDF text…")
    text = extract_text(str(PDF_PATH))
    CACHE.write_text(text, encoding="utf-8")
    print(f"Extracted {len(text)} chars → {CACHE}")
else:
    text = CACHE.read_text("utf-8")
    print(f"Loaded from cache: {len(text)} chars")

# Split by article: "Статья NNNN."
ARTICLE_RE = re.compile(r"(Статья\s+\d{4}[\.\-][^\n]{0,120})", re.MULTILINE)

chunks = []
parts = ARTICLE_RE.split(text)

# parts: [preamble, "Статья NNNN.", body, "Статья NNNN.", body, ...]
if len(parts) > 1:
    # Skip preamble (parts[0]) if short
    i = 1
    while i < len(parts) - 1:
        header = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        full = f"{header}\n\n{body}".strip()
        if len(full) > 50:
            # Extract article number
            m = re.search(r"Статья\s+(\d+)", header)
            art_num = int(m.group(1)) if m else 0
            chunks.append((art_num, header[:120], full))
        i += 2
else:
    # Fallback: split by pages / 3000-char chunks
    CHUNK = 3000
    start, part = 0, 1
    while start < len(text):
        end = min(start + CHUNK, len(text))
        chunks.append((part, f"Раздел {part}", text[start:end].strip()))
        start = end - 200
        part += 1

print(f"Split into {len(chunks)} articles/chunks")

# Load into SQLite
conn = sqlite3.connect(DB_PATH)
conn.execute("DROP TABLE IF EXISTS gk_rf4_fts")
conn.execute("DROP TABLE IF EXISTS gk_rf4")
conn.execute("""CREATE TABLE gk_rf4(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_num INTEGER,
    title TEXT,
    content TEXT,
    char_count INTEGER
)""")
conn.execute("""CREATE VIRTUAL TABLE gk_rf4_fts
    USING fts5(title, content, content=gk_rf4, content_rowid=id)""")
conn.commit()

conn.executemany(
    "INSERT INTO gk_rf4(article_num, title, content, char_count) VALUES(?,?,?,?)",
    [(n, t, c, len(c)) for n, t, c in chunks],
)
conn.commit()
print(f"Inserted {len(chunks)} rows")

conn.execute("INSERT INTO gk_rf4_fts(gk_rf4_fts) VALUES('rebuild')")
conn.commit()
print(f"FTS rebuilt: {conn.execute('SELECT COUNT(*) FROM gk_rf4_fts').fetchone()[0]}")

# Quick tests
print("\n--- Тест: синхронизация ---")
for r in conn.execute(
    "SELECT title, snippet(gk_rf4_fts,1,'>>','<<','...',20) FROM gk_rf4_fts WHERE gk_rf4_fts MATCH ? LIMIT 3",
    ("синхронизация",)
):
    print(f"  [{r[0][:50]}] {r[1][:90]}")

print("\n--- Тест: лицензионный договор ---")
for r in conn.execute(
    "SELECT title, snippet(gk_rf4_fts,1,'>>','<<','...',20) FROM gk_rf4_fts WHERE gk_rf4_fts MATCH ? LIMIT 3",
    ("лицензионный договор",)
):
    print(f"  [{r[0][:50]}] {r[1][:90]}")

print("\n--- Тест: авторское право ---")
for r in conn.execute(
    "SELECT title, snippet(gk_rf4_fts,1,'>>','<<','...',20) FROM gk_rf4_fts WHERE gk_rf4_fts MATCH ? LIMIT 3",
    ("авторское право",)
):
    print(f"  [{r[0][:50]}] {r[1][:90]}")

conn.close()
print("\nOK")
