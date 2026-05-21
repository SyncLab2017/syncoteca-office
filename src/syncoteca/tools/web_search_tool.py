from __future__ import annotations

import os
import httpx
from crewai.tools import BaseTool


class WebSearchTool(BaseTool):
    name: str = "WebSearchTool"
    description: str = "General web search. Args: query (str)."

    def _run(self, query: str) -> str:
        api_key = os.getenv("SERPER_API_KEY")
        if api_key:
            return self._serper(query, api_key)
        return self._duckduckgo(query)

    def _serper(self, query: str, api_key: str) -> str:
        try:
            resp = httpx.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": query, "num": 5, "hl": "ru"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            lines = []
            if ans := data.get("answerBox", {}).get("answer"):
                lines.append(f"Быстрый ответ: {ans}")
            for item in data.get("organic", [])[:5]:
                lines.append(f"- {item.get('title')}: {item.get('snippet')}\n  {item.get('link')}")
            return "\n".join(lines) if lines else "Результатов нет."
        except Exception as e:
            return f"Ошибка поиска: {e}"

    def _duckduckgo(self, query: str) -> str:
        try:
            resp = httpx.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
                timeout=10,
                follow_redirects=True,
            )
            resp.raise_for_status()
            data = resp.json()
            lines = []
            if abstract := data.get("AbstractText"):
                lines.append(abstract)
                if src := data.get("AbstractURL"):
                    lines.append(f"Источник: {src}")
            for rel in data.get("RelatedTopics", [])[:4]:
                if isinstance(rel, dict) and rel.get("Text"):
                    lines.append(f"- {rel['Text']}")
            return "\n".join(lines) if lines else "Быстрый ответ не найден. Добавь SERPER_API_KEY для полноценного поиска."
        except Exception as e:
            return f"Ошибка поиска: {e}"
