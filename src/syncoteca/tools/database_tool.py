import os
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime
from crewai.tools import BaseTool

DB_PATH = Path(__file__).parents[3] / "data" / "syncoteca.db"


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def rows_to_json(rows: list[sqlite3.Row]) -> str:
    return json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2, default=str)


class DatabaseTool(BaseTool):
    """
    SYNC LAB knowledge base (SQLite, 9471 records total).

    Tables:
      contracts (310)     — real licensing deals: licensee, project, territory, cost
      musical_works (460) — tracks used in contracts, linked by contract_id
      tracks (7662)       — Yandex Music catalog with authors, labels, genres
      labels (447)        — music labels with parent-child hierarchy
      contacts (592)      — rights holders, publishers, supervisors with emails

    Actions:
      --- Contracts ---
      search_contracts      query=<text>        Search by licensee/project/territory/licensor
      get_contract          query=<id>          Full contract + its musical works
      list_contracts        query=<type|"">     Filter by project_type (Реклама/Фильм/Сериал)
      stats_contracts                           Stats by type, top licensees

      --- Musical Works ---
      search_works          query=<text>        Search by title/performer/authors
      get_work              query=<id>          Work + its contract context
      list_works            query=<contract_id> All works for a contract

      --- Tracks (Yandex Music) ---
      search_tracks         query=<text>        Search by title/artist/author/label/genre
      get_track             query=<id>          Full track record
      list_tracks_by_label  query=<label>       All tracks by label
      list_tracks_by_author query=<author>      All tracks by music/lyrics author
      stats_tracks                              Stats by genre, top labels

      --- Labels ---
      search_labels         query=<text>        Search label by name
      get_label             query=<id|name>     Label details + track count
      list_labels           query=<parent|"">   List labels, filter by parent

      --- Contacts ---
      search_contacts       query=<text>        Search by name/email/type/info
      get_contact           query=<id>          Full contact record
      list_contacts_by_type query=<type|"">     List by owner_type or show summary

      --- RAO Registry ---
      search_rao            query=<title>       Search 322,689 RAO-registered works by title
      search_rao_by_composer query=<name>       All works by composer/lyricist in RAO
      search_rao_by_genre   query=<genre>       Works by RAO genre category
      stats_rao                                 Stats: top genres, top composers

      --- New Tracks Catalog ---
      add_track             data={...}          Add new track (agents' catalog)
      list_new_tracks                           List tracks added by agents

      --- ГК РФ Часть 4 (126 статей об интеллектуальных правах, FTS5) ---
      search_gk_rf4         query=<text>        Поиск по тексту статей ГК РФ ч.4
      get_gk_rf4_article    query=<num|title>   Полный текст статьи (напр. 1235)
      list_gk_rf4                               Список всех статей ГК РФ ч.4
    """

    name: str = "database"
    description: str = (
        "SYNC LAB knowledge base: 310 contracts, 460 musical works, 7662 Yandex Music tracks, "
        "447 labels, 592 contacts, 322,689 RAO-registered works, "
        "book 'Музыкальный редактор' (380 pages FTS), ГК РФ ч.4 (126 articles FTS). "
        "Actions: search_contracts, get_contract, list_contracts, stats_contracts, "
        "search_works, get_work, list_works, "
        "search_tracks, get_track, list_tracks_by_label, list_tracks_by_author, stats_tracks, "
        "search_labels, get_label, list_labels, "
        "search_contacts, get_contact, list_contacts_by_type, "
        "search_rao, search_rao_by_composer, search_rao_by_genre, stats_rao, "
        "search_book, get_book_chapter, list_book_chapters, "
        "search_gk_rf4, get_gk_rf4_article, list_gk_rf4, "
        "add_track, list_new_tracks. "
        "Input: action (str), query (str, optional), data (dict, optional)."
    )

    def _run(self, action: str, query: str | None = None, data: dict | None = None) -> str:
        if not DB_PATH.exists():
            return (
                "DB not found. Run: python scripts/import_sql.py\n"
                f"Expected path: {DB_PATH}"
            )

        handlers = {
            # Contracts (310 real deals)
            "search_contracts": self._search_contracts,
            "get_contract": self._get_contract,
            "list_contracts": self._list_contracts,
            "stats_contracts": self._stats_contracts,
            # Musical works (460 works linked to contracts)
            "search_works": self._search_works,
            "get_work": self._get_work,
            "list_works": self._list_works,
            # Tracks from Yandex Music (7662 tracks)
            "search_tracks": self._search_tracks,
            "get_track": self._get_track,
            "list_tracks_by_label": self._list_tracks_by_label,
            "list_tracks_by_author": self._list_tracks_by_author,
            "stats_tracks": self._stats_tracks,
            # Labels (447 labels)
            "search_labels": self._search_labels,
            "get_label": self._get_label,
            "list_labels": self._list_labels,
            # Contacts (592 contacts — rights holders, publishers, supervisors)
            "search_contacts": self._search_contacts,
            "get_contact": self._get_contact,
            "list_contacts_by_type": self._list_contacts_by_type,
            # RAO Registry (322,689 registered works)
            "search_rao": self._search_rao,
            "search_rao_by_composer": self._search_rao_by_composer,
            "search_rao_by_genre": self._search_rao_by_genre,
            "stats_rao": self._stats_rao,
            # Book: "Музыкальный редактор" by Denis Sharko (FTS5)
            "search_book": self._search_book,
            "get_book_chapter": self._get_book_chapter,
            "list_book_chapters": self._list_book_chapters,
            # ГК РФ Часть 4 — 126 статей (FTS5)
            "search_gk_rf4": self._search_gk_rf4,
            "get_gk_rf4_article": self._get_gk_rf4_article,
            "list_gk_rf4": self._list_gk_rf4,
            # Catalog (new tracks added by agents)
            "add_track": self._add_track,
            "list_new_tracks": self._list_new_tracks,
        }
        handler = handlers.get(action)
        if not handler:
            return f"Unknown action: '{action}'. Available: {', '.join(handlers)}"
        return handler(query=query, data=data)

    # --- Contracts ---

    def _search_contracts(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query= (licensee name, project name, or territory)"
        q = f"%{query}%"
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT id, licensee_name, project_name, project_type,
                       license_term_start, license_term_end, territory,
                       media_channels, licensor_name, license_cost
                FROM contracts
                WHERE licensee_name LIKE ? OR project_name LIKE ?
                   OR territory LIKE ? OR licensor_name LIKE ?
                ORDER BY id DESC LIMIT 8
                """,
                (q, q, q, q),
            ).fetchall()
        if not rows:
            return f"No contracts found for: {query}"
        return f"Found {len(rows)} contract(s):\n" + rows_to_json(rows)

    def _get_contract(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query=<contract_id>"
        try:
            cid = int(query)
        except ValueError:
            return f"contract_id must be integer, got: {query}"
        with get_conn() as conn:
            contract = conn.execute(
                "SELECT * FROM contracts WHERE id=?", (cid,)
            ).fetchone()
            if not contract:
                return f"Contract #{cid} not found"
            works = conn.execute(
                "SELECT * FROM musical_works WHERE contract_id=?", (cid,)
            ).fetchall()
        return json.dumps(
            {"contract": dict(contract), "musical_works": [dict(w) for w in works]},
            ensure_ascii=False, indent=2, default=str,
        )

    def _list_contracts(self, query: str | None = None, **_) -> str:
        with get_conn() as conn:
            if query and query.strip():
                rows = conn.execute(
                    """SELECT id, licensee_name, project_name, project_type,
                              license_term_start, license_term_end, territory, license_cost
                       FROM contracts WHERE project_type LIKE ? ORDER BY id DESC LIMIT 8""",
                    (f"%{query}%",),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, licensee_name, project_name, project_type,
                              license_term_start, license_term_end, territory, license_cost
                       FROM contracts ORDER BY id DESC LIMIT 8"""
                ).fetchall()
        return f"{len(rows)} contracts:\n" + rows_to_json(rows)

    def _stats_contracts(self, **_) -> str:
        with get_conn() as conn:
            by_type = conn.execute(
                "SELECT project_type, COUNT(*) as cnt FROM contracts GROUP BY project_type ORDER BY cnt DESC"
            ).fetchall()
            by_licensee = conn.execute(
                "SELECT licensee_name, COUNT(*) as cnt FROM contracts GROUP BY licensee_name ORDER BY cnt DESC LIMIT 10"
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
            works_total = conn.execute("SELECT COUNT(*) FROM musical_works").fetchone()[0]

        return (
            f"=== Статистика базы SYNC LAB ===\n"
            f"Договоров: {total}\n"
            f"Музыкальных произведений: {works_total}\n\n"
            f"По типу проекта:\n"
            + "\n".join(f"  {r['project_type'] or 'н/у'}: {r['cnt']}" for r in by_type)
            + f"\n\nТоп лицензиатов:\n"
            + "\n".join(f"  {r['licensee_name']}: {r['cnt']}" for r in by_licensee)
        )

    # --- Musical Works ---

    def _search_works(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query= (title, performer, or author name)"
        q = f"%{query}%"
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT mw.id, mw.contract_id, mw.title, mw.performer,
                       mw.music_authors, mw.lyrics_authors, mw.usage_details,
                       c.project_name, c.project_type, c.licensee_name,
                       c.territory, c.license_term_start, c.license_term_end
                FROM musical_works mw
                JOIN contracts c ON c.id = mw.contract_id
                WHERE mw.title LIKE ? OR mw.performer LIKE ?
                   OR mw.music_authors LIKE ? OR mw.lyrics_authors LIKE ?
                ORDER BY mw.id DESC LIMIT 8
                """,
                (q, q, q, q),
            ).fetchall()
        if not rows:
            return f"No works found for: {query}"
        return f"Found {len(rows)} work(s):\n" + rows_to_json(rows)

    def _get_work(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query=<work_id>"
        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT mw.*, c.licensee_name, c.project_name, c.project_type,
                       c.territory, c.media_channels, c.licensor_name,
                       c.license_term_start, c.license_term_end, c.license_cost
                FROM musical_works mw
                JOIN contracts c ON c.id = mw.contract_id
                WHERE mw.id=?
                """,
                (int(query),),
            ).fetchone()
        return json.dumps(dict(row), ensure_ascii=False, indent=2, default=str) if row else f"Work #{query} not found"

    def _list_works(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query=<contract_id>"
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM musical_works WHERE contract_id=?", (int(query),)
            ).fetchall()
        return rows_to_json(rows)

    # --- Tracks (Yandex Music catalog, 7662 tracks) ---

    def _search_tracks(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query= (title, artist, author, label, genre)"
        q = f"%{query}%"
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT id, title, artist, album, duration, label, genre_1, genre_2,
                          music_author, lyrics_author, link, author_check_status
                   FROM tracks
                   WHERE title LIKE ? OR artist LIKE ? OR music_author LIKE ?
                      OR lyrics_author LIKE ? OR label LIKE ?
                      OR music_author_canonical LIKE ? OR lyrics_author_canonical LIKE ?
                      OR genre_1 LIKE ? OR genre_2 LIKE ?
                   ORDER BY id LIMIT 8""",
                (q, q, q, q, q, q, q, q, q),
            ).fetchall()
        if not rows:
            return f"No tracks found for: {query}"
        return f"Found {len(rows)} track(s):\n" + rows_to_json(rows)

    def _get_track(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query=<track_id>"
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM tracks WHERE id=?", (int(query),)).fetchone()
        return json.dumps(dict(row), ensure_ascii=False, indent=2, default=str) if row else f"Track #{query} not found"

    def _list_tracks_by_label(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query=<label name>"
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT id, title, artist, album, duration, genre_1, music_author, link
                   FROM tracks WHERE label LIKE ? ORDER BY artist, title LIMIT 8""",
                (f"%{query}%",),
            ).fetchall()
        return f"{len(rows)} tracks for label '{query}':\n" + rows_to_json(rows)

    def _list_tracks_by_author(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query=<author name>"
        q = f"%{query}%"
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT id, title, artist, album, label, genre_1,
                          music_author, lyrics_author, link, author_check_status
                   FROM tracks
                   WHERE music_author LIKE ? OR lyrics_author LIKE ?
                      OR music_author_canonical LIKE ? OR lyrics_author_canonical LIKE ?
                   ORDER BY artist, title LIMIT 8""",
                (q, q, q, q),
            ).fetchall()
        return f"{len(rows)} tracks by '{query}':\n" + rows_to_json(rows)

    def _stats_tracks(self, **_) -> str:
        with get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            by_genre = conn.execute(
                """SELECT genre_1, COUNT(*) as cnt FROM tracks
                   WHERE genre_1 IS NOT NULL GROUP BY genre_1 ORDER BY cnt DESC LIMIT 15"""
            ).fetchall()
            by_label = conn.execute(
                """SELECT label, COUNT(*) as cnt FROM tracks
                   WHERE label IS NOT NULL GROUP BY label ORDER BY cnt DESC LIMIT 10"""
            ).fetchall()
            pending = conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE author_check_status='pending'"
            ).fetchone()[0]
        return (
            f"=== Треки Яндекс.Музыки ===\n"
            f"Всего треков: {total}\n"
            f"Ожидают проверки автора: {pending}\n\n"
            f"По жанрам:\n"
            + "\n".join(f"  {r['genre_1']}: {r['cnt']}" for r in by_genre)
            + f"\n\nТоп лейблов:\n"
            + "\n".join(f"  {r['label']}: {r['cnt']}" for r in by_label)
        )

    # --- Labels (447 labels) ---

    def _search_labels(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query=<label name>"
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT id, name, active, parent FROM labels WHERE name LIKE ? ORDER BY name LIMIT 8",
                (f"%{query}%",),
            ).fetchall()
        if not rows:
            return f"No labels found for: {query}"
        return f"Found {len(rows)} label(s):\n" + rows_to_json(rows)

    def _get_label(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query=<label_id or name>"
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM labels WHERE id=? OR name=?", (query, query)).fetchone()
            if not row:
                return f"Label not found: {query}"
            # Also get tracks for this label
            tracks = conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE label LIKE ?", (f"%{dict(row)['name']}%",)
            ).fetchone()[0]
        return json.dumps({**dict(row), "tracks_in_catalog": tracks}, ensure_ascii=False, indent=2, default=str)

    def _list_labels(self, query: str | None = None, **_) -> str:
        with get_conn() as conn:
            if query:
                rows = conn.execute(
                    "SELECT id, name, active, parent FROM labels WHERE parent LIKE ? ORDER BY name",
                    (f"%{query}%",),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, name, active, parent FROM labels WHERE active=1 ORDER BY name LIMIT 8"
                ).fetchall()
        return f"{len(rows)} labels:\n" + rows_to_json(rows)

    # --- Contacts (592 contacts) ---

    def _search_contacts(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query= (name, email, owner_type, or additional_info)"
        q = f"%{query}%"
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT id, first_name, last_name, patronymic, email,
                          owner_type, additional_info, black_list
                   FROM contacts
                   WHERE first_name LIKE ? OR last_name LIKE ? OR email LIKE ?
                      OR owner_type LIKE ? OR additional_info LIKE ?
                   ORDER BY last_name, first_name LIMIT 8""",
                (q, q, q, q, q),
            ).fetchall()
        if not rows:
            return f"No contacts found for: {query}"
        return f"Found {len(rows)} contact(s):\n" + rows_to_json(rows)

    def _get_contact(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query=<contact_id>"
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM contacts WHERE id=?", (int(query),)).fetchone()
        return json.dumps(dict(row), ensure_ascii=False, indent=2, default=str) if row else f"Contact #{query} not found"

    def _list_contacts_by_type(self, query: str | None = None, **_) -> str:
        with get_conn() as conn:
            if query:
                rows = conn.execute(
                    """SELECT id, first_name, last_name, email, owner_type, additional_info
                       FROM contacts WHERE owner_type LIKE ? AND (black_list IS NULL OR black_list=0)
                       ORDER BY last_name, first_name LIMIT 8""",
                    (f"%{query}%",),
                ).fetchall()
            else:
                # Show summary by type
                rows_summary = conn.execute(
                    "SELECT owner_type, COUNT(*) as cnt FROM contacts GROUP BY owner_type ORDER BY cnt DESC"
                ).fetchall()
                return "Contacts by type:\n" + rows_to_json(rows_summary)
        return f"{len(rows)} contacts of type '{query}':\n" + rows_to_json(rows)

    # --- RAO Registry (322,689 registered works) ---

    def _search_rao(self, query: str | None = None, **_) -> str:
        """Search RAO registry by work title (fast full-text style via LIKE on indexed column)."""
        if not query:
            return "Provide query= (work title or part of title)"
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT id, title, genre, composer, lyrics_author, other_authors
                   FROM rao_registry
                   WHERE title LIKE ?
                   ORDER BY title LIMIT 8""",
                (f"%{query.upper()}%",),
            ).fetchall()
        if not rows:
            # Try case-insensitive fallback
            with get_conn() as conn:
                rows = conn.execute(
                    """SELECT id, title, genre, composer, lyrics_author, other_authors
                       FROM rao_registry
                       WHERE UPPER(title) LIKE ?
                       ORDER BY title LIMIT 8""",
                    (f"%{query.upper()}%",),
                ).fetchall()
        if not rows:
            return f"Произведение '{query}' не найдено в реестре РАО ({322689:,} записей)."
        return f"Найдено {len(rows)} в реестре РАО:\n" + rows_to_json(rows)

    def _search_rao_by_composer(self, query: str | None = None, **_) -> str:
        """Search RAO registry by composer or lyrics author name."""
        if not query:
            return "Provide query= (composer or author name)"
        q = f"%{query.upper()}%"
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT id, title, genre, composer, lyrics_author, other_authors
                   FROM rao_registry
                   WHERE UPPER(composer) LIKE ? OR UPPER(lyrics_author) LIKE ?
                      OR UPPER(other_authors) LIKE ?
                   ORDER BY composer, title LIMIT 8""",
                (q, q, q),
            ).fetchall()
        if not rows:
            return f"Автор '{query}' не найден в реестре РАО."
        # Group by composer for summary
        composers: dict[str, int] = {}
        for r in rows:
            key = r["composer"] or r["lyrics_author"] or "?"
            composers[key] = composers.get(key, 0) + 1
        summary = "\n".join(f"  {k}: {v} произведений" for k, v in composers.items())
        return (
            f"Найдено {len(rows)} произведений для автора '{query}' в реестре РАО:\n"
            f"{summary}\n\n"
            + rows_to_json(rows[:20])
        )

    def _search_rao_by_genre(self, query: str | None = None, **_) -> str:
        """List works in RAO registry by genre."""
        if not query:
            return (
                "Provide query= (genre name). Common genres:\n"
                "ПЕСНЯ, ПЬЕСА ИНСТРУМЕНТАЛЬНАЯ, РЕКЛАМА (МУЗЫКА), "
                "МУЗЫКА К КИНОФИЛЬМУ, РОМАНС, СТИХОТВОРЕНИЕ / БАЛЛАДА"
            )
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT id, title, composer, lyrics_author, other_authors
                   FROM rao_registry
                   WHERE UPPER(genre) LIKE ?
                   ORDER BY title LIMIT 8""",
                (f"%{query.upper()}%",),
            ).fetchall()
        return f"{len(rows)} произведений жанра '{query}':\n" + rows_to_json(rows)

    def _stats_rao(self, **_) -> str:
        with get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM rao_registry").fetchone()[0]
            genres = conn.execute(
                "SELECT genre, COUNT(*) c FROM rao_registry GROUP BY genre ORDER BY c DESC LIMIT 15"
            ).fetchall()
            top_composers = conn.execute(
                """SELECT composer, COUNT(*) c FROM rao_registry
                   WHERE composer IS NOT NULL
                   GROUP BY composer ORDER BY c DESC LIMIT 10"""
            ).fetchall()
        return (
            f"=== Реестр РАО ===\n"
            f"Всего произведений: {total:,}\n\n"
            f"Топ жанров:\n"
            + "\n".join(f"  {r['genre'] or 'н/у':<50} {r['c']:>6}" for r in genres)
            + f"\n\nТоп композиторов:\n"
            + "\n".join(f"  {r['composer']}: {r['c']}" for r in top_composers)
        )

    # --- Book: "Музыкальный редактор" (Denis Sharko) — FTS5 full-text search ---

    def _search_book(self, query: str | None = None, **_) -> str:
        """Full-text search across the book using SQLite FTS5."""
        if not query:
            return (
                "Provide query= (any topic: роялти, правообладатель, договор, "
                "синхронизация, лицензия, налоги, переговоры, ...)"
            )
        with get_conn() as conn:
            # Check FTS table exists
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='book_fts'"
            ).fetchone()
            if not exists:
                return "Книга не загружена. Run: python scripts/import_book.py"

            rows = conn.execute(
                """SELECT bc.chapter,
                          snippet(book_fts, 1, '**', '**', '...', 40) as excerpt,
                          bc.char_count
                   FROM book_fts
                   JOIN book_chapters bc ON bc.id = book_fts.rowid
                   WHERE book_fts MATCH ?
                   ORDER BY rank
                   LIMIT 5""",
                (query,),
            ).fetchall()

        if not rows:
            return f"По запросу «{query}» ничего не найдено в книге «Музыкальный редактор»."

        results = []
        for r in rows:
            results.append({
                "chapter": r["chapter"],
                "excerpt": r["excerpt"],
            })
        header = f"Найдено {len(rows)} релевантных фрагментов из книги «Музыкальный редактор» (Денис Шарко):\n"
        return header + json.dumps(results, ensure_ascii=False, indent=2)

    def _get_book_chapter(self, query: str | None = None, **_) -> str:
        """Get full text of a specific chapter by name or number."""
        if not query:
            return "Provide query= (chapter name or 'Глава 3')"
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT id, chapter, content, char_count FROM book_chapters WHERE chapter LIKE ? ORDER BY id LIMIT 5",
                (f"%{query}%",),
            ).fetchall()
        if not rows:
            return f"Глава «{query}» не найдена. Используй list_book_chapters."
        # Return first match full text
        r = rows[0]
        return (
            f"=== {r['chapter']} ({r['char_count']} символов) ===\n\n"
            + r["content"][:6000]
            + ("\n\n[текст обрезан, глава продолжается...]" if r["char_count"] > 6000 else "")
        )

    def _list_book_chapters(self, **_) -> str:
        """List all chapters/sections stored from the book."""
        with get_conn() as conn:
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='book_chapters'"
            ).fetchone()
            if not exists:
                return "Книга не загружена. Run: python scripts/import_book.py"
            rows = conn.execute(
                "SELECT id, chapter, char_count FROM book_chapters ORDER BY id"
            ).fetchall()
        lines = [f"Книга «Музыкальный редактор» — {len(rows)} разделов:\n"]
        for r in rows:
            lines.append(f"  #{r['id']:>3} [{r['char_count']:>5} символов] {r['chapter']}")
        return "\n".join(lines)

    # --- Catalog (new tracks added by agents, JSON sidecar) ---

    @property
    def _catalog_path(self) -> Path:
        p = DB_PATH.parent / "catalog.json"
        if not p.exists():
            p.write_text(json.dumps({"tracks": []}, ensure_ascii=False, indent=2))
        return p

    def _load_catalog(self) -> dict:
        return json.loads(self._catalog_path.read_text(encoding="utf-8"))

    def _save_catalog(self, cat: dict) -> None:
        self._catalog_path.write_text(json.dumps(cat, ensure_ascii=False, indent=2), encoding="utf-8")

    def _add_track(self, data: dict | None = None, **_) -> str:
        if not data:
            return "Provide data={title, artist, ...}"
        cat = self._load_catalog()
        data["id"] = f"new_{len(cat['tracks']) + 1:04d}"
        data["created_at"] = datetime.now().isoformat()
        cat["tracks"].append(data)
        self._save_catalog(cat)
        return f"Track added: {data['id']}\n{json.dumps(data, ensure_ascii=False, indent=2)}"

    def _list_new_tracks(self, **_) -> str:
        return json.dumps(self._load_catalog()["tracks"], ensure_ascii=False, indent=2)

    # --- ГК РФ Часть 4 (126 статей, FTS5) ---

    def _search_gk_rf4(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query= (тема, ключевые слова, номер статьи)"
        with get_conn() as conn:
            # Try FTS first
            try:
                rows = conn.execute(
                    """SELECT g.article_num, g.title,
                              snippet(gk_rf4_fts, 1, '>>', '<<', '...', 30) as snip
                       FROM gk_rf4_fts
                       JOIN gk_rf4 g ON g.id = gk_rf4_fts.rowid
                       WHERE gk_rf4_fts MATCH ?
                       ORDER BY rank LIMIT 5""",
                    (query,),
                ).fetchall()
            except Exception:
                rows = []
            if not rows:
                # Fallback: LIKE search
                q = f"%{query}%"
                rows = conn.execute(
                    """SELECT article_num, title, substr(content,1,300) as snip
                       FROM gk_rf4
                       WHERE title LIKE ? OR content LIKE ?
                       ORDER BY article_num LIMIT 5""",
                    (q, q),
                ).fetchall()
        if not rows:
            return f"Статьи по запросу '{query}' не найдены в ГК РФ ч.4"
        out = [f"ГК РФ ч.4 — результаты по '{query}':\n"]
        for r in rows:
            out.append(f"Ст.{r[0]} {r[1][:60]}\n  {r[2][:200]}\n")
        return "\n".join(out)

    def _get_gk_rf4_article(self, query: str | None = None, **_) -> str:
        if not query:
            return "Provide query=<article_number> (например: 1235)"
        with get_conn() as conn:
            try:
                art_num = int(query.strip())
                row = conn.execute(
                    "SELECT article_num, title, content FROM gk_rf4 WHERE article_num=?",
                    (art_num,),
                ).fetchone()
            except ValueError:
                # Search by title keyword
                q = f"%{query}%"
                row = conn.execute(
                    "SELECT article_num, title, content FROM gk_rf4 WHERE title LIKE ? ORDER BY article_num LIMIT 1",
                    (q,),
                ).fetchone()
        if not row:
            return f"Статья {query} не найдена в ГК РФ ч.4"
        return f"Статья {row[0]}. {row[1]}\n\n{row[2]}"

    def _list_gk_rf4(self, **_) -> str:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT article_num, title FROM gk_rf4 ORDER BY article_num"
            ).fetchall()
        return "\n".join(f"Ст.{r[0]}: {r[1][:80]}" for r in rows)
