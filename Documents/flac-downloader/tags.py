"""
Reading and writing metadata tags across the formats the app handles.

Pure mutagen — no ffmpeg, no pywebview, no GUI — so it can be driven from a
bare REPL like convert.py and preview.py.

This deliberately does NOT refactor utils.tag_flac_file. That function sits on
the download hot path, takes a Qobuz-shaped dict, and only ever *sets* values;
merging the two would put an editor's semantics on the downloader's critical
path for no benefit.

Clearing is explicit, and that is the important contract here. `values` sets
tags and `clear` removes them:

    write_tags(p, {"artist": "Boards of Canada"}, clear=["comment"])

An empty or missing entry in `values` means "leave this tag exactly as it is".
The only way to remove a tag is to name it in `clear`. Deleting somebody's
metadata is not something a blank text box should be able to do by accident —
this is a music library, and the person editing it may only have wanted to fix
a typo in the title.
"""

import os
import shutil
import time
from pathlib import Path

# The fields the editor offers, in display order. Each maps to a mutagen
# "easy" key, which normalises the same concept across Vorbis comments, ID3
# frames and MP4 atoms — 'artist' is TPE1 in an MP3 and ©ART in an M4A, and
# easy mode hides that.
FIELDS = ("title", "artist", "album", "albumartist", "date", "genre",
          "tracknumber", "discnumber")

# Human labels, so the UI and any error message agree on what to call things.
LABELS = {
    "title": "Title", "artist": "Artist", "album": "Album",
    "albumartist": "Album artist", "date": "Year", "genre": "Genre",
    "tracknumber": "Track", "discnumber": "Disc",
}

# Extensions mutagen can write text tags to. Deliberately narrower than the
# Convert tab's input list: being able to *decode* a format says nothing about
# being able to tag it.
#
# WAV and AIFF are excluded on purpose, and it is not an oversight. Both store
# ID3, but mutagen has no "easy" wrapper for either, so their .tags is a raw
# ID3 object that rejects a plain string with "not a Frame instance" — the
# write fails at save time, after the UI has already said it would work.
# Verified per format rather than assumed: FLAC, MP3, M4A, OGG, Opus, WavPack
# and WMA all round-trip; WAV and AIFF both raise.
TAGGABLE = {".mp3", ".flac", ".m4a", ".m4b", ".mp4", ".ogg", ".oga", ".opus",
            ".wma", ".wv", ".ape"}

# Of those, the ones we can also write embedded cover art to. WMA, WavPack and
# APE take text tags fine, but their picture support is either absent from
# mutagen or so patchy across players that writing one would be a lie.
COVERABLE = {".mp3", ".flac", ".m4a", ".m4b", ".mp4", ".ogg", ".oga", ".opus"}


def can_tag(path) -> bool:
    return Path(path).suffix.lower() in TAGGABLE


def can_set_cover(path) -> bool:
    return Path(path).suffix.lower() in COVERABLE


def read_tags(path) -> dict:
    """
    Current tag values for one file.

    Returns {'ok', 'msg', 'values': {field: str}, 'cover': bool,
    'can_cover': bool, 'format': str}. A file with no tag block at all is a
    success with empty values — that is a brand new file, not an error.
    """
    p = Path(path)
    out = {"ok": False, "msg": "", "values": {f: "" for f in FIELDS},
           "cover": False, "can_cover": can_set_cover(p), "format": ""}
    if not p.is_file():
        out["msg"] = "That file is no longer there."
        return out
    if not can_tag(p):
        out["msg"] = f"{p.suffix.lstrip('.').upper()} files can't be tagged."
        return out

    try:
        import mutagen
    except Exception as e:
        out["msg"] = f"mutagen isn't available: {e}"
        return out

    try:
        audio = mutagen.File(str(p), easy=True)
    except Exception as e:
        out["msg"] = f"Couldn't read that file: {e}"
        return out
    if audio is None:
        out["msg"] = "That file's format wasn't recognised."
        return out

    out["format"] = type(audio).__name__
    tags = audio.tags
    if tags:
        for f in FIELDS:
            try:
                v = tags.get(f)
            except Exception:
                v = None
            if v:
                # Easy tags are always lists; a file can legitimately carry
                # two artists, and joining beats showing only the first.
                out["values"][f] = "; ".join(str(x) for x in v) if isinstance(v, list) else str(v)

    out["cover"] = has_cover(p)
    out["ok"] = True
    return out


