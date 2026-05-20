"""Thread-safe event bus for the Синкотека pixel office."""

import asyncio
import json
import threading
from collections import deque
from datetime import datetime
from typing import AsyncGenerator

# Recent events (shown on page load)
_history: deque[dict] = deque(maxlen=100)
_history_lock = threading.Lock()

# Live SSE subscribers
_subscribers: list[asyncio.Queue] = []
_sub_lock = threading.Lock()

# Agent statuses
_agent_status: dict[str, str] = {
    "ekaterina": "idle",
    "ksusha": "idle",
    "marina": "idle",
    "sasha": "idle",
    "biz_dev": "idle",
    "developer": "idle",
}
_status_lock = threading.Lock()

# Current task per agent
_agent_task: dict[str, str] = {}

AGENT_DISPLAY = {
    "ekaterina": "Екатерина",
    "ksusha": "Ксюша",
    "marina": "Марина",
    "sasha": "Саша",
    "biz_dev": "Директор",
    "developer": "Разработчик",
    "license_manager": "Екатерина",
    "lawyer": "Ксюша",
    "accountant": "Марина",
    "content_manager": "Саша",
}

AGENT_ROLE_TO_MEM = {
    "license_manager": "ekaterina",
    "lawyer": "ksusha",
    "accountant": "marina",
    "content_manager": "sasha",
    "biz_dev": "biz_dev",
    "developer": "developer",
}


def _normalize(agent: str) -> str:
    return AGENT_ROLE_TO_MEM.get(agent, agent)


def emit(agent: str, event_type: str, message: str, status: str | None = None) -> None:
    """Emit an event. Safe to call from any thread."""
    agent_key = _normalize(agent)
    display = AGENT_DISPLAY.get(agent_key, agent_key)

    if status:
        with _status_lock:
            _agent_status[agent_key] = status
        if status == "working":
            with _status_lock:
                _agent_task[agent_key] = message
        elif status == "idle":
            with _status_lock:
                _agent_task.pop(agent_key, None)

    event = {
        "agent": agent_key,
        "display": display,
        "type": event_type,
        "message": message,
        "status": status or _agent_status.get(agent_key, "idle"),
        "ts": datetime.now().strftime("%H:%M:%S"),
    }

    with _history_lock:
        _history.appendleft(event)

    # Notify async subscribers (from any thread)
    with _sub_lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(event)
        except Exception:
            pass


def get_state() -> dict:
    """Snapshot of current office state."""
    with _status_lock:
        statuses = dict(_agent_status)
        tasks = dict(_agent_task)
    with _history_lock:
        history = list(_history)[:30]
    return {"statuses": statuses, "tasks": tasks, "history": history}


async def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    with _sub_lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    with _sub_lock:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


async def sse_stream(q: asyncio.Queue) -> AsyncGenerator[str, None]:
    """Yields SSE-formatted strings from the queue. Keeps stream alive with pings."""
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=15.0)
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except asyncio.TimeoutError:
                yield 'data: {"type":"ping"}\n\n'
    except asyncio.CancelledError:
        pass
    finally:
        unsubscribe(q)
