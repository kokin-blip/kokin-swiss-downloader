"""
One-file-in, one-file-out media operations driven from the History tab.

Same rule as convert.py and preview.py: nothing here knows about pywebview or
the API object. ffmpeg's path, progress callbacks and the abort check all
arrive as arguments, so every operation can be exercised from a bare REPL.

Why these live here rather than in convert.py: convert.build_cmd serves the
Convert tab's (target, quality, opts) contract and every conversion, remux and
stream-copy retry funnels through its five-way branch. A clip is not a format
change — it has a start and a duration and deliberately keeps the source
container — so bolting it on would widen the riskiest function in the codebase
for no shared behaviour. Operations that genuinely *are* an encode with a
filter attached (loudness normalisation) call convert.build_cmd instead of
reimplementing its encoder table.

Every function returns {'ok', 'path', 'msg'} and never raises for an expected
failure — a missing file, an impossible request and a user abort are all
ordinary answers the UI prints as a sentence. A result may also carry
'skip': True, meaning "the right thing to do was nothing" — which is a
success, but not one that should leave a history row.
"""

import math
import os
from pathlib import Path

import convert
import preview
from utils import app_data_dir

# A clip is a short excerpt of something the user already has, so it gets a
# near-transparent CRF the Convert tab would find too expensive across a
# 40-file batch — and a fast preset, because the run is seconds either way.
_CLIP_CRF = "18"
_CLIP_PRESET = "veryfast"

# Re-encode flags per video container. Mirrors the constraints
# convert.build_cmd enforces for the same targets: WebM takes only VP8/VP9 +
# Opus/Vorbis, and AVI gets mpeg4/XVID rather than H.264 because H.264-in-AVI
# plays in VLC and nothing else. Anything not listed gets H.264 + AAC.
_CLIP_ENCODERS = {
    "webm": ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "30", "-row-mt", "1",
             "-c:a", "libopus", "-b:a", "128k"],
    "avi":  ["-c:v", "mpeg4", "-vtag", "XVID", "-q:v", "4",
             "-c:a", "libmp3lame", "-b:a", "192k"],
}
_CLIP_ENCODERS_DEFAULT = [
    "-c:v", "libx264", "-preset", _CLIP_PRESET, "-crf", _CLIP_CRF,
    # A 4:4:4 or 10-bit source otherwise yields a file QuickTime, WMP and most
    # phones refuse to play — same reason convert.py pins this.
    "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-b:a", "192k",
]

# How far a keyframe-aligned fast cut may land from the requested start before
# we tell the user about it. Below this nobody would notice; above it they
# would, and silently handing back a clip that begins a second early is exactly
# the kind of thing that gets blamed on the app rather than on the codec.
_DRIFT_TOLERANCE = 0.25


def build_clip_cmd(ffmpeg_exe: str, src, tmp_dst, start: float, dur: float,
                   copy: bool, ext: str, is_audio: bool = False) -> list:
    """
    ffmpeg argv for cutting `dur` seconds starting at `start`.

    Always -t, never -to. With -ss placed before -i, ffmpeg measures -to from
    the seek point rather than from the start of the file, so the two options
    disagree about what "8" means; a duration means the same thing under every
    combination. (This is the same trap convert.build_cmd used to fall into.)

    -ss stays before -i: it seeks by keyframe index and then decodes forward to
    the exact timestamp, which is both the fast form and — for a re-encode —
    the accurate one. -ss after -i would decode the whole file up to the cut
    point, taking minutes on a long video for nothing.
    """
    cmd = [ffmpeg_exe, "-hide_banner", "-nostdin", "-loglevel", "warning",
           "-progress", "pipe:1", "-nostats", "-y",
           "-ss", f"{max(0.0, start):.3f}", "-i", str(src),
           "-t", f"{max(0.01, dur):.3f}"]

    if is_audio:
        cmd += ["-map", "0:a:0", "-vn"]
    else:
        # The trailing ? keeps a silent video working instead of failing on a
        # stream that isn't there.
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]

    if copy:
        # make_zero rebases the timestamps of the copied packets. Without it a
        # cut from 10:00 produces a file whose first frame claims t=600, which
        # some players show as a 10-minute clip that is blank for 10 minutes.
        cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    elif is_audio:
        # The container dictates the codec — an MP3 stream in a .flac simply
        # won't mux — so take the encoder from convert.py's table rather than
        # deciding it a second time here. An extension we have no target for
        # (.ape, .aiff) gets no -c:a and ffmpeg's own default for that muxer.
        cmd += convert.audio_args(ext, convert.DEFAULT_QUALITY.get(ext, 3), {})
    else:
        cmd += _CLIP_ENCODERS.get(ext, _CLIP_ENCODERS_DEFAULT)

    if ext in ("mp4", "mov", "m4a"):
        cmd += ["-movflags", "+faststart"]
    cmd += ["-map_metadata", "0", str(tmp_dst)]
    return cmd


