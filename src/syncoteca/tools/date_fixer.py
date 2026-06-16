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


def get_tracks_batch(
    limit: int = 500,
    only_null: bool = True,
    after_id: int = 0,
    label: Optional[str] = None,
    artist: Optional[str] = None,
    date_from: Optional[str] = None,
) -> list[dict]:
    """Return up to `limit` tracks to process from Supabase.

    only_null=True  → WHERE release_date IS NULL  (unprocessed tracks)
    only_null=False → all tracks, id > after_id  (full re-verification)
    label           → filter by label name (ilike)
    artist          → filter by artist name (ilike)
    date_from       → filter by created_at >= YYYY-MM-DD
    """
    params: dict = {
        "select": "id,title,artist,album,release_date,label",
        "order": "id.asc",
        "limit": str(limit),
    }
    if only_null:
        params["release_date"] = "is.null"
    if after_id:
        params["id"] = f"gt.{after_id}"
    if label:
        params["label"] = f"ilike.*{label}*"
    if artist:
        params["artist"] = f"ilike.*{artist}*"
    if date_from:
        params["created_at"] = f"gte.{date_from}T00:00:00"

    r = httpx.get(f"{_sb_base()}/rest/v1/tracks", headers=_sb_headers(), params=params, timeout=10)
    r.raise_for_status()
    return r.json()


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


def _extract_min_year(results: list, artist_norm: str) -> int | None:
    a_key = re.sub(r"[^a-zа-яёA-ZА-ЯЁ0-9]", "", artist_norm.lower())
    # Detect script: if artist is Cyrillic but Discogs result artists are Latin (or vice versa),
    # skip artist-match check — can't compare cross-script strings.
    a_is_cyrillic = bool(_CYRILLIC.search(a_key))
    a_is_latin = bool(_LATIN.search(a_key))
    years = []
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
            years.append(int(y))
    return min(years) if years else None


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


def search_discogs_year_by_album(artist: str, album: str) -> int | None:
    """Return earliest Discogs release year for artist/album, or None."""
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
                y = _extract_min_year(data.get("results", []), search_artist)
                if y:
                    return y
        for search_type in ("master", "release"):
            data = _discogs_request({"q": f"{search_artist} {album}", "type": search_type, "per_page": "8"})
            if data:
                y = _extract_min_year(data.get("results", []), search_artist)
                if y:
                    return y
    return None


def search_discogs_year(artist: str, title: str) -> int | None:
    """Return earliest Discogs release year for artist/title, or None."""
    title = title.replace("🔞", "").strip()
    sa = _normalize_artist(artist)
    sa_noyo = sa.replace("ё", "е").replace("Ё", "Е")
    candidates = [sa] if sa == sa_noyo else [sa, sa_noyo]

    for search_artist in candidates:
        for search_type in ("master", "release"):
            data = _discogs_request({"artist": search_artist, "track": title, "type": search_type, "per_page": "5"})
            if data:
                y = _extract_min_year(data.get("results", []), search_artist)
                if y:
                    return y
        for search_type in ("master", "release"):
            data = _discogs_request({"q": f"{search_artist} {title}", "type": search_type, "per_page": "8"})
            if data:
                y = _extract_min_year(data.get("results", []), search_artist)
                if y:
                    return y
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
            f"🗃️ Ковальски запускает проверку дат Discogs\n"
            f"Режим: {scope}{scope_note} | Лимит: {limit}\n"
            f"Скорость: ~1 трек/сек → {limit // 60 + 1} мин",
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
        updated_log: list[str] = []  # "Артист — Трек (старый_год → новый)"
        # Album-level year cache: (artist, album) → year or None (None = not found on Discogs)
        _album_cache: dict[tuple[str, str], int | None] = {}

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
                cache_key = (_artist, _album) if _album else None

                # 1. Try album-level cache
                if cache_key is not None:
                    if cache_key in _album_cache:
                        year = _album_cache[cache_key]
                    else:
                        year = await loop.run_in_executor(None, search_discogs_year_by_album, _artist, _album)
                        _album_cache[cache_key] = year
                        made_api_call = True

                # 2. Fallback: search by track title
                if year is None:
                    year = await loop.run_in_executor(None, search_discogs_year, _artist, title)
                    made_api_call = True

                if year is None:
                    await loop.run_in_executor(None, update_track_date, track_id, "not_found")
                    not_found += 1
                elif current_year and year >= current_year:
                    if not current_date or current_date == "not_found":
                        await loop.run_in_executor(None, update_track_date, track_id, str(year))
                        updated += 1
                        updated_log.append(f"• {_artist} — {title} ({year})")
                    else:
                        skipped += 1
                else:
                    await loop.run_in_executor(None, update_track_date, track_id, str(year))
                    updated += 1
                    old = str(current_year) if current_year else "?"
                    updated_log.append(f"• {_artist} — {title} ({old} → {year})")

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
            # Telegram message limit ~4096 chars — cap list at 50 entries
            shown = updated_log[:50]
            summary_lines.append("\n🎵 Обновлённые треки:")
            summary_lines.extend(shown)
            if len(updated_log) > 50:
                summary_lines.append(f"… и ещё {len(updated_log) - 50}")
        await bot.send_message(chat_id, "\n".join(summary_lines))

    finally:
        _running = False
