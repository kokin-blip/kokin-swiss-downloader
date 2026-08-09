"""
The conversion engine behind the Convert tab.

Deliberately knows nothing about the app: no pywebview, no `API`, no emitting.
Progress, cancellation and the process handle all travel through callbacks the
caller supplies, so every function here can be exercised from a bare REPL with
no window open. That is the whole point of the split — `backend.py` is already
four concerns in one file and none of them are testable without a GUI.

Two engines live here, picked by target category:
  * ffmpeg for audio and video (the bundled static binary, see utils.find_ffmpeg)
  * Pillow for images — ffmpeg has no ICO muxer and mangles animated GIF/WebP
"""

import json
import os
import re
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

# Suppress the console window ffmpeg would otherwise flash on every spawn.
# The shipped build is --windowed, and a 40-file batch is 80 spawns (probe +
# encode per file), so without this a batch strobes the screen. Precedent:
# backend.py's updater relaunch. 0 is the documented default off Windows.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW

# Refuse to walk more than this many matching files in folder mode. Picked to
# be comfortably more than an album or a season of a show, and comfortably less
# than "I pointed it at C:\Users by accident".
SCAN_CAP = 500

# Suffix for the in-progress output. Conversions write here and are renamed
# into place only on success, so an abort or a crash can never leave a corrupt
# file wearing a valid-looking name.
TMP_TAG = ".swissconv-tmp"


class SameFileError(Exception):
    """Output path resolved to the input path — refusing to eat the source."""


# ── Format registries ────────────────────────────────────────────────────────

# What we accept as *input*, keyed by extension. Generous on purpose: ffmpeg
# will tell us soon enough if it can't read something, and a too-strict list
# silently hides files from folder mode with no explanation.
CATEGORY_OF_EXT = {}
for _ext in (".mp3 .flac .wav .m4a .aac .ogg .oga .opus .wma .alac .aiff .aif "
             ".ape .wv .mka .m4b .mp2 .ac3 .dts .amr .au".split()):
    CATEGORY_OF_EXT[_ext] = "audio"
for _ext in (".mp4 .mkv .webm .mov .avi .flv .wmv .m4v .mpg .mpeg .ts .m2ts "
             ".3gp .ogv .vob .divx .asf .rm .rmvb .mts".split()):
    CATEGORY_OF_EXT[_ext] = "video"
for _ext in (".png .jpg .jpeg .webp .bmp .gif .ico .tif .tiff .tga .ppm .pgm "
             ".jfif .avif .heic".split()):
    CATEGORY_OF_EXT[_ext] = "image"
del _ext

# What we can produce. `ext` is authoritative for naming; `cat` picks the engine.
TARGETS = {
    # audio
    "mp3":  {"cat": "audio", "ext": ".mp3",  "label": "MP3"},
    "flac": {"cat": "audio", "ext": ".flac", "label": "FLAC"},
    "wav":  {"cat": "audio", "ext": ".wav",  "label": "WAV"},
    "m4a":  {"cat": "audio", "ext": ".m4a",  "label": "M4A (AAC)"},
    "aac":  {"cat": "audio", "ext": ".aac",  "label": "AAC"},
    "ogg":  {"cat": "audio", "ext": ".ogg",  "label": "OGG (Vorbis)"},
    "opus": {"cat": "audio", "ext": ".opus", "label": "Opus"},
    "wma":  {"cat": "audio", "ext": ".wma",  "label": "WMA"},
    # video
    "mp4":  {"cat": "video", "ext": ".mp4",  "label": "MP4"},
    "mkv":  {"cat": "video", "ext": ".mkv",  "label": "MKV"},
    "webm": {"cat": "video", "ext": ".webm", "label": "WebM"},
    "mov":  {"cat": "video", "ext": ".mov",  "label": "MOV"},
    "avi":  {"cat": "video", "ext": ".avi",  "label": "AVI"},
    "gif":  {"cat": "video", "ext": ".gif",  "label": "GIF (animated)"},
    # image
    "png":  {"cat": "image", "ext": ".png",  "label": "PNG"},
    "jpg":  {"cat": "image", "ext": ".jpg",  "label": "JPEG"},
    "webp": {"cat": "image", "ext": ".webp", "label": "WebP"},
    "bmp":  {"cat": "image", "ext": ".bmp",  "label": "BMP"},
    "imgif": {"cat": "image", "ext": ".gif", "label": "GIF (still)"},
    "ico":  {"cat": "image", "ext": ".ico",  "label": "ICO (icon)"},
}

# Extensions that mean "this is already the target format". Mostly identity,
# but a few targets have synonyms we must not re-encode pointlessly.
_SAME_AS = {
    "jpg":  {".jpg", ".jpeg", ".jfif"},
    "ogg":  {".ogg", ".oga"},
    "imgif": {".gif"},
}

# Which codecs each container will accept verbatim, for stream-copy (remux)
# instead of a full re-encode. None means "this container takes anything",
# which for practical purposes is true of Matroska.
# Getting this right is the single biggest win in the feature: MKV->MP4 on a
# H.264/AAC file drops from a 20-minute transcode to under a second.
COPY_OK = {
    "mp4":  {"v": {"h264", "hevc", "mpeg4", "av1"},
             "a": {"aac", "mp3", "ac3", "alac"}},
    "mov":  {"v": {"h264", "hevc", "mpeg4", "prores"},
             "a": {"aac", "pcm_s16le", "alac"}},
    "mkv":  {"v": None, "a": None},
    "webm": {"v": {"vp8", "vp9", "av1"},
             "a": {"opus", "vorbis"}},
    "avi":  {"v": {"mpeg4", "mjpeg"},
             "a": {"mp3", "ac3", "pcm_s16le"}},
}

