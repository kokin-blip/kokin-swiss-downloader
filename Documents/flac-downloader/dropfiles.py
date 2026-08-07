"""
Turn a pywebview drop event into paths we are willing to act on.

Pure data: hand it a dict, get plain lists back. No window, no ffmpeg, no
threads — so the awkward cases below are testable with hand-built dicts.

This module exists because the event pywebview hands us is not trustworthy.
See paths_from_event for the specifics; the short version is that the full
filesystem path arrives through a side channel matched up by *filename*, and
the bookkeeping for that channel is a module-global list that is never cleared.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

import convert

# A drop is a bulk gesture — someone can select their entire Music folder and
# let go. Past this we stop rather than queue an hour of work by accident.
MAX_DROP = 200


def paths_from_event(event: dict) -> dict:
    """
    The absolute paths in a drop event.

    Returns {'paths': [str], 'unresolved': int, 'total': int}, where
    `unresolved` counts entries Windows did not hand us a usable path for. The
    caller reports that count rather than pretending the drop was smaller than
    it was.

    Two pywebview facts drive the defensive checks (verified in 6.2.1):

    * The real path is attached as 'pywebviewFullPath' by webview/util.py, and
      the key is simply *absent* when it could not be worked out — so .get(),
      never [].
    * It is matched to the JS-side File by basename, run through
      urllib.parse.unquote(), and popped from webview.dom._dnd_state['paths'].
      That list is module-global and is never cleared. A real file named
      'Mix%20A.mp3' unquotes to 'Mix A.mp3', never matches, and stays in the
      list for the life of the process — where it will attach itself to the
      next drop of a file genuinely called 'Mix A.mp3'. Comparing the basename
      back against the name JS reported is what stops a stale entry handing us
      a file the user never dropped.
    """
    out = {"paths": [], "unresolved": 0, "total": 0}
    files = ((event or {}).get("dataTransfer") or {}).get("files") or []
    if not isinstance(files, (list, tuple)):
        return out

    out["total"] = len(files)
    for f in files:
        if not isinstance(f, dict):
            out["unresolved"] += 1
            continue
        path = f.get("pywebviewFullPath")
        name = f.get("name") or ""
        if not path:
            out["unresolved"] += 1
            continue
        if name and os.path.basename(path).lower() != name.lower():
            out["unresolved"] += 1
            continue
        out["paths"].append(path)
    return out


def classify(paths: Iterable[str]) -> dict:
    """
    Sort dropped paths into what we can do with them.

    Returns {'media', 'folders', 'other', 'gone', 'cats'} — all lists of str
    except `cats`, which is the distinct convert categories present ('audio',
    'video', 'image'), used to pick a sensible output format.

    Unknown extensions land in 'other' and are deliberately NOT treated as
    media. The file picker has an "All files" escape hatch because the user is
    choosing one thing on purpose; a drop is a whole selection, and people drop
    folders containing .txt, .nfo and desktop.ini without noticing. Passing
    those to a converter buys a per-file ffmpeg failure half a minute later
    instead of an honest count now.

    Never raises: an unreadable network path lands in 'gone'.
    """
    out = {"media": [], "folders": [], "other": [], "gone": [], "cats": []}
    seen = set()
    for raw in paths or []:
        p = str(raw or "")
        if not p:
            continue
        key = p.lower()             # Windows: same file, different casing
        if key in seen:
            continue
        seen.add(key)

        try:
            path = Path(p)
            if path.is_dir():
                out["folders"].append(p)
                continue
            if not path.exists():
                out["gone"].append(p)
                continue
            cat = convert.CATEGORY_OF_EXT.get(path.suffix.lower(), "")
        except OSError:
            out["gone"].append(p)
            continue

        if cat:
            out["media"].append(p)
            if cat not in out["cats"]:
                out["cats"].append(cat)
        else:
            out["other"].append(p)
    return out
