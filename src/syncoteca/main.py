"""
SYNC LAB — Multi-Agent Office entry point.

Usage:
    python -m syncoteca.main                          # interactive menu
    python -m syncoteca.main --crew licensing         # run licensing crew
    python -m syncoteca.main --crew biz_dev           # run BD crew
    python -m syncoteca.main --crew content           # run content crew
    python -m syncoteca.main --crew full              # full hierarchical crew
"""

import argparse
import os
import sys
from dotenv import load_dotenv

load_dotenv(override=True)


def check_env() -> bool:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set. Copy .env.example → .env and add your key.")
        return False
    return True


def run_licensing(inputs: dict | None = None) -> None:
    from .crew import SyncotecaCrew
    inputs = inputs or {
        "request": "Нужна sync-лицензия для трека 'Clair de Lune' (Дебюсси) для рекламы авто.",
        "track_info": "Claude Debussy — Clair de Lune (Suite bergamasque, 1905)",
        "project_details": "Рекламный ролик для автобренда, 30 сек, Россия, 1 год",
        "rights_holder": "TBD — определить в ходе поиска",
        "language": "Russian",
        "contract_text": "TBD",
        "deal_data": "fee=150000, currency=RUB, deal_type=flat_fee",
    }
    result = SyncotecaCrew().licensing_crew().kickoff(inputs=inputs)
    print("\n=== РЕЗУЛЬТАТ LICENSING CREW ===")
    print(result)


def run_biz_dev(inputs: dict | None = None) -> None:
    from .crew import SyncotecaCrew
    inputs = inputs or {
        "request": "Написать питч music supervisor в Netflix для нашего электронного каталога.",
        "recipient_info": "Music Supervisor, Netflix Originals, США",
        "language": "English",
        "client_project": "Netflix Original Series — Drama",
    }
    result = SyncotecaCrew().biz_dev_crew().kickoff(inputs=inputs)
    print("\n=== РЕЗУЛЬТАТ BIZ DEV CREW ===")
    print(result)


def run_content(inputs: dict | None = None) -> None:
    from .crew import SyncotecaCrew
    inputs = inputs or {
        "track_data": {
            "title": "Morning Drive",
            "artist": "Алексей Иванов",
            "bpm": 120,
            "key": "C major",
            "mood": "energetic, uplifting",
            "genre": "electronic",
            "instrumentation": "synth, drums, bass",
            "has_vocal": False,
            "has_instrumental": True,
            "has_stems": True,
            "isrc": "RU-A00-25-00001",
            "composer": "Алексей Иванов",
            "publisher": "SYNC LAB",
            "territory": "Worldwide",
        },
        "task_description": "Add new electronic track with full stems to catalog",
        "current_schema": "",
    }
    result = SyncotecaCrew().content_crew().kickoff(inputs=inputs)
    print("\n=== РЕЗУЛЬТАТ CONTENT CREW ===")
    print(result)


def run_full(inputs: dict | None = None) -> None:
    from .crew import SyncotecaCrew
    request = inputs or {
        "request": (
            "Клиент — рекламное агентство BBDO. Ищут энергичный электронный трек "
            "для нового автомобильного рекламного ролика (30 сек, Россия + СНГ, 1 год). "
            "Бюджет на лицензию: 200 000 руб. Нужен быстрый ответ — дедлайн 3 дня."
        ),
        "track_info": "Энергичный электронный трек для автомобильной рекламы, ~30 сек, BPM 120-140",
        "project_details": "Рекламный ролик для автобренда, 30 сек, Россия + СНГ, 1 год, бюджет 200 000 руб",
        "rights_holder": "TBD — определить в ходе поиска в базе",
        "language": "Russian",
        "contract_text": "Стандартный договор синхронизации, территория Россия + СНГ, срок 1 год, flat fee",
        "deal_data": "fee=200000, currency=RUB, deal_type=flat_fee, territory=Russia+CIS, term=1year",
    }
    result = SyncotecaCrew().full_crew().kickoff(inputs=request)
    print("\n=== РЕЗУЛЬТАТ FULL CREW ===")
    print(result)


CREWS = {
    "licensing": run_licensing,
    "biz_dev": run_biz_dev,
    "content": run_content,
    "full": run_full,
}


def interactive_menu() -> None:
    print("\n╔══════════════════════════════════════╗")
    print("║   СИНКОТЕКА — Multi-Agent Office     ║")
    print("╚══════════════════════════════════════╝\n")
    print("Выберите режим работы:\n")
    options = list(CREWS.keys())
    for i, name in enumerate(options, 1):
        labels = {
            "licensing": "Лицензирование треков (поиск → письмо → договор → роялти)",
            "biz_dev": "Развитие бизнеса (питч supervisors и брендам)",
            "content": "Контент-менеджмент (метаданные и каталог)",
            "full": "Полный офис (все агенты, иерархический режим)",
        }
        print(f"  {i}. {name:12} — {labels[name]}")

    print("\n  0. Выход\n")
    choice = input("Введите номер: ").strip()

    if choice == "0":
        sys.exit(0)

    try:
        idx = int(choice) - 1
        crew_name = options[idx]
        print(f"\nЗапуск: {crew_name}...\n")
        CREWS[crew_name]()
    except (ValueError, IndexError):
        print("Неверный выбор.")
        interactive_menu()


def run() -> None:
    if not check_env():
        sys.exit(1)

    parser = argparse.ArgumentParser(description="SYNC LAB Multi-Agent Office")
    parser.add_argument(
        "--crew",
        choices=list(CREWS.keys()),
        help="Run specific crew without interactive menu",
    )
    args = parser.parse_args()

    if args.crew:
        CREWS[args.crew]()
    else:
        interactive_menu()


if __name__ == "__main__":
    run()
