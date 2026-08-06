"""
Thumbnails and frame extraction for the History tab.

Same rule as convert.py: this module knows nothing about pywebview or the API
object. Everything arrives as arguments and leaves as return values or
callbacks, so any of it can be driven from a bare REPL with no window open.

Why frames are cut here in ffmpeg rather than in the browser. The preview
<video> *can* be drawn to a canvas — the media server sends CORS headers, so
the canvas isn't tainted — but that path grabs whatever the compositor happens
to be showing: subject to the element's size, the player's own frame dropping,
and re-encoded through canvas. `ffmpeg -ss` seeks to the same timestamp and
writes the true source frame at full resolution, losslessly for PNG. When the
whole point of the feature is "keep exactly this frame", the accurate one wins.
"""

import hashlib
import re
import subprocess
import sys
from pathlib import Path

import convert
from utils import app_data_dir

# Mirrors convert._NO_WINDOW. Without it every thumbnail in a 60-row history
# would pop a console window in the shipped --windowed build.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Thumbnails are small (a few KB each); this cap is about not letting the
# folder grow forever across years of use, not about disk pressure.
CACHE_MAX = 500

# Hard ceiling on one extraction run. A 2-hour film at "every frame" is
# ~170,000 files, which is not a feature, it's a way to make Explorer
# unusable and fill a disk.
FRAME_CAP = 2000


def kind_of(path) -> str:
    """'video' | 'audio' | 'image' | '' — reuses the Convert tab's registry."""
    return convert.CATEGORY_OF_EXT.get(Path(path).suffix.lower(), "")


# ── Thumbnails ───────────────────────────────────────────────────────────────

def cache_dir() -> Path:
    d = app_data_dir() / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_key(src: Path, width: int) -> str:
    """
    Identity of a thumbnail: which file, which version of it, what size.

    mtime and size are in the key so that re-downloading or converting a file
    to the same path shows the new content instead of a stale picture. Hashing
    the path (rather than sanitising it) keeps non-ASCII titles and 300-char
    paths from producing filenames Windows rejects.
    """
    try:
        st = src.stat()
        stamp = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        stamp = "0:0"
    raw = f"{str(src).lower()}|{stamp}|{width}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:20]