def clip(ffmpeg_exe: str, src, start: float, end: float, out_dir: str = "",
         fast: bool = False, on_pct=None, should_abort=None,
         on_proc=None) -> dict:
    """
    Cut src[start:end] to its own file. Returns {'ok', 'path', 'msg'}.

    The output keeps the source's own extension. A fast (stream-copy) cut can
    then never hit a codec/container mismatch, and a re-encode never surprises
    someone by handing back a different format than they started with.

    `fast` trades accuracy for speed: a stream copy cannot start anywhere but a
    keyframe, so the clip begins at the nearest one before the requested point.
    That is why Precise is the default — a clip is short, and landing on the
    right frame is the entire point of the feature.

    `fast` is ignored for audio. It buys nothing there (audio frames are
    milliseconds, so re-encoding is already both instant and sample-accurate)
    and it actively breaks the result: a copied FLAC keeps the source's
    STREAMINFO, so a 3-second cut of an 8-second track reports itself as 8
    seconds and scrubs wrongly in every player.
    """
    src = Path(src)
    if not src.is_file():
        return {"ok": False, "path": "", "msg": "That file is no longer there."}
    if not ffmpeg_exe:
        return {"ok": False, "path": "", "msg": "ffmpeg wasn't found."}
    kind = preview.kind_of(src)
    if kind not in ("video", "audio"):
        return {"ok": False, "path": "",
                "msg": "Only videos and audio can be clipped."}
    is_audio = kind == "audio"
    if is_audio:
        fast = False        # see the docstring — it is worse, not faster

    try:
        start, end = max(0.0, float(start)), float(end)
    except (TypeError, ValueError):
        return {"ok": False, "path": "", "msg": "Bad in/out points."}
    dur = end - start
    if dur < 0.05:
        return {"ok": False, "path": "",
                "msg": "The out point has to come after the in point."}

    ext = src.suffix.lower().lstrip(".")
    folder = Path(out_dir) if out_dir else src.parent
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "path": "", "msg": f"Can't write to that folder: {e}"}

    name = (f"{preview.safe_stem(src.stem)}_clip_"
            f"{preview.stamp(start)}-{preview.stamp(end)}.{ext}")
    dst = preview.unique_path(folder / name)
    tmp = convert.temp_path(dst)

    cmd = build_clip_cmd(ffmpeg_exe, src, tmp, start, dur, fast, ext,
                         is_audio=is_audio)
    rc, tail = convert.run_ffmpeg(cmd, dur, on_pct=on_pct,
                                  should_abort=should_abort, on_proc=on_proc)

    if rc == -1:
        _discard(tmp)
        return {"ok": False, "path": "", "msg": "Stopped."}
    if rc != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        _discard(tmp)
        # A stream copy is the one that fails for a reason we can name: not
        # every codec survives being cut without re-encoding.
        if fast:
            return {"ok": False, "path": "",
                    "msg": "That won't cut without re-encoding — untick Fast cut."}
        return {"ok": False, "path": "",
                "msg": (tail.splitlines()[-1] if tail
                        else "ffmpeg couldn't write that clip.")}

    try:
        tmp.replace(dst)
    except OSError as e:
        _discard(tmp)
        return {"ok": False, "path": "", "msg": f"Couldn't save the clip: {e}"}

    msg = f"Saved {dst.name}"
    if fast:
        # One header read. Worth it: a fast cut that silently came out 1.8s
        # long is the feature's main way of disappointing someone.
        got = convert.probe(ffmpeg_exe, dst).get("duration", 0.0)
        if got and abs(got - dur) > _DRIFT_TOLERANCE:
            msg += (f" — but it's {got:.1f}s, not {dur:.1f}s. Fast cuts start "
                    f"at the nearest keyframe; untick Fast cut for an exact one.")
    return {"ok": True, "path": str(dst), "msg": msg}


