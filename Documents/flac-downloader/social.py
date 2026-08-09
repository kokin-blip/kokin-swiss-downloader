"""
Re-shape one video for the places people post video.

Same rule as convert.py, mediaops.py and preview.py: nothing here knows about
pywebview or the API object. ffmpeg's path, progress callbacks and the abort
check all arrive as arguments, so every function can be exercised from a bare
REPL.

Why this is its own module rather than more of mediaops.py: mediaops is
one-file-in, one-file-out, driven by a History selection. This is N sources
times M platforms, driven by its own tab, and the fan-out is the whole feature.
The arithmetic the two genuinely share — how many bits fit in a megabyte, and
how to land a two-pass encode under a limit — lives in mediaops as
budget_bitrate() and two_pass_encode(), which this calls. Nothing is copied.

The unit of work is a *plan row*: plan_one() decides everything about one
(file, platform) pair without encoding a frame, and render() carries that row
out verbatim. That is deliberate, and it is the same relationship plan_fit has
with fit_under — a preview that is computed differently from the encode is a
preview that eventually lies.

Every function returns {'ok', 'path', 'msg'} and never raises for an expected
failure.
"""

import math
import os
from pathlib import Path

import convert
import mediaops
import preview
from utils import app_data_dir

_MB = 1_000_000


# ── The platform table ───────────────────────────────────────────────────────
#
# These numbers move. Discord's free tier went 8 MB then 25 then 10; Shorts went
# 60s then 180. So rather than a header comment claiming the whole table was
# checked on some date — an assertion that silently expires — every entry
# carries its own 'checked' date, which the UI shows on hover, and every size
# cap is editable in the UI. The editable field is the real defence: a user who
# hits a moved limit types the new number and carries on instead of waiting for
# a release.
#
#   w/h        the frame we aim at. Never upscaled past what the source can
#              fill — see frame_for().
#   aspect     a display label only. Asserted against w/h at import, because
#              two sources of truth for one number is how "1080x1350, 9:16"
#              ships.
#   fps        a cap, applied only when the source is faster (see build_filter).
#   max_secs   0 means the platform imposes nothing worth enforcing.
#   max_mb     None means no cap a user needs to think about. A cap here does
#              not mean a two-pass encode — see plan_one, which decides that
#              per file.
#   maxrate    the quality ceiling for the CRF path, in bits/s.
PLATFORMS = {
    "ig-reel": dict(
        label="Instagram Reel", group="Instagram", w=1080, h=1920,
        aspect="9:16", fps=30, max_secs=90, max_mb=None, maxrate=10_000_000,
        abps=128_000, container="mp4", checked="2026-08"),
    "ig-square": dict(
        label="Instagram Feed (square)", group="Instagram", w=1080, h=1080,
        aspect="1:1", fps=30, max_secs=60, max_mb=None, maxrate=8_000_000,
        abps=128_000, container="mp4", checked="2026-08"),
    "ig-portrait": dict(
        label="Instagram Feed (4:5)", group="Instagram", w=1080, h=1350,
        aspect="4:5", fps=30, max_secs=60, max_mb=None, maxrate=8_000_000,
        abps=128_000, container="mp4", checked="2026-08"),
    "ig-story": dict(
        label="Instagram Story", group="Instagram", w=1080, h=1920,
        aspect="9:16", fps=30, max_secs=60, max_mb=None, maxrate=10_000_000,
        abps=128_000, container="mp4", checked="2026-08"),

    "tiktok": dict(
        label="TikTok", group="TikTok", w=1080, h=1920,
        aspect="9:16", fps=60, max_secs=600, max_mb=287.6, maxrate=10_000_000,
        abps=128_000, container="mp4", checked="2026-08"),

    "yt-shorts": dict(
        label="YouTube Short", group="YouTube", w=1080, h=1920,
        aspect="9:16", fps=60, max_secs=180, max_mb=None, maxrate=12_000_000,
        abps=192_000, container="mp4", checked="2026-08"),
    "yt-1080p": dict(
        label="YouTube 1080p", group="YouTube", w=1920, h=1080,
        aspect="16:9", fps=60, max_secs=0, max_mb=None, maxrate=12_000_000,
        abps=192_000, container="mp4", checked="2026-08"),

    "fb-feed": dict(
        label="Facebook Feed", group="Facebook", w=1920, h=1080,
        aspect="16:9", fps=30, max_secs=0, max_mb=None, maxrate=8_000_000,
        abps=128_000, container="mp4", checked="2026-08"),
    "fb-reel": dict(
        label="Facebook Reel", group="Facebook", w=1080, h=1920,
        aspect="9:16", fps=30, max_secs=90, max_mb=None, maxrate=10_000_000,
        abps=128_000, container="mp4", checked="2026-08"),

    "x": dict(
        label="X / Twitter", group="X", w=1280, h=720,
        aspect="16:9", fps=30, max_secs=140, max_mb=512, maxrate=5_000_000,
        abps=128_000, container="mp4", checked="2026-08"),

    "discord-free": dict(
        label="Discord (free)", group="Discord", w=1280, h=720,
        aspect="16:9", fps=30, max_secs=0, max_mb=10, maxrate=4_000_000,
        abps=128_000, container="mp4", checked="2026-08"),
    "discord-basic": dict(
        label="Discord Nitro Basic", group="Discord", w=1920, h=1080,
        aspect="16:9", fps=30, max_secs=0, max_mb=50, maxrate=6_000_000,
        abps=128_000, container="mp4", checked="2026-08"),
    "discord-nitro": dict(
        label="Discord Nitro", group="Discord", w=1920, h=1080,
        aspect="16:9", fps=60, max_secs=0, max_mb=500, maxrate=10_000_000,
        abps=192_000, container="mp4", checked="2026-08"),

    "whatsapp": dict(
        label="WhatsApp", group="Other", w=1280, h=720,
        aspect="16:9", fps=30, max_secs=0, max_mb=16, maxrate=3_000_000,
        abps=128_000, container="mp4", checked="2026-08"),
    "snapchat": dict(
        label="Snapchat Spotlight", group="Other", w=1080, h=1920,
        aspect="9:16", fps=30, max_secs=60, max_mb=None, maxrate=8_000_000,
        abps=128_000, container="mp4", checked="2026-08"),
    "reddit": dict(
        label="Reddit", group="Other", w=1920, h=1080,
        aspect="16:9", fps=30, max_secs=900, max_mb=1000, maxrate=8_000_000,
        abps=128_000, container="mp4", checked="2026-08"),
    "linkedin": dict(
        label="LinkedIn", group="Other", w=1920, h=1080,
        aspect="16:9", fps=30, max_secs=600, max_mb=5000, maxrate=8_000_000,
        abps=128_000, container="mp4", checked="2026-08"),
    "telegram": dict(
        label="Telegram", group="Other", w=1920, h=1080,
        aspect="16:9", fps=30, max_secs=0, max_mb=2000, maxrate=8_000_000,
        abps=128_000, container="mp4", checked="2026-08"),
    "bluesky": dict(
        label="Bluesky", group="Other", w=1920, h=1080,
        aspect="16:9", fps=30, max_secs=60, max_mb=50, maxrate=6_000_000,
        abps=128_000, container="mp4", checked="2026-08"),
}


