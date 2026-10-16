"""Yandex Music URL parser (track/album/artist) → dict/list via ZenRows.

Ports logic from parcer-Yandex/enrich_universal.js + collect_label_releases_selectel.js.
Supports:
  - /track/{id}
  - /album/{id}/track/{id}
  - /album/{id}
  - /artist/{id}

Public API:
  parse_yandex_url(url: str) -> dict
    returns {
      kind: 'track' | 'album' | 'artist',
      tracks: list[dict],   # 1 for track, N for album, up to 5 for artist
      album?: dict,         # for album kind
      artist?: dict,        # for artist kind
      error?: str,
    }
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

ZENROWS_KEY = os.getenv("ZENROWS_KEY", "6ff4aa38190297fb3c60b19dd7fe0817779b68a1")
DELAY_S = 1.5

TRACK_URL_RE = re.compile(r"music\.yandex\.ru/(?:album/\d+/)?track/(\d+)")
ALBUM_URL_RE = re.compile(r"music\.yandex\.ru/album/(\d+)(?:/track/\d+)?")
ARTIST_URL_RE = re.compile(r"music\.yandex\.ru/artist/(\d+)")


def _zenrows_get(url: str, timeout: float = 60.0) -> Optional[dict]:
    params = {
        "apikey": ZENROWS_KEY,
        "url": url,
        "proxy_country": "ru",
        "premium_proxy": "true",
    }
    full = f"https://api.zenrows.com/v1/?{urlencode(params)}"
    try:
        r = httpx.get(full, timeout=timeout)
        if r.status_code != 200:
            return None
        data = r.json()
        body = data.get("data") or data.get("body") or data
        if isinstance(body, str):
            import json
            body = json.loads(body)
        return body
    except Exception:
        return None


def _fmt_duration(ms: Optional[int]) -> Optional[str]:
    if not ms:
        return None
    m = ms // 60000
    s = (ms % 60000) // 1000
    return f"{m:02d}:{s:02d}"


def _fmt_release_date(album_obj: dict) -> Optional[str]:
    rd = album_obj.get("releaseDate")
    if rd:
        return rd[:10]
    yr = album_obj.get("year")
    return str(yr) if yr else None


def _authors(artists_list: list[dict], composer: bool) -> Optional[str]:
    if not artists_list:
        return None
    names = [
        a.get("name") for a in artists_list
        if bool(a.get("composer")) == composer and a.get("name")
    ]
    return ", ".join(names) or None


def _credits(credits_list: list[dict], title: str) -> Optional[str]:
    for c in credits_list or []:
        if c.get("title") == title:
            return c.get("value")
    return None


def _fetch_track_credits(track_id: str) -> dict:
    body = _zenrows_get(f"https://api.music.yandex.ru/tracks/{track_id}/credits")
    if not body:
        return {}
    credits = (body.get("result") or {}).get("credits") or []
    return {
        "label": _credits(credits, "Лейбл"),
        "music_author": _credits(credits, "Автор музыки") or _credits(credits, "Композитор"),
        "lyrics_author": _credits(credits, "Автор текста") or _credits(credits, "Автор слов"),
    }


def _normalize_track(track_obj: dict, album_obj: Optional[dict] = None) -> dict:
    if album_obj is None:
        albums = track_obj.get("albums") or []
        album_obj = albums[0] if albums else {}

    artists = track_obj.get("artists") or []
    return {
        "yandex_id": str(track_obj.get("id") or ""),
        "title": track_obj.get("title"),
        "artist": _authors(artists, composer=False),
        "album": album_obj.get("title") if album_obj else None,
        "album_id": str(album_obj.get("id") or "") if album_obj else None,
        "duration": _fmt_duration(track_obj.get("durationMs")),
        "release_date": _fmt_release_date(album_obj) if album_obj else None,
        "genre_1": (album_obj.get("genre") if album_obj else None),
        "composers": _authors(artists, composer=True),
        "cover_url": (track_obj.get("coverUri") or (album_obj.get("coverUri") if album_obj else None) or "").replace(
            "%%", "400x400"
        ) or None,
        "link": f"https://music.yandex.ru/album/{album_obj.get('id')}/track/{track_obj.get('id')}"
        if album_obj and album_obj.get("id") else f"https://music.yandex.ru/track/{track_obj.get('id')}",
    }


def _parse_track(track_id: str) -> dict:
    body = _zenrows_get(f"https://api.music.yandex.ru/tracks/{track_id}")
    if not body:
        return {"kind": "track", "tracks": [], "error": "ZenRows вернул пусто"}

    result = body.get("result")
    if not result:
        return {"kind": "track", "tracks": [], "error": "no result in response"}
    track = result[0] if isinstance(result, list) else result
    if not track or not track.get("title"):
        return {"kind": "track", "tracks": [], "error": "трек не найден"}

    row = _normalize_track(track)
    time.sleep(DELAY_S)
    cred = _fetch_track_credits(track_id)
    row.update({k: v for k, v in cred.items() if v})
    return {"kind": "track", "tracks": [row]}


def _parse_album(album_id: str, with_credits: bool = True, limit: Optional[int] = None) -> dict:
    body = _zenrows_get(f"https://api.music.yandex.ru/albums/{album_id}/with-tracks")
    if not body:
        return {"kind": "album", "tracks": [], "error": "ZenRows вернул пусто"}

    album = body.get("result")
    if not album or not album.get("id"):
        return {"kind": "album", "tracks": [], "error": "альбом не найден"}

    volumes = album.get("volumes") or []
    all_tracks: list[dict] = []
    for vol in volumes:
        all_tracks.extend(vol or [])
    if limit:
        all_tracks = all_tracks[:limit]

    rows: list[dict] = []
    album_meta = {
        "id": album.get("id"),
        "title": album.get("title"),
        "year": album.get("year"),
        "releaseDate": album.get("releaseDate"),
        "genre": album.get("genre"),
        "coverUri": album.get("coverUri"),
    }
    for i, t in enumerate(all_tracks):
        row = _normalize_track(t, album_obj=album_meta)
        if with_credits and t.get("id"):
            time.sleep(DELAY_S)
            cred = _fetch_track_credits(str(t["id"]))
            row.update({k: v for k, v in cred.items() if v})
        rows.append(row)

    return {
        "kind": "album",
        "album": {
            "title": album.get("title"),
            "year": album.get("year"),
            "release_date": _fmt_release_date(album_meta),
            "genre": album.get("genre"),
            "artist": _authors(album.get("artists") or [], composer=False),
            "tracks_count": len(all_tracks),
            "link": f"https://music.yandex.ru/album/{album.get('id')}",
        },
        "tracks": rows,
    }


def _parse_artist(artist_id: str, with_credits: bool = True) -> dict:
    body = _zenrows_get(f"https://api.music.yandex.ru/artists/{artist_id}/tracks")
    if not body:
        return {"kind": "artist", "tracks": [], "error": "ZenRows вернул пусто"}

    result = body.get("result") or {}
    tracks = result.get("tracks") or []
    if not tracks:
        return {"kind": "artist", "tracks": [], "error": "у артиста нет треков в API"}

    rows: list[dict] = []
    for t in tracks:
        row = _normalize_track(t)
        if with_credits and t.get("id"):
            time.sleep(DELAY_S)
            cred = _fetch_track_credits(str(t["id"]))
            row.update({k: v for k, v in cred.items() if v})
        rows.append(row)

    artist_name = None
    if tracks and tracks[0].get("artists"):
        first = tracks[0]["artists"][0]
        artist_name = first.get("name") if first else None

    return {
        "kind": "artist",
        "artist": {
            "id": artist_id,
            "name": artist_name,
            "tracks_count": len(tracks),
            "link": f"https://music.yandex.ru/artist/{artist_id}",
        },
        "tracks": rows,
    }


def parse_yandex_url(url: str, album_limit: int = 30) -> dict:
    """Dispatch URL → specific parser.

    album_limit: max tracks fetched for /album/ URL (safety against huge albums + ZenRows quota).
    """
    url = url.strip()
    m_track = TRACK_URL_RE.search(url)
    if m_track:
        return _parse_track(m_track.group(1))

    m_artist = ARTIST_URL_RE.search(url)
    if m_artist:
        return _parse_artist(m_artist.group(1))

    m_album = ALBUM_URL_RE.search(url)
    if m_album:
        return _parse_album(m_album.group(1), limit=album_limit)

    return {"kind": "unknown", "tracks": [], "error": f"не распознал URL: {url}"}


def format_result_for_tg(data: dict) -> str:
    """Pretty-print parse result for Telegram (HTML parse_mode)."""
    def esc(s: Any) -> str:
        if s is None:
            return ""
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    if data.get("error"):
        return f"❌ Ковальски: {esc(data['error'])}"

    kind = data.get("kind")
    tracks = data.get("tracks") or []

    if kind == "track" and tracks:
        t = tracks[0]
        lines = [f"🗃️ <b>Ковальски — трек</b>", ""]
        lines.append(f"🎵 <b>{esc(t.get('title'))}</b> — {esc(t.get('artist'))}")
        if t.get("album"):
            lines.append(f"💿 Альбом: {esc(t['album'])}")
        if t.get("release_date"):
            lines.append(f"📅 Дата: {esc(t['release_date'])}")
        if t.get("duration"):
            lines.append(f"⏱ {esc(t['duration'])}")
        if t.get("genre_1"):
            lines.append(f"🎸 Жанр: {esc(t['genre_1'])}")
        if t.get("music_author") or t.get("composers"):
            lines.append(f"🎼 Музыка: {esc(t.get('music_author') or t.get('composers'))}")
        if t.get("lyrics_author"):
            lines.append(f"✍️ Текст: {esc(t['lyrics_author'])}")
        if t.get("label"):
            lines.append(f"🏷 Лейбл: {esc(t['label'])}")
        if t.get("link"):
            lines.append(f"🔗 {esc(t['link'])}")
        return "\n".join(lines)

    if kind == "album":
        a = data.get("album") or {}
        header = [
            f"🗃️ <b>Ковальски — альбом</b>",
            "",
            f"💿 <b>{esc(a.get('title'))}</b>",
            f"👤 {esc(a.get('artist'))}",
            f"📅 {esc(a.get('release_date') or a.get('year'))}",
            f"🎸 {esc(a.get('genre'))}" if a.get("genre") else "",
            f"🎵 Треков: {len(tracks)}",
            "",
        ]
        header = [h for h in header if h]
        body_lines = []
        for i, t in enumerate(tracks, 1):
            line = f"{i}. {esc(t.get('title'))} — {esc(t.get('artist'))}"
            extras = []
            if t.get("music_author"):
                extras.append(f"🎼 {esc(t['music_author'])}")
            if t.get("lyrics_author"):
                extras.append(f"✍️ {esc(t['lyrics_author'])}")
            if extras:
                line += "\n   " + " · ".join(extras)
            body_lines.append(line)
        return "\n".join(header + body_lines)

    if kind == "artist":
        a = data.get("artist") or {}
        header = [
            f"🗃️ <b>Ковальски — артист</b>",
            "",
            f"👤 <b>{esc(a.get('name'))}</b>",
            f"🎵 Треков (Yandex отдал): {len(tracks)}",
            f"🔗 {esc(a.get('link'))}",
            "",
        ]
        body_lines = []
        for i, t in enumerate(tracks, 1):
            line = f"{i}. {esc(t.get('title'))}"
            if t.get("album"):
                line += f" · {esc(t['album'])}"
            if t.get("release_date"):
                line += f" ({esc(t['release_date'])})"
            body_lines.append(line)
        return "\n".join(header + body_lines)

    return "❌ Ковальски: пустой ответ"
