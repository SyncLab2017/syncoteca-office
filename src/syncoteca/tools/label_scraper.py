"""Yandex Music label catalog scraper.

Fetches all albums + tracks for a label via the Yandex Music API through
ZenRows proxy, storing results in the label_catalog Supabase table.
Mirrors label_catalog_scraper.js logic in Python for Railway deployment.
"""
import json
import os
import re
import time
from datetime import date
from typing import Callable, Optional

import httpx

ZENROWS_KEY = os.getenv("ZENROWS_KEY", "ed22055fcc6e65f4ebb401a7fdb3243c11592594")
DELAY_ALBUM_S = 5.0
AVG_TRACKS_PER_ALBUM = 10  # rough estimate for ETA

_running: bool = False
_cancel_requested: bool = False


def cancel_scrape() -> None:
    global _cancel_requested
    _cancel_requested = True


def is_running() -> bool:
    return _running


def analyze_label(label_id: str) -> Optional[dict]:
    """Fetch album count from page 0 pager. Returns {album_count, estimated_tracks, eta_seconds} or None."""
    try:
        raw = _zenrows_get(f"https://api.music.yandex.ru/labels/{label_id}/albums?page=0")
        body = _parse_body(raw)
        result = body.get("result") or {}
        pager = result.get("pager") or {}
        album_count = pager.get("total", 0)
        already_done = len(get_processed_album_ids(label_id))
        remaining = max(0, album_count - already_done)
        eta_s = int(remaining * (DELAY_ALBUM_S + 1.5))
        return {
            "album_count": album_count,
            "already_done": already_done,
            "remaining": remaining,
            "estimated_tracks": remaining * AVG_TRACKS_PER_ALBUM,
            "eta_seconds": eta_s,
        }
    except Exception:
        return None


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


def _parse_body(raw: dict) -> dict:
    body = raw.get("data") or raw.get("body") or raw
    if isinstance(body, str):
        body = json.loads(body)
    return body


def find_label_in_db(query: str) -> Optional[tuple[str, str]]:
    """Search labels table by name. Returns (yandex_id, label_name) or None."""
    params = {
        "select": "id,name",
        "name": f"ilike.*{query}*",
        "limit": "5",
        "order": "name.asc",
    }
    r = httpx.get(f"{_sb_base()}/rest/v1/labels", headers=_sb_headers(), params=params, timeout=10)
    r.raise_for_status()
    results = r.json()
    if results:
        return (results[0]["id"], results[0]["name"])
    return None