def _aspect_value(label: str) -> float:
    a, b = str(label).split(":")
    return float(a) / float(b)


def _check_table():
    """
    Catch a mistyped preset at import rather than three encodes in.

    Odd dimensions are the one that matters: libx264 rejects them outright
    ("Task finished with error code: -22") and the failure surfaces as an
    unexplained dead unit halfway through somebody's batch.
    """
    for key, p in PLATFORMS.items():
        if p["w"] % 2 or p["h"] % 2:
            raise ValueError(f"social: {key} has odd dimensions "
                             f"{p['w']}x{p['h']}; libx264 will refuse it")
        want = _aspect_value(p["aspect"])
        got = p["w"] / p["h"]
        if abs(got - want) / want > 0.01:
            raise ValueError(f"social: {key} says {p['aspect']} but "
                             f"{p['w']}x{p['h']} is {got:.3f}")


_check_table()


REFRAME_MODES = ("blur", "crop", "pad", "none")
DEFAULT_REFRAME = "blur"

REFRAME_LABELS = {
    "blur": "Blurred bars",
    "crop": "Crop to fill",
    "pad": "Letterbox",
    "none": "Keep as-is",
}

# Background blur strength, applied at 1/8 scale (see build_filter).
_BLUR_SIGMA = 6

# Quality target for the no-size-cap path. 21 is a notch better than x264's
# default 23 because these files get re-encoded *again* by the platform, and
# generation loss compounds.
_CRF = "21"
_PRESET = "medium"

