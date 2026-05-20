#!/usr/bin/env python3
"""
Добавить знания из PDF/DOCX/TXT в память агента.

Использование:
  python scripts/ingest_doc.py <агент> <файл> [--note "заметка"]

Агенты: marina, ekaterina, ksusha, sasha, biz_dev, developer

Примеры:
  python scripts/ingest_doc.py marina договор_НДС.pdf
  python scripts/ingest_doc.py ekaterina правообладатели.docx --note "контакты 2026"
  python scripts/ingest_doc.py marina налоги.txt

Файл также можно положить в data/knowledge/docs/ и указать только имя:
  python scripts/ingest_doc.py marina договор.pdf
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent

# Load .env from project root
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=True)
except ImportError:
    pass
KNOWLEDGE_DIR = ROOT / "data" / "knowledge"
DOCS_DIR = KNOWLEDGE_DIR / "docs"

AGENT_ALIASES = {
    "марина": "marina",
    "marina": "marina",
    "accountant": "marina",
    "бухгалтер": "marina",
    "катя": "ekaterina",
    "екатерина": "ekaterina",
    "ekaterina": "ekaterina",
    "license": "ekaterina",
    "ксюша": "ksusha",
    "ksusha": "ksusha",
    "lawyer": "ksusha",
    "саша": "sasha",
    "sasha": "sasha",
    "content": "sasha",
    "biz_dev": "biz_dev",
    "developer": "developer",
}


def resolve_agent(name: str) -> str:
    key = name.lower().strip()
    resolved = AGENT_ALIASES.get(key)
    if not resolved:
        print(f"Неизвестный агент: {name}")
        print(f"Доступные: {', '.join(sorted(set(AGENT_ALIASES.values())))}")
        sys.exit(1)
    return resolved


def resolve_file(path_str: str) -> Path:
    p = Path(path_str)
    if p.exists():
        return p.resolve()
    # Try in docs/ folder
    candidate = DOCS_DIR / path_str
    if candidate.exists():
        return candidate.resolve()
    print(f"Файл не найден: {path_str}")
    print(f"Искал в: {Path(path_str).resolve()} и {candidate}")
    sys.exit(1)


def extract_pdf(path: Path) -> str:
    try:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    text_parts.append(t)
        text = "\n\n".join(text_parts)
        if len(text.strip()) > 100:
            return text
    except Exception:
        pass

    # Fallback: pdfminer
    try:
        from pdfminer.high_level import extract_text
        text = extract_text(str(path))
        if len(text.strip()) > 100:
            return text
    except Exception:
        pass

    # Last resort: OCR via OpenAI Vision (for scanned PDFs)
    return _ocr_via_openai(path)


def _ocr_via_openai(path: Path) -> str:
    """OCR scanned PDF pages using OpenAI GPT-4o Vision."""
    import os, base64, tempfile
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        print("⚠️  PDF отсканирован (нет текста). Для OCR нужен OPENAI_API_KEY в .env")
        return ""

    try:
        from openai import OpenAI
        import fitz  # pymupdf — try
    except ImportError:
        try:
            import fitz
        except ImportError:
            print("⚠️  PDF отсканирован. Установи pymupdf: pip install pymupdf")
            return ""

    print("   🔍 Скан-документ — запускаю OCR через GPT-4o Vision...")
    client = OpenAI(api_key=api_key)
    doc = fitz.open(str(path))
    all_text = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        pix = page.get_pixmap(dpi=200)
        img_bytes = pix.tobytes("png")
        b64 = base64.b64encode(img_bytes).decode()

        print(f"   стр. {page_num + 1}/{len(doc)}...", end=" ", flush=True)
        response = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Перепиши весь печатный текст с этого изображения. "
                            "Сохраняй структуру, нумерацию и абзацы. "
                            "Только текст, без комментариев."
                        )
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"}
                    }
                ]
            }]
        )
        page_text = response.choices[0].message.content
        all_text.append(f"--- Страница {page_num + 1} ---\n{page_text}")
        print("✓")

    doc.close()
    return "\n\n".join(all_text)


def extract_docx(path: Path) -> str:
    import docx
    doc = docx.Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def extract_txt(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf(path)
    elif suffix in (".docx", ".doc"):
        return extract_docx(path)
    elif suffix in (".txt", ".md"):
        return extract_txt(path)
    else:
        print(f"Неподдерживаемый формат: {suffix}. Поддерживаются: .pdf .docx .txt .md")
        sys.exit(1)


def save_to_knowledge(agent: str, text: str, source: str, note: str) -> None:
    json_path = KNOWLEDGE_DIR / f"{agent}.json"
    entries = []
    if json_path.exists():
        try:
            entries = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    label = f"[из файла: {source}]" + (f" — {note}" if note else "")
    entry = {"ts": ts, "text": f"{label}\n\n{text}"}
    entries.insert(0, entry)

    json_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ [{ts}] Сохранено в знания {agent} ({len(text)} символов из {source})")


def main():
    parser = argparse.ArgumentParser(
        description="Загрузить документ в память агента Syncoteca"
    )
    parser.add_argument("agent", help="Имя агента (marina, ekaterina, ksusha, sasha, biz_dev, developer)")
    parser.add_argument("file", help="Путь к файлу (.pdf, .docx, .txt) или имя файла в data/knowledge/docs/")
    parser.add_argument("--note", default="", help="Краткая заметка о документе")
    parser.add_argument("--preview", action="store_true", help="Показать извлечённый текст без сохранения")
    args = parser.parse_args()

    agent = resolve_agent(args.agent)
    path = resolve_file(args.file)

    print(f"📄 Читаю: {path.name}")
    text = extract_text(path)

    if not text.strip():
        print("Текст не извлечён — файл пустой или защищён.")
        sys.exit(1)

    print(f"   Извлечено: {len(text)} символов")

    if args.preview:
        print("\n--- ПРЕДПРОСМОТР (первые 1000 символов) ---")
        print(text[:1000])
        print("---")
        print("Для сохранения запусти без --preview")
        return

    save_to_knowledge(agent, text, path.name, args.note)
    print(f"   Агент загрузит при следующем запуске бота.")


if __name__ == "__main__":
    main()
