"""Step 1: Parse book text into chapters CSV."""
import csv, re
from pathlib import Path

REPL = chr(0xFFFD)
CHAPTER_RE = re.compile(
    r"^(ПРОЛОГ|ОТ АВТОРА|С чего все началось|Зачем я делюсь опытом"
    r"|Слова благодарности|ПЕРВАЯ ЧАСТЬ|ВТОРАЯ ЧАСТЬ|ТРЕТЬЯ ЧАСТЬ"
    r"|ЗАКЛЮЧИТЕЛЬНАЯ ЧАСТЬ|ЗАКЛЮЧЕНИЕ|ГЛАВА\s+\d+\.)",
    re.I,
)

def clean(page):
    out = []
    for line in page.split("\n"):
        if line.count(REPL) >= 3: continue
        if re.search(r"\.{4,}\s*\d+\s*$", line): continue
        if re.fullmatch(r"\s*\d{1,3}\s*", line): continue
        if len(line) > 10 and re.search(r"([А-ЯЁA-Z]\s){5,}", line): continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()

raw = Path("data/book_muzredaktor.txt").read_text("utf-8")
cur_t, cur_p, chapters = "Вступление", [], []
for pg in raw.split("\x0c"):
    c = clean(pg)
    if not c: continue
    fl = next((l.strip() for l in c.split("\n") if l.strip()), "")
    if CHAPTER_RE.match(fl) and REPL not in fl:
        if cur_p: chapters.append((cur_t, "\n\n".join(cur_p)))
        cur_t, cur_p = fl[:100], [c]
    else:
        cur_p.append(c)
if cur_p: chapters.append((cur_t, "\n\n".join(cur_p)))

CHUNK = 3000
rows = []
for title, content in chapters:
    if REPL in title or len(content) < 300: continue
    if len(content) <= CHUNK:
        rows.append((title, content))
    else:
        start, part = 0, 1
        while start < len(content):
            end = min(start + CHUNK, len(content))
            if end < len(content):
                nl = content.rfind("\n\n", start, end)
                if nl > start + CHUNK // 2: end = nl
            rows.append((f"{title} [ч.{part}]", content[start:end].strip()))
            start = end - 200; part += 1

out = Path("data/book_chapters.csv")
with open(out, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["chapter", "content", "char_count"])
    for t, c in rows:
        w.writerow([t, c, len(c)])

print(f"Saved {len(rows)} chunks to {out}")
