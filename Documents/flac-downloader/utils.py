"""Shared utilities — ffmpeg detection, app data directory."""

import sys
import shutil
from pathlib import Path


def find_ffmpeg() -> Path | None:
    """
    Locate ffmpeg, checking in order:
      1. System PATH  (works for anyone who has ffmpeg installed normally)
      2. A 'ffmpeg' sub-folder next to this script  (bundled distribution)
      3. The script's own directory                  (flat bundle)
      4. ~/.spotiflac                                (backward-compat for existing users)
    Returns the *directory* containing the ffmpeg binary, or None if not found.
    """
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"

    # 1. System PATH
    found = shutil.which("ffmpeg")
    if found:
        return Path(found).parent

    # 2. Bundled sub-folder
    script_dir = Path(__file__).parent
    if (script_dir / "ffmpeg" / exe).exists():
        return script_dir / "ffmpeg"

    # 3. Flat next to script (PyInstaller one-file unpacks here)
    if (script_dir / exe).exists():
        return script_dir

    # 4. spotiflac backward-compat (Windows only, non-fatal if absent)
    spoti = Path.home() / ".spotiflac" / exe
    if spoti.exists():
        return spoti.parent

    return None


def app_data_dir() -> Path:
    """
    Return (and create) a platform-appropriate user data directory.
      Windows : %LOCALAPPDATA%\\flac-downloader
      macOS   : ~/Library/Application Support/flac-downloader
      Linux   : ~/.local/share/flac-downloader
    Falls back to ~/.flac-downloader if platformdirs is unavailable.
    """
    try:
        from platformdirs import user_data_dir
        d = Path(user_data_dir("flac-downloader"))
    except ImportError:
        d = Path.home() / ".flac-downloader"
    d.mkdir(parents=True, exist_ok=True)
    return d
