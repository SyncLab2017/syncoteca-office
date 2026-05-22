from __future__ import annotations

import os
import httpx
from crewai.tools import BaseTool


class AsanaSearchTool(BaseTool):
    name: str = "AsanaSearchTool"
    description: str = (
        "Search completed Asana tasks for historical licensing deals. "
        "Args: query (str) — track name, artist, brand, or rights holder name. "
        "Returns task names, notes, and links with previous deal context."
    )

    def _run(self, query: str) -> str:
        token = os.getenv("ASANA_TOKEN", "")
        project_id = os.getenv("ASANA_PROJECT_ID", "")
        if not token or not project_id:
            return "ASANA_TOKEN или ASANA_PROJECT_ID не настроены."

        try:
            headers = {"Authorization": f"Bearer {token}"}
            resp = httpx.get(
                "https://app.asana.com/api/1.0/tasks",
                headers=headers,
                params={
                    "project": project_id,
                    "opt_fields": "name,notes,completed,permalink_url,created_at",
                    "limit": 100,
                },
                timeout=15,
            )
            resp.raise_for_status()
            tasks = resp.json().get("data", [])

            q = query.lower()
            matches = [
                t for t in tasks
                if q in (t.get("name") or "").lower()
                or q in (t.get("notes") or "").lower()
            ]

            if not matches:
                return f"Задач по запросу «{query}» в Asana не найдено."

            lines = [f"Найдено {len(matches)} задач по «{query}»:\n"]
            for t in matches[:10]:
                status = "✅" if t.get("completed") else "🔄"
                lines.append(f"{status} {t['name']}")
                if notes := t.get("notes", "").strip():
                    lines.append(f"   {notes[:300]}")
                if url := t.get("permalink_url"):
                    lines.append(f"   {url}")
                lines.append("")
            return "\n".join(lines)

        except Exception as e:
            return f"Ошибка Asana: {e}"
