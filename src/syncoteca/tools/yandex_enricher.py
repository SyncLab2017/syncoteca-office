"""Yandex Music track enrichment via ZenRows proxy.

Replicates enrich_universal.js logic in Python for Telegram bot integration.
Fetches tracks where album_processed=false, enriches via Yandex Music API,
updates Supabase, and reports progress.
"""
import json
import os
import re
import time
from typing import Callable, Optional

import httpx


ZENROWS_KEY = os.getenv("ZENROWS_KEY", "ed22055fcc6e65f4ebb401a7fdb3243c11592594")
DELAY_S = 4.0
DEFAULT_BATCH = 250


def _sb_headers() -> dict:
    key = os.getenv("SUPABASE_KEY", "")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def _sb_base() -> str:
    return os.getenv("SUPABASE_URL", "").rstrip("/")


def get_empty_tracks(limit: int = DEFAULT_BATCH, source: Optional[str] = None) -> list[dict]:
    """Tracks with album_processed=false and link set."""
    params = {
        "album_processed": "is.false",
        "link": "neq.",
        "select": "*",
        "limit": str(limit),
        "order": "id.asc",
    }
    if source:
        params["source_type"] = f"eq.{source}"
    r = httpx.get(f"{_sb_base()}/rest/v1/tracks", headers=_sb_headers(), params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def count_empty_tracks() -> int:
    """Count tracks with album_processed=false and link set."""
    params = {
        "album_processed": "is.false",
        "link": "neq.",
        "select": "id",
        "limit": "1",
    }
    headers = {**_sb_headers(), "Prefer": "count=exact"}
    r = httpx.get(f"{_sb_base()}/rest/v1/tracks", headers=headers, params=params, timeout=15)
    r.raise_for_status()
    content_range = r.headers.get("Content-Range", "")
    m = re.search(r'/(\d+)', content_range)
    return int(m.group(1)) if m else 0


def _zenrows_get(url: str) -> dict:
    params = {
        "apikey": ZENROWS_KEY,
        "url": url,
        "antibot": "true",
        "proxy_country": "ru",
        "premium_proxy": "true",
    }
    r = httpx.get("https://api.zenrows.com/v1/", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _extract_ids(link: str) -> Optional[tuple[str, str]]:
    m = re.search(r'/album/(\d+)/track/(\d+)', link or '')
    return (m.group(1), m.group(2)) if m else None


def _format_duration(ms) -> Optional[str]:
    if not ms:
        return None
    total_s = int(ms) // 1000
    return f"{total_s // 60:02d}:{total_s % 60:02d}"


def fetch_track_info(track_id: str) -> Optional[dict]:
    try:
        raw = _zenrows_get(f"https://api.music.yandex.ru/tracks/{track_id}")
        body = raw.get("data") or raw.get("body") or raw
        if isinstance(body, str):
            body = json.loads(body)
        result = body.get("result")
        if not result:
            return None
        track = result[0] if isinstance(result, list) else result
        if not track.get("title"):
            return None

        performers = [a["name"] for a in track.get("artists", []) if not a.get("composer")]
        composers = [a["name"] for a in track.get("artists", []) if a.get("composer")]
        album_obj = (track.get("albums") or [{}])[0]

        release_date = None
        if album_obj.get("releaseDate"):
            try:
                from datetime import date
                release_date = date.fromisoformat(album_obj["releaseDate"][:10]).strftime("%d.%m.%Y")
            except Exception:
                pass
        elif album_obj.get("year"):
            release_date = str(album_obj["year"])

        return {
            "title": track.get("title"),
            "artist": ", ".join(performers) or None,
            "album": album_obj.get("title"),
            "duration": _format_duration(track.get("durationMs")),
            "release_date": release_date,
            "genre_1": album_obj.get("genre"),
            "music_author": ", ".join(composers) or None,
        }
    except Exception:
        return None


def fetch_credits(track_id: str) -> dict:
    try:
        raw = _zenrows_get(f"https://api.music.yandex.ru/tracks/{track_id}/credits")
        body = raw.get("data") or raw.get("body") or raw
        if isinstance(body, str):
            body = json.loads(body)
        credits = (body.get("result") or {}).get("credits") or []
        find = lambda key: next((c["value"] for c in credits if c.get("title") == key), None)
        return {
            "label": find("Лейбл"),
            "music_author": find("Автор музыки") or find("Композитор"),
            "lyrics_author": find("Автор текста") or find("Автор слов"),
        }
    except Exception:
        return {}


def update_track(link: str, fields: dict) -> None:
    r = httpx.patch(
        f"{_sb_base()}/rest/v1/tracks",
        headers=_sb_headers(),
        params={"link": f"eq.{link}"},
        json=fields,
        timeout=15,
    )
    r.raise_for_status()


def enrich_batch(
    limit: int = DEFAULT_BATCH,
    source: Optional[str] = None,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    """Enrich a batch of empty tracks.

    progress_cb(done, total, last_title) called every 10 tracks.
    Returns {"total": N, "ok": N, "skipped": N, "errors": N}.
    """
    tracks = get_empty_tracks(limit=limit, source=source)
    total = len(tracks)
    if not tracks:
        return {"total": 0, "ok": 0, "skipped": 0, "errors": 0, "min_id": 0}

    min_id = tracks[0].get("id", 0) if tracks else 0

    ok = skipped = errors = 0

    for i, t in enumerate(tracks):
        link = t.get("link", "")
        ids = _extract_ids(link)
        if not ids:
            skipped += 1
            continue

        _, track_id = ids
        updates: dict = {"album_processed": True}

        needs_meta = not t.get("title") or not t.get("artist")
        needs_credits = not t.get("lyrics_author") or not t.get("music_author") or not t.get("label")

        if needs_meta:
            info = fetch_track_info(track_id)
            if info:
                for field in ("title", "artist", "album", "duration", "release_date", "genre_1", "music_author"):
                    if not t.get(field) and info.get(field):
                        updates[field] = info[field]
                if updates.get("music_author"):
                    needs_credits = not t.get("lyrics_author") or not t.get("label")

        if needs_credits:
            cr = fetch_credits(track_id)
            for field in ("label", "music_author", "lyrics_author"):
                if not t.get(field) and cr.get(field):
                    updates[field] = cr[field]

        try:
            update_track(link, updates)
            ok += 1
        except Exception:
            errors += 1
            time.sleep(DELAY_S)
            continue

        last_title = (updates.get("title") or t.get("title") or "?")
        last_artist = (updates.get("artist") or t.get("artist") or "?")
        if progress_cb and (i + 1) % 10 == 0:
            progress_cb(i + 1, total, f"«{last_title}» — {last_artist}")

        time.sleep(DELAY_S)

    return {"total": total, "ok": ok, "skipped": skipped, "errors": errors, "min_id": min_id}