# Audio codec each audio-only target wants, so video->audio extraction can
# stream-copy when the source track already matches (mp4/aac -> m4a is common,
# sub-second, and bit-perfect).
AUDIO_CODEC_OF = {
    "mp3": "mp3", "flac": "flac", "wav": "pcm_s16le", "m4a": "aac",
    "aac": "aac", "ogg": "vorbis", "opus": "opus", "wma": "wmav2",
}


def category_for_target(target: str) -> str:
    """'audio' | 'video' | 'image'. Empty string for an unknown target."""
    return TARGETS.get(target, {}).get("cat", "")


def is_same_format(src: Path, target: str) -> bool:
    """True when converting this file would be a no-op re-encode."""
    ext = src.suffix.lower()
    return ext in _SAME_AS.get(target, {TARGETS.get(target, {}).get("ext", "")})


# ── Folder scanning ──────────────────────────────────────────────────────────

def _scan_categories(target: str) -> set:
    """
    Which input categories folder mode should collect for this target.

    Audio targets deliberately sweep up video too — "convert this folder to
    mp3" meaning "and rip the audio out of the mp4s" is the whole point of
    extraction. Video and image targets stay in their lane, because "folder ->
    mp4" over a music folder would otherwise produce 40 black-screen videos.
    """
    cat = category_for_target(target)
    if cat == "audio":
        return {"audio", "video"}
    return {cat} if cat else set()


def _is_junk(p: Path) -> bool:
    name = p.name
    if name.startswith(".") or name.startswith("~$"):
        return True
    if TMP_TAG in name:          # our own in-progress output from a live run
        return True
    try:
        if p.stat().st_size < 1024:
            return True
    except OSError:
        return True
    return False


def scan_dir(folder, target: str, recursive: bool = False,
             cap: int = SCAN_CAP, out_dir: str = "", force: bool = False) -> dict:
    """
    Collect convertible files from a folder.

    Returns {'files': [{'path','name','size'}], 'same_fmt': int,
             'over_cap': bool, 'total': int}
    where `same_fmt` counts files skipped for already being the target format
    (reported to the user rather than silently dropped) and `over_cap` means we
    stopped early — the caller should refuse rather than truncate, because a
    silently partial batch is worse than an error.
    """
    root = Path(folder)
    if not root.is_dir():
        return {"files": [], "same_fmt": 0, "over_cap": False, "total": 0}

    wanted = _scan_categories(target)
    # Anything already sitting in the output folder is last run's work; picking
    # it up again would re-convert our own output on every pass.
    out_resolved = None
    if out_dir:
        try:
            out_resolved = Path(out_dir).resolve()
        except OSError:
            pass

    files, same_fmt, total = [], 0, 0
    walker = root.rglob("*") if recursive else root.glob("*")
    for p in walker:
        if not p.is_file():
            continue
        if CATEGORY_OF_EXT.get(p.suffix.lower()) not in wanted:
            continue
        if _is_junk(p):
            continue
        if out_resolved is not None:
            try:
                if p.resolve().parent == out_resolved:
                    continue
            except OSError:
                pass
        total += 1
        if is_same_format(p, target) and not force:
            same_fmt += 1
            continue
        if len(files) >= cap:
            return {"files": files, "same_fmt": same_fmt,
                    "over_cap": True, "total": total}
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        files.append({"path": str(p), "name": p.name, "size": size})

    files.sort(key=lambda f: f["name"].lower())
    return {"files": files, "same_fmt": same_fmt, "over_cap": False,
            "total": total}


# ── Output naming ────────────────────────────────────────────────────────────

def resolve_output(src, out_dir: str, target: str,
                   collision: str = "rename") -> Path | None:
    """
    Where this file's output should land.

    `out_dir` empty means "next to the source", which is what a converter
    should do by default. Returns None when the policy says to skip. Raises
    SameFileError if the result would be the input itself — reachable via
    overwrite + same-format + force, and the one case worth refusing outright
    rather than quietly destroying somebody's original.
    """
    src = Path(src)
    ext = TARGETS[target]["ext"]
    folder = Path(out_dir) if out_dir else src.parent
    dst = folder / (src.stem + ext)

    if dst.exists():
        if collision == "skip":
            return None
        if collision == "rename":
            n = 1
            while True:
                cand = folder / f"{src.stem} ({n}){ext}"
                if not cand.exists():
                    dst = cand
                    break
                n += 1
        # "overwrite" falls through with dst unchanged

    # resolve() is non-strict, so this works whether or not dst exists yet.
    try:
        same = dst.resolve() == src.resolve()
    except OSError:
        same = str(dst).lower() == str(src).lower()
    if same:
        raise SameFileError(str(src))
    return dst


def temp_path(dst) -> Path:
    """
    Sibling scratch file for an in-progress conversion.

    Keeps the real extension last so ffmpeg still infers the muxer from it and
    we don't have to pass an explicit -f.
    """
    dst = Path(dst)
    return dst.with_name(dst.stem + TMP_TAG + dst.suffix)


# ── Probing ──────────────────────────────────────────────────────────────────

_RE_DUR = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")
_RE_STREAM = re.compile(r"Stream #\d+:\d+.*?:\s*(Video|Audio):\s*([A-Za-z0-9_]+)")


