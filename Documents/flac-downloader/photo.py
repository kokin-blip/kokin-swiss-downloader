"""
Cut the subject out of a photo, and do the things people actually want next.

Same rule as convert.py, mediaops.py, preview.py and social.py: nothing here
knows about pywebview or the API object. Progress callbacks and the abort check
arrive as arguments, so every function can be exercised from a bare REPL.

Why its own module rather than more of convert.py: convert.py's image half is a
transcoder — one format in, another out, no understanding of what is *in* the
picture. This needs a segmentation model, a compositor and a notion of "the
subject", and it is driven by its own tab. The arithmetic the two genuinely
share — flattening alpha onto white, the LANCZOS resize, the Pillow bomb guard —
lives in convert.py and is called from here. Nothing is copied.

The unit of work is a *recipe*: a dict from RECIPES saying what the finished
picture should be, carried out verbatim by render(). One pipeline serves both
the live preview and the saved file, and `proxy` is the only thing that differs
between them. That is the whole reason the preview can be trusted: a preview
computed by a different code path is a preview that eventually lies.

Every public function returns {'ok', 'path', 'msg'} and never raises for an
expected failure.

## On rembg

rembg normally downloads its model through pooch, to $U2NET_HOME (falling back
to $XDG_DATA_HOME/.u2net, else ~/.u2net), the first time a session is built. In
a --windowed frozen exe that is a silent 176 MB download that reads as a hang,
so this module never lets it happen: _seed_model() puts the bundled u2net.onnx
into app_data_dir()/models, points U2NET_HOME at that folder *before* rembg is
imported, and asks for the "u2net_custom" session, which takes an explicit
model_path and skips download and checksum verification entirely.

Every rembg and onnxruntime import is inside a function, never at module scope.
Importing them costs several seconds and loads native DLLs; doing it at import
time would slow the app's startup for everyone, including the people who never
open this tab.
"""

import os
import shutil
import sys
import threading
from pathlib import Path

import convert
import preview
from utils import app_data_dir

# Longest edge of a preview render. The model runs at 320x320 internally
# whatever we hand it, so this barely touches inference cost — it is the mask
# upscale, the composite and the PNG encode that scale with it. 1024 keeps a
# maximised window's stage (max 460px tall) above one image pixel per CSS pixel.
PROXY_EDGE = 1024

# The model file we bundle, and the session type that can be pointed straight at
# it. u2net_custom uses U^2-Net preprocessing, so this pairing is not
# interchangeable with an isnet or BiRefNet .onnx — those need their own
# custom-session classes and their own 1024x1024 preprocessing.
MODEL_FILE = "u2net.onnx"
MODEL_SESSION = "u2net_custom"


# ── The recipe table ─────────────────────────────────────────────────────────
#
# One row is a finished intention, not a pile of switches. That is the whole
# design: a cutout is almost never what somebody wants, it is what they need on
# the way to a listing photo or a profile picture, and a tab full of orthogonal
# toggles makes the user assemble the intention themselves every time.
#
#   cutout    run the segmentation model at all. False is not a degenerate
#             case — "Just enhance" is a real thing to want, and it is the one
#             recipe that works with no model and no numpy.
#   bg        what goes behind the subject.
#               'none'  leave it transparent (forces PNG, see out_format)
#               'color' fill with `color`
#               'blur'  the original photo, blurred — "portrait mode"
#               'image' a file the user picks
#   autocrop  crop to the subject's alpha bounding box, leaving this fraction
#             of the subject's longest edge as margin. None means don't.
#   aspect    'square' | None. Applied after autocrop, before resize.
#   size      longest edge of the finished picture. None means don't resize.
#             Never upscales — see _resize_within.
#   shadow    soft drop shadow under the subject. Only meaningful with a bg.
#   outline   contour width in px at 1000px scale; 0 for none. Sticker only.
#   enhance   'auto' runs autocontrast over the *subject only*; 'none' doesn't.
#   fmt       preferred container. Overridden to png whenever alpha survives,
#             because a JPEG cutout is a bug, not a choice.
RECIPES = {
    "product": dict(
        label="Product shot", group="Sell it",
        hint="Cut out, dropped on pure white, squared up with a little room "
             "to breathe. What a marketplace listing wants.",
        cutout=True, bg="color", color="#ffffff", autocrop=0.08,
        aspect="square", size=1600, shadow=True, outline=0,
        enhance="auto", fmt="jpg"),
    "product_tp": dict(
        label="Product, transparent", group="Sell it",
        hint="The same framing, but transparent — for dropping onto your own "
             "background later.",
        cutout=True, bg="none", color="#ffffff", autocrop=0.04,
        aspect="square", size=1600, shadow=False, outline=0,
        enhance="auto", fmt="png"),

    "profile": dict(
        label="Profile picture", group="Me",
        hint="Square, 1024px, with the room behind you blurred out.",
        cutout=True, bg="blur", color="#ffffff", autocrop=None,
        aspect="square", size=1024, shadow=False, outline=0,
        enhance="none", fmt="jpg"),
    "portrait": dict(
        label="Blur the background", group="Me",
        hint="Keeps the photo exactly as it is and blurs everything that "
             "isn't you. Portrait mode, after the fact.",
        cutout=True, bg="blur", color="#ffffff", autocrop=None,
        aspect=None, size=None, shadow=False, outline=0,
        enhance="none", fmt="jpg"),

    "sticker": dict(
        label="Sticker", group="Fun",
        hint="Tight cutout with a white contour round it, transparent "
             "everywhere else.",
        cutout=True, bg="none", color="#ffffff", autocrop=0.02,
        aspect=None, size=1024, shadow=False, outline=12,
        enhance="none", fmt="png"),
    "cutout": dict(
        label="Just the cutout", group="Fun",
        hint="The subject on transparency. Nothing cropped, nothing resized, "
             "nothing else touched.",
        cutout=True, bg="none", color="#ffffff", autocrop=None,
        aspect=None, size=None, shadow=False, outline=0,
        enhance="none", fmt="png"),

    "replace": dict(
        label="New background", group="Custom",
        hint="Cut out and dropped on a colour you pick, or a picture of your "
             "own.",
        cutout=True, bg="color", color="#008080", autocrop=None,
        aspect=None, size=None, shadow=True, outline=0,
        enhance="none", fmt="jpg"),
    "enhance": dict(
        label="Just enhance", group="Custom",
        hint="No cutting out at all — levels and contrast pulled straight, "
             "and resized if you ask.",
        cutout=False, bg="none", color="#ffffff", autocrop=None,
        aspect=None, size=None, shadow=False, outline=0,
        enhance="auto", fmt="jpg"),
}