def _discard(tmp: Path):
    """Remove a scratch file, ignoring failure — it is already the sad path."""
    try:
        tmp.unlink()
    except OSError:
        pass


# ── Contact sheet ────────────────────────────────────────────────────────────

# Enough tiles to see the shape of a video at a glance, few enough that each
# still shows a recognisable face at the default tile width.
SHEET_MAX_TILES = 100
SHEET_TILE_WIDTH = 320


def build_sheet_cmd(ffmpeg_exe: str, src, dst, cols: int, rows: int,
                    width: int, duration: float, fmt: str) -> list:
    """
    ffmpeg argv for one contact sheet: sample, scale, tile, write one image.

    The sample rate is tiles/duration, so the grid spans the whole video
    regardless of length. Expressed as a fraction rather than a rounded
    decimal — 16/3600 is exact where 0.004 is not, and on a long film the
    rounding is the difference between the last tile and the credits.

    No drawtext timestamps in this version. Windows font paths inside a
    filtergraph need escaping ("C\\:/Windows/Fonts/consola.ttf") and fail in
    ways that read as "the whole feature is broken" rather than "no captions".
    """
    tiles = max(1, cols * rows)
    return [
        ffmpeg_exe, "-hide_banner", "-nostdin", "-loglevel", "warning",
        "-progress", "pipe:1", "-nostats", "-y", "-i", str(src),
        "-vf", f"fps={tiles}/{max(0.001, duration):.4f},"
               f"scale={int(width)}:-1,tile={cols}x{rows}",
        "-frames:v", "1", "-an", "-sn",
        # A sheet is a lossless grid of stills; -q:v only applies to JPEG.
        *(["-q:v", "3"] if fmt == "jpg" else []),
        str(dst),
    ]