def _prune_cache():
    """Drop the oldest thumbnails once the folder outgrows CACHE_MAX."""
    try:
        files = sorted(cache_dir().glob("*.jpg"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for p in files[:-CACHE_MAX] if len(files) > CACHE_MAX else []:
        try:
            p.unlink()
        except OSError:
            pass


def thumbnail(ffmpeg_exe: str, src, width: int = 160) -> str:
    """
    Path to a cached JPEG thumbnail for `src`, or "" if one can't be made.

    Handles all three media kinds:
      * video — a frame from ~10% in
      * audio — the embedded cover art, if the file has any
      * image — a scaled-down copy

    "" is a perfectly normal answer (an audio file with no artwork, a codec
    ffmpeg can't open) and callers must render a placeholder rather than treat
    it as an error.
    """
    src = Path(src)
    if not src.is_file() or not ffmpeg_exe:
        return ""

    out = cache_dir() / f"{_cache_key(src, width)}.jpg"
    if out.exists():
        # Touch it so pruning evicts genuinely cold entries, not merely old ones.
        try:
            out.touch()
        except OSError:
            pass
        return str(out)

    kind = kind_of(src)
    if kind == "video":
        # Seek 10% in. The first frames of a video are very often black, a fade
        # or a title card, which makes every thumbnail in the list look
        # identical and useless.
        dur = convert.probe(ffmpeg_exe, src).get("duration", 0.0)
        at = max(0.0, min(dur * 0.1, 60.0)) if dur else 1.0
        cmd = [ffmpeg_exe, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-ss", f"{at:.3f}", "-i", str(src), "-frames:v", "1",
               "-vf", f"scale={width}:-2", "-q:v", "4", "-y", str(out)]
    elif kind == "audio":
        # Cover art is carried as a video stream; -map 0:v:0 fails cleanly when
        # there is none, which is the "" case above.
        cmd = [ffmpeg_exe, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-i", str(src), "-map", "0:v:0", "-frames:v", "1",
               "-vf", f"scale={width}:-2", "-q:v", "4", "-y", str(out)]
    elif kind == "image":
        cmd = [ffmpeg_exe, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-i", str(src), "-frames:v", "1",
               "-vf", f"scale={width}:-2", "-q:v", "4", "-y", str(out)]
    else:
        return ""

    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       stdin=subprocess.DEVNULL, timeout=45,
                       creationflags=_NO_WINDOW)
    except Exception:
        return ""

    if out.exists() and out.stat().st_size > 0:
        _prune_cache()
        return str(out)
    # ffmpeg can leave a zero-byte file behind on failure; don't cache that as
    # a "real" thumbnail or it will never be retried.
    try:
        out.unlink()
    except OSError:
        pass
    return ""


def clear_cache() -> int:
    n = 0
    for p in cache_dir().glob("*.jpg"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


# ── Single frame capture ─────────────────────────────────────────────────────

def stamp(seconds: float) -> str:
    """Timestamp for a filename: 00-01-23-456. Colons are illegal on Windows."""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:          # rounding carried into the next second
        ms, s = 0, s + 1
    return f"{h:02d}-{m:02d}-{s:02d}-{ms:03d}"


def unique_path(path: Path) -> Path:
    """First free name at or after `path`, so a capture never overwrites."""
    if not path.exists():
        return path
    n = 1
    while True:
        cand = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not cand.exists():
            return cand
        n += 1


def save_frame(ffmpeg_exe: str, src, seconds: float, out_dir="",
               fmt: str = "png") -> tuple[str, str]:
    """
    Write the frame at `seconds` to disk. Returns (path, error).

    `out_dir` empty means "next to the source video", matching what the Convert
    tab does when no output folder is chosen.

    -ss is placed *before* -i, which seeks by keyframe index and then decodes
    forward to the exact timestamp. It is both the fast form and the accurate
    one; -ss after -i would decode the whole file up to that point, taking
    minutes on a long video for no gain.
    """
    src = Path(src)
    if not src.is_file():
        return "", "That file is no longer there."
    if not ffmpeg_exe:
        return "", "ffmpeg wasn't found, so frames can't be saved."
    if kind_of(src) != "video":
        return "", "Only videos have frames to save."

    fmt = "jpg" if str(fmt).lower() in ("jpg", "jpeg") else "png"
    folder = Path(out_dir) if out_dir else src.parent
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return "", f"Can't write to that folder: {e}"

    dst = unique_path(folder / f"{src.stem}_frame_{stamp(seconds)}.{fmt}")

    cmd = [ffmpeg_exe, "-hide_banner", "-loglevel", "error", "-nostdin",
           "-ss", f"{max(0.0, float(seconds)):.3f}", "-i", str(src),
           "-frames:v", "1"]
    if fmt == "jpg":
        cmd += ["-q:v", "2"]
    cmd += ["-y", str(dst)]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                              timeout=120, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return "", "Timed out reading that frame."
    except Exception as e:
        return "", str(e)

    if dst.exists() and dst.stat().st_size > 0:
        return str(dst), ""
    err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
    # Seeking past the end is the one failure a user hits by accident, and
    # ffmpeg's own wording for it ("Output file does not contain any stream")
    # explains nothing.
    return "", (err[-1] if err else "No frame there — try a moment earlier.")


# ── Bulk / automatic extraction ──────────────────────────────────────────────

# 'every'  — one frame every N seconds
# 'count'  — N frames spread evenly across the whole video
# 'scene'  — only frames where the picture changes a lot (cut detection)
MODES = ("every", "count", "scene")


def plan_extraction(ffmpeg_exe: str, src, mode: str, value: float) -> dict:
    """
    Work out what an extraction would produce, without running it.

    Returned before the job starts so the UI can say "this will write about 340
    files" rather than letting someone discover it afterwards.

    Returns {'duration', 'estimate', 'capped', 'error'}. For 'scene' the
    estimate is None — how many cuts a video contains genuinely can't be known
    without decoding it.
    """
    src = Path(src)
    out = {"duration": 0.0, "estimate": None, "capped": False, "error": ""}
    if not src.is_file():
        out["error"] = "That file is no longer there."
        return out
    if kind_of(src) != "video":
        out["error"] = "Only videos can be split into frames."
        return out

    dur = convert.probe(ffmpeg_exe, src).get("duration", 0.0)
    out["duration"] = dur

    if mode == "every":
        step = max(0.01, float(value or 1))
        out["estimate"] = int(dur / step) + 1 if dur else None
    elif mode == "count":
        out["estimate"] = max(1, int(value or 1))
    elif mode == "scene":
        out["estimate"] = None
    else:
        out["error"] = f"Unknown extraction mode: {mode}"
        return out

    if out["estimate"] and out["estimate"] > FRAME_CAP:
        out["capped"] = True
    return out


def build_extract_cmd(ffmpeg_exe: str, src, out_pattern, mode: str,
                      value: float, duration: float, fmt: str = "png",
                      width: int = 0) -> list:
    """ffmpeg argv for one extraction run."""
    filters = []
    if mode == "every":
        step = max(0.01, float(value or 1))
        filters.append(f"fps=1/{step}")
    elif mode == "count":
        n = max(1, int(value or 1))
        if duration and duration > 0:
            # n frames across the whole thing. Guard the n==1 case, which would
            # otherwise be a divide-by-zero disguised as fps=1/0.
            filters.append(f"fps={n / duration:.6f}" if n > 1 else "fps=1/999999")
        else:
            filters.append("fps=1")
    elif mode == "scene":
        # `value` is the difference threshold, 0..1 — lower catches more cuts.
        # Below ~0.2 a slow pan registers as a cut and you get hundreds of
        # near-identical frames; 0.4 is the usual starting point.
        thr = min(0.99, max(0.01, float(value or 0.4)))
        # +eq(n,0) ORs in the very first frame. The scene score is undefined
        # for frame 0, so a single-shot video — a talking head, a lyric video —
        # would otherwise select nothing at all and the run would look broken
        # when it was merely accurate.
        filters.append(f"select='gt(scene,{thr})+eq(n\\,0)'")

    if width:
        filters.append(f"scale={int(width)}:-2")

    cmd = [ffmpeg_exe, "-hide_banner", "-loglevel", "error", "-nostdin",
           "-progress", "pipe:1", "-nostats", "-i", str(src),
           "-vf", ",".join(filters),
           # Timestamps come out of select/fps irregularly; vsync vfr keeps one
           # output image per selected frame instead of duplicating to fill a
           # constant rate.
           "-vsync", "vfr",
           "-frames:v", str(FRAME_CAP)]
    if fmt == "jpg":
        cmd += ["-q:v", "2"]
    cmd += ["-y", str(out_pattern)]
    return cmd


def extract_frames(ffmpeg_exe: str, src, out_dir="", mode: str = "every",
                   value: float = 1.0, fmt: str = "png", width: int = 0,
                   on_pct=None, should_abort=None, on_proc=None) -> dict:
    """
    Pull many frames out of a video into their own folder.

    Returns {'ok', 'folder', 'count', 'msg'}.

    Frames always land in a dedicated sub-folder named after the video. Writing
    300 stills next to the source would bury it, and the Convert tab's folder
    scanner would then find them all on its next pass.
    """
    src = Path(src)
    if not src.is_file():
        return {"ok": False, "folder": "", "count": 0,
                "msg": "That file is no longer there."}
    if not ffmpeg_exe:
        return {"ok": False, "folder": "", "count": 0,
                "msg": "ffmpeg wasn't found, so frames can't be extracted."}
    if kind_of(src) != "video":
        return {"ok": False, "folder": "", "count": 0,
                "msg": "Only videos can be split into frames."}
    if mode not in MODES:
        return {"ok": False, "folder": "", "count": 0,
                "msg": f"Unknown extraction mode: {mode}"}

    fmt = "jpg" if str(fmt).lower() in ("jpg", "jpeg") else "png"
    base = Path(out_dir) if out_dir else src.parent
    folder = unique_dir(base / f"{safe_stem(src.stem)} frames")
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "folder": "", "count": 0,
                "msg": f"Can't create the output folder: {e}"}

    duration = convert.probe(ffmpeg_exe, src).get("duration", 0.0)
    pattern = folder / f"{safe_stem(src.stem)}_%05d.{fmt}"
    cmd = build_extract_cmd(ffmpeg_exe, src, pattern, mode, value,
                            duration, fmt, width)

    # run_ffmpeg already solves the two things that make this hard: draining
    # stderr on a second thread (a noisy decode otherwise fills the 64 KB pipe
    # and deadlocks) and distinguishing a user abort from a real failure.
    rc, tail = convert.run_ffmpeg(cmd, duration, on_pct=on_pct,
                                  should_abort=should_abort, on_proc=on_proc)

    made = sorted(folder.glob(f"*.{fmt}"))
    count = len(made)

    if rc == -1:
        # Keep what was already written — a user who stops a long extraction
        # usually stops it *because* they have enough frames.
        return {"ok": False, "folder": str(folder), "count": count,
                "msg": f"Stopped. Kept {count} frame{'s' if count != 1 else ''}."}
    if count == 0:
        try:
            folder.rmdir()          # leave no empty folder behind
        except OSError:
            pass
        # "Nothing selected" and "ffmpeg fell over" both exit non-zero, so the
        # exit code can't tell them apart — the mode can. Reaching here in
        # scene mode means the filter matched nothing, which is a sentence a
        # user can act on; ffmpeg's own "at least one of its streams received
        # no packets" is not.
        if mode == "scene":
            msg = "No scene changes found — try a lower threshold."
        elif tail:
            msg = tail.splitlines()[-1]
        else:
            msg = "No frames came out of that."
        return {"ok": False, "folder": "", "count": 0, "msg": msg}

    msg = f"Saved {count} frame{'s' if count != 1 else ''}."
    if count >= FRAME_CAP:
        msg += f" Stopped at the {FRAME_CAP}-frame limit."
    elif mode == "scene" and count == 1:
        # The only frame is the opening one eq(n,0) forces in, so the threshold
        # found nothing. Saying "saved 1 frame" would read as success.
        msg += " That's just the opening frame — lower the threshold to catch cuts."
    return {"ok": True, "folder": str(folder), "count": count, "msg": msg}


_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_stem(stem: str) -> str:
    """
    Make a filename fragment out of a video title.

    Titles come from yt-dlp and routinely contain ':' and '?', which Windows
    rejects outright — and the failure would surface as an opaque ffmpeg write
    error rather than anything a user could act on.
    """
    cleaned = _BAD_CHARS.sub("_", stem).strip(" .")
    return (cleaned or "frames")[:80]


def unique_dir(path: Path) -> Path:
    if not path.exists():
        return path
    n = 1
    while True:
        cand = path.with_name(f"{path.name} ({n})")
        if not cand.exists():
            return cand
        n += 1
