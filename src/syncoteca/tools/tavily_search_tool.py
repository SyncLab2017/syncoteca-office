from __future__ import annotations

import os
import httpx
from crewai.tools import BaseTool


class TavilySearchTool(BaseTool):
    name: str = "TavilySearchTool"
    description: str = (
        "Web search via Tavily AI. Returns full page content. "
        "Args: query (str) — brand name, track title, artist, or any search term."
    )

    def _run(self, query: str) -> str:
        api_key = os.getenv("TAVILY_API_KEY", "")
        if not api_key:
            return ""
        try:
            resp = httpx.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": api_key,
                    "query": query,
                    "search_depth": "basic",
                    "max_results": 3,
                    "include_answer": True,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            lines = []
            if answer := data.get("answer"):
                lines.append(f"Краткий ответ: {answer}")
            for r in data.get("results", [])[:3]:
                title = r.get("title", "")
                url = r.get("url", "")
                content = (r.get("content") or "")[:400]
                lines.append(f"\n— {title}\n  {url}\n  {content}")
            return "\n".join(lines) if lines else ""
        except Exception:
            return ""