def has_cover(path) -> bool:
    """Whether the file carries embedded artwork. False on any doubt."""
    p = Path(path)
    try:
        import mutagen
        raw = mutagen.File(str(p))
        if raw is None:
            return False
        if getattr(raw, "pictures", None):            # FLAC
            return True
        tags = raw.tags
        if tags is None:
            return False
        if hasattr(tags, "getall") and tags.getall("APIC"):    # ID3
            return True
        if "covr" in tags:                                     # MP4
            return True
        if "metadata_block_picture" in tags:                   # Vorbis/Opus
            return True
    except Exception:
        pass
    return False


def write_tags(path, values: dict = None, clear=(), cover_path: str = "",
               remove_cover: bool = False) -> dict:
    """
    Apply tag changes to one file. Returns {'ok', 'msg'}.

    `values`  — {field: text}. Blank or absent means "leave this tag alone".
    `clear`   — field names to delete outright. This is the ONLY way to
                remove a tag; see the module docstring for why.
    `cover_path` — an image file to embed. A path, never bytes: artwork does
                not need to cross the JS bridge, and passing megabytes of
                base64 through it would be slow and pointless.
    `remove_cover` — strip existing artwork.

    The file is copied, the copy is tagged, and only then does it replace the
    original. mutagen rewrites in place, so a failure partway through a direct
    write leaves a damaged file — and this is somebody's music library, where
    the cost of that is not "run it again".
    """
    p = Path(path)
    values = values or {}
    clear = [c for c in (clear or []) if c in FIELDS]

    if not p.is_file():
        return {"ok": False, "msg": "That file is no longer there."}
    if not can_tag(p):
        return {"ok": False,
                "msg": f"{p.suffix.lstrip('.').upper()} files can't be tagged."}
    if (cover_path or remove_cover) and not can_set_cover(p):
        return {"ok": False,
                "msg": f"Cover art can't be written to {p.suffix.lstrip('.').upper()} files."}

    cover_bytes = b""
    if cover_path:
        cp = Path(cover_path)
        if not cp.is_file():
            return {"ok": False, "msg": "That image is no longer there."}
        try:
            cover_bytes = cp.read_bytes()
        except OSError as e:
            return {"ok": False, "msg": f"Couldn't read that image: {e}"}
        if not cover_bytes:
            return {"ok": False, "msg": "That image file is empty."}

    tmp = p.with_name(p.stem + ".swisstag-tmp" + p.suffix)
    try:
        shutil.copy2(str(p), str(tmp))
    except OSError as e:
        return {"ok": False, "msg": f"Couldn't make a working copy: {e}"}

    try:
        err = _apply(tmp, values, clear, cover_bytes, remove_cover)
        if err:
            _discard(tmp)
            return {"ok": False, "msg": err}
    except Exception as e:
        _discard(tmp)
        return {"ok": False, "msg": f"Couldn't write those tags: {e}"}

    err = _replace_with_retry(tmp, p)
    if err:
        _discard(tmp)
        return {"ok": False, "msg": err}

    n_set = len([k for k in values
                 if k in FIELDS and k not in clear and str(values[k]).strip()])
    bits = []
    if n_set:
        bits.append(f"{n_set} tag{'s' if n_set != 1 else ''} updated")
    if clear:
        # Counted separately on purpose: reporting a deletion as "1 tag
        # updated" would be exactly the wrong reassurance.
        bits.append(f"{len(clear)} cleared")
    if cover_bytes:
        bits.append("cover art added")
    if remove_cover:
        bits.append("cover art removed")
    return {"ok": True, "msg": f"Saved — {', '.join(bits) or 'no changes'}."}