def _probe_stderr(ffmpeg_exe: str, path) -> str:
    """
    Raw `ffmpeg -i` stderr for one file, or '' if it couldn't be run.

    Deliberately not ffprobe: the static ffprobe build is ~100 MB against an
    already-large exe, and `ffmpeg -i` with no output prints everything we need
    to stderr after reading only the header — milliseconds even on a 4 GB file.

    Note this call *always exits non-zero* ("At least one output file must be
    specified"). That is the expected path, not a failure.
    """
    try:
        proc = subprocess.run(
            [ffmpeg_exe, "-hide_banner", "-i", str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, timeout=30,
            creationflags=_NO_WINDOW,
        )
        return proc.stderr.decode("utf-8", "replace")
    except Exception:
        return ""


def probe(ffmpeg_exe: str, path) -> dict:
    """
    Read duration and stream codecs out of a file's header.

    Returns {'duration': float, 'vcodec': str, 'acodec': str}. A parse failure
    yields duration 0.0, which callers must treat as "unknown, show an
    indeterminate bar" — never as a reason to fail the conversion.

    Kept deliberately narrow: this runs on the conversion hot path and four
    call sites depend on exactly this shape. Anything richer goes in
    probe_full(), which costs the same subprocess but parses far more.
    """
    info = {"duration": 0.0, "vcodec": "", "acodec": ""}
    err = _probe_stderr(ffmpeg_exe, path)
    if not err:
        return info

    m = _RE_DUR.search(err)
    if m:
        h, mi, s = m.groups()
        info["duration"] = int(h) * 3600 + int(mi) * 60 + float(s)
    for kind, codec in _RE_STREAM.findall(err):
        key = "vcodec" if kind == "Video" else "acodec"
        if not info[key]:
            info[key] = codec.lower()
    return info


# Parsers for probe_full. ffmpeg's -i banner is a human report, not an API, so
# each of these is deliberately anchored on something stable and returns a
# neutral value rather than raising when the line looks unfamiliar.
_RE_FORMAT = re.compile(r"^Input #\d+,\s*(.+?), from ", re.M)
_RE_KBPS = re.compile(r"(\d+)\s*kb/s")
_RE_STREAM_LINE = re.compile(
    r"^\s*Stream #\d+:\d+.*?:\s*(Video|Audio):\s*(.+)$", re.M)
# 1920x1080 — but *not* the 0x31637661 half of a fourcc like (avc1 / 0x31637661).
# Requiring two digits either side is what rules that out: the hex form always
# has a single '0' in front of the 'x'.
_RE_DIMS = re.compile(r"(?<![\dA-Fa-fxX])(\d{2,5})x(\d{2,5})(?![\dA-Fa-fxX])")
_RE_FPS = re.compile(r"(\d+(?:\.\d+)?)\s*fps")
_RE_HZ = re.compile(r"(\d+)\s*Hz")
# s16 / s32 / fltp / u8 ... — the sample format token on an audio stream line.
_RE_SAMPLE_FMT = re.compile(r"\b(u8|s16|s32|s64|flt|dbl)(p?)\b")
_RE_PIXFMT = re.compile(
    r"\b(yuvj?[0-9]{3}[a-z0-9]*|gbrp[a-z0-9]*|rgb[a-z0-9]*|bgr[a-z0-9]*"
    r"|gray[a-z0-9]*|nv[0-9]+|p0[0-9]{2}[a-z0-9]*)\b")
# The parenthesised group right after a codec name is a profile ("High",
# "LC", "Baseline") — unless it holds a '/', which makes it the fourcc.
_RE_PROFILE = re.compile(r"^[A-Za-z0-9_]+\s*\(([^()/]+)\)")
# "Display Matrix: rotation of -90.00 degrees", printed as stream side data on
# the lines *after* the Stream line — hence searched against the whole blob
# rather than the stream line, which is safe because a file with more than one
# rotated video stream doesn't occur outside a test suite.
#
# Case-insensitive with an optional space because ffmpeg has spelled this both
# "displaymatrix" and "Display Matrix" across versions; 8.1 prints the latter,
# and matching only the former is a silent no-op rather than an error.
_RE_ROTATION = re.compile(
    r"display\s*matrix:\s*rotation of\s*(-?\d+(?:\.\d+)?)\s*degrees", re.I)

_CHANNEL_NAMES = {"mono": 1, "stereo": 2, "2.1": 3, "quad": 4, "4.0": 4,
                  "5.0": 5, "5.1": 6, "6.1": 7, "7.1": 8, "downmix": 2}


def _first_int(rx, text, default=0) -> int:
    m = rx.search(text)
    if not m:
        return default
    try:
        return int(m.group(1))
    except ValueError:
        return default


def probe_full(ffmpeg_exe: str, path) -> dict:
    """
    Everything the History tab shows about a file, from one header read.

    Same subprocess cost as probe(); the difference is entirely in the parsing.
    Every field degrades to 0 or '' rather than failing, because "we couldn't
    read the frame rate" must never stop somebody previewing a video.

    Returns duration, format, bitrate (bits/s, 0 = unknown), has_cover, and the
    first video/audio stream's details.

    Cover art is the trap here: an MP3 or FLAC's embedded artwork is reported
    as a real video stream ("Video: mjpeg ... 640x640 ... (attached pic)").
    Such a stream sets has_cover and nothing else — treating it as footage
    would show a music file as a 640x640 video.

    'width'/'height' are the *coded* dimensions, which for phone video are not
    the dimensions anybody sees — see display_dims().
    """
    out = {
        "duration": 0.0, "format": "", "bitrate": 0, "has_cover": False,
        "vcodec": "", "vprofile": "", "width": 0, "height": 0, "fps": 0.0,
        "vbitrate": 0, "pix_fmt": "", "rotation": 0,
        "acodec": "", "aprofile": "", "sample_rate": 0, "channels": 0,
        "abitrate": 0, "sample_fmt": "",
    }
    err = _probe_stderr(ffmpeg_exe, path)
    if not err:
        return out

    m = _RE_DUR.search(err)
    if m:
        h, mi, s = m.groups()
        out["duration"] = int(h) * 3600 + int(mi) * 60 + float(s)

    m = _RE_FORMAT.search(err)
    if m:
        out["format"] = m.group(1).strip()

    m = _RE_ROTATION.search(err)
    if m:
        try:
            out["rotation"] = int(round(float(m.group(1)))) % 360
        except ValueError:
            pass

    # The container bitrate lives on the Duration line; a stream's own kb/s
    # appears later, so slice to that line rather than searching the whole blob.
    dur_line = next((ln for ln in err.splitlines() if "Duration:" in ln), "")
    out["bitrate"] = _first_int(_RE_KBPS, dur_line) * 1000

    for kind, rest in _RE_STREAM_LINE.findall(err):
        rest = rest.strip()
        codec = rest.split(",")[0].split(" ")[0].split("(")[0].strip().lower()
        pm = _RE_PROFILE.match(rest)
        profile = pm.group(1).strip() if pm else ""

        if kind == "Video":
            if "attached pic" in rest:
                out["has_cover"] = True
                continue
            if out["vcodec"]:
                continue
            out["vcodec"], out["vprofile"] = codec, profile
            d = _RE_DIMS.search(rest)
            if d:
                out["width"], out["height"] = int(d.group(1)), int(d.group(2))
            f = _RE_FPS.search(rest)
            if f:
                try:
                    out["fps"] = float(f.group(1))
                except ValueError:
                    pass
            p = _RE_PIXFMT.search(rest)
            if p:
                out["pix_fmt"] = p.group(1)
            out["vbitrate"] = _first_int(_RE_KBPS, rest) * 1000
        else:
            if out["acodec"]:
                continue
            out["acodec"], out["aprofile"] = codec, profile
            out["sample_rate"] = _first_int(_RE_HZ, rest)
            out["abitrate"] = _first_int(_RE_KBPS, rest) * 1000
            sf = _RE_SAMPLE_FMT.search(rest)
            if sf:
                out["sample_fmt"] = sf.group(1) + sf.group(2)
            for field in (x.strip().lower() for x in rest.split(",")):
                if field in _CHANNEL_NAMES:
                    out["channels"] = _CHANNEL_NAMES[field]
                    break
                if field.endswith(" channels"):
                    out["channels"] = _int(field.split(" ")[0])
                    break
    return out


def display_dims(info: dict) -> tuple:
    """
    The dimensions a viewer actually sees, given probe_full's coded ones.

    A portrait phone clip is stored 1920x1080 with a rotation flag, not
    1080x1920 — the pixels really are landscape and the player turns them.
    ffmpeg applies that rotation itself before any -vf runs, so this changes
    nothing about an encode; it exists so the *description* of a file matches
    what the user is looking at. Telling somebody their 9:16 phone video is
    16:9 and will be letterboxed, when it won't be, is worse than saying
    nothing.
    """
    w, h = _int(info.get("width")), _int(info.get("height"))
    return (h, w) if abs(_int(info.get("rotation"))) % 180 == 90 else (w, h)


def can_stream_copy(target: str, info: dict, opts: dict,
                    quality_is_default: bool) -> tuple[bool, bool]:
    """
    Whether the video/audio streams can be remuxed instead of re-encoded.

    Returns (copy_video, copy_audio), decided independently — a WebM with AV1
    video and Opus audio going to MP4 correctly yields (True, False).
    """
    if not opts.get("remux", True) or target == "gif":
        return (False, False)
    if category_for_target(target) == "image":
        return (False, False)
    # Any explicit transform makes a byte-for-byte copy meaningless.
    if _has_reencode_override(opts) or not quality_is_default:
        return (False, False)

    vcodec = (info.get("vcodec") or "").lower()
    acodec = (info.get("acodec") or "").lower()

    if category_for_target(target) == "audio":
        # Extraction: copy only when the source track is already the codec we
        # were going to produce anyway.
        return (False, bool(acodec) and acodec == AUDIO_CODEC_OF.get(target))

    allowed = COPY_OK.get(target)
    if allowed is None:
        return (False, False)
    ok_v = bool(vcodec) and (allowed["v"] is None or vcodec in allowed["v"])
    ok_a = bool(acodec) and (allowed["a"] is None or acodec in allowed["a"])
    return (ok_v, ok_a)


def _has_reencode_override(opts: dict) -> bool:
    """True when an Advanced field asks for something a stream copy can't do."""
    for k in ("width", "height", "fps", "sampleRate", "channels"):
        if _int(opts.get(k)):
            return True
    for k in ("bitrate", "crf", "trimStart", "trimEnd"):
        if str(opts.get(k) or "").strip():
            return True
    # Loudness normalisation is an audio filter, and a stream copy runs no
    # filters. Without this the copy path wins, build_cmd emits -c:a copy, the
    # -af is suppressed by its own guard, and the user gets a bit-identical
    # file they believe is normalised — wrong and completely silent.
    if loudnorm_preset(opts):
        return True
    return False


# ── Command building ─────────────────────────────────────────────────────────

# Slider index -> setting. These mirror ui/index.html's CONVERT_QUALITY table
# and backend.py's _audio_postproc(), so a file converted here is identical to
# one the Audio tab produces. Change one, change all three.
_BITRATE_STEPS = ["128k", "192k", "256k", "320k", "320k"]
_OPUS_STEPS = ["96k", "128k", "160k", "192k", "256k"]
_CRF_X264 = [32, 28, 23, 20, 17]
_CRF_VP9 = [45, 38, 32, 27, 22]
_QV_MPEG4 = [8, 6, 4, 3, 2]
# (fps, width) per GIF quality step
_GIF_STEPS = [(8, 320), (10, 480), (12, 480), (15, 640), (20, 720)]

# Default slider position per target, used to detect "user didn't touch it"
# for the remux decision.
DEFAULT_QUALITY = {
    "mp3": 3, "m4a": 3, "aac": 3, "wma": 3, "opus": 3, "ogg": 7,
    "flac": 5, "wav": 0,
    "mp4": 2, "mkv": 2, "mov": 2, "webm": 2, "avi": 2, "gif": 2,
    "jpg": 90, "webp": 85, "png": 6, "bmp": 0, "imgif": 0, "ico": 0,
}

_RE_TIME = re.compile(r"^\d{1,2}(:\d{1,2}){0,2}(\.\d+)?$")


def _int(v, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _clamp_idx(q, steps: list):
    return steps[max(0, min(len(steps) - 1, _int(q)))]


def _time_arg(v) -> str:
    """Accept HH:MM:SS(.ms) / MM:SS / SS, reject anything else silently."""
    s = str(v or "").strip()
    return s if s and _RE_TIME.match(s) else ""


def _time_secs(v) -> float:
    """The same accepted formats as _time_arg, in seconds. 0.0 if unparseable."""
    s = _time_arg(v)
    if not s:
        return 0.0
    total = 0.0
    try:
        for part in s.split(":"):
            total = total * 60 + float(part)
    except ValueError:
        return 0.0
    return total


def audio_args(target: str, quality, opts: dict) -> list:
    """
    Encoder flags for an audio target, honouring an Advanced bitrate.

    Public because mediaops.py needs the same table: an audio container will
    only accept certain codecs, and there is no version of "clip a FLAC" that
    should be deciding that independently of "convert to FLAC".
    """
    br = str(opts.get("bitrate") or "").strip()
    if br and not br.endswith("k"):
        br += "k"

    if target == "mp3":
        # Top slider step is VBR V0, matching the Audio tab's "V0 (~245 kbps)".
        if br:
            return ["-c:a", "libmp3lame", "-b:a", br]
        if _int(quality) >= 4:
            return ["-c:a", "libmp3lame", "-q:a", "0"]
        return ["-c:a", "libmp3lame", "-b:a", _clamp_idx(quality, _BITRATE_STEPS)]
    if target == "flac":
        lvl = max(0, min(12, _int(quality, 5)))
        return ["-c:a", "flac", "-compression_level", str(lvl)]
    if target == "wav":
        return ["-c:a", "pcm_s16le"]
    if target in ("m4a", "aac"):
        a = ["-c:a", "aac", "-b:a", br or _clamp_idx(quality, _BITRATE_STEPS)]
        if target == "m4a":
            a += ["-movflags", "+faststart"]
        return a
    if target == "ogg":
        return ["-c:a", "libvorbis", "-q:a", str(max(0, min(10, _int(quality, 7))))]
    if target == "opus":
        return ["-c:a", "libopus", "-b:a", br or _clamp_idx(quality, _OPUS_STEPS)]
    if target == "wma":
        return ["-c:a", "wmav2", "-b:a", br or _clamp_idx(quality, _BITRATE_STEPS)]
    return []


# ── Loudness ─────────────────────────────────────────────────────────────────

# name -> (I, TP, LRA): integrated loudness in LUFS, true peak in dBTP, and
# loudness range. "streaming" is the EBU R128-derived level Spotify/YouTube/
# Apple all normalise to, so a file at -14 plays back unchanged there.
LOUDNORM_PRESETS = {
    "streaming": (-14.0, -1.0, 11.0),
    "broadcast": (-23.0, -2.0, 7.0),
    "loud":       (-9.0, -1.0, 15.0),
}


def loudnorm_preset(opts: dict):
    """The (I, TP, LRA) triple this job asks for, or None when it doesn't."""
    return LOUDNORM_PRESETS.get(str(opts.get("loudnorm") or "").strip().lower())


def _loudnorm_measure_cmd(ffmpeg_exe: str, src, preset,
                          duration_limit: float = 0.0) -> list:
    i, tp, lra = preset
    cmd = [
        ffmpeg_exe, "-hide_banner", "-nostdin",
        # NOT -loglevel warning, which build_cmd uses: loudnorm prints its JSON
        # at info level, so warning suppresses it entirely and every
        # measurement silently comes back empty. Verified on ffmpeg 8.1.
        "-loglevel", "info",
        "-progress", "pipe:1", "-nostats",
        "-i", str(src),
    ]
    # Measure only the part that will actually be shipped. Without this, a
    # 90-second cut of a ten-minute video is normalised against the loudness of
    # the nine and a half minutes nobody will hear, which is exactly the
    # mismatch two-pass exists to prevent.
    if duration_limit and duration_limit > 0:
        cmd += ["-t", f"{float(duration_limit):.3f}"]
    return cmd + [
        "-af", f"loudnorm=I={i}:TP={tp}:LRA={lra}:print_format=json",
        "-f", "null", "-",
    ]


def _parse_loudnorm_json(text: str) -> dict:
    """Pull the last {...} block out of ffmpeg's stderr tail."""
    end = text.rfind("}")
    start = text.rfind("{", 0, end)
    if start < 0 or end < 0:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return {}
    # Every value arrives as a string, and a silent file yields "-inf".
    out = {}
    for k in ("input_i", "input_tp", "input_lra", "input_thresh",
              "target_offset"):
        try:
            v = float(data[k])
        except (KeyError, TypeError, ValueError):
            return {}
        if v != v or v in (float("inf"), float("-inf")):
            return {}
        out[k] = v
    return out


def measure_loudness(ffmpeg_exe: str, src, preset, total_secs: float = 0.0,
                     on_pct=None, should_abort=None, on_proc=None,
                     duration_limit: float = 0.0) -> dict:
    """
    Pass one: measure the file's actual loudness.

    Returns the five measured values, or {} if anything at all went wrong —
    the caller then falls back to single-pass, which still normalises, just
    less precisely. Never raises: a failed measurement must not fail the job.

    `duration_limit` measures only the first N seconds, for callers that are
    going to trim the file to that length anyway. 0 measures all of it.
    """
    rc, tail = run_ffmpeg(_loudnorm_measure_cmd(ffmpeg_exe, src, preset,
                                                duration_limit),
                          total_secs, on_pct=on_pct,
                          should_abort=should_abort, on_proc=on_proc)
    if rc != 0:
        return {}
    return _parse_loudnorm_json(tail)


def loudnorm_filter(opts: dict, info: dict) -> str:
    """
    The -af chain for loudness normalisation, or '' when not asked for.

    Two things beyond the loudnorm call itself, both of which are silent
    quality/size regressions if left out:

    * loudnorm resamples to 192 kHz internally and *outputs* at 192 kHz, so a
      normalised 44.1 kHz FLAC comes out nearly 4x the size for no benefit.
      aresample pins it back to the source rate.
    * with the measured values from pass one it can normalise linearly — one
      constant gain applied to the whole file. Without them it falls back to
      dynamic mode, which rides the level and audibly pumps on quiet intros.
    """
    preset = loudnorm_preset(opts)
    if not preset:
        return ""
    i, tp, lra = preset
    f = f"loudnorm=I={i}:TP={tp}:LRA={lra}"

    m = opts.get("loudnormMeasured") or {}
    if m:
        f += (f":measured_I={m['input_i']}:measured_TP={m['input_tp']}"
              f":measured_LRA={m['input_lra']}"
              f":measured_thresh={m['input_thresh']}"
              f":offset={m['target_offset']}:linear=true")

    rate = _int(opts.get("sampleRate")) or _int(info.get("sample_rate"))
    if rate:
        f += f",aresample={rate}"
    return f


def _loudnorm_sample_fmt(target: str, opts: dict, info: dict) -> list:
    """
    Keep a normalised lossless file at the source's bit depth.

    loudnorm works in floating point, so FLAC otherwise promotes a 16-bit
    source to 24-bit and roughly triples the file for zero audible gain.
    Only ever pins *down* to what the source already was.
    """
    if target != "flac" or not loudnorm_preset(opts):
        return []
    return ["-sample_fmt", "s16"] if info.get("sample_fmt") == "s16" else []


def _video_filters(opts: dict, target: str) -> str:
    """scale/fps filter chain from the Advanced fields. '' when untouched."""
    parts = []
    w, h = _int(opts.get("width")), _int(opts.get("height"))
    if w or h:
        # -2 keeps the other axis proportional *and* even, which x264 requires.
        parts.append(f"scale={w or -2}:{h or -2}")
    fps = _int(opts.get("fps"))
    if fps:
        parts.append(f"fps={fps}")
    return ",".join(parts)


def build_cmd(ffmpeg_exe: str, src, tmp_dst, target: str, quality,
              opts: dict, info: dict, copy_v: bool = False,
              copy_a: bool = False) -> list:
    """
    Assemble the full ffmpeg argv for one file.

    Always a list, never a shell string — paths routinely contain spaces,
    quotes and non-ASCII, and there is no shell here to re-quote for.
    """
    src, tmp_dst = Path(src), Path(tmp_dst)
    cat = category_for_target(target)
    src_cat = CATEGORY_OF_EXT.get(src.suffix.lower(), "")

    cmd = [ffmpeg_exe, "-hide_banner", "-nostdin", "-loglevel", "warning",
           "-progress", "pipe:1", "-nostats", "-y"]

    # -ss before -i seeks by keyframe index instead of decoding to the cut
    # point, which is the difference between instant and minutes on a long file.
    ss = _time_arg(opts.get("trimStart"))
    if ss:
        cmd += ["-ss", ss]
    cmd += ["-i", str(src)]

    # "Trim end" means an end time measured from the start of the file, which
    # is the only reading of that label. -to would *not* give that: with -ss
    # before -i, ffmpeg measures -to from the seek point, so "start 5, end 8"
    # produced an 8-second clip instead of a 3-second one. Express it as a
    # duration and the ambiguity disappears.
    to = _time_arg(opts.get("trimEnd"))
    dur_arg = ""
    if to:
        span = _time_secs(to) - _time_secs(ss)
        # An end at or before the start is a typo, not a request for an empty
        # file; ignore the field rather than write zero bytes.
        if span > 0:
            dur_arg = f"{span:.3f}"
    if dur_arg:
        cmd += ["-t", dur_arg]

    if target == "gif":
        fps, width = _GIF_STEPS[max(0, min(4, _int(quality, 2)))]
        fps = _int(opts.get("fps")) or fps
        width = _int(opts.get("width")) or width
        # Single pass: palettegen and paletteuse in one graph, so there is no
        # temp palette PNG to leak if we're killed mid-run. stats_mode=diff
        # plus diff_mode=rectangle stops static backgrounds from shimmering.
        cmd += ["-filter_complex",
                f"[0:v]fps={fps},scale={width}:-1:flags=lanczos,split[a][b];"
                f"[a]palettegen=stats_mode=diff[p];"
                f"[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
                "-loop", "0", "-an"]
        # A GIF of a feature film is gigabytes nobody wants; cap it unless the
        # user set an explicit trim.
        if not dur_arg:
            cmd += ["-t", str(_int(opts.get("gifSeconds"), 30) or 30)]
        cmd.append(str(tmp_dst))
        return cmd

    if cat == "audio":
        keep_art = (opts.get("keepArt", True) and src_cat == "audio"
                    and target in ("mp3", "flac", "m4a"))
        if keep_art:
            # Cover art is a video stream in disguise; copy it as an attached
            # picture rather than letting the encoder treat it as footage.
            cmd += ["-c:v", "copy", "-disposition:v", "attached_pic"]
        else:
            cmd += ["-vn"]
        # Guarded on copy_a because a stream copy runs no filter graph: asking
        # for both is an ffmpeg error, not a silently ignored flag.
        af = "" if copy_a else loudnorm_filter(opts, info)
        if af:
            cmd += ["-af", af]
        cmd += ["-c:a", "copy"] if copy_a else audio_args(target, quality, opts)
        if not copy_a:
            cmd += _loudnorm_sample_fmt(target, opts, info)
        sr, ch = _int(opts.get("sampleRate")), _int(opts.get("channels"))
        if sr and not copy_a:
            cmd += ["-ar", str(sr)]
        if ch and not copy_a:
            cmd += ["-ac", str(ch)]
        cmd += ["-map_metadata", "0", str(tmp_dst)]
        return cmd

    # ── video ──
    # Without explicit maps a multi-audio MKV becomes an MP4 with six audio
    # tracks; the trailing ? on the audio map keeps a silent source working.
    cmd += ["-map", "0:v:0", "-map", "0:a:0?"]

    vf = _video_filters(opts, target)
    if copy_v and not vf:
        cmd += ["-c:v", "copy"]
    else:
        crf = str(opts.get("crf") or "").strip()
        if target == "webm":
            cmd += ["-c:v", "libvpx-vp9", "-b:v", "0", "-row-mt", "1",
                    "-crf", crf or str(_clamp_idx(quality, _CRF_VP9))]
        elif target == "avi":
            # mpeg4/XVID, not H.264: H.264-in-AVI plays in VLC and nothing
            # else, and legacy compatibility is the only reason to pick AVI.
            cmd += ["-c:v", "mpeg4", "-vtag", "XVID",
                    "-q:v", str(_clamp_idx(quality, _QV_MPEG4))]
        else:
            cmd += ["-c:v", "libx264", "-preset", "medium",
                    "-crf", crf or str(_clamp_idx(quality, _CRF_X264)),
                    # Non-negotiable: a 4:4:4 or 10-bit source otherwise makes
                    # a file QuickTime, WMP and most phones refuse to play.
                    "-pix_fmt", "yuv420p"]
        if vf:
            cmd += ["-vf", vf]

    # Same seam as the audio branch: a normalised MP4 is a normal request.
    af_a = "" if copy_a else loudnorm_filter(opts, info)
    if af_a:
        cmd += ["-af", af_a]

    if copy_a:
        cmd += ["-c:a", "copy"]
    elif target == "webm":
        cmd += ["-c:a", "libopus", "-b:a", "128k"]
    elif target == "avi":
        cmd += ["-c:a", "libmp3lame", "-b:a", "192k"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "192k"]

    if target == "mkv":
        cmd += ["-c:s", "copy"]      # only listed container that takes any sub
    else:
        cmd += ["-sn"]
    if target in ("mp4", "mov"):
        cmd += ["-movflags", "+faststart"]

    cmd += ["-map_metadata", "0", str(tmp_dst)]
    return cmd


# ── Running ──────────────────────────────────────────────────────────────────

# Note on stream-copy failures: callers must retry with a full re-encode after
# *any* non-zero exit from a copy attempt, rather than pattern-matching the
# error. ffmpeg words these inconsistently — "Could not find tag for codec",
# "Nothing was written into output file, because at least one of its streams
# received no packets", plain "Invalid data" — and a missed pattern means the
# user sees a muxer error for a file we could simply have re-encoded. The cost
# of guessing wrong is one wasted fast attempt; the cost of a missed pattern is
# a failed conversion.


def run_ffmpeg(cmd: list, total_secs: float, on_pct=None,
               should_abort=None, on_proc=None) -> tuple[int, str]:
    """
    Run one ffmpeg command, reporting progress and honouring cancellation.

    Returns (returncode, stderr_tail). rc -1 means the user aborted.

    `on_pct(pct)` is called with 0-100 as the encode advances, or with None
    when the duration is unknown so the caller can show an indeterminate bar.
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
        errors="replace", creationflags=_NO_WINDOW,
    )
    if on_proc:
        on_proc(proc)

    # stderr must be drained concurrently. We read stdout in this thread, and
    # with a noisy source (thousands of decode warnings) an undrained stderr
    # fills its 64 KB pipe buffer and ffmpeg blocks forever on the write.
    tail = deque(maxlen=40)

    def _drain():
        try:
            for line in proc.stderr:
                line = line.strip()
                if line:
                    tail.append(line)
        except Exception:
            pass

    t = threading.Thread(target=_drain, daemon=True)
    t.start()

    aborted = False
    try:
        for line in proc.stdout:
            if should_abort and should_abort():
                aborted = True
                proc.kill()
                break
            line = line.strip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key not in ("out_time_us", "out_time_ms"):
                continue
            # out_time_ms is a long-standing misnomer — ffmpeg writes
            # MICROseconds under both keys. Dividing by 1e3 would report
            # 1000x progress.
            try:
                secs = int(val) / 1_000_000
            except ValueError:
                continue
            if on_pct:
                on_pct(min(99.9, secs / total_secs * 100)
                       if total_secs > 0 else None)
    except Exception:
        pass
    finally:
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        t.join(timeout=2)
        if on_proc:
            on_proc(None)

    # The caller may also kill the process directly (Abort does, so a stalled
    # encode that emits no progress lines still dies instantly). That closes
    # stdout and ends the loop above without should_abort() ever being polled,
    # so re-check here — otherwise a cancelled job reports whatever exit code
    # the kill produced and we surface a bogus ffmpeg error to the user.
    if not aborted and should_abort and should_abort():
        aborted = True

    if aborted:
        return -1, "aborted"
    return proc.returncode, "\n".join(tail)


# ── Images (Pillow) ──────────────────────────────────────────────────────────

def pillow_available() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except Exception:
        return False


def probe_image(path) -> dict:
    """
    Dimensions and frame count for a still or animated image, via Pillow.

    ffmpeg is the wrong tool for this. Its webp demuxer skips the ANIM chunk
    outright — an animated WebP comes back as "unspecified size" with no width
    at all — and it reports every still as a 25 fps video. Pillow reads the
    header of everything the Convert tab already accepts, so the History Info
    panel can show a resolution for images ffmpeg simply shrugs at.

    Returns {} when Pillow is missing or the file can't be read; callers fall
    back to probe_full().
    """
    try:
        from PIL import Image
        with Image.open(str(path)) as im:
            frames = getattr(im, "n_frames", 1)
            return {"width": im.width, "height": im.height,
                    "format": (im.format or "").lower(),
                    "frames": frames, "animated": frames > 1}
    except Exception:
        return {}


def convert_image(src, dst, target: str, quality, opts: dict) -> str:
    """
    Convert one image. Returns a note to log ('' when there's nothing to say).

    Pillow rather than ffmpeg because ffmpeg has no ICO muxer at all and
    handles animated GIF/WebP frame sets poorly.
    """
    from PIL import Image, ImageSequence

    # A 40000x40000 PNG is ~6 GB decoded. Pillow's default bomb guard warns at
    # ~89 Mpx; raise it to something a real photo could hit but a malicious or
    # corrupt file can't, and let the caller treat the error as a normal
    # per-file failure instead of taking the whole batch down.
    Image.MAX_IMAGE_PIXELS = 300_000_000

    src, dst = Path(src), Path(dst)
    note = ""
    img = Image.open(src)

    frames = getattr(img, "n_frames", 1)
    animated = frames > 1
    want_animation = target in ("webp", "imgif") and animated

    w, h = _int(opts.get("width")), _int(opts.get("height"))
    if w or h:
        ratio = img.width / img.height if img.height else 1
        new = (w or int(round((h or img.height) * ratio)),
               h or int(round((w or img.width) / ratio)))
        img = img.resize(new, Image.LANCZOS)

    q = _int(quality, DEFAULT_QUALITY.get(target, 90))
    params = {}

    if target == "jpg":
        img = _flatten(img)
        params = {"format": "JPEG", "quality": max(1, min(100, q)),
                  "optimize": True}
    elif target == "webp":
        params = {"format": "WEBP", "quality": max(1, min(100, q)),
                  "lossless": q >= 100, "method": 4}
    elif target == "png":
        params = {"format": "PNG", "compress_level": max(0, min(9, q)),
                  "optimize": True}
    elif target == "bmp":
        img = _flatten(img)
        params = {"format": "BMP"}
    elif target == "imgif":
        img = img.convert("P", palette=Image.ADAPTIVE)
        params = {"format": "GIF"}
    elif target == "ico":
        # 256 is the format's hard ceiling; anything larger simply can't be
        # stored in an .ico.
        n = max(16, min(256, _int(opts.get("icoSize"), 256) or 256))
        if img.width > n or img.height > n:
            img.thumbnail((n, n), Image.LANCZOS)
        if img.mode not in ("RGBA", "RGB"):
            img = img.convert("RGBA")
        params = {"format": "ICO", "sizes": [(img.width, img.height)]}
    else:
        raise ValueError(f"not an image target: {target}")

    if want_animation:
        seq = [f.copy() for f in ImageSequence.Iterator(Image.open(src))]
        first = seq[0]
        if target == "imgif":
            first = first.convert("P", palette=Image.ADAPTIVE)
        first.save(dst, save_all=True, append_images=seq[1:],
                   loop=0, duration=img.info.get("duration", 100), **params)
        note = f"kept {len(seq)} frames"
    else:
        if animated:
            note = f"took first frame of {frames}"
        img.save(dst, **params)
    return note


def _flatten(img):
    """
    Drop transparency onto white.

    JPEG and BMP have no alpha channel; saving an RGBA image to either raises
    OSError, and converting straight to RGB turns transparent pixels black.
    """
    from PIL import Image
    if img.mode in ("RGBA", "LA") or (img.mode == "P"
                                      and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return img.convert("RGB") if img.mode != "RGB" else img
