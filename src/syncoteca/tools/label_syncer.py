"""Sync missing labels from tracks table into the labels registry.

Replicates sync_labels.js logic in Python for Telegram bot integration.
For each unique label in tracks not yet in labels table:
  1. Fetches Yandex Music album data via ZenRows to get real label ID
  2. Inserts via rpc/insert_label
Reports progress to Telegram.
"""
import json
import os
import re
import time
from typing import Callable, Optional

import httpx

ZENROWS_KEY = os.getenv("ZENROWS_KEY", "ed22055fcc6e65f4ebb401a7fdb3243c11592594")
DELAY_S = 10.0
PAGE_SIZE = 1000

_running: bool = False
_cancel_requested: bool = False


def cancel_sync() -> None:
    global _cancel_requested
    _cancel_requested = True


def is_running() -> bool:
    return _running


def _sb_headers() -> dict:
    key = os.getenv("SUPABASE_KEY", "")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _sb_base() -> str:
    return os.getenv("SUPABASE_URL", "").rstrip("/")


def get_all_tracks_labels() -> list[dict]:
    """Paginate tracks table, return all rows with label+link."""
    all_rows: list[dict] = []
    offset = 0
    while True:
        r = httpx.get(
            f"{_sb_base()}/rest/v1/tracks",
            headers={**_sb_headers(), "Prefer": "return=minimal"},
            params={"select": "label,link", "label": "not.is.null",
                    "limit": str(PAGE_SIZE), "offset": str(offset)},
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        all_rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return all_rows


def get_label_from_db(name: str) -> Optional[dict]:
    """Return label row from labels table by exact name, or None."""
    r = httpx.get(
        f"{_sb_base()}/rest/v1/labels",
        headers=_sb_headers(),
        params={"select": "id,name", "name": f"eq.{name}", "limit": "1"},
        timeout=10,
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def insert_label(label_id: Optional[str], name: str) -> dict:
    """Insert label via rpc/insert_label stored procedure."""
    r = httpx.post(
        f"{_sb_base()}/rest/v1/rpc/insert_label",
        headers=_sb_headers(),
        json={"p_id": label_id if label_id else name, "p_name": name},
        timeout=15,
    )
    r.raise_for_status()
    return r.json() or {}


def fetch_album_label_id(album_id: str, label_name: str) -> Optional[str]:
    """Fetch Yandex album via ZenRows and extract matching label ID."""
    params = {
        "apikey": ZENROWS_KEY,
        "url": f"https://api.music.yandex.ru/albums/{album_id}",
        "antibot": "true",
        "proxy_country": "ru",
        "premium_proxy": "true",
    }
    try:
        r = httpx.get("https://api.zenrows.com/v1/", params=params, timeout=60)
        r.raise_for_status()
        raw = r.json()
        body = raw.get("data") or raw.get("body") or raw
        if isinstance(body, str):
            body = json.loads(body)
        album = body.get("result")
        if not album or not album.get("labels"):
            return None
        # Try exact match first, fall back to first label
        name_lower = label_name.strip().lower()
        match = next(
            (lb for lb in album["labels"] if lb.get("name", "").strip().lower() == name_lower),
            album["labels"][0],
        )
        return str(match["id"]) if match.get("id") else None
    except Exception:
        return None


def _extract_album_id(link: str) -> Optional[str]:
    m = re.search(r'/album/(\d+)', link or '')
    return m.group(1) if m else None


def get_unique_labels(tracks: list[dict]) -> list[dict]:
    """Return list of {label_name, album_id} deduped by label_name."""
    seen: set[str] = set()
    result: list[dict] = []
    for t in tracks:
        label_raw = (t.get("label") or "").strip()
        link = t.get("link") or ""
        if not label_raw or not link:
            continue
        album_id = _extract_album_id(link)
        if not album_id:
            continue
        for name in [n.strip() for n in label_raw.split(",") if n.strip()]:
            if name not in seen:
                seen.add(name)
                result.append({"label_name": name, "album_id": album_id})
    return result


def sync_labels(
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    """Sync missing labels from tracks into labels table.

    progress_cb(done, total, info) called after each processed label.
    Returns {"added": N, "skipped": N, "errors": N, "added_list": [...]}
    """
    global _running, _cancel_requested

    if _running:
        return {"error": "already_running"}

    _running = True
    _cancel_requested = False

    try:
        tracks = get_all_tracks_labels()
        uniq = get_unique_labels(tracks)

        added: list[dict] = []
        skipped = errors = 0
        cancelled = False

        for i, item in enumerate(uniq):
            if _cancel_requested:
                cancelled = True
                break

            name = item["label_name"]
            album_id = item["album_id"]

            # Already in labels table?
            try:
                existing = get_label_from_db(name)
            except Exception:
                errors += 1
                continue

            if existing:
                skipped += 1
                if progress_cb:
                    progress_cb(i + 1, len(uniq), f"— {name} (уже есть)")
                continue

            # New label — get Yandex ID from album
            time.sleep(DELAY_S)
            if _cancel_requested:
                cancelled = True
                break

            label_id = fetch_album_label_id(album_id, name)

            try:
                row = insert_label(label_id, name)
                saved_id = row.get("id") if isinstance(row, dict) else (label_id or name)
                added.append({"name": name, "id": saved_id})
                info = f"✅ {name} (ID: {saved_id or '—'})"
            except Exception as e:
                errors += 1
                info = f"✗ {name}: {e}"

            if progress_cb:
                progress_cb(i + 1, len(uniq), info)

        return {
            "tracks_total": len(tracks),
            "labels_unique": len(uniq),
            "added": len(added),
            "skipped": skipped,
            "errors": errors,
            "cancelled": cancelled,
            "added_list": added,
        }
    finally:
        _running = False
        _cancel_requested = False