def contact_sheet(ffmpeg_exe: str, src, out_dir: str = "", cols: int = 4,
                  rows: int = 4, width: int = SHEET_TILE_WIDTH,
                  fmt: str = "jpg", on_pct=None, should_abort=None,
                  on_proc=None) -> dict:
    """
    One image showing the whole video as a grid of stills.

    Returns {'ok', 'path', 'msg'}.
    """
    src = Path(src)
    if not src.is_file():
        return {"ok": False, "path": "", "msg": "That file is no longer there."}
    if not ffmpeg_exe:
        return {"ok": False, "path": "", "msg": "ffmpeg wasn't found."}
    if preview.kind_of(src) != "video":
        return {"ok": False, "path": "", "msg": "Only videos have a contact sheet."}

    cols, rows = max(1, int(cols or 1)), max(1, int(rows or 1))
    if cols * rows > SHEET_MAX_TILES:
        return {"ok": False, "path": "",
                "msg": f"That's {cols * rows} tiles — keep it to {SHEET_MAX_TILES}."}

    duration = convert.probe(ffmpeg_exe, src).get("duration", 0.0)
    if duration <= 0:
        # Without a duration the sample rate is unknowable, and guessing would
        # silently produce a sheet of the first few seconds.
        return {"ok": False, "path": "",
                "msg": "Couldn't read how long that video is, so the grid "
                       "can't be spaced out."}

    fmt = "jpg" if str(fmt).lower() in ("jpg", "jpeg") else "png"
    folder = Path(out_dir) if out_dir else src.parent
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "path": "", "msg": f"Can't write to that folder: {e}"}

    dst = preview.unique_path(
        folder / f"{preview.safe_stem(src.stem)}_sheet_{cols}x{rows}.{fmt}")

    cmd = build_sheet_cmd(ffmpeg_exe, src, dst, cols, rows, width, duration, fmt)
    rc, tail = convert.run_ffmpeg(cmd, duration, on_pct=on_pct,
                                  should_abort=should_abort, on_proc=on_proc)

    if rc == -1:
        _discard(dst)
        return {"ok": False, "path": "", "msg": "Stopped."}
    # tile flushes a partial grid at EOF on current ffmpeg, so a short video
    # normally still yields a sheet. Handle the empty case anyway: an ffmpeg
    # that doesn't would otherwise exit 0 having written nothing at all.
    if rc != 0 or not dst.exists() or dst.stat().st_size == 0:
        _discard(dst)
        if rc == 0:
            return {"ok": False, "path": "",
                    "msg": f"That video is too short for a {cols}×{rows} "
                           f"sheet — try fewer tiles."}
        return {"ok": False, "path": "",
                "msg": (tail.splitlines()[-1] if tail
                        else "ffmpeg couldn't build that sheet.")}

    return {"ok": True, "path": str(dst),
            "msg": f"Saved {dst.name} ({cols}×{rows} tiles)"}


# ── Fit under a size limit ───────────────────────────────────────────────────

# Decimal megabytes, not MiB. Every service that imposes one of these limits —
# Discord, Gmail, Slack, WhatsApp — states it in decimal MB, and a user typing
# "25" means the number that service told them.
_MB = 1_000_000

# Headroom for container overhead the bitrate maths can't see: the moov atom,
# per-packet headers, and x264 missing its target by a percent or two. MKV
# carries more per-frame overhead than MP4, hence the lower figure.
_SAFETY = {"mp4": 0.97, "mov": 0.97, "mkv": 0.96, "webm": 0.96}

# Below this, video is a smear regardless of resolution, so refuse rather than
# hand back something worthless that took ten minutes to make.
_MIN_VIDEO_BPS = 100_000

# Containers that take H.264 + AAC. Anything else becomes an MP4, because the
# point of the feature is a file you can send to somebody.
_FIT_KEEP_EXT = ("mp4", "mov", "mkv")


def _audio_budget(target_mb: float) -> int:
    """Audio bitrate to reserve. Small limits can't afford 128k of it."""
    if target_mb >= 10:
        return 128_000
    return 96_000 if target_mb >= 5 else 64_000


