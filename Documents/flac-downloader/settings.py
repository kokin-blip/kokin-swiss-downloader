"""
Persistent non-sensitive settings saved to app_data_dir()/settings.json.

No credentials are stored in THIS file — the app uses anonymous proxy services.
Browser sign-in cookies are credentials and deliberately live elsewhere, under
app_data_dir()/cookies/; see cookies.py.
"""

import json
from pathlib import Path
from utils import app_data_dir

DEFAULTS: dict = {
    "auto_fallback":  True,
    "qobuz_format":   6,   # 6=FLAC16  7=FLAC24/96  27=FLAC24/192
    "proxy":          "",  # e.g. "http://127.0.0.1:8080" or "socks5://..."
    "debug_log":      False,  # write tracebacks to app_data_dir()/logs/
    "notify_done":    True,   # chime + flash the taskbar when the queue drains

    # Filename templates, in yt-dlp's output-template syntax. The ".%(ext)s"
    # suffix is appended by the backend and deliberately not user-editable —
    # a template without it silently produces extensionless files.
    "outtmpl_audio":  "%(artist,uploader)s - %(title)s",
    "outtmpl_video":  "%(uploader)s - %(title)s",

    "sub_langs":      "en",   # comma-separated; "all" fetches everything

    # SponsorBlock (YouTube only; a no-op elsewhere)
    "sponsorblock":       False,
    "sponsorblock_cats":  ["sponsor", "selfpromo", "interaction"],
    "embed_chapters":     False,

    "concurrent_fragments": 5,   # parallel HLS/DASH fragment fetches
    "rate_limit_kbps":      0,   # 0 = unlimited
}


def _file() -> Path:
    return app_data_dir() / "settings.json"


def load() -> dict:
    f = _file()
    if f.exists():
        try:
            saved = json.loads(f.read_text(encoding="utf-8"))
            return {**DEFAULTS, **saved}
        except Exception:
            pass
    return dict(DEFAULTS)


def save(data: dict) -> None:
    _file().write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
