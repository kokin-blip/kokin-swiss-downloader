"""
A record of what was actually downloaded.

Two reasons this exists beyond "it's nice to have a list":

  1. The log scrolls away and is cleared; when a friend asks "did that album
     ever finish?" there was previously nothing to consult.
  2. It is the honest answer to a whole class of bug this app has had — a
     download that is skipped or lands somewhere unexpected while the UI still
     reports success. A history keyed on the file yt-dlp actually wrote makes
     that visible instead of hiding it behind a green tick.

Stored as JSON in app_data_dir()/history.json, newest first, capped at MAX
entries. Failures are recorded too: knowing something failed at 3am, and how,
is the point.
"""

import json
import time
from pathlib import Path

from utils import app_data_dir

MAX = 300


def _file() -> Path:
    return app_data_dir() / "history.json"


def load() -> list:
    f = _file()
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        # A corrupt history is an annoyance, never a reason to fail a download.
        return []


def _save(entries: list) -> None:
    try:
        _file().write_text(
            json.dumps(entries[:MAX], indent=1, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def add(kind: str, url: str, status: str,
        title: str = "", path: str = "") -> dict:
    """Record one finished download. Returns the entry that was stored."""
    entry = {
        "ts":     int(time.time()),
        "kind":   kind,          # 'audio' | 'video'
        "url":    url,
        "status": status,        # 'done' | 'failed' | 'cancelled'
        "title":  title or "",
        "path":   path or "",
    }
    entries = load()
    entries.insert(0, entry)
    _save(entries)
    return entry


def clear() -> int:
    n = len(load())
    _save([])
    return n


def listing(limit: int = 100) -> list:
    return load()[:limit]