def plan_fit(ffmpeg_exe: str, src, target_mb: float) -> dict:
    """
    Work out whether a size target is achievable, without encoding anything.

    Returned before the job starts so the UI can say "that leaves 24 kbps for
    video" instead of letting somebody wait ten minutes for a smear. Also used
    by fit_under itself, so the refusal and the preview can never disagree.

    Returns {'ok', 'msg', 'fits_already', 'video_bps', 'duration', 'size',
    'max_seconds', 'min_mb'}.
    """
    out = {"ok": False, "msg": "", "fits_already": False, "video_bps": 0,
           "duration": 0.0, "size": 0, "max_seconds": 0.0, "min_mb": 0.0}
    src = Path(src)
    if not src.is_file():
        out["msg"] = "That file is no longer there."
        return out
    try:
        target_mb = float(target_mb)
    except (TypeError, ValueError):
        out["msg"] = "Enter a size in MB."
        return out
    if target_mb <= 0:
        out["msg"] = "Enter a size greater than zero."
        return out

    out["size"] = src.stat().st_size
    duration = convert.probe(ffmpeg_exe, src).get("duration", 0.0)
    out["duration"] = duration
    if duration <= 0:
        out["msg"] = "Couldn't read how long that video is."
        return out

    if out["size"] <= target_mb * _MB:
        out["ok"] = True
        out["fits_already"] = True
        out["msg"] = (f"That's already {out['size'] / _MB:.1f} MB — "
                      f"under {_fmt_mb(target_mb)}. Nothing to do.")
        return out

    ext = src.suffix.lower().lstrip(".")
    safety = _SAFETY.get(ext, 0.97)
    audio_bps = _audio_budget(target_mb)
    video_bps = int(target_mb * 8 * _MB * safety / duration) - audio_bps

    # Both figures are worth showing: "too long for that limit" and "that limit
    # is too small" are the same fact, but a user is only ever able to change
    # one of them.
    usable_bps = target_mb * 8 * _MB * safety
    out["max_seconds"] = usable_bps / (_MIN_VIDEO_BPS + audio_bps)
    out["min_mb"] = math.ceil(
        (_MIN_VIDEO_BPS + audio_bps) * duration / (8 * _MB * safety) * 10) / 10

    if video_bps < _MIN_VIDEO_BPS:
        out["msg"] = (
            f"{_fmt_mb(target_mb)} over {_clock_words(duration)} leaves only "
            f"{max(0, video_bps) // 1000} kbps for video. At that limit you "
            f"could fit about {_clock_words(out['max_seconds'])}; for this "
            f"video you'd need about {_fmt_mb(out['min_mb'])}.")
        return out

    out["ok"] = True
    out["video_bps"] = video_bps
    out["msg"] = (f"{out['size'] / _MB:.1f} MB → {_fmt_mb(target_mb)} at about "
                  f"{video_bps // 1000} kbps video.")
    return out


def _scale_filter(video_bps: int) -> str:
    """
    Cap the resolution so the bitrate isn't spread over pixels it can't feed.

    1080p at 400 kbps is mush; the same 400 kbps at 480p is watchable. min()
    means this only ever downscales — a 360p source stays 360p — and -2 keeps
    the width even, which x264 requires.
    """
    if video_bps < 500_000:
        return "scale=-2:'min(ih,480)'"
    if video_bps < 1_200_000:
        return "scale=-2:'min(ih,720)'"
    return ""


def build_fit_cmds(ffmpeg_exe: str, src, dst, video_bps: int, audio_bps: int,
                   passlog: Path, ext: str) -> tuple[list, list]:
    """
    The two ffmpeg argvs for a two-pass encode at a fixed bitrate.

    Two passes because the whole feature is "land under this number". One pass
    at a target bitrate overshoots on anything with a busy scene; the analysis
    pass lets x264 spend the budget where it matters and hit the size.

    -passlogfile is mandatory, not tidiness: without it x264 writes
    "ffmpeg2pass-0.log" (and a .mbtree beside it) into the *process* working
    directory, which for the frozen exe is wherever the user happened to
    double-click. Files appearing on someone's Desktop is how an app gets
    reported as malware.
    """
    vf = _scale_filter(video_bps)
    common = [ffmpeg_exe, "-hide_banner", "-nostdin", "-loglevel", "warning",
              "-progress", "pipe:1", "-nostats", "-y", "-i", str(src),
              "-c:v", "libx264", "-b:v", str(video_bps),
              "-preset", "medium", "-pix_fmt", "yuv420p",
              "-passlogfile", str(passlog)]
    if vf:
        common += ["-vf", vf]

    # Pass 1 decodes and analyses only — no audio, no output file. -f null
    # rather than a scratch file so there is nothing to clean up if we're
    # killed mid-run.
    one = common + ["-pass", "1", "-an", "-f", "null", "-"]
    two = common + ["-pass", "2", "-c:a", "aac", "-b:a", str(audio_bps)]
    if ext in ("mp4", "mov"):
        two += ["-movflags", "+faststart"]
    two += ["-map_metadata", "0", str(dst)]
    return one, two


