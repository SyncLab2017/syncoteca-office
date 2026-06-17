"""Discogs release-date verification for Supabase tracks table.

Queries tracks WHERE release_date IS NULL (or all tracks), searches
Discogs for the earliest known year, and patches the record in Supabase.
Designed to run as a background asyncio task with Telegram progress reports.
"""
import asyncio
import json
import os
import re
import time
from typing import Callable, Optional

import httpx

_DELAY_S = 1.3  # Discogs rate-limit: ~60 req/min; 1.3s is safe
_REPORT_EVERY = 10  # send Telegram progress every N tracks

_ARTIST_ALIASES = {
    "Дельфин": "Dolphin",
    "Леонид Утёсов": "Леонид Утесов",
    "Алёна Апина": "Алена Апина",
    "Вирус": "Вирус!",
    "Белый орёл": "Белый Орёл",
}


# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_headers() -> dict:
    key = os.getenv("SUPABASE_KEY", "")
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

def _sb_base() -> str:
    return os.getenv("SUPABASE_URL", "").rstrip("/")


_PAGE = 1000  # Supabase Max Rows per request


def get_tracks_batch(
    limit: int = 500,
    only_null: bool = True,
    after_id: int = 0,
    label: Optional[str] = None,
    artist: Optional[str] = None,
    date_from: Optional[str] = None,
) -> list[dict]:
    """Return up to `limit` tracks to process from Supabase (paginated).

    only_null=True  → WHERE release_date IS NULL  (unprocessed tracks)
    only_null=False → all tracks, id > after_id  (full re-verification)
    label           → filter by label name (ilike)
    artist          → filter by artist name (ilike)
    date_from       → filter by created_at >= YYYY-MM-DD
    """
    base_params: dict = {
        "select": "id,title,artist,album,release_date,label",
        "order": "id.asc",
    }
    if only_null:
        base_params["release_date"] = "is.null"
    if label:
        base_params["label"] = f"ilike.*{label}*"
    if artist:
        base_params["artist"] = f"ilike.*{artist}*"
    if date_from:
        base_params["created_at"] = f"gte.{date_from}T00:00:00"

    all_rows: list[dict] = []
    # Use cursor pagination by id — more reliable than offset for large tables
    current_after_id = after_id
    while len(all_rows) < limit:
        page_limit = min(_PAGE, limit - len(all_rows))
        params = {**base_params, "limit": str(page_limit), "id": f"gt.{current_after_id}"}
        r = httpx.get(f"{_sb_base()}/rest/v1/tracks", headers=_sb_headers(), params=params, timeout=10)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        all_rows.extend(batch)
        if len(batch) < page_limit:
            break
        current_after_id = batch[-1]["id"]
    return all_rows


def patch_track_date_by_search(artist_q: str, title_q: str, new_year: str) -> int:
    """Find tracks matching artist+title (ilike) and set release_date. Returns count updated."""
    params = {
        "select": "id",
        "artist": f"ilike.*{artist_q}*",
        "title": f"ilike.*{title_q}*",
        "limit": "10",
    }
    r = httpx.get(f"{_sb_base()}/rest/v1/tracks", headers=_sb_headers(), params=params, timeout=10)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return 0
    for row in rows:
        update_track_date(row["id"], new_year)
    return len(rows)


def update_track_date(track_id: int, new_date: str) -> None:
    """PATCH release_date for a single track in Supabase."""
    r = httpx.patch(
        f"{_sb_base()}/rest/v1/tracks",
        headers={**_sb_headers(), "Prefer": "return=minimal"},
        params={"id": f"eq.{track_id}"},
        json={"release_date": new_date},
        timeout=10,
    )
    r.raise_for_status()


# ── Discogs helpers ───────────────────────────────────────────────────────────

def _discogs_token() -> str:
    return os.getenv("DISCOGS_TOKEN", "")


def _normalize_artist(artist: str) -> str:
    a = artist.split(",")[0].strip()
    return _ARTIST_ALIASES.get(a, a)


_CYRILLIC = re.compile(r'[а-яёА-ЯЁ]')
_LATIN = re.compile(r'[a-zA-Z]')