def find_sublabels(label_name: str) -> list[dict]:
    """Return sublabels where parent = label_name. Each item: {id, name}."""
    params = {
        "select": "id,name",
        "parent": f"eq.{label_name}",
        "order": "name.asc",
        "limit": "200",
    }
    r = httpx.get(f"{_sb_base()}/rest/v1/labels", headers=_sb_headers(), params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_label_album_ids(label_id: str) -> list[str]:
    """Fetch all album IDs for a label via Yandex Music API (paginated)."""
    all_ids: list[str] = []
    page = 0
    while True:
        raw = _zenrows_get(f"https://api.music.yandex.ru/labels/{label_id}/albums?page={page}")
        body = _parse_body(raw)
        result = body.get("result")
        if not result:
            break
        albums = result.get("albums") or []
        if not albums:
            break
        for a in albums:
            all_ids.append(str(a["id"]))
        pager = result.get("pager") or {}
        total = pager.get("total", 0)
        per_page = pager.get("perPage", 100)
        page += 1
        if page * per_page >= total:
            break
        time.sleep(1.0)
    return list(dict.fromkeys(all_ids))  # deduplicate, preserve order


def fetch_album_tracks(album_id: str) -> Optional[dict]:
    """Fetch album data with full track list from Yandex Music API."""
    raw = _zenrows_get(f"https://api.music.yandex.ru/albums/{album_id}/with-tracks")
    body = _parse_body(raw)
    return body.get("result")


def get_processed_album_ids(label_id: str) -> set:
    """Return album IDs already stored in label_catalog for this label."""
    params = {"label_id": f"eq.{label_id}", "select": "album_id"}
    r = httpx.get(f"{_sb_base()}/rest/v1/label_catalog", headers=_sb_headers(), params=params, timeout=15)
    if not r.is_success:
        return set()
    return {row["album_id"] for row in r.json()}


def _insert_track(row: dict) -> str:
    """Insert one track row. Returns 'added', 'skipped', or 'error'."""
    headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=minimal"}
    r = httpx.post(f"{_sb_base()}/rest/v1/label_catalog", headers=headers, json=row, timeout=10)
    if r.status_code == 409:
        return "skipped"
    return "added" if r.is_success else "error"


def _fmt_duration(ms) -> str:
    if not ms:
        return "00:00"
    s = int(ms) // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


def _fmt_date(iso: Optional[str]) -> Optional[str]:
    if not iso:
        return None
    try:
        d = date.fromisoformat(iso[:10])
        return f"{d.day:02d}.{d.month:02d}.{d.year}"
    except Exception:
        return iso


def scrape_label(
    label_id: str,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    """Scrape full label catalog into label_catalog table.

    progress_cb(albums_done, albums_total, info_str) called after each album.
    Returns {"label_name", "added", "skipped", "errors", "albums_total", "albums_done"}.
    """
    global _running, _cancel_requested

    if _running:
        return {"error": "already_running"}

    _running = True
    _cancel_requested = False
    try:
        all_ids = fetch_label_album_ids(label_id)
        if not all_ids:
            return {"label_name": None, "added": 0, "skipped": 0, "errors": 0,
                    "albums_total": 0, "albums_done": 0}

        processed = get_processed_album_ids(label_id)
        to_process = [aid for aid in all_ids if aid not in processed]

        label_name = None
        added = skipped = errors = 0
        ai = -1

        cancelled = False
        for ai, album_id in enumerate(to_process):
            if _cancel_requested:
                cancelled = True
                break
            time.sleep(DELAY_ALBUM_S)
            if _cancel_requested:
                cancelled = True
                break
            try:
                album = fetch_album_tracks(album_id)
            except Exception:
                errors += 1
                continue

            if not album:
                continue

            if not label_name and album.get("labels"):
                label_name = album["labels"][0].get("name")

            album_title = album.get("title") or "?"
            album_artists = ", ".join(a["name"] for a in (album.get("artists") or []))
            album_year = str(album["year"]) if album.get("year") else None
            album_genre = album.get("genre")
            release_date = _fmt_date(album.get("releaseDate"))
            all_tracks = [t for vol in (album.get("volumes") or []) for t in vol]

            track_num = 0
            for track in all_tracks:
                if not track or not track.get("title") or track.get("isRemoved"):
                    continue
                track_num += 1
                track_id = str(track.get("id") or "")
                track_artists = ", ".join(
                    a["name"] for a in (track.get("artists") or album.get("artists") or [])
                )
                row = {
                    "label_id": label_id,
                    "label_name": label_name,
                    "album_id": str(album_id),
                    "album_title": album_title,
                    "track_id": track_id,
                    "track_title": track.get("title"),
                    "artist": track_artists or album_artists,
                    "release_year": album_year,
                    "release_date": release_date,
                    "duration": _fmt_duration(track.get("durationMs")),
                    "genre": track.get("genre") or album_genre,
                    "track_number": track.get("trackNumber") or track_num,
                    "yandex_url": f"https://music.yandex.ru/album/{album_id}/track/{track_id}",
                }
                res = _insert_track(row)
                if res == "added":
                    added += 1
                elif res == "skipped":
                    skipped += 1
                else:
                    errors += 1

            if progress_cb:
                info = f"{album_artists} — {album_title} ({track_num} тр.)"
                progress_cb(ai + 1, len(to_process), info)

        return {
            "label_name": label_name,
            "added": added,
            "skipped": skipped,
            "errors": errors,
            "albums_total": len(all_ids),
            "albums_done": ai + 1 if to_process else 0,
            "cancelled": cancelled,
        }
    finally:
        _running = False
        _cancel_requested = False