def _apply(target: Path, values: dict, clear: list, cover: bytes,
           remove_cover: bool) -> str:
    """Tag `target` in place. Returns '' or an error message."""
    import mutagen
    from mutagen import MutagenError

    audio = mutagen.File(str(target), easy=True)
    if audio is None:
        return "That file's format wasn't recognised."
    if audio.tags is None:
        # A file that has never been tagged has no tag block to write into.
        # MP3 and MP4 need one created first; formats that don't support this
        # raise, and that is a real answer rather than a crash.
        try:
            audio.add_tags()
        except (MutagenError, Exception) as e:
            return f"That file can't hold tags: {e}"

    for field in clear:
        try:
            del audio.tags[field]
        except (KeyError, Exception):
            pass          # already absent is the outcome we wanted anyway

    for field in FIELDS:
        if field in clear:
            continue
        text = str(values.get(field, "") or "").strip()
        if not text:
            continue      # blank means "leave alone" — see the docstring
        try:
            audio.tags[field] = text
        except Exception as e:
            return f"{LABELS.get(field, field)} couldn't be written: {e}"

    try:
        audio.save()
    except Exception as e:
        return f"Couldn't save the tags: {e}"

    if cover or remove_cover:
        return _apply_cover(target, cover, remove_cover)
    return ""


def _apply_cover(target: Path, cover: bytes, remove: bool) -> str:
    """
    Write or strip embedded artwork.

    Cover art is the one thing mutagen's easy layer does not abstract, so this
    branches per container: a FLAC Picture block, an ID3 APIC frame, an MP4
    'covr' atom and a base64 metadata_block_picture in Vorbis comments are
    four genuinely different things.
    """
    import base64
    import mutagen
    from utils import _prepare_cover

    ext = target.suffix.lower()
    data, mime, width, height = (b"", "image/jpeg", 0, 0)
    if cover:
        # Normalises anything Pillow can open into a clean JPEG, and gives us
        # the dimensions the FLAC and Vorbis picture blocks require.
        data, mime, width, height = _prepare_cover(cover, "image/jpeg")

    try:
        if ext == ".flac":
            from mutagen.flac import FLAC, Picture
            audio = FLAC(str(target))
            audio.clear_pictures()
            if data:
                pic = Picture()
                pic.type, pic.mime, pic.data = 3, mime, data
                pic.width, pic.height, pic.depth = width, height, 24
                audio.add_picture(pic)
            audio.save()

        elif ext == ".mp3":
            from mutagen.id3 import ID3, APIC, error as ID3Error
            try:
                tags = ID3(str(target))
            except ID3Error:
                tags = ID3()
            tags.delall("APIC")
            if data:
                tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover",
                              data=data))
            tags.save(str(target))

        elif ext in (".m4a", ".m4b", ".mp4"):
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(str(target))
            if data:
                fmt = (MP4Cover.FORMAT_PNG if mime == "image/png"
                       else MP4Cover.FORMAT_JPEG)
                audio["covr"] = [MP4Cover(data, imageformat=fmt)]
            elif "covr" in audio:
                del audio["covr"]
            audio.save()

        elif ext in (".ogg", ".oga", ".opus"):
            from mutagen.flac import Picture
            audio = mutagen.File(str(target))
            if data:
                pic = Picture()
                pic.type, pic.mime, pic.data = 3, mime, data
                pic.width, pic.height, pic.depth = width, height, 24
                audio["metadata_block_picture"] = [
                    base64.b64encode(pic.write()).decode("ascii")]
            elif "metadata_block_picture" in audio:
                del audio["metadata_block_picture"]
            audio.save()
        else:
            return f"Cover art isn't supported for {ext.lstrip('.').upper()} files."
    except Exception as e:
        return f"Couldn't write the cover art: {e}"
    return ""


def _replace_with_retry(tmp: Path, dst: Path, attempts: int = 5,
                        delay: float = 0.15) -> str:
    """
    Move the tagged copy over the original, waiting out a transient lock.

    The preview player holds the file open through the loopback media server,
    and releasing it is asynchronous — the socket teardown finishes shortly
    after the page drops the <video> src. Retrying briefly turns a race that
    would otherwise surface as "Access is denied" into a non-event.
    """
    for i in range(attempts):
        try:
            os.replace(str(tmp), str(dst))
            return ""
        except PermissionError:
            if i == attempts - 1:
                return ("That file is in use — close it in any other player "
                        "and try again.")
            time.sleep(delay)
        except OSError as e:
            return f"Couldn't save over the original: {e}"
    return ""


def _discard(p: Path):
    try:
        p.unlink()
    except OSError:
        pass