def _extract_min_year_and_url(results: list, artist_norm: str) -> tuple[int, str] | None:
    a_key = re.sub(r"[^a-zа-яёA-ZА-ЯЁ0-9]", "", artist_norm.lower())
    # Detect script: if artist is Cyrillic but Discogs result artists are Latin (or vice versa),
    # skip artist-match check — can't compare cross-script strings.
    a_is_cyrillic = bool(_CYRILLIC.search(a_key))
    a_is_latin = bool(_LATIN.search(a_key))
    best: tuple[int, str] | None = None
    for r in results:
        title = r.get("title", "")
        if "various" in title.lower():
            continue
        artist_part = re.sub(r"[^a-zа-яёA-ZА-ЯЁ0-9]", "", title.split(" - ")[0].lower())
        # Cross-script mismatch: relax artist check
        ap_is_cyrillic = bool(_CYRILLIC.search(artist_part))
        ap_is_latin = bool(_LATIN.search(artist_part))
        cross_script = (a_is_cyrillic and ap_is_latin) or (a_is_latin and ap_is_cyrillic)
        if not cross_script and not (artist_part in a_key or a_key in artist_part):
            continue
        y = r.get("year")
        if y and 1900 < int(y) <= 2030:
            url = r.get("uri", "")
            if url and not url.startswith("http"):
                url = f"https://www.discogs.com{url}"
            if best is None or int(y) < best[0]:
                best = (int(y), url)
    return best


def _discogs_request(params: dict) -> dict | None:
    token = _discogs_token()
    if not token:
        return None
    for attempt in range(3):
        try:
            r = httpx.get(
                "https://api.discogs.com/database/search",
                params=params,
                headers={
                    "Authorization": f"Discogs token={token}",
                    "User-Agent": "SynclabDateFixer/1.0 +https://synclab.pro",
                },
                timeout=15,
            )
            if r.status_code == 429:
                time.sleep(35)
                continue
            if r.status_code == 401:
                raise RuntimeError("Discogs token invalid — stop")
            if not r.is_success:
                return None
            return r.json()
        except httpx.TimeoutException:
            return None
    return None


def search_discogs_year_by_album(artist: str, album: str) -> tuple[int, str] | None:
    """Return (earliest year, source URL) for artist/album from Discogs, or None."""
    if not album:
        return None
    sa = _normalize_artist(artist)
    sa_noyo = sa.replace("ё", "е").replace("Ё", "Е")
    candidates = [sa] if sa == sa_noyo else [sa, sa_noyo]
    for search_artist in candidates:
        for search_type in ("master", "release"):
            data = _discogs_request({
                "artist": search_artist,
                "release_title": album,
                "type": search_type,
                "per_page": "5",
            })
            if data:
                result = _extract_min_year_and_url(data.get("results", []), search_artist)
                if result:
                    return result
        for search_type in ("master", "release"):
            data = _discogs_request({"q": f"{search_artist} {album}", "type": search_type, "per_page": "8"})
            if data:
                result = _extract_min_year_and_url(data.get("results", []), search_artist)
                if result:
                    return result
    return None


def search_discogs_year(artist: str, title: str) -> tuple[int, str] | None:
    """Return (earliest year, source URL) for artist/title from Discogs, or None."""
    title = title.replace("🔞", "").strip()
    sa = _normalize_artist(artist)
    sa_noyo = sa.replace("ё", "е").replace("Ё", "Е")
    candidates = [sa] if sa == sa_noyo else [sa, sa_noyo]

    for search_artist in candidates:
        for search_type in ("master", "release"):
            data = _discogs_request({"artist": search_artist, "track": title, "type": search_type, "per_page": "5"})
            if data:
                result = _extract_min_year_and_url(data.get("results", []), search_artist)
                if result:
                    return result
        for search_type in ("master", "release"):
            data = _discogs_request({"q": f"{search_artist} {title}", "type": search_type, "per_page": "8"})
            if data:
                result = _extract_min_year_and_url(data.get("results", []), search_artist)
                if result:
                    return result
    return None


