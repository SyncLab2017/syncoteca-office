"""
Запуск Синкотека: Telegram-бот + веб-офис одной командой.

    python start.py

Офис:  http://127.0.0.1:7788
Бот:   @DenisSharko_bot
"""
import os
import threading
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from dotenv import load_dotenv
load_dotenv(override=True)


def run_office():
    import uvicorn
    from syncoteca.office_server import app
    port = int(os.getenv("PORT", 7788))
    host = "0.0.0.0" if os.getenv("RAILWAY_ENVIRONMENT") else "127.0.0.1"
    print(f"🖥️  Офис запущен: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


def run_bot():
    from syncoteca.bot import run_bot as _run
    print("🤖 Telegram-бот запущен: @DenisSharko_bot")
    _run()


if __name__ == "__main__":
    print("\n╔══════════════════════════════════╗")
    print("║   СИНКОТЕКА — запуск системы     ║")
    print("╚══════════════════════════════════╝\n")

    # Office server in background thread
    t = threading.Thread(target=run_office, daemon=True)
    t.start()

    import time
    time.sleep(1)  # wait for server to bind

    # Bot in main thread (blocking)
    run_bot()
