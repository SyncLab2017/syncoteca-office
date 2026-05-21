"""FastAPI server for the SYNC LAB pixel office dashboard."""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import events as ev

STATIC_DIR = Path(__file__).parent / "static"
KNOWLEDGE_DIR = Path(__file__).parent.parent.parent / "data" / "knowledge"

VALID_AGENTS = {"ekaterina", "ksusha", "marina", "sasha", "biz_dev", "developer"}

app = FastAPI(title="SYNC LAB Office", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _read_entries(agent: str) -> list[dict]:
    json_path = KNOWLEDGE_DIR / f"{agent}.json"
    md_path = KNOWLEDGE_DIR / f"{agent}.md"

    if json_path.exists():
        try:
            return json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            return []

    # Migrate from .md
    if md_path.exists():
        content = md_path.read_text(encoding="utf-8").strip()
        if content:
            entries = [{"ts": datetime.now().strftime("%Y-%m-%d %H:%M"), "text": content}]
            KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
            json_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
            return entries

    return []


def _write_entries(agent: str, entries: list[dict]) -> None:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    path = KNOWLEDGE_DIR / f"{agent}.json"
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


class KnowledgeEntry(BaseModel):
    text: str


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "office.html").read_text(encoding="utf-8")


@app.get("/api/state")
async def state():
    return JSONResponse(ev.get_state())


@app.get("/api/knowledge/{agent}")
async def get_knowledge(agent: str):
    if agent not in VALID_AGENTS:
        return JSONResponse({"error": "unknown agent"}, status_code=404)
    return JSONResponse({"entries": _read_entries(agent), "agent": agent})


@app.post("/api/knowledge/{agent}/entry")
async def add_entry(agent: str, body: KnowledgeEntry):
    if agent not in VALID_AGENTS:
        return JSONResponse({"error": "unknown agent"}, status_code=404)
    text = body.text.strip()
    if not text:
        return JSONResponse({"error": "empty"}, status_code=400)
    entries = _read_entries(agent)
    entries.insert(0, {"ts": datetime.now().strftime("%Y-%m-%d %H:%M"), "text": text})
    _write_entries(agent, entries)
    return JSONResponse({"ok": True})


@app.delete("/api/knowledge/{agent}/entry/{index}")
async def delete_entry(agent: str, index: int):
    if agent not in VALID_AGENTS:
        return JSONResponse({"error": "unknown agent"}, status_code=404)
    entries = _read_entries(agent)
    if 0 <= index < len(entries):
        entries.pop(index)
        _write_entries(agent, entries)
    return JSONResponse({"ok": True})


@app.get("/api/agent-log/{agent}")
async def agent_log(agent: str):
    if agent not in VALID_AGENTS:
        return JSONResponse({"error": "unknown agent"}, status_code=404)
    state_data = ev.get_state()
    filtered = [e for e in state_data.get("history", []) if e.get("agent") == agent]
    return JSONResponse({"agent": agent, "log": filtered})


@app.get("/events")
async def sse():
    q = await ev.subscribe()

    async def generator():
        while True:
            try:
                async for chunk in ev.sse_stream(q):
                    yield chunk
            except Exception:
                break

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def run_office(host: str = "127.0.0.1", port: int = 7788) -> None:
    import uvicorn
    print(f"\n🖥️  SYNC LAB Office: http://{host}:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run_office()
