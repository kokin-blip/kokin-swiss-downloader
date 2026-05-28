"""Shared utilities — ffmpeg detection, app data directory, FLAC tagging."""

import sys
import shutil
from pathlib import Path
from typing import Optional


def find_ffmpeg() -> Path | None:
    """
    Locate ffmpeg, checking in order:
      1. PyInstaller bundle (sys._MEIPASS/ffmpeg/)  — when running as a .exe
      2. A 'ffmpeg' sub-folder next to this script  (dev / bundled distribution)
      3. The script's own directory                  (flat bundle)
      4. System PATH                                 (any normal install)
      5. ~/.spotiflac                                (backward-compat)
    Returns the *directory* containing the ffmpeg binary, or None if not found.
    """
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"

    # 1. PyInstaller bundled location (highest priority — guaranteed to match)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "ffmpeg"
        if (bundled / exe).exists():
            return bundled
        if (Path(meipass) / exe).exists():
            return Path(meipass)

    # 2. Bundled sub-folder
    script_dir = Path(__file__).parent
    if (script_dir / "ffmpeg" / exe).exists():
        return script_dir / "ffmpeg"

    # 3. Flat next to script
    if (script_dir / exe).exists():
        return script_dir

    # 4. System PATH
    found = shutil.which("ffmpeg")
    if found:
        return Path(found).parent

    # 5. spotiflac backward-compat (Windows only, non-fatal if absent)
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


def flac_cover_info(path: Path) -> dict:
    """
    Read back a FLAC file and report whether it has a valid PICTURE block.
    Used after tagging to confirm the cover actually landed in the file.

    Returns:
      {"present": True, "count": int, "size": int, "width": int, "height": int,
       "type": int, "mime": str}
      or
      {"present": False, "reason": str}
    """
    try:
        from mutagen.flac import FLAC
        audio = FLAC(str(path))
        if not audio.pictures:
            return {"present": False, "reason": "no PICTURE block"}
        pic = audio.pictures[0]
        return {
            "present": True,
            "count":   len(audio.pictures),
            "size":    len(pic.data),
            "width":   pic.width,
            "height":  pic.height,
            "type":    pic.type,
            "mime":    pic.mime,
        }
    except Exception as e:
        return {"present": False, "reason": str(e)}


def _read_jpeg_dim(data: bytes) -> tuple[int, int]:
    """Parse width/height from a JPEG by walking markers. (0,0) on failure."""
    if data[:2] != b'\xff\xd8':
        return 0, 0
    i, L = 2, len(data)
    while i < L - 9:
        if data[i] != 0xff:
            return 0, 0
        while data[i] == 0xff and i + 1 < L:
            i += 1
        marker = data[i]
        i += 1
        # SOFn markers (Start Of Frame): 0xC0-0xCF except DHT/JPG/DAC
        if 0xc0 <= marker <= 0xcf and marker not in (0xc4, 0xc8, 0xcc):
            # length(2), precision(1), height(2), width(2)
            height = (data[i+3] << 8) | data[i+4]
            width  = (data[i+5] << 8) | data[i+6]
            return width, height
        if marker in (0xd8, 0xd9):  # SOI / EOI, no segment
            continue
        # Skip segment by length field
        seg_len = (data[i] << 8) | data[i+1]
        i += seg_len
    return 0, 0


def _read_png_dim(data: bytes) -> tuple[int, int]:
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        return 0, 0
    return int.from_bytes(data[16:20], 'big'), int.from_bytes(data[20:24], 'big')


def _prepare_cover(data: bytes, mime_hint: str) -> tuple[bytes, str, int, int]:
    """
    Return (bytes, mime, width, height) for embedding. Uses Pillow to
    re-encode to clean JPEG when available, otherwise parses dimensions
    from the original file's header (no Pillow dependency required).
    """
    # Best case: Pillow available — normalize to a clean JPEG
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if img.mode != "RGB":
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=92, optimize=True)
        return out.getvalue(), "image/jpeg", img.width, img.height
    except Exception:
        pass

    # No Pillow — detect format + dimensions from magic bytes alone
    if data[:3] == b'\xff\xd8\xff':
        w, h = _read_jpeg_dim(data)
        return data, "image/jpeg", w or 600, h or 600
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        w, h = _read_png_dim(data)
        return data, "image/png",  w or 600, h or 600
    return data, mime_hint or "image/jpeg", 600, 600


def tag_flac_file(path: Path,
                  track_info: dict,
                  cover_data: Optional[bytes] = None,
                  cover_mime: str = "image/jpeg") -> tuple[bool, str]:
    """
    Write Vorbis tags + optional cover art to a FLAC file.

    Returns (success, error_message). On success error_message is "".
    Each potential failure point is wrapped separately so the returned
    message is specific enough to debug from the log.
    """
    try:
        from mutagen.flac import FLAC, Picture
    except Exception as e:
        return False, f"mutagen import failed: {e}"

    try:
        audio = FLAC(str(path))
    except Exception as e:
        return False, f"cannot open FLAC: {e}"

    try:
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
    except Exception as e:
        return False, f"setting tags failed: {e}"

    if cover_data:
        try:
            img_bytes, mime, w, h = _prepare_cover(cover_data, cover_mime)
            pic = Picture()
            pic.type   = 3          # COVER_FRONT
            pic.mime   = mime
            pic.desc   = "Cover"
            pic.width  = w
            pic.height = h
            pic.depth  = 24
            pic.colors = 0
            pic.data   = img_bytes
            audio.clear_pictures()
            audio.add_picture(pic)
        except Exception as e:
            return False, f"building picture block failed: {e}"

    try:
        audio.save()
    except Exception as e:
        return False, f"save failed: {e}"

    return True, ""
