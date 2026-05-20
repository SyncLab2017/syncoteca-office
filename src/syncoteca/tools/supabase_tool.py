import os
import json
import httpx
from crewai.tools import BaseTool
from datetime import datetime


def _sb_headers() -> dict:
    key = os.getenv("SUPABASE_KEY", "")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _sb_url(path: str) -> str:
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    return f"{base}/rest/v1/{path}"


def _get(path: str, params: dict | None = None) -> list | dict:
    r = httpx.get(_sb_url(path), headers=_sb_headers(), params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def _post(path: str, data: dict) -> list | dict:
    r = httpx.post(_sb_url(path), headers=_sb_headers(), json=data, timeout=10)
    r.raise_for_status()
    return r.json()


def _patch(path: str, data: dict, params: dict | None = None) -> list | dict:
    r = httpx.patch(_sb_url(path), headers=_sb_headers(), json=data, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def _fmt(rows: list | dict) -> str:
    if not rows:
        return "Ничего не найдено."
    if isinstance(rows, dict):
        rows = [rows]
    return json.dumps(rows, ensure_ascii=False, indent=2, default=str)


class SupabaseTool(BaseTool):
    """
    Live Sync Lab database (Supabase). Real data — contacts, tracks, authors, rights, agent memory.

    Tables:
      contacts (live)         — rights holders, publishers, supervisors with emails
      authors (live)          — composers, lyricists with RAO status
      rights_management       — author_id ↔ contact_id (who manages whose rights)
      tracks (11,452)         — music catalog with authors, labels, genres
      labels (471)            — music labels
      agent_memory            — persistent facts/notes per named agent
      agent_sessions          — conversation history per agent session

    Actions:
      --- Contacts ---
      search_contacts   query=<name/email/owner_type>   Find rights holders, publishers, supervisors
      get_contact       query=<contact_id>               Full contact record
      list_contact_types                                 Summary of owner_type categories

      --- Authors & Rights Chain ---
      search_authors    query=<composer/lyricist name>   Find author by name
      get_author_rights query=<author_id>                Full chain: author → rights contacts
      find_rights_chain query=<author name>              One-shot: find author + all their contacts

      --- Tracks ---
      search_tracks     query=<title/artist/author/label>

      --- Agent Memory (persistent across sessions) ---
      save_memory       data={agent_name, content, memory_type?, tags?}   Save a fact or note
      get_memory        data={agent_name} + query=<optional keyword>        Recall memories
      delete_memory     query=<memory_id>

      --- Sessions (conversation continuity) ---
      save_session      data={session_id, agent_name, messages, summary?, task_context?}
      get_session       query=<session_id> + data={agent_name}
      get_last_session  data={agent_name}                                   Most recent session
    """

    name: str = "synclab_db"
    description: str = (
        "Live Sync Lab database (Supabase): contacts of rights holders/publishers/supervisors, "
        "11,452 tracks, 471 labels, authors with rights chain, agent persistent memory. "
        "Actions: search_contacts, get_contact, list_contact_types, "
        "search_authors, get_author_rights, find_rights_chain, "
        "search_tracks, save_memory, get_memory, delete_memory, "
        "save_session, get_session, get_last_session. "
        "Input: action (str), query (str, optional), data (dict, optional)."
    )

    def _run(self, action: str, query: str | None = None, data: dict | None = None) -> str:
        if not os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_KEY") == "your-anon-key":
            return (
                "SUPABASE_KEY не настроен. Добавь в .env:\n"
                "SUPABASE_URL=https://zpuvorqtdvjbmqmjgtll.supabase.co\n"
                "SUPABASE_KEY=<anon-key из Supabase dashboard → Settings → API>"
            )

        handlers = {
            "search_contacts": self._search_contacts,
            "get_contact": self._get_contact,
            "list_contact_types": self._list_contact_types,
            "search_authors": self._search_authors,
            "get_author_rights": self._get_author_rights,
            "find_rights_chain": self._find_rights_chain,
            "search_tracks": self._search_tracks,
            "save_memory": self._save_memory,
            "get_memory": self._get_memory,
            "delete_memory": self._delete_memory,
            "save_session": self._save_session,
            "get_session": self._get_session,
            "get_last_session": self._get_last_session,
        }
        handler = handlers.get(action)
        if not handler:
            return f"Unknown action: '{action}'. Available: {', '.join(handlers)}"
        try:
            return handler(query=query, data=data)
        except httpx.HTTPStatusError as e:
            return f"Supabase error {e.response.status_code}: {e.response.text[:400]}"
        except Exception as e:
            return f"Error: {e}"

    # --- Contacts ---

    def _search_contacts(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query= (name, email, or owner_type)"
        or_filter = (
            f"(first_name.ilike.*{query}*,"
            f"last_name.ilike.*{query}*,"
            f"email.ilike.*{query}*,"
            f"owner_type.ilike.*{query}*,"
            f"adittional_info.ilike.*{query}*)"
        )
        rows = _get("contacts", {"or": or_filter, "limit": "10", "order": "last_name.asc"})
        return f"Найдено {len(rows)} контакт(ов) для '{query}':\n" + _fmt(rows)

    def _get_contact(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query=<contact_id>"
        rows = _get("contacts", {"id": f"eq.{query}"})
        return _fmt(rows)

    def _list_contact_types(self, **_) -> str:
        rows = _get("contacts", {"select": "owner_type", "limit": "1000"})
        counts: dict[str, int] = {}
        for r in rows:
            t = r.get("owner_type") or "не указан"
            counts[t] = counts.get(t, 0) + 1
        lines = [f"  {t}: {c}" for t, c in sorted(counts.items(), key=lambda x: -x[1])]
        return f"Типы контактов ({len(rows)} всего):\n" + "\n".join(lines)

    # --- Authors & Rights Chain ---

    def _search_authors(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query= (author name)"
        rows = _get(
            "authors",
            {"author_name": f"ilike.*{query}*", "limit": "10", "order": "author_name.asc"},
        )
        return f"Авторы по запросу '{query}':\n" + _fmt(rows)

    def _get_author_rights(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query=<author_id>"
        # Get author
        author_rows = _get("authors", {"id": f"eq.{query}"})
        if not author_rows:
            return f"Автор #{query} не найден"
        # Get rights_management links
        rm_rows = _get("rights_management", {"author_id": f"eq.{query}"})
        contacts = []
        for rm in rm_rows:
            c = _get("contacts", {"id": f"eq.{rm['contact_id']}"})
            contacts.extend(c)
        return json.dumps(
            {"author": author_rows[0], "rights_contacts": contacts},
            ensure_ascii=False, indent=2, default=str
        )

    def _find_rights_chain(self, query: str | None = None, **_) -> str:
        """One-shot: find author by name + all their rights management contacts."""
        if not query:
            return "Provide query= (author name)"
        # Find authors
        authors = _get("authors", {"author_name": f"ilike.*{query}*", "limit": "5"})
        if not authors:
            return f"Автор '{query}' не найден в базе Sync Lab."
        results = []
        for author in authors:
            rm_rows = _get("rights_management", {"author_id": f"eq.{author['id']}"})
            contacts = []
            for rm in rm_rows:
                c = _get("contacts", {"id": f"eq.{rm['contact_id']}"})
                contacts.extend(c)
            results.append({
                "author": author,
                "rights_contacts": contacts,
                "has_contacts": len(contacts) > 0,
            })
        return (
            f"Цепочка прав для '{query}' ({len(results)} авторов):\n"
            + json.dumps(results, ensure_ascii=False, indent=2, default=str)
        )

    # --- Tracks ---

    def _search_tracks(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query= (title, artist, author, label, genre)"
        or_filter = (
            f"(title.ilike.*{query}*,"
            f"artist.ilike.*{query}*,"
            f"music_author.ilike.*{query}*,"
            f"lyrics_author.ilike.*{query}*,"
            f"label.ilike.*{query}*,"
            f"genre_1.ilike.*{query}*)"
        )
        rows = _get("tracks", {"or": or_filter, "limit": "8", "order": "title.asc"})
        return f"{len(rows)} треков по '{query}':\n" + _fmt(rows)

    # --- Agent Memory ---

    def _save_memory(self, data: dict | None = None, **_) -> str:
        if not data or "agent_name" not in data or "content" not in data:
            return "Provide data={agent_name, content, memory_type?, tags?}"
        payload = {
            "agent_name": data["agent_name"],
            "content": data["content"],
            "memory_type": data.get("memory_type", "fact"),
            "tags": data.get("tags", []),
            "importance": data.get("importance", 1),
        }
        rows = _post("agent_memory", payload)
        if isinstance(rows, list) and rows:
            return f"Память сохранена (id={rows[0]['id']}): {data['content'][:100]}"
        return f"Память сохранена: {data['content'][:100]}"

    def _get_memory(self, query: str | None = None, data: dict | None = None, **_) -> str:
        agent_name = (data or {}).get("agent_name")
        memory_type = (data or {}).get("memory_type")
        if not agent_name:
            return "Provide data={agent_name} and optional query= for keyword filter"
        params: dict = {
            "agent_name": f"eq.{agent_name}",
            "order": "importance.desc,created_at.desc",
            "limit": "20",
        }
        if memory_type:
            params["memory_type"] = f"eq.{memory_type}"
        rows = _get("agent_memory", params)
        if query and rows:
            q = query.lower()
            rows = [r for r in rows if q in r.get("content", "").lower()]
        return f"Воспоминания {agent_name} ({len(rows)}):\n" + _fmt(rows)

    def _delete_memory(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query=<memory_id>"
        r = httpx.delete(
            _sb_url("agent_memory"),
            headers=_sb_headers(),
            params={"id": f"eq.{query}"},
            timeout=10,
        )
        r.raise_for_status()
        return f"Память #{query} удалена."

    # --- Sessions ---

    def _save_session(self, data: dict | None = None, **_) -> str:
        if not data or "session_id" not in data or "agent_name" not in data:
            return "Provide data={session_id, agent_name, messages, summary?, task_context?}"
        payload = {
            "session_id": data["session_id"],
            "agent_name": data["agent_name"],
            "messages": data.get("messages", []),
            "summary": data.get("summary"),
            "task_context": data.get("task_context"),
            "updated_at": datetime.utcnow().isoformat(),
        }
        # Upsert
        existing = _get(
            "agent_sessions",
            {"session_id": f"eq.{data['session_id']}", "agent_name": f"eq.{data['agent_name']}"},
        )
        if existing:
            _patch(
                "agent_sessions",
                {k: v for k, v in payload.items() if v is not None},
                params={
                    "session_id": f"eq.{data['session_id']}",
                    "agent_name": f"eq.{data['agent_name']}",
                },
            )
            return f"Сессия {data['session_id']} обновлена для {data['agent_name']}."
        _post("agent_sessions", payload)
        return f"Сессия {data['session_id']} создана для {data['agent_name']}."

    def _get_session(self, query: str | None = None, data: dict | None = None, **_) -> str:
        if not query:
            return "Provide query=<session_id>"
        agent_name = (data or {}).get("agent_name")
        params: dict = {"session_id": f"eq.{query}"}
        if agent_name:
            params["agent_name"] = f"eq.{agent_name}"
        rows = _get("agent_sessions", params)
        return _fmt(rows)

    def _get_last_session(self, data: dict | None = None, **_) -> str:
        agent_name = (data or {}).get("agent_name")
        if not agent_name:
            return "Provide data={agent_name}"
        rows = _get(
            "agent_sessions",
            {"agent_name": f"eq.{agent_name}", "order": "updated_at.desc", "limit": "1"},
        )
        return _fmt(rows)