def fit_under(ffmpeg_exe: str, src, out_dir: str = "", target_mb: float = 25.0,
              on_pct=None, should_abort=None, on_proc=None,
              on_stage=None) -> dict:
    """
    Re-encode a video to land under `target_mb` decimal megabytes.

    Returns {'ok', 'path', 'msg'} and possibly 'skip'. A file that already
    fits is left alone: re-encoding it would only lose quality to prove a
    point it already satisfies.
    """
    src = Path(src)
    if not ffmpeg_exe:
        return {"ok": False, "path": "", "msg": "ffmpeg wasn't found."}
    if src.is_file() and preview.kind_of(src) != "video":
        return {"ok": False, "path": "", "msg": "Only videos can be resized."}

    plan = plan_fit(ffmpeg_exe, src, target_mb)
    if not plan["ok"]:
        return {"ok": False, "path": "", "msg": plan["msg"]}
    if plan["fits_already"]:
        return {"ok": True, "path": "", "msg": plan["msg"], "skip": True}

    target_mb = float(target_mb)
    ext = src.suffix.lower().lstrip(".")
    if ext not in _FIT_KEEP_EXT:
        ext = "mp4"
    folder = Path(out_dir) if out_dir else src.parent
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "path": "", "msg": f"Can't write to that folder: {e}"}

    dst = preview.unique_path(
        folder / f"{preview.safe_stem(src.stem)}_{_mb_tag(target_mb)}.{ext}")
    tmp = convert.temp_path(dst)

    tmpdir = app_data_dir() / "tmp"
    try:
        tmpdir.mkdir(parents=True, exist_ok=True)
    except OSError:
        tmpdir = folder
    passlog = tmpdir / f"fit-{os.getpid()}-{abs(hash(str(src))) % 100000}"

    audio_bps = _audio_budget(target_mb)
    one, two = build_fit_cmds(ffmpeg_exe, src, tmp, plan["video_bps"],
                              audio_bps, passlog, ext)
    duration = plan["duration"]

    try:
        # Pass 1 is analysis-only and genuinely faster than the encode, so it
        # gets the smaller share of the bar rather than a misleading half.
        if on_stage:
            on_stage("Analysing…")
        rc, tail = convert.run_ffmpeg(
            one, duration,
            on_pct=(lambda p: on_pct(p * 0.45 if p is not None else None))
            if on_pct else None,
            should_abort=should_abort, on_proc=on_proc)
        if rc == -1:
            return {"ok": False, "path": "", "msg": "Stopped."}
        if rc != 0:
            return {"ok": False, "path": "",
                    "msg": (tail.splitlines()[-1] if tail
                            else "The analysis pass failed.")}

        # x264's two-pass ratecontrol aims at -b:v but does not guarantee it:
        # measured 587 kbps against a 550 kbps request on hard content, ~7%
        # over. No fixed safety margin fixes that — too small and the file
        # misses the limit, too large and every file comes out needlessly
        # worse. So encode, measure, and if it missed, re-run pass 2 with the
        # bitrate scaled by exactly how much it missed by. Pass 1's analysis
        # holds (same source, resolution and preset), so a correction costs
        # one pass rather than two, and one is always enough in practice.
        limit = target_mb * _MB
        video_bps = plan["video_bps"]
        for attempt in (1, 2):
            if on_stage:
                on_stage("Encoding…" if attempt == 1 else "Tightening…")
            base = 45 if attempt == 1 else 75
            span = 30 if attempt == 1 else 25
            rc, tail = convert.run_ffmpeg(
                two, duration,
                on_pct=(lambda p, b=base, s=span:
                        on_pct(b + p * s / 100 if p is not None else None))
                if on_pct else None,
                should_abort=should_abort, on_proc=on_proc)
            if rc == -1:
                _discard(tmp)
                return {"ok": False, "path": "", "msg": "Stopped."}
            if rc != 0 or not tmp.exists() or tmp.stat().st_size == 0:
                _discard(tmp)
                return {"ok": False, "path": "",
                        "msg": (tail.splitlines()[-1] if tail
                                else "ffmpeg couldn't re-encode that.")}

            got = tmp.stat().st_size
            if got <= limit or attempt == 2:
                break
            # Scale the *total* budget by the miss, then take audio back off.
            # The extra 2% stops a second near-miss on a marginal file.
            total_bps = video_bps + audio_bps
            video_bps = int(total_bps * (limit / got) * 0.98) - audio_bps
            if video_bps < _MIN_VIDEO_BPS:
                break        # nothing left to give; report what we have
            _, two = build_fit_cmds(ffmpeg_exe, src, tmp, video_bps,
                                    audio_bps, passlog, ext)
    finally:
        # x264 writes more than the one log it is asked for: "-0.log",
        # "-0.log.mbtree", and a ".temp" of each while encoding. Globbing the
        # prefix catches every variant, including any this ffmpeg version adds
        # later — a hardcoded list quietly leaves files behind instead.
        for leftover in passlog.parent.glob(passlog.name + "*"):
            _discard(leftover)

    got = tmp.stat().st_size
    try:
        tmp.replace(dst)
    except OSError as e:
        _discard(tmp)
        return {"ok": False, "path": "", "msg": f"Couldn't save it: {e}"}

    msg = f"Saved {dst.name} — {got / _MB:.2f} MB, under {_fmt_mb(target_mb)}"
    if got > target_mb * _MB:
        # Report the miss rather than quietly handing back an oversized file
        # the user is about to try to upload.
        msg = (f"Saved {dst.name} — {got / _MB:.2f} MB, which is still over "
               f"{_fmt_mb(target_mb)}. This one won't compress that far "
               f"without going lower; try {_fmt_mb(round(target_mb * 0.85, 1))}.")
    return {"ok": True, "path": str(dst), "msg": msg}