# Bits per pixel per frame, for guessing what a CRF encode will cost before
# running it. Measured around 0.05-0.08 for x264 at CRF 21 on ordinary footage;
# the figure only feeds the "roughly N MB" preview, never an encoder argument.
_BPP = 0.06


def presets() -> list:
    """
    The table as a JSON-safe ordered list, for the UI.

    Same reason loudness_presets() exists: so no platform's dimensions or size
    limit are ever written down in JavaScript, where they would silently
    disagree with the ones the encoder actually uses.
    """
    return [dict(key=k, **v) for k, v in PLATFORMS.items()]


def preset(key: str):
    """A defensive copy, or None for an unknown key."""
    p = PLATFORMS.get(str(key or ""))
    return dict(p) if p else None


def reframe_modes() -> list:
    return [{"key": m, "label": REFRAME_LABELS[m]} for m in REFRAME_MODES]


# ── Framing ──────────────────────────────────────────────────────────────────

def _even(n) -> int:
    """Round down to an even number. x264 requires both axes even."""
    return max(2, int(n) // 2 * 2)


def frame_for(src_w: int, src_h: int, out_w: int, out_h: int,
              mode: str) -> tuple:
    """
    The frame we will actually write: the platform's aspect, but never bigger
    than the source can genuinely fill.

    A 480p clip does not become sharper by being written at 1080x1920; it
    becomes four times the bitrate for the same detail, and on a size-capped
    preset those bits come straight out of the quality. Same principle as
    fit_under's scale filter, which uses min() so it can only ever downscale.

    The platform dimensions are a ceiling, not a requirement — every service
    on the list accepts smaller and scales it up on the viewer's device.
    """
    if not src_w or not src_h:
        return out_w, out_h
    ar = out_w / out_h

    if mode == "none":
        # Keep the source's own shape, shrunk to fit inside the target box.
        r = min(out_w / src_w, out_h / src_h, 1.0)
        return _even(src_w * r), _even(src_h * r)

    if mode == "crop":
        # What a centre crop at the target aspect natively measures.
        if src_w / src_h > ar:
            nat_w, nat_h = src_h * ar, src_h
        else:
            nat_w, nat_h = src_w, src_w / ar
    else:
        # pad / blur: the whole source sits inside the frame, so the frame can
        # shrink until the source fits it at 1:1.
        r = min(out_w / src_w, out_h / src_h)
        nat_w, nat_h = (out_w / r, out_h / r) if r > 1 else (out_w, out_h)

    return _even(min(out_w, nat_w)), _even(min(out_h, nat_h))


def build_filter(src_w: int, src_h: int, src_fps: float,
                 out_w: int, out_h: int, fps_cap: int,
                 mode: str = DEFAULT_REFRAME,
                 pad_color: str = "black") -> tuple:
    """
    ('vf' | 'filter_complex', graph) for one output. Pure string building.

    Three things are load-bearing and none of them are obvious:

    * Every chain ends `setsar=1`. scale() emits a non-unity sample aspect
      ratio when it changes the shape of the frame — measured SAR 18221:18225
      on a 1918x1080 -> 1080x608 scale — which makes a file whose pixels are
      1080x1920 still *display* as 16:9. There is no ffprobe in this app, so
      nothing else would catch it before someone posted a squashed Reel.

    * `force_divisible_by=2` on every `decrease` scale. Without it the fitted
      size can land odd and libx264 refuses the encode outright. It is not on
      the `increase` legs on purpose: it rounds down, which would leave the
      image a pixel short of the crop it is about to feed.

    * `fps=N` is a *floor* as well as a ceiling — it duplicates frames upward,
      so a 24 fps source through `fps=30` really does emit 30 fps of
      duplicates (measured: 60 frames out of a 2-second 24 fps clip). The cap
      is therefore applied here, in Python, only when the source is faster.
    """
    mode = mode if mode in REFRAME_MODES else DEFAULT_REFRAME
    fps_el = ""
    if fps_cap and src_fps and src_fps > fps_cap + 0.01:
        fps_el = f"fps={int(fps_cap)},"

    # When the source already has the target shape, crop, pad and blur all
    # reduce to the same plain fit — no bars would be drawn and no pixels
    # cropped. Taking that path avoids building a filter_complex to overlay a
    # background that is completely hidden.
    same_aspect = bool(src_w and src_h and
                       abs(src_w / src_h - out_w / out_h) < 0.01)

    if mode == "none" or same_aspect:
        if (src_w, src_h) == (out_w, out_h):
            # Nothing to resize. setsar is metadata, not a resample, so it
            # still costs nothing and still guarantees square pixels.
            return "vf", f"{fps_el}setsar=1"
        # An exact scale, deliberately not force_original_aspect_ratio: the
        # frame has already been fitted by frame_for(), and asking ffmpeg to
        # fit it a second time makes it re-derive the ratio from the *rounded*
        # box and come back two pixels short on one axis. Measured: a planned
        # 1080x606 arriving as 1078x606.
        return "vf", f"{fps_el}scale={out_w}:{out_h},setsar=1"

    if mode == "crop":
        return "vf", (f"{fps_el}scale={out_w}:{out_h}"
                      f":force_original_aspect_ratio=increase,"
                      f"crop={out_w}:{out_h},setsar=1")

    if mode == "pad":
        return "vf", (f"{fps_el}scale={out_w}:{out_h}"
                      f":force_original_aspect_ratio=decrease"
                      f":force_divisible_by=2,"
                      f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:"
                      f"color={pad_color},setsar=1")

    # blur — the background is blurred at 1/8 scale and then enlarged, rather
    # than blurred at full resolution. Measured 1.50s against 2.57s of filter
    # time for 10s of 1080p, and the falloff comes out smoother because the
    # upscale does some of the blurring for free.
    bw, bh = max(16, _even(out_w // 8)), max(16, _even(out_h // 8))
    return "filter_complex", (
        f"[0:v]{fps_el}split=2[bg][fg];"
        f"[bg]scale={bw}:{bh}:force_original_aspect_ratio=increase,"
        f"crop={bw}:{bh},gblur=sigma={_BLUR_SIGMA},scale={out_w}:{out_h}[b];"
        f"[fg]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease"
        f":force_divisible_by=2[f];"
        f"[b][f]overlay=(W-w)/2:(H-h)/2,setsar=1[v]")


# ── Argv ─────────────────────────────────────────────────────────────────────

_ARGV_HEAD = ["-hide_banner", "-nostdin", "-loglevel", "warning",
              "-progress", "pipe:1", "-nostats", "-y"]


def _map_args(kind: str, audio: bool) -> list:
    """
    The stream maps, which differ between the two filter forms.

    With -filter_complex the video comes out of the graph as [v], and mapping
    0:v:0 as well would mux the untouched source video in as a second stream —
    a file that plays correctly in VLC and wrongly everywhere else.

    The '?' on the audio map is not optional: silent screen recordings are
    ordinary input here, and without it every one of them fails to map.
    """
    maps = ['-map', '[v]'] if kind == "filter_complex" else ["-map", "0:v:0"]
    return maps + (["-map", "0:a:0?"] if audio else ["-an"])


def build_social_cmd(ffmpeg_exe: str, src, tmp_dst, row: dict, info: dict,
                     video_bps: int = 0, pass_no: int = 0,
                     passlog=None) -> list:
    """
    One ffmpeg argv for one output.

    video_bps > 0 selects the fixed-bitrate two-pass path; 0 is single-pass
    CRF. pass_no 1 is the analysis leg (no audio, no output file), 2 is the
    encode leg of a two-pass run, 0 is a single pass.
    """
    cmd = [ffmpeg_exe] + _ARGV_HEAD + ["-i", str(src)]

    # An output-side -t, so it means "this many seconds from the start" — the
    # same reading build_clip_cmd settled on. run_ffmpeg must be handed this
    # duration too, or the bar stops at the trimmed fraction of the source.
    if row.get("trimmed") and row.get("out_secs"):
        cmd += ["-t", f"{float(row['out_secs']):.3f}"]

    kind, graph = row.get("filter_kind", ""), row.get("filter_graph", "")
    want_audio = bool(info.get("acodec")) and pass_no != 1
    if graph:
        cmd += (["-filter_complex", graph] if kind == "filter_complex"
                else ["-vf", graph])
    cmd += _map_args(kind, want_audio)

    cmd += ["-c:v", "libx264", "-preset", _PRESET, "-pix_fmt", "yuv420p",
            "-profile:v", "high", "-level", "4.0"]

    # A keyframe every two seconds. Every one of these platforms re-segments
    # the upload for streaming, and a file with sparse keyframes gets either a
    # coarser re-encode or a worse scrub experience.
    fps = int(row.get("fps") or 30)
    cmd += ["-g", str(fps * 2), "-keyint_min", str(fps), "-sc_threshold", "0"]

    if video_bps:
        cmd += ["-b:v", str(int(video_bps))]
        if passlog is not None:
            cmd += ["-passlogfile", str(passlog)]
        if pass_no:
            cmd += ["-pass", str(pass_no)]
    else:
        maxrate = int(row.get("maxrate") or 8_000_000)
        cmd += ["-crf", _CRF, "-maxrate", str(maxrate),
                "-bufsize", str(maxrate * 2)]

    if pass_no == 1:
        # Analysis only. -f null rather than a scratch file so there is
        # nothing to clean up if we are killed mid-run.
        return cmd + ["-f", "null", "-"]

    if want_audio:
        af = row.get("af") or ""
        if af:
            cmd += ["-af", af]
        cmd += ["-c:a", "aac", "-b:a", str(int(row.get("audio_bps") or 128_000))]
        # Only downmix what is actually multichannel. Forcing stereo on a mono
        # screen recording doubles its audio for nothing.
        if int(info.get("channels") or 0) > 2:
            cmd += ["-ac", "2"]

    cmd += ["-map_metadata", "-1" if row.get("strip_meta") else "0"]
    if str(row.get("container", "mp4")) in ("mp4", "mov"):
        cmd += ["-movflags", "+faststart"]
    return cmd + [str(tmp_dst)]


def build_cover_cmd(ffmpeg_exe: str, src, dst, row: dict, at_secs: float) -> list:
    """
    A still at the platform's own framing, for use as the thumbnail.

    -ss before -i so it seeks rather than decoding up to the timestamp, and a
    timestamp that is never 0 — the first frame of a video is very often black
    or a fade-in, which makes for a cover nobody would click.
    """
    kind, graph = row.get("filter_kind", ""), row.get("filter_graph", "")
    cmd = [ffmpeg_exe] + _ARGV_HEAD + ["-ss", f"{max(0.0, at_secs):.3f}",
                                       "-i", str(src)]
    if graph:
        cmd += (["-filter_complex", graph] if kind == "filter_complex"
                else ["-vf", graph])
    cmd += _map_args(kind, False)
    return cmd + ["-frames:v", "1", "-q:v", "2", str(dst)]


# ── Planning ─────────────────────────────────────────────────────────────────

def _fmt_dims(w, h) -> str:
    return f"{int(w)}×{int(h)}"


def plan_one(ffmpeg_exe: str, src, key: str, opts: dict = None,
             info: dict = None) -> dict:
    """
    Everything about one (file, platform) pair, decided without encoding.

    The returned dict *is* the encode instruction: render() reads it and adds
    nothing of its own. A preview computed by different code from the encode
    is a preview that eventually disagrees with the file it described.

    `info` accepts a cached probe_full so planning one file against six
    platforms costs one subprocess rather than six.
    """
    opts = dict(opts or {})
    out = {"ok": False, "key": key, "label": "", "src": str(src),
           "msg": "", "warn": "", "out_w": 0, "out_h": 0, "fps": 30,
           "out_secs": 0.0, "src_secs": 0.0, "trimmed": False,
           "two_pass": False, "video_bps": 0, "audio_bps": 0,
           "target_mb": 0.0, "est_mb": 0.0, "maxrate": 0, "container": "mp4",
           "filter_kind": "", "filter_graph": "", "af": "",
           "strip_meta": bool(opts.get("strip_meta")),
           "mode": opts.get("reframe") or DEFAULT_REFRAME,
           "cover": bool(opts.get("cover")), "info": {}}

    p = PLATFORMS.get(str(key or ""))
    if not p:
        out["msg"] = "Unknown platform."
        return out
    out["label"] = p["label"]
    out["container"] = p["container"]
    out["maxrate"] = p["maxrate"]

    src = Path(src)
    if not src.is_file():
        out["msg"] = "That file is no longer there."
        return out
    if not ffmpeg_exe:
        out["msg"] = "ffmpeg wasn't found."
        return out

    if info is None:
        info = convert.probe_full(ffmpeg_exe, src)
    out["info"] = info
    if not info.get("vcodec"):
        out["msg"] = "There's no video in that file."
        return out

    duration = float(info.get("duration") or 0.0)
    if duration <= 0:
        out["msg"] = "Couldn't read how long that video is."
        return out
    out["src_secs"] = duration

    # Coded dimensions are not displayed dimensions: a portrait phone clip is
    # stored 1920x1080 with a rotation flag. ffmpeg autorotates before -vf, so
    # the encode is right either way — but planning against the coded size
    # would tell somebody their 9:16 video is about to be letterboxed when it
    # is not.
    src_w, src_h = convert.display_dims(info)
    src_fps = float(info.get("fps") or 0.0)

    mode = out["mode"] if out["mode"] in REFRAME_MODES else DEFAULT_REFRAME
    out["mode"] = mode
    out_w, out_h = frame_for(src_w, src_h, p["w"], p["h"], mode)
    out["out_w"], out["out_h"] = out_w, out_h
    out["fps"] = int(min(src_fps, p["fps"]) or p["fps"])
    out["filter_kind"], out["filter_graph"] = build_filter(
        src_w, src_h, src_fps, out_w, out_h, p["fps"], mode,
        str(opts.get("pad_color") or "black"))

    # ── duration ──
    out_secs = duration
    limit = int(p["max_secs"] or 0)
    over = bool(limit and duration > limit + 0.05)
    if over and opts.get("trim_to_fit"):
        out_secs = float(limit)
        out["trimmed"] = True
    out["out_secs"] = out_secs

    if over:
        if out["trimmed"]:
            out["warn"] = (f"{mediaops.clock_words(duration)} is over "
                           f"{p['label']}'s {mediaops.clock_words(limit)} "
                           f"limit — keeping the first "
                           f"{mediaops.clock_words(limit)}.")
        else:
            out["warn"] = (f"{mediaops.clock_words(duration)} is over "
                           f"{p['label']}'s {mediaops.clock_words(limit)} "
                           f"limit — tick 'Trim to fit' or it may be rejected.")

    # ── audio ──
    audio_bps = int(p["abps"])
    if not info.get("acodec"):
        audio_bps = 0
    out["audio_bps"] = audio_bps
    if opts.get("loudness") and info.get("acodec"):
        # Reuses the shipped filter builder, including its aresample tail —
        # loudnorm resamples to 192 kHz otherwise.
        out["af"] = convert.loudnorm_filter(
            {"loudnorm": str(opts.get("loudness_preset") or "streaming")}, info)

    # ── size ──
    # Whether a cap binds is a property of this file at this length, not of the
    # platform: Instagram's 4 GB never binds, and Discord's 10 MB does not bind
    # on a four-second clip either.
    raw = (opts.get("size_mb") or {}).get(key, p["max_mb"])
    try:
        target_mb = float(raw) if raw not in (None, "") else 0.0
    except (TypeError, ValueError):
        target_mb = float(p["max_mb"] or 0)
    out["target_mb"] = target_mb

    if target_mb > 0:
        b = mediaops.budget_bitrate(target_mb, out_secs, p["container"],
                                    audio_bps or 64_000)
        if not b["ok"]:
            out["msg"] = b["msg"]
            return out
        if b["video_bps"] < p["maxrate"]:
            out["two_pass"] = True
            out["video_bps"] = b["video_bps"]
            out["est_mb"] = target_mb

    if not out["two_pass"]:
        # CRF path, where the size genuinely is not knowable in advance — it
        # depends entirely on how hard the footage is to compress. Two loose
        # upper bounds are available: what the source already spends, scaled by
        # the change in pixel count, and what CRF 21 typically costs per pixel.
        # Take the lower, call it an upper bound in the sentence, and never
        # promise a figure the encoder hasn't earned.
        src_bps = float(info.get("vbitrate") or 0)
        if not src_bps and info.get("bitrate"):
            src_bps = max(0.0, float(info["bitrate"]) - audio_bps)
        bounds = [float(p["maxrate"]), _BPP * out_w * out_h * out["fps"]]
        if src_bps and src_w and src_h:
            bounds.append(src_bps * min(1.0, (out_w * out_h) /
                                        float(src_w * src_h)))
        out["est_mb"] = (min(bounds) + audio_bps) * out_secs / (8 * _MB)

    out["ok"] = True
    src_mb = src.stat().st_size / _MB
    # A size target is a promise the two-pass encode keeps; a CRF estimate is a
    # guess about footage nobody has looked at yet. Wording them the same way
    # would make one of them a lie.
    size = (f"under {mediaops.fmt_mb(target_mb)}" if out["two_pass"]
            else f"roughly {mediaops.fmt_mb(round(out['est_mb'], 1))}")
    out["msg"] = (
        f"{_fmt_dims(src_w, src_h)} · {mediaops.clock_words(duration)} · "
        f"{src_mb:.1f} MB  →  {_fmt_dims(out_w, out_h)} ({p['aspect']}) · "
        f"{size}")
    if not out["two_pass"] and out["est_mb"] >= src_mb:
        out["msg"] += " — this one may not get any smaller"
    return out


def plan_batch(ffmpeg_exe: str, paths: list, keys: list, opts: dict = None,
               probe=None) -> dict:
    """
    plan_one across every (file, platform) pair.

    `probe(path) -> info` is injected so the caller's cache is used and this
    module still knows nothing about the app. Rows come back in the order they
    will be encoded, failures included — the caller decides what to show and
    what to drop.
    """
    rows = []
    for path in paths or []:
        info = None
        if probe:
            try:
                info = probe(path)
            except Exception:
                info = None
        for key in keys or []:
            row = plan_one(ffmpeg_exe, path, key, opts, info)
            # A source that is broken is broken for every platform; probe once,
            # report once, and let the caller skip the rest.
            if info is None and row.get("info"):
                info = row["info"]
            rows.append(row)
    ok = sum(1 for r in rows if r["ok"])
    return {"ok": bool(rows), "rows": rows, "made": ok,
            "msg": f"{ok} of {len(rows)} ready" if rows else "Nothing to plan."}


# ── Rendering ────────────────────────────────────────────────────────────────

def _remap(on_pct, lo: float, hi: float):
    """Squeeze a 0-100 callback into the [lo, hi] slice of the bar."""
    if not on_pct:
        return None
    return lambda p: on_pct(lo + p * (hi - lo) / 100 if p is not None else None)


def out_name(src: Path, row: dict, overridden: bool) -> str:
    """
    'holiday_ig-reel.mp4'.

    The platform key already carries the distinction that matters — nobody
    confuses discord-free with discord-nitro — so the size is *not* in the name
    normally. It goes in only when the user overrode the preset's figure, which
    is the one thing the filename cannot otherwise tell them.
    """
    stem = f"{preview.safe_stem(src.stem)}_{row['key']}"
    if overridden and row.get("target_mb"):
        stem += f"_{mediaops.mb_tag(row['target_mb'])}"
    return f"{stem}.{row.get('container', 'mp4')}"


def render(ffmpeg_exe: str, src, row: dict, out_dir: str = "",
           opts: dict = None, measured: dict = None,
           on_pct=None, should_abort=None, on_proc=None,
           on_stage=None) -> dict:
    """
    One source x one platform -> one file.

    Returns {'ok', 'path', 'msg'} plus 'warn', 'cover' and 'measured' — the
    last so a caller exporting one file to six platforms can measure its
    loudness once and hand the result back in.

    Progress is emitted as an absolute 0-100 for this unit; the caller is
    responsible for placing that inside the batch.
    """
    opts = dict(opts or {})
    src = Path(src)
    out = {"ok": False, "path": "", "msg": "", "warn": row.get("warn", ""),
           "cover": "", "measured": measured}

    if not row.get("ok"):
        out["msg"] = row.get("msg") or "Couldn't plan that one."
        return out
    if not ffmpeg_exe:
        out["msg"] = "ffmpeg wasn't found."
        return out

    folder = Path(out_dir) if out_dir else src.parent
    if opts.get("folder_per_platform"):
        folder = folder / preview.safe_stem(
            PLATFORMS[row["key"]]["group"] or row["key"])
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        out["msg"] = f"Can't write to that folder: {e}"
        return out

    overridden = bool((opts.get("size_mb") or {}).get(row["key"]))
    dst = preview.unique_path(folder / out_name(src, row, overridden))
    tmp = convert.temp_path(dst)

    # ── loudness measurement, if asked for and not already known ──
    lo = 0.0
    if row.get("af") and measured is None:
        lo = 20.0
        if on_stage:
            on_stage("Measuring loudness…")
        preset_name = str(opts.get("loudness_preset") or "streaming")
        measured = convert.measure_loudness(
            ffmpeg_exe, src,
            convert.LOUDNORM_PRESETS.get(preset_name,
                                         convert.LOUDNORM_PRESETS["streaming"]),
            row["out_secs"],
            on_pct=_remap(on_pct, 0, lo), should_abort=should_abort,
            on_proc=on_proc,
            # Measure only what will be shipped. Normalising a 90-second cut
            # against ten minutes of source lands it several LU off.
            duration_limit=row["out_secs"] if row.get("trimmed") else 0.0)
        out["measured"] = measured
        if should_abort and should_abort():
            out["msg"] = "Stopped."
            return out
    elif measured:
        out["measured"] = measured

    # Fold the measurement into the audio filter, the same way normalize does.
    if row.get("af") and measured:
        row = dict(row)
        row["af"] = convert.loudnorm_filter(
            {"loudnorm": str(opts.get("loudness_preset") or "streaming"),
             "loudnormMeasured": measured}, row.get("info") or {})

    hi = 98.0 if row.get("cover") else 100.0
    info = row.get("info") or {}

    if row["two_pass"]:
        tmpdir = app_data_dir() / "tmp"
        try:
            tmpdir.mkdir(parents=True, exist_ok=True)
        except OSError:
            tmpdir = folder
        passlog = tmpdir / (f"social-{os.getpid()}-"
                            f"{abs(hash(str(src) + row['key'])) % 100000}")
        one = build_social_cmd(ffmpeg_exe, src, tmp, row, info,
                               video_bps=row["video_bps"], pass_no=1,
                               passlog=passlog)
        r = mediaops.two_pass_encode(
            one,
            lambda bps: build_social_cmd(ffmpeg_exe, src, tmp, row, info,
                                         video_bps=bps, pass_no=2,
                                         passlog=passlog),
            row["out_secs"], tmp, row["target_mb"] * _MB, row["video_bps"],
            row["audio_bps"], passlog,
            on_pct=_remap(on_pct, lo, hi), should_abort=should_abort,
            on_proc=on_proc, on_stage=on_stage)
        if not r["ok"]:
            out["msg"] = r["msg"]
            return out
        got = r["size"]
    else:
        if on_stage:
            on_stage("Encoding…")
        cmd = build_social_cmd(ffmpeg_exe, src, tmp, row, info)
        rc, tail = convert.run_ffmpeg(
            cmd, row["out_secs"], on_pct=_remap(on_pct, lo, hi),
            should_abort=should_abort, on_proc=on_proc)
        if rc == -1:
            mediaops.discard(tmp)
            out["msg"] = "Stopped."
            return out
        if rc != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            mediaops.discard(tmp)
            out["msg"] = (tail.splitlines()[-1] if tail
                          else "ffmpeg couldn't re-encode that.")
            return out
        got = tmp.stat().st_size

    # Close the window explicitly. two_pass_encode reserves its top quarter for
    # a correction pass that usually does not run, so it commonly finishes
    # reporting 75 — fine when the caller forces 100 afterwards, but inside a
    # batch it would leave every unit's slice visibly short before the next one
    # jumps in.
    if on_pct:
        on_pct(hi)

    try:
        tmp.replace(dst)
    except OSError as e:
        mediaops.discard(tmp)
        out["msg"] = f"Couldn't save it: {e}"
        return out

    # ── cover frame ──
    if row.get("cover"):
        if on_stage:
            on_stage("Cover frame…")
        at = min(1.0, max(0.5, row["out_secs"] * 0.1))
        cov = preview.unique_path(dst.with_name(f"{dst.stem}_cover.jpg"))
        rc, _ = convert.run_ffmpeg(
            build_cover_cmd(ffmpeg_exe, src, cov, row, at), 0,
            should_abort=should_abort, on_proc=on_proc)
        # A missing cover is a disappointment, not a failure — the video that
        # was actually asked for is already on disk.
        if rc == 0 and cov.exists() and cov.stat().st_size:
            out["cover"] = str(cov)
        if on_pct:
            on_pct(100)

    out["ok"] = True
    out["path"] = str(dst)
    out["msg"] = (f"{dst.name} — {_fmt_dims(row['out_w'], row['out_h'])}, "
                  f"{got / _MB:.1f} MB")
    if row["two_pass"] and got > row["target_mb"] * _MB:
        # Say so rather than handing back an oversized file somebody is about
        # to try to upload.
        out["warn"] = (f"{dst.name} came out {got / _MB:.1f} MB, over "
                       f"{mediaops.fmt_mb(row['target_mb'])}. It won't "
                       f"compress that far without going lower.")
    return out