def search_musicbrainz_year(artist: str, title: str) -> tuple[int, str] | None:
    """Return (earliest year, MusicBrainz URL) for artist/title, or None.

    Uses the MusicBrainz JSON API (1 req/sec rate limit; we sleep in caller).
    Good fallback for Soviet/Russian artists absent or mis-dated on Discogs.
    """
    sa = _normalize_artist(artist)
    # Try with and without ё→е substitution
    sa_noyo = sa.replace("ё", "е").replace("Ё", "Е")
    title_clean = title.replace("🔞", "").strip()
    candidates = [sa] if sa == sa_noyo else [sa, sa_noyo]

    for search_artist in candidates:
        # Lucene query: artist + recording title
        q = f'artist:"{search_artist}" AND recording:"{title_clean}"'
        try:
            r = httpx.get(
                "https://musicbrainz.org/ws/2/recording",
                params={"query": q, "fmt": "json", "limit": "10"},
                headers={"User-Agent": "SynclabDateFixer/1.0 (denis@synclab.pro)"},
                timeout=15,
            )
            if not r.is_success:
                continue
            recordings = r.json().get("recordings", [])
            best_year: int | None = None
            best_url = ""
            for rec in recordings:
                date_str = rec.get("first-release-date", "")
                if not date_str:
                    continue
                m = re.match(r"(\d{4})", date_str)
                if not m:
                    continue
                y = int(m.group(1))
                if 1900 < y <= 2030 and (best_year is None or y < best_year):
                    best_year = y
                    rec_id = rec.get("id", "")
                    best_url = f"https://musicbrainz.org/recording/{rec_id}" if rec_id else ""
            if best_year:
                return (best_year, best_url)
        except Exception:
            continue
    return None


def _extract_year(s: str | None) -> int | None:
    if not s:
        return None
    m = re.search(r"\((\d{4})\)", str(s))
    if m:
        return int(m.group(1))
    m = re.search(r"\b(19\d{2}|20\d{2})\b", str(s))
    return int(m.group(1)) if m else None


# ── Background task ───────────────────────────────────────────────────────────

# Module-level flag to prevent two runs simultaneously
_running: bool = False


