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
ordinary answers the UI prints as a sentence.
"""

from pathlib import Path

import convert
import preview

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