def _mb_num(mb: float) -> str:
    """
    '25' / '9.5' / '0.05'.

    One decimal is right for the sizes people actually type, but rounding
    there would print a target of 0.05 as "0.1" — and the sentence it appears
    in is the one explaining why their number doesn't work. Anything under
    1 MB keeps its own precision instead.
    """
    mb = float(mb)
    if mb.is_integer():
        return str(int(mb))
    return f"{mb:g}" if mb < 1 else f"{mb:.1f}"


def _fmt_mb(mb: float) -> str:
    """'25 MB' — for sentences."""
    return f"{_mb_num(mb)} MB"


def _mb_tag(mb: float) -> str:
    """Filename-safe size suffix: 25MB, 9.5MB. No space — it's a filename."""
    return f"{_mb_num(mb)}MB"


# ── Loudness normalisation ───────────────────────────────────────────────────

# Pass one only decodes, so it runs far faster than the encode; splitting the
# bar 30/70 keeps it from crawling in the second half.
_MEASURE_SHARE = 30.0


def normalize(ffmpeg_exe: str, src, out_dir: str = "", preset: str = "streaming",
              on_pct=None, should_abort=None, on_proc=None,
              on_stage=None) -> dict:
    """
    Even out a file's loudness. Returns {'ok', 'path', 'msg'}.

    Output keeps the source's own format, so normalising a FLAC hands back a
    FLAC rather than quietly transcoding somebody's lossless library to MP3.
    That also means this rides convert.build_cmd and inherits its encoder
    table, temp-file discipline and cover-art handling rather than
    reimplementing them.

    Two passes: measure, then apply the measurement. Single-pass loudnorm is a
    *dynamic* normaliser that lands 1-3 LU off and audibly pumps on quiet
    intros. If the measurement fails for any reason we still run the second
    pass without it — a slightly imprecise result beats no result.
    """
    src = Path(src)
    if not src.is_file():
        return {"ok": False, "path": "", "msg": "That file is no longer there."}
    if not ffmpeg_exe:
        return {"ok": False, "path": "", "msg": "ffmpeg wasn't found."}
    if not convert.LOUDNORM_PRESETS.get(str(preset).lower()):
        return {"ok": False, "path": "", "msg": "Unknown loudness target."}
    kind = preview.kind_of(src)
    if kind not in ("video", "audio"):
        return {"ok": False, "path": "",
                "msg": "Only videos and audio have a loudness to even out."}

    # category_for_target takes a bare extension; CATEGORY_OF_EXT is keyed with
    # the dot and answers a different question (what a *source* file is, not
    # what we can encode to). Only the former tells us we can write this back.
    target = src.suffix.lower().lstrip(".")
    if not convert.category_for_target(target):
        return {"ok": False, "path": "",
                "msg": f"Can't write {target.upper()} files."}

    info = convert.probe_full(ffmpeg_exe, src)
    if not info.get("acodec"):
        return {"ok": False, "path": "",
                "msg": "There's no audio in that file to normalise."}
    duration = info.get("duration", 0.0)

    folder = Path(out_dir) if out_dir else src.parent
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "path": "", "msg": f"Can't write to that folder: {e}"}

    dst = preview.unique_path(
        folder / f"{preview.safe_stem(src.stem)}_normalized.{target}")
    tmp = convert.temp_path(dst)

    # ── pass 1 ──
    if on_stage:
        on_stage("Measuring loudness…")
    measured = convert.measure_loudness(
        ffmpeg_exe, src, convert.LOUDNORM_PRESETS[str(preset).lower()],
        duration,
        on_pct=(lambda p: on_pct(p * _MEASURE_SHARE / 100)) if on_pct and duration else on_pct,
        should_abort=should_abort, on_proc=on_proc)
    if should_abort and should_abort():
        return {"ok": False, "path": "", "msg": "Stopped."}

    # ── pass 2 ──
    if on_stage:
        on_stage("Applying…" if measured else "Applying (estimated)…")
    opts = {"loudnorm": str(preset).lower(), "loudnormMeasured": measured}
    cmd = convert.build_cmd(ffmpeg_exe, src, tmp, target,
                            convert.DEFAULT_QUALITY.get(target, 3), opts, info)
    rc, tail = convert.run_ffmpeg(
        cmd, duration,
        on_pct=(lambda p: on_pct(_MEASURE_SHARE + p * (100 - _MEASURE_SHARE) / 100))
        if on_pct else None,
        should_abort=should_abort, on_proc=on_proc)

    if rc == -1:
        _discard(tmp)
        return {"ok": False, "path": "", "msg": "Stopped."}
    if rc != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        _discard(tmp)
        return {"ok": False, "path": "",
                "msg": (tail.splitlines()[-1] if tail
                        else "ffmpeg couldn't normalise that file.")}
    try:
        tmp.replace(dst)
    except OSError as e:
        _discard(tmp)
        return {"ok": False, "path": "", "msg": f"Couldn't save it: {e}"}

    i_target = convert.LOUDNORM_PRESETS[str(preset).lower()][0]
    msg = f"Saved {dst.name} — now about {i_target:g} LUFS"
    if measured:
        msg += f" (was {measured['input_i']:g})"
    else:
        # Honest about the degraded path rather than silently doing less.
        msg += ". Couldn't measure it first, so this is an estimate."
    return {"ok": True, "path": str(dst), "msg": msg}


def _clock_words(secs: float) -> str:
    """'4m 20s' — for sentences, where 0:04:20 reads as a timestamp."""
    secs = max(0, int(secs))
    h, m, s = secs // 3600, secs % 3600 // 60, secs % 60
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s" if m else f"{s}s"