async def run_date_fix(
    chat_id: int,
    bot,
    limit: int = 500,
    only_null: bool = True,
    after_id: int = 0,
    label: Optional[str] = None,
    artist: Optional[str] = None,
    date_from: Optional[str] = None,
) -> None:
    """Background asyncio task: fix Discogs dates and report to Telegram."""
    global _running

    if _running:
        await bot.send_message(chat_id, "⚠️ Проверка дат уже идёт. Подожди завершения.")
        return

    if not _discogs_token():
        await bot.send_message(chat_id, "❌ DISCOGS_TOKEN не задан в Railway → Variables.")
        return

    _running = True
    loop = asyncio.get_event_loop()

    try:
        scope = "только без даты" if only_null else "все треки"
        scope_note = f" (id > {after_id})" if after_id else ""
        if label:
            scope_note += f" | лейбл: {label}"
        if artist:
            scope_note += f" | артист: {artist}"
        if date_from:
            scope_note += f" | с {date_from}"
        await bot.send_message(
            chat_id,
            f"🗃️ Ковальски запускает проверку дат\n"
            f"Источники: Discogs + MusicBrainz\n"
            f"Режим: {scope}{scope_note} | Лимит: {limit}\n"
            f"Скорость: ~2-3 сек/трек → {limit * 2 // 60 + 1} мин",
        )

        # Fetch batch (sync, runs in executor)
        tracks = await loop.run_in_executor(
            None,
            lambda: get_tracks_batch(limit, only_null, after_id, label=label, artist=artist, date_from=date_from),
        )

        if not tracks:
            await bot.send_message(chat_id, "✅ Ковальски: треков без даты не найдено — база актуальна.")
            return

        start_msg = await bot.send_message(chat_id, f"📋 Найдено {len(tracks)} треков для проверки. Начинаю…")
        progress_msg = await bot.send_message(chat_id, "⏳ [0/" + str(len(tracks)) + "] Стартую…")

        updated = skipped = not_found = errors = 0
        updated_log: list[str] = []  # "Артист — Трек (старый_год → новый) — URL"
        # Album-level cache: (artist, album) → (year, url) | None
        _album_cache: dict[tuple[str, str], tuple[int, str] | None] = {}

        for i, track in enumerate(tracks):
            _artist = track.get("artist") or ""
            title = track.get("title") or ""
            _album = track.get("album") or ""
            track_id = track.get("id")
            current_date = track.get("release_date")
            current_year = _extract_year(current_date)
            made_api_call = False

            try:
                year: int | None = None
                source_url: str = ""
                cache_key = (_artist, _album) if _album else None

                # 1. Try album-level cache
                if cache_key is not None:
                    if cache_key in _album_cache:
                        cached = _album_cache[cache_key]
                        if cached:
                            year, source_url = cached
                    else:
                        res = await loop.run_in_executor(None, search_discogs_year_by_album, _artist, _album)
                        _album_cache[cache_key] = res
                        made_api_call = True
                        if res:
                            year, source_url = res

                # 2. Fallback: search Discogs by track title
                if year is None:
                    res2 = await loop.run_in_executor(None, search_discogs_year, _artist, title)
                    made_api_call = True
                    if res2:
                        year, source_url = res2

                # 3. MusicBrainz fallback — fires when Discogs found nothing
                if year is None and title:
                    await asyncio.sleep(_DELAY_S)  # MB rate limit 1 req/s
                    res3 = await loop.run_in_executor(None, search_musicbrainz_year, _artist, title)
                    made_api_call = True
                    if res3:
                        year, source_url = res3

                # 4. Cross-check: if Discogs returned ≥2000, see if MB has earlier date
                elif year is not None and year >= 2000 and title:
                    await asyncio.sleep(_DELAY_S)
                    res3 = await loop.run_in_executor(None, search_musicbrainz_year, _artist, title)
                    if res3 and res3[0] < year:
                        year, source_url = res3

                def _h(s: str) -> str:
                    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

                def _year_link(y: int, url: str) -> str:
                    return f'<a href="{url}">{y}</a>' if url else str(y)

                if year is None:
                    await loop.run_in_executor(None, update_track_date, track_id, "not_found")
                    not_found += 1
                elif current_year and year >= current_year:
                    if not current_date or current_date == "not_found":
                        await loop.run_in_executor(None, update_track_date, track_id, str(year))
                        updated += 1
                        updated_log.append(
                            f"• {_h(_artist)} — {_h(title)} (? → {_year_link(year, source_url)})"
                        )
                    else:
                        skipped += 1
                else:
                    await loop.run_in_executor(None, update_track_date, track_id, str(year))
                    updated += 1
                    old = str(current_year) if current_year else "?"
                    updated_log.append(
                        f"• {_h(_artist)} — {_h(title)} ({old} → {_year_link(year, source_url)})"
                    )

            except Exception as e:
                errors += 1
                made_api_call = True
                if "invalid" in str(e).lower():
                    await bot.send_message(chat_id, f"❌ Ковальски: Discogs token недействителен. Остановка.")
                    return

            # Edit single progress message every N tracks — show last 5 updates
            if (i + 1) % _REPORT_EVERY == 0:
                remaining = len(tracks) - (i + 1)
                recent = "\n".join(updated_log[-5:]) if updated_log else "—"
                try:
                    await progress_msg.edit_text(
                        f"⏳ [{i+1}/{len(tracks)}] осталось ~{remaining}с\n"
                        f"✅ Обновлено: {updated}\n"
                        f"Последние:\n{recent}",
                    )
                except Exception:
                    pass

            # Rate-limit: only sleep when a real Discogs request was made
            if made_api_call:
                await asyncio.sleep(_DELAY_S)

        try:
            await progress_msg.delete()
        except Exception:
            pass

        from datetime import date as _date
        today = _date.today().strftime("%d.%m.%Y")
        summary_lines = [
            f"🗃️ Ковальски: проверка дат завершена — {today}",
            f"✅ Обновлено: {updated} | 📊 Обработано: {len(tracks)}",
        ]
        if updated_log:
            # Telegram message limit ~4096 chars — each entry may have 2 lines (track + URL)
            shown = updated_log[:30]
            summary_lines.append("\n🎵 Обновлённые треки:")
            summary_lines.extend(shown)
            if len(updated_log) > 30:
                summary_lines.append(f"… и ещё {len(updated_log) - 30}")
        await bot.send_message(chat_id, "\n".join(summary_lines), parse_mode="HTML")

    finally:
        _running = False
