"""Shared utilities — ffmpeg detection, app data directory, FLAC tagging."""

import sys
import shutil
from pathlib import Path
from typing import Optional


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


def tag_flac_file(path: Path,
                  track_info: dict,
                  cover_data: Optional[bytes] = None,
                  cover_mime: str = "image/jpeg") -> bool:
    """
    Write Vorbis tags + optional cover art to a FLAC file using mutagen.
    track_info is a Qobuz API track dict (or any dict with the same keys).
    Returns True on success, False if mutagen is unavailable or tagging fails.
    """
    try:
        from mutagen.flac import FLAC, Picture

        audio = FLAC(str(path))

        title     = track_info.get("title", "")
        artist    = (track_info.get("performer") or {}).get("name", "") or \
                    track_info.get("artist", "")
        album_obj = track_info.get("album") or {}
        album     = album_obj.get("title", "") or track_info.get("album_title", "")
        rel_ts    = album_obj.get("released_at") or 0
        track_num = track_info.get("track_number") or track_info.get("position") or ""
        disc_num  = track_info.get("media_number", "")

        if title:     audio["title"]       = [title]
        if artist:    audio["artist"]      = [artist]
        if album:     audio["album"]       = [album]
        if track_num: audio["tracknumber"] = [str(track_num)]
        if disc_num:  audio["discnumber"]  = [str(disc_num)]
        if rel_ts:
            import datetime
            audio["date"] = [str(datetime.datetime.fromtimestamp(rel_ts).year)]

        if cover_data:
            pic        = Picture()
            pic.type   = 3          # COVER_FRONT
            pic.mime   = cover_mime
            pic.data   = cover_data
            audio.clear_pictures()
            audio.add_picture(pic)

        audio.save()
        return True
    except Exception:
        return False
