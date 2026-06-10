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
from typing import Callable

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


def get_tracks_batch(limit: int = 50, only_null: bool = True, after_id: int = 0) -> list[dict]:
    """Return up to `limit` tracks to process from Supabase.

    only_null=True  → WHERE release_date IS NULL  (unprocessed tracks)
    only_null=False → all tracks, id > after_id  (full re-verification)
    """
    params: dict = {
        "select": "id,title,artist,release_date",
        "order": "id.asc",
        "limit": str(limit),
    }
    if only_null:
        params["release_date"] = "is.null"
    if after_id:
        params["id"] = f"gt.{after_id}"

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


def _extract_min_year(results: list, artist_norm: str) -> int | None:
    a_key = re.sub(r"[^a-zа-яёA-ZА-ЯЁ0-9]", "", artist_norm.lower())
    years = []
    for r in results:
        title = r.get("title", "")
        if "various" in title.lower():
            continue
        artist_part = re.sub(r"[^a-zа-яёA-ZА-ЯЁ0-9]", "", title.split(" - ")[0].lower())
        if not (artist_part in a_key or a_key in artist_part):
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
    limit: int = 50,
    only_null: bool = True,
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
        await bot.send_message(
            chat_id,
            f"🗃️ Ковальски запускает проверку дат Discogs\n"
            f"Режим: {'только без даты' if only_null else 'все треки'} | Лимит: {limit}\n"
            f"Скорость: ~1 трек/сек → {limit // 60 + 1} мин",
        )

        # Fetch batch (sync, runs in executor)
        tracks = await loop.run_in_executor(None, get_tracks_batch, limit, only_null, 0)

        if not tracks:
            await bot.send_message(chat_id, "✅ Ковальски: треков без даты не найдено — база актуальна.")
            return

        await bot.send_message(chat_id, f"📋 Найдено {len(tracks)} треков для проверки. Начинаю…")

        updated = skipped = not_found = errors = 0

        for i, track in enumerate(tracks):
            artist = track.get("artist") or ""
            title = track.get("title") or ""
            track_id = track.get("id")
            current_date = track.get("release_date")
            current_year = _extract_year(current_date)

            try:
                year = await loop.run_in_executor(None, search_discogs_year, artist, title)

                if year is None:
                    # Mark as checked so it won't block future runs
                    await loop.run_in_executor(None, update_track_date, track_id, "not_found")
                    not_found += 1
                elif current_year and year >= current_year:
                    # No improvement — mark as verified with existing year
                    if not current_date or current_date == "not_found":
                        await loop.run_in_executor(None, update_track_date, track_id, str(year))
                        updated += 1
                    else:
                        skipped += 1
                else:
                    # Earlier year found — update
                    await loop.run_in_executor(None, update_track_date, track_id, str(year))
                    updated += 1

            except Exception as e:
                errors += 1
                if "invalid" in str(e).lower():
                    await bot.send_message(chat_id, f"❌ Ковальски: Discogs token недействителен. Остановка.")
                    return

            # Progress report every N tracks
            if (i + 1) % _REPORT_EVERY == 0:
                remaining = len(tracks) - (i + 1)
                await bot.send_message(
                    chat_id,
                    f"⏳ [{i+1}/{len(tracks)}] осталось ~{remaining}с\n"
                    f"✅ Обновлено: {updated} | ⏭ Без изменений: {skipped} | "
                    f"❓ Не найдено: {not_found} | ❌ Ошибки: {errors}",
                )

            await asyncio.sleep(_DELAY_S)

        await bot.send_message(
            chat_id,
            f"🗃️ Ковальски: проверка завершена\n"
            f"✅ Обновлено: {updated}\n"
            f"⏭ Без изменений: {skipped}\n"
            f"❓ Не найдено на Discogs: {not_found}\n"
            f"❌ Ошибки: {errors}\n"
            f"📊 Всего обработано: {len(tracks)}",
        )

    finally:
        _running = False