# Fields the UI is allowed to override per run. Anything not listed here is a
# property of the recipe, not a knob — which is what keeps the tab from
# degenerating into the pile of switches the presets exist to replace.
OVERRIDABLE = ("bg", "color", "bg_image", "autocrop", "aspect", "size",
               "shadow", "outline", "enhance", "fmt")

BG_MODES = ("none", "color", "blur", "image")
BG_LABELS = {
    "none":  "Transparent",
    "color": "A colour",
    "blur":  "The photo, blurred",
    "image": "A picture of mine",
}

ASPECTS = {
    None:       "Leave the shape alone",
    "square":   "Square (1:1)",
    "portrait": "Portrait (4:5)",
    "wide":     "Wide (16:9)",
}
_ASPECT_RATIO = {"square": 1.0, "portrait": 4 / 5, "wide": 16 / 9}

# Shadow geometry, as a fraction of the canvas's longest edge, so it looks the
# same at 512px and at 4000px instead of vanishing on big pictures.
_SHADOW_BLUR = 0.012
_SHADOW_DROP = 0.008
_SHADOW_ALPHA = 110

# Background blur radius, likewise proportional.
_BG_BLUR = 0.02

# JPEG quality for everything this module writes. Matches convert.py's default
# for jpg so a photo that goes through here and one that goes through the
# Convert tab come out the same.
_JPEG_Q = 90


def _check_table():
    """
    Catch a mistyped recipe at import rather than three renders in.

    The one that actually matters is bg='none' with fmt='jpg': it would not
    raise anywhere, it would silently flatten somebody's cutout onto white and
    look like the model had failed.
    """
    for key, r in RECIPES.items():
        missing = {"label", "group", "hint", "cutout", "bg", "color",
                   "autocrop", "aspect", "size", "shadow", "outline",
                   "enhance", "fmt"} - set(r)
        if missing:
            raise ValueError(f"photo: {key} is missing {sorted(missing)}")
        if r["bg"] not in BG_MODES:
            raise ValueError(f"photo: {key} has bg={r['bg']!r}, "
                             f"expected one of {BG_MODES}")
        # Only a *cutout* with no background carries alpha. "Just enhance"
        # leaves bg='none' because there is nothing to put a background behind,
        # and jpg is right for it — hence the same condition out_format uses,
        # rather than the tempting but wrong `bg == 'none'` on its own.
        if r["cutout"] and r["bg"] == "none" and r["fmt"] != "png":
            raise ValueError(f"photo: {key} keeps transparency but writes "
                             f"{r['fmt']}; that would flatten the cutout")
        if r["aspect"] is not None and r["aspect"] not in _ASPECT_RATIO:
            raise ValueError(f"photo: {key} has aspect={r['aspect']!r}")
        if not r["cutout"] and r["bg"] != "none":
            raise ValueError(f"photo: {key} asks for a background without "
                             f"cutting anything out")
        if r["shadow"] and r["bg"] == "none":
            raise ValueError(f"photo: {key} wants a shadow with nothing to "
                             f"cast it onto")
        if not (0.0 <= float(r["autocrop"] or 0.0) <= 1.0):
            raise ValueError(f"photo: {key} has autocrop={r['autocrop']!r}")
        if not _hex_rgb(r["color"]):
            raise ValueError(f"photo: {key} has an unreadable colour "
                             f"{r['color']!r}")


