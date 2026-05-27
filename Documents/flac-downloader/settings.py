"""
Persistent non-sensitive settings saved to app_data_dir()/settings.json.
No credentials are stored — the app uses anonymous proxy services.
"""

import json
from pathlib import Path
from utils import app_data_dir

DEFAULTS: dict = {
    "auto_fallback":  True,
    "qobuz_format":   6,   # 6=FLAC16  7=FLAC24/96  27=FLAC24/192
    "proxy":          "",  # e.g. "http://127.0.0.1:8080" or "socks5://..."
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
