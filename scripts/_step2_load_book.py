"""Step 2: Load book chapters CSV into SQLite with FTS5."""
import csv, sqlite3
from pathlib import Path

CSV_PATH = Path("data/book_chapters.csv")
DB_PATH  = Path("data/syncoteca.db")

conn = sqlite3.connect(DB_PATH)
conn.execute("DROP TABLE IF EXISTS book_fts")
conn.execute("DROP TRIGGER IF EXISTS book_chapters_ai")
conn.execute("DROP TABLE IF EXISTS book_chapters")
conn.execute("""CREATE TABLE book_chapters(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_title TEXT, author TEXT, chapter TEXT, content TEXT, char_count INTEGER
)""")
conn.execute("CREATE VIRTUAL TABLE book_fts USING fts5(chapter, content, content=book_chapters, content_rowid=id)")
conn.commit()

with open(CSV_PATH, encoding="utf-8") as f:
    reader = csv.DictReader(f)
    rows = [(r["chapter"], r["content"], int(r["char_count"])) for r in reader]

conn.executemany(
    "INSERT INTO book_chapters(book_title,author,chapter,content,char_count) VALUES('Музыкальный редактор','Денис Шарко',?,?,?)",
    rows,
)
conn.commit()
print(f"Inserted {len(rows)} rows")

# Bulk rebuild FTS index
conn.execute("INSERT INTO book_fts(book_fts) VALUES('rebuild')")
conn.commit()
print(f"FTS rebuilt: {conn.execute('SELECT COUNT(*) FROM book_fts').fetchone()[0]}")

# Quick test
print("\n--- Тест: синхронизация ---")
for r in conn.execute("SELECT chapter, snippet(book_fts,1,'>>','<<','...',20) FROM book_fts WHERE book_fts MATCH 'синхронизация' LIMIT 2"):
    print(f"  [{r[0][:35]}] {r[1][:90]}")
print("\n--- Тест: договор ---")
for r in conn.execute("SELECT chapter, snippet(book_fts,1,'>>','<<','...',20) FROM book_fts WHERE book_fts MATCH 'договор' LIMIT 2"):
    print(f"  [{r[0][:35]}] {r[1][:90]}")

conn.close()
print("OK")