def recipes() -> list:
    """
    The table as a JSON-safe ordered list, for the UI.

    Same reason social.presets() exists: so no size, margin or colour is ever
    written down in JavaScript, where it would silently disagree with the one
    the renderer actually uses.
    """
    return [dict(key=k, **v) for k, v in RECIPES.items()]


def recipe(key: str):
    """A defensive copy, or None for an unknown key."""
    r = RECIPES.get(str(key or ""))
    return dict(r) if r else None


def bg_modes() -> list:
    return [{"key": m, "label": BG_LABELS[m]} for m in BG_MODES]


def aspects() -> list:
    return [{"key": k or "", "label": v} for k, v in ASPECTS.items()]


def resolve(key: str, opts: dict = None) -> dict:
    """
    A recipe with the user's overrides folded in, ready for render().

    Kept here rather than in backend.py so the rules about which fields are
    overridable, and what a bg change does to the output format, live in one
    place with the table they police.
    """
    r = recipe(key)
    if r is None:
        return {}
    for field in OVERRIDABLE:
        if opts and field in opts and opts[field] is not None:
            r[field] = opts[field]
    r["key"] = key
    # A recipe is not free to be internally inconsistent just because a user
    # moved one control: switching the background off has to take the format
    # with it, or the flatten in _save quietly undoes the cutout.
    r["fmt"] = out_format(r)
    if r["bg"] == "none":
        r["shadow"] = False
    return r


def out_format(r: dict) -> str:
    """
    The format this recipe must be written in.

    Computed, never chosen. Transparency and JPEG are mutually exclusive, and
    of the two it is the transparency that was asked for.
    """
    keeps_alpha = r.get("cutout") and r.get("bg") == "none"
    if keeps_alpha:
        return "png"
    fmt = str(r.get("fmt") or "jpg").lower()
    return fmt if fmt in ("jpg", "png", "webp") else "jpg"


def _hex_rgb(value, fallback=(255, 255, 255)):
    """'#rrggbb' or '#rgb' -> (r, g, b). None for anything unreadable."""
    s = str(value or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return None if value is not None else fallback
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


_check_table()


# ── The model ────────────────────────────────────────────────────────────────

_session_lock = threading.Lock()
_sessions: dict = {}
_seeded = False

# Serialises inference. Not a correctness requirement — an onnxruntime
# InferenceSession is safe to call concurrently — but two threads genuinely want
# a mask at once here: the batch worker and the preview thread when a control
# moves. Two inferences racing for the same cores finish later than the same two
# in sequence, and they make the window stop repainting.
_infer_lock = threading.Lock()

# Masks, keyed on the source's identity and the only two things that change one.
# This is what makes the live stage usable: u2net squashes its input to 320x320
# regardless, so the mask is invariant to colour, crop, aspect, shadow, size and
# enhance. Moving any of those re-composites in Pillow and re-runs no inference
# at all — milliseconds instead of seconds.
_mask_lock = threading.Lock()
_mask_cache: dict = {}
# Deliberately small: a 1 MP mask is ~1 MB, and the access pattern is one photo
# being fiddled with at a time.
_MASK_CACHE_MAX = 8


def _bundled_model() -> Path:
    """
    Where the .onnx sits in the running program.

    sys._MEIPASS when frozen (PyInstaller's --add-data unpacks there), the
    source tree otherwise. Same shape as utils.find_ffmpeg's first step.
    """
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / "assets" / "models" / MODEL_FILE


def model_path() -> Path:
    """The model's permanent home — inside U2NET_HOME, which rembg requires."""
    return app_data_dir() / "models" / MODEL_FILE


def _seed_model() -> tuple:
    """
    Put the bundled model somewhere durable and point rembg at it.

    Returns (path, error). Must run before rembg is imported: rembg reads
    U2NET_HOME at import time to build its default model directory.

    The file is copied out of _MEIPASS rather than used in place because
    _MEIPASS is a temp directory that is deleted when the process exits, and
    because rembg's custom session validates that model_path lives under
    U2NET_HOME.
    """
    global _seeded
    dst = model_path()
    # U2NET_HOME is set every call, not just on the first: it is cheap, and it
    # means an environment where something else has already set it can't send
    # rembg looking somewhere we haven't seeded.
    os.environ["U2NET_HOME"] = str(dst.parent)
    if _seeded and dst.is_file():
        return dst, ""
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return None, f"Can't create the model folder: {e}"

    if dst.is_file() and dst.stat().st_size > 1_000_000:
        _seeded = True
        return dst, ""

    src = _bundled_model()
    if not src.is_file():
        return None, (f"The background-removal model ({MODEL_FILE}) wasn't "
                      f"found in this build. Reinstall Swiss Downloader.")
    try:
        # Copy to a scratch name and rename, so a half-copied 176 MB file can
        # never be mistaken for a good one on the next launch.
        tmp = dst.with_suffix(dst.suffix + ".part")
        shutil.copyfile(src, tmp)
        tmp.replace(dst)
    except OSError as e:
        return None, f"Couldn't unpack the model: {e}"
    _seeded = True
    return dst, ""


def available() -> tuple:
    """
    (ready, reason). Cheap — imports nothing heavy.

    Mirrors convert.pillow_available(), but returns a reason so the UI can say
    *why* the tab is unusable instead of just greying a button out.
    """
    if not convert.pillow_available():
        return False, "Pillow isn't installed, so no image work is possible."
    try:
        import importlib.util
        for mod in ("rembg", "onnxruntime", "numpy"):
            if importlib.util.find_spec(mod) is None:
                return False, (f"{mod} isn't installed, so backgrounds can't "
                               f"be removed. Everything else still works.")
    except Exception as e:
        return False, f"Couldn't check for rembg: {e}"
    if not (_bundled_model().is_file() or model_path().is_file()):
        return False, (f"The background-removal model ({MODEL_FILE}) is "
                       f"missing from this build.")
    return True, ""


def get_session():
    """
    The one cached rembg session. Returns (session, error).

    Building a session loads a 176 MB graph into onnxruntime and takes seconds;
    doing it per image would dominate every batch. The lock guards
    *construction* only — an InferenceSession is safe to call concurrently, it
    is just expensive to make, and the preview thread and a running batch both
    arrive here.
    """
    with _session_lock:
        if MODEL_SESSION in _sessions:
            return _sessions[MODEL_SESSION], ""

        path, err = _seed_model()
        if err:
            return None, err

        # rembg imports pymatting at module scope, which pulls numba and
        # llvmlite whether or not alpha matting is ever switched on. Under a
        # frozen build numba wants a writable cache directory and loads
        # llvmlite's DLL through a path lookup, both of which are exactly the
        # kind of thing that works locally and fails in the exe. Disabling the
        # JIT costs nothing when matting is off, and when it is on it trades
        # some speed for actually running.
        os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
        try:
            # Imported here, never at module scope: this pulls onnxruntime's
            # native DLLs and costs several seconds.
            from rembg import new_session
        except Exception as e:
            return None, f"Couldn't load rembg: {e}"

        try:
            sess = new_session(MODEL_SESSION, model_path=str(path))
        except Exception as first:
            # u2net_custom is the path that cannot reach the network. If it
            # refuses the file anyway, the plain session will find the same
            # pre-seeded model in U2NET_HOME and skip its download too.
            try:
                from rembg import new_session as _ns
                sess = _ns("u2net")
            except Exception:
                return None, (f"Couldn't start the background remover: "
                              f"{first}")
        _sessions[MODEL_SESSION] = sess
        return sess, ""


def _mask_for(src, img, should_abort=None) -> tuple:
    """
    The subject's alpha for this image, as an 'L'. Returns (mask, error).

    only_mask=True rather than a full RGBA cutout on purpose: the mask is the
    expensive part and the only part worth caching, and having it separately is
    what lets every other control re-composite without touching the model.

    There is deliberately no alpha-matting option. rembg offers one, and it was
    tried and removed: remove() tests only_mask *before* alpha_matting and
    returns early, so the two cannot be combined at all — and routing round that
    to measure it properly gave 395s for one photo (pymatting runs pure-Python
    because NUMBA_DISABLE_JIT is set) and produced a *harder* edge than the plain
    mask, 0.79% soft-alpha pixels against 3.05%. Slower and worse is not a
    tradeoff worth a checkbox.
    """
    try:
        st = Path(src).stat()
        key = (str(src), st.st_mtime_ns, st.st_size, img.width, img.height)
    except OSError:
        key = None

    if key is not None:
        with _mask_lock:
            hit = _mask_cache.get(key)
        if hit is not None:
            return hit, ""

    sess, err = get_session()
    if err:
        return None, err
    if should_abort and should_abort():
        return None, "Stopped."

    try:
        from rembg import remove
    except Exception as e:
        return None, f"Couldn't load rembg: {e}"

    # Checked again inside the lock, so a preview that was superseded while it
    # waited for a batch's inference drops instead of running its own.
    with _infer_lock:
        if should_abort and should_abort():
            return None, "Stopped."
        try:
            mask = remove(img.convert("RGB"), session=sess, only_mask=True)
        except Exception as e:
            return None, f"Couldn't remove the background: {e}"

    if mask.size != img.size:
        # Only reachable if a caller skipped _open()'s exif_transpose. Loud,
        # because the symptom otherwise is a sideways mask nobody can explain.
        return None, (f"The mask came back {mask.size} for a {img.size} "
                      f"image — the photo wasn't straightened first.")

    if key is not None:
        with _mask_lock:
            if len(_mask_cache) >= _MASK_CACHE_MAX:
                _mask_cache.clear()
            _mask_cache[key] = mask
    return mask, ""


def warm():
    """
    Build the session ahead of being asked. Returns (ready, reason).

    Called when the tab is first opened, on a throwaway thread, so the model is
    already in memory by the time anybody has picked a file. Without this the
    first cutout of every run stalls for several seconds with nothing to show
    for it.
    """
    ready, why = available()
    if not ready:
        return False, why
    _, err = get_session()
    return (not err), err


# ── Looking at the picture ───────────────────────────────────────────────────

# Below this mean-absolute-deviation across the border samples, the backdrop is
# uniform enough to call plain. ~4% of the range: loose enough to survive JPEG
# noise and a vignette, tight enough that a room behind somebody never passes.
_PLAIN_MAD = 10.0
# All channels above this and the plain backdrop is specifically a white one.
_WHITE_FLOOR = 240


def analyse(src) -> dict:
    """
    What kind of photo is this? Cheap, no model, no numpy.

    Returns {'ok', 'msg', 'width', 'height', 'plain', 'white', 'cut',
    'suggest'} — 'suggest' being a recipe key the UI preselects.

    This is the whole of the tab's cleverness and it is deliberately this
    small. Guessing at the *subject* needs the model we are trying to avoid
    running on every drop; guessing at the *backdrop* needs sixteen pixels.
    """
    out = {"ok": False, "msg": "", "width": 0, "height": 0, "animated": False,
           "plain": False, "white": False, "cut": False, "suggest": ""}
    src = Path(src)
    if not src.is_file():
        out["msg"] = "That file is no longer there."
        return out
    if preview.kind_of(src) != "image":
        out["msg"] = "That isn't an image."
        return out
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 300_000_000
        with Image.open(str(src)) as im:
            out["width"], out["height"] = im.width, im.height
            out["animated"] = getattr(im, "n_frames", 1) > 1
            # draft() makes libjpeg decode at 1/8 scale, which is roughly ten
            # times faster on a 12 MP phone photo and is the reason a drop of
            # forty files doesn't visibly stall. A no-op on every other format,
            # so it costs nothing to always ask.
            try:
                im.draft("RGB", (256, 256))
            except Exception:
                pass
            im.load()
            # An image that already carries a real cutout must not be re-cut:
            # it is slow, and the second pass eats the edge the first one made.
            out["cut"] = _has_alpha(im)
            small = im.convert("RGBA" if out["cut"] else "RGB")
            small.thumbnail((256, 256), Image.LANCZOS)
            rgb = small.convert("RGB")
    except Exception as e:
        out["msg"] = f"Couldn't read that image: {e}"
        return out

    px = rgb.load()
    w, h = rgb.size
    if w < 8 or h < 8:
        out["ok"] = True
        out["suggest"] = "cutout"
        return out

    # The border ring, subsampled, plus the corners — where a seamless backdrop
    # shows and a subject almost never does.
    step = max(1, min(w, h) // 32)
    samples = []
    for x in range(0, w, step):
        samples.append(px[x, 0])
        samples.append(px[x, h - 1])
    for y in range(0, h, step):
        samples.append(px[0, y])
        samples.append(px[w - 1, y])

    med = tuple(sorted(s[c] for s in samples)[len(samples) // 2]
                for c in (0, 1, 2))
    mad = max(sum(abs(s[c] - med[c]) for s in samples) / len(samples)
              for c in (0, 1, 2))

    out["ok"] = True
    # A picture that is already a cutout has no backdrop to have an opinion
    # about — its border samples are transparent, which flattens to a uniform
    # black and would otherwise be reported as a plain backdrop.
    if not out["cut"]:
        out["plain"] = mad < _PLAIN_MAD
        out["white"] = out["plain"] and all(v >= _WHITE_FLOOR for v in med)

    if out["cut"]:
        out["suggest"] = "replace"
        out["msg"] = "Already a cutout — skipping the model."
    elif out["white"]:
        out["suggest"] = "product"
        out["msg"] = "Looks like a plain white backdrop."
    elif out["plain"]:
        out["suggest"] = "product"
        out["msg"] = "Looks like a plain backdrop."
    else:
        out["suggest"] = "portrait"
        out["msg"] = "Looks like an ordinary photo."
    return out


def _has_alpha(im) -> bool:
    """
    True if this image carries transparency worth respecting.

    Mode alone isn't enough: plenty of PNGs are RGBA with a fully opaque alpha
    channel, and treating those as pre-cut would skip the model on a photo that
    very much needs it.
    """
    if im.mode not in ("RGBA", "LA") and not (
            im.mode == "P" and "transparency" in im.info):
        return False
    try:
        alpha = im.convert("RGBA").getchannel("A")
        lo, hi = alpha.getextrema()
        # Some genuinely transparent pixels, and not a uniform sheet of them.
        return lo < 250 and hi > 5
    except Exception:
        return False


# ── The pipeline ─────────────────────────────────────────────────────────────

def _open(src):
    """
    Open an image ready to work on. Returns (image, error).

    Two things happen here and nowhere else, so every path gets them:

    exif_transpose, because a phone photo carries its rotation in a tag rather
    than in its pixels. Without this the cutout is correct but the saved file
    has lost the tag that was orienting it, so the result arrives sideways — a
    silently wrong picture rather than an error.

    Refusing animation, because rembg would quietly operate on frame one and
    hand back a still, which looks like the animation was thrown away for no
    stated reason.
    """
    try:
        from PIL import Image, ImageOps
        Image.MAX_IMAGE_PIXELS = 300_000_000
        img = Image.open(str(src))
        img.load()
    except Exception as e:
        return None, f"Couldn't open that image: {e}"

    if getattr(img, "n_frames", 1) > 1:
        return None, ("That's animated — I can only edit a still. Pull a frame "
                      "out in the Convert tab first.")
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass            # a broken EXIF block is not a reason to refuse the file
    return img, ""


def out_name(src: Path, r: dict) -> str:
    """'shoe_product.jpg'. The recipe key carries the intent; nothing else."""
    stem = f"{preview.safe_stem(src.stem)}_{r.get('key', 'photo')}"
    return f"{stem}.{out_format(r)}"


def render(src, r: dict, out_dir: str = "", proxy: int = 0,
           on_pct=None, on_stage=None, should_abort=None) -> dict:
    """
    One photo x one recipe -> one file. Returns {'ok', 'path', 'msg'}.

    `proxy` is the only difference between the live preview and the saved
    result: non-zero clamps the longest edge to that many pixels and writes
    into the proxy cache instead of out_dir. Everything else — the model, the
    crop, the composite, the enhance — is the same code in the same order, so
    what the stage shows is what the file will be.

    Progress is an absolute 0-100 for this one photo; placing that inside a
    batch is the caller's job.
    """
    out = {"ok": False, "path": "", "msg": "", "width": 0, "height": 0}
    src = Path(src)
    r = dict(r or {})
    if not r:
        out["msg"] = "No recipe to apply."
        return out
    if not src.is_file():
        out["msg"] = "That file is no longer there."
        return out
    if preview.kind_of(src) != "image":
        out["msg"] = "That isn't an image."
        return out
    if not convert.pillow_available():
        out["msg"] = "Pillow isn't installed, so images can't be edited."
        return out

    def stage(msg):
        if on_stage:
            on_stage(msg)

    def pct(p):
        if on_pct:
            on_pct(p)

    def stopped():
        return bool(should_abort and should_abort())

    img, err = _open(src)
    if err:
        out["msg"] = err
        return out

    pre_cut = _has_alpha(img)

    # A proxy is scaled down first so every step after it is cheap. Doing it
    # here rather than at the end is what makes the preview fast, and it is
    # safe because every geometry figure below is derived from the canvas, not
    # written in pixels.
    if proxy:
        img = _resize_within(img, int(proxy))

    pct(2)

    # ── 1. the cutout ──
    # Everything downstream needs to know where the subject is.
    if r.get("cutout") and not pre_cut:
        stage("Finding the subject…")
        mask, err = _mask_for(src, img, should_abort=should_abort)
        if err:
            out["msg"] = err
            return out
        img = img.convert("RGBA")
        img.putalpha(mask)
    elif r.get("cutout"):
        stage("Already a cutout — keeping it…")
        img = img.convert("RGBA")
    else:
        img = img.convert("RGBA")

    if stopped():
        out["msg"] = "Stopped."
        return out
    pct(60)

    # ── 2. crop to the subject ──
    # Before any resize: cropping first is one resample instead of two.
    if r.get("autocrop") is not None and r.get("cutout"):
        img = _autocrop(img, float(r["autocrop"]))

    # ── 3. enhance the subject ──
    # Must precede the composite. autocontrast over a picture that is 60% pure
    # white does nothing useful, so the histogram is masked to the subject.
    if str(r.get("enhance") or "none") == "auto":
        stage("Pulling the levels straight…")
        img = _enhance(img)

    # ── 4. geometry ──
    # Settled before compositing, so the background is generated at final size
    # and never resampled. Resampling a gradient bands; resampling a blur is
    # work thrown away.
    if r.get("aspect"):
        img = _to_aspect(img, str(r["aspect"]))
    if r.get("size") and not proxy:
        img = _resize_within(img, int(r["size"]))
    pct(72)

    # ── 5. the background, the shadow, the subject ──
    if r.get("bg") != "none":
        stage("Putting it on its background…")
        img = _composite(img, r, src)
    pct(88)

    # ── 6. the outline ──
    if int(r.get("outline") or 0) > 0 and r.get("bg") == "none":
        img = _outline(img, int(r["outline"]), _hex_rgb(r.get("color")))

    if stopped():
        out["msg"] = "Stopped."
        return out

    # ── save ──
    fmt = out_format(r)
    if proxy:
        dst = _proxy_path()
    else:
        folder = Path(out_dir) if out_dir else src.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            out["msg"] = f"Can't write to that folder: {e}"
            return out
        dst = preview.unique_path(folder / out_name(src, r))

    stage("Saving…")
    err = _save(img, dst, fmt)
    if err:
        out["msg"] = err
        return out
    pct(100)

    out["ok"] = True
    out["path"] = str(dst)
    out["width"], out["height"] = img.width, img.height
    # A proxy's dimensions are the proxy's, not the finished file's, so it must
    # not be reported in a way that reads as the latter. Nor should its scratch
    # filename ever reach the UI.
    out["msg"] = (f"Preview · {img.width}×{img.height}" if proxy
                  else f"{dst.name} — {img.width}×{img.height}")
    return out


def _save(img, dst: Path, fmt: str) -> str:
    """Write via a scratch sibling and rename. Returns an error, or ''."""
    tmp = convert.temp_path(dst)
    try:
        if fmt == "png":
            img.save(tmp, format="PNG", optimize=True)
        elif fmt == "webp":
            img.save(tmp, format="WEBP", quality=_JPEG_Q, method=4)
        else:
            # convert._flatten is the project's one place that knows how to put
            # transparency onto white without turning it black.
            convert._flatten(img).save(tmp, format="JPEG", quality=_JPEG_Q,
                                       optimize=True)
    except Exception as e:
        _discard(tmp)
        return f"Couldn't write {dst.name}: {e}"
    try:
        tmp.replace(dst)
    except OSError as e:
        _discard(tmp)
        return f"Couldn't save it: {e}"
    return ""


def _discard(p: Path):
    try:
        p.unlink()
    except OSError:
        pass


def _proxy_dir() -> Path:
    d = app_data_dir() / "photoproxy"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _proxy_path() -> Path:
    """
    One fixed file, overwritten every preview.

    Not a per-recipe cache: the point of the stage is the picture you are
    looking at right now, and a folder that accumulated one PNG per slider
    position would be pure litter. The URL gets a cache-buster instead.
    """
    return _proxy_dir() / "preview.png"


# ── Geometry ─────────────────────────────────────────────────────────────────

def _resize_within(img, longest: int):
    """
    Scale so the longest edge is `longest`. Never upscales.

    The never-upscale rule is the same one social.frame_for and
    mediaops._scale_filter already follow: inventing pixels makes a file bigger
    and a picture no better, and a user who asked for 1600 from a 900px source
    wants their 900px source.
    """
    from PIL import Image
    longest = max(1, int(longest))
    cur = max(img.width, img.height)
    if cur <= longest:
        return img
    scale = longest / cur
    return img.resize((max(1, round(img.width * scale)),
                       max(1, round(img.height * scale))), Image.LANCZOS)


def _autocrop(img, margin: float):
    """
    Crop to the subject's alpha bounding box, plus `margin` of breathing room.

    Falls through untouched when there is no alpha to measure or the subject
    fills the frame — a crop that does nothing is better than one that guesses.
    """
    try:
        box = img.getchannel("A").getbbox()
    except Exception:
        return img
    if not box:
        return img
    left, top, right, bottom = box
    pad = int(round(max(right - left, bottom - top) * max(0.0, margin)))
    return img.crop((max(0, left - pad), max(0, top - pad),
                     min(img.width, right + pad),
                     min(img.height, bottom + pad)))


def _to_aspect(img, aspect: str):
    """
    Pad — never crop — out to the target shape, centred, transparently.

    Padding rather than cropping because by this point the subject is the only
    thing left in the frame, and cropping it to make a square is precisely the
    thing nobody wants. The transparent margin is filled in by _composite when
    there is a background; when there isn't, transparent is the right answer.
    """
    from PIL import Image
    want = _ASPECT_RATIO.get(aspect)
    if not want:
        return img
    w, h = img.width, img.height
    if abs((w / h) - want) < 0.005:
        return img
    if (w / h) > want:
        new_w, new_h = w, max(1, round(w / want))
    else:
        new_w, new_h = max(1, round(h * want)), h
    canvas = Image.new("RGBA", (new_w, new_h), (0, 0, 0, 0))
    canvas.paste(img, ((new_w - w) // 2, (new_h - h) // 2))
    return canvas


# ── Pixels ───────────────────────────────────────────────────────────────────

def _enhance(img):
    """
    Auto levels over the subject only.

    ImageOps.autocontrast has no mask argument, so the subject is cut to its
    own bounding box, stretched there, and pasted back through the original
    alpha. Stretching the whole canvas instead would hand autocontrast a
    histogram dominated by transparent black, and the result would be a picture
    with its midtones pushed around for no reason.
    """
    from PIL import Image, ImageOps
    try:
        alpha = img.getchannel("A")
        box = alpha.getbbox()
        if not box:
            return img
        rgb = img.convert("RGB").crop(box)
        # cutoff clips the extreme 0.5% before stretching, so one blown
        # highlight or one black speck can't flatten the whole curve.
        stretched = ImageOps.autocontrast(rgb, cutoff=0.5)
        out = img.copy()
        patch = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]))
        patch.paste(stretched)
        patch.putalpha(alpha.crop(box))
        out.paste(patch, (box[0], box[1]))
        return out
    except Exception:
        # An enhance that fails is a disappointment, not a failed render.
        return img


def _composite(img, r: dict, src: Path):
    """The background, then the shadow, then the subject."""
    from PIL import Image
    w, h = img.width, img.height
    mode = str(r.get("bg") or "color")

    if mode == "blur":
        bg = _blurred_source(src, w, h)
    elif mode == "image" and r.get("bg_image"):
        bg = _picked_image(r["bg_image"], w, h)
    else:
        bg = None
    if bg is None:
        bg = Image.new("RGB", (w, h),
                       _hex_rgb(r.get("color")) or (255, 255, 255))
    bg = bg.convert("RGBA")

    if r.get("shadow"):
        bg = _shadow(bg, img)

    bg.alpha_composite(img)
    return bg


def _blurred_source(src: Path, w: int, h: int):
    """
    The original photo, cropped to fill and blurred. Portrait mode.

    Deliberately the *source* and not the cutout's leftovers: blurring the
    holes the subject was cut out of leaves a smeared silhouette of itself,
    which is the tell that gives away a cheap fake bokeh.
    """
    from PIL import Image, ImageFilter
    # Through _open, so the backdrop gets the same exif_transpose the subject
    # did. Skipping it here would rotate the background out from under a phone
    # photo's subject.
    im, err = _open(src)
    if err:
        return None
    # A source that is already a cutout has no backdrop to blur, and flattening
    # its transparency would blur a sheet of black into place. Returning None
    # lets _composite fall back to the colour, which is the only honest answer.
    if _has_alpha(im):
        return None
    base = im.convert("RGB")
    scale = max(w / base.width, h / base.height)
    base = base.resize((max(1, round(base.width * scale)),
                        max(1, round(base.height * scale))), Image.LANCZOS)
    left, top = (base.width - w) // 2, (base.height - h) // 2
    base = base.crop((left, top, left + w, top + h))
    return base.filter(ImageFilter.GaussianBlur(
        max(1.0, max(w, h) * _BG_BLUR)))


def _picked_image(path, w: int, h: int):
    """A user's own picture, cropped to fill. None if it can't be read."""
    from PIL import Image
    try:
        with Image.open(str(path)) as im:
            im.load()
            base = im.convert("RGB")
    except Exception:
        return None
    scale = max(w / base.width, h / base.height)
    base = base.resize((max(1, round(base.width * scale)),
                        max(1, round(base.height * scale))), Image.LANCZOS)
    left, top = (base.width - w) // 2, (base.height - h) // 2
    return base.crop((left, top, left + w, top + h))


def _shadow(bg, subject):
    """
    A soft shadow under the subject, drawn between background and subject.

    It has to happen here and not after the composite — a shadow painted on top
    of the thing casting it is just a grey smear.
    """
    from PIL import Image, ImageFilter
    w, h = bg.width, bg.height
    try:
        alpha = subject.getchannel("A")
    except Exception:
        return bg
    blur = max(1.0, max(w, h) * _SHADOW_BLUR)
    drop = int(round(max(w, h) * _SHADOW_DROP))

    mask = Image.new("L", (w, h), 0)
    mask.paste(alpha, (0, drop))
    mask = mask.filter(ImageFilter.GaussianBlur(blur))
    mask = mask.point(lambda v: v * _SHADOW_ALPHA // 255)

    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    layer.putalpha(mask)
    return Image.alpha_composite(bg, layer)


def _outline(img, width: int, color):
    """
    A contour round the subject, sticker-style.

    The band is grown from the alpha with MaxFilter, which is a true dilation —
    a blur-and-threshold would round off every corner and lose the shape.
    Width is given at 1000px scale so a sticker looks the same at any size.
    """
    from PIL import Image, ImageFilter
    w, h = img.width, img.height
    px = max(1, int(round(width * max(w, h) / 1000)))
    # MaxFilter needs an odd kernel, and grows the mask by (size-1)/2 per pass.
    kernel = px * 2 + 1
    if kernel > 9:
        # Pillow caps MaxFilter at 9; wider bands come from repeated passes,
        # which dilate further without asking it for a kernel it will refuse.
        passes, kernel = max(1, round(px / 4)), 9
    else:
        passes = 1
    try:
        band = img.getchannel("A")
        for _ in range(passes):
            band = band.filter(ImageFilter.MaxFilter(kernel))
    except Exception:
        return img
    grown = Image.new("RGBA", (w, h), tuple(color or (255, 255, 255)) + (0,))
    grown.putalpha(band)
    return Image.alpha_composite(grown, img)
