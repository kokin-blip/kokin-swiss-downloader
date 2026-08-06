"""
A loopback HTTP server that hands local files to the webview.

This exists for one reason, and it is not architectural taste: **the UI cannot
load local files directly.** A <video> pointed at file:///C:/... fails with

    MEDIA_ELEMENT_ERROR: Media load rejected by URL safety check

no matter how the path is escaped. The cause is easy to get wrong, so it is
worth writing down: although app.py passes a filesystem path to
create_window(), pywebview does *not* leave the page on a file:// origin — it
starts its own bottle server and serves ui/ over it, so the document actually
lives at http://127.0.0.1:<pywebview's port>/index.html. Chromium categorically
forbids an http: page from loading file: subresources, so every inline preview
in the History tab would be dead on arrival without this module.

We can't reuse pywebview's server either: it only exposes the ui/ folder, and
the files being previewed are wherever the user downloads things.

Serving over our own http://127.0.0.1 port fixes it completely: the media
loads, and because we answer Range requests the user can scrub the timeline
instead of being limited to playing from the start. That matters — seeking is
what makes "pause on the frame you want and save it" possible at all.

Security. A loopback port is reachable by every other process and every web
page on this machine, and this app has files worth not leaking. Three
independent guards, none of which relies on the others:

  1. **Nothing is addressable by path.** The client never sends a filename; it
     asks for an opaque id that only exists because Python already handed that
     id out for a file it chose. Path traversal is not defended against here,
     it is unrepresentable.
  2. **A per-run secret token** in the URL. 32 hex chars, regenerated every
     launch, so a URL that leaks into someone's notes is worthless tomorrow.
  3. **Host header pinning.** A malicious page can't reach us by DNS rebinding
     (resolving evil.com to 127.0.0.1) because we reject any request whose Host
     isn't the literal loopback address and port we bound.

Bound to 127.0.0.1, never 0.0.0.0, so nothing outside this machine can connect
in the first place.
"""

import mimetypes
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

# Chunk size for streaming. 64 KB keeps memory flat on a 4 GB movie while still
# being large enough that we aren't paying syscall overhead per frame of video.
_CHUNK = 65536

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

# Containers the webview may not recognise from the extension alone. Everything
# else falls through to mimetypes, which is right for mp4/webm/mp3/jpeg/etc.
_EXTRA_TYPES = {
    ".mkv":  "video/x-matroska",
    ".m4v":  "video/mp4",
    ".ts":   "video/mp2t",
    ".flac": "audio/flac",
    ".m4a":  "audio/mp4",
    ".opus": "audio/ogg",
    ".oga":  "audio/ogg",
    ".weba": "audio/webm",
    ".webp": "image/webp",
    ".avif": "image/avif",
}


def guess_type(path) -> str:
    ext = Path(path).suffix.lower()
    if ext in _EXTRA_TYPES:
        return _EXTRA_TYPES[ext]
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


class _Handler(BaseHTTPRequestHandler):
    # Set by MediaServer.start() before the server accepts anything.
    server_version = "SwissDL"
    sys_version = ""

    def log_message(self, *args):
        # The default handler writes to stderr, which is /dev/null in the
        # shipped --windowed build, and every scrub of a video timeline fires
        # several requests. Silence is correct here.
        pass

    # ── request plumbing ────────────────────────────────────────────────────

    def _reject(self, code=404):
        try:
            self.send_error(code)
        except Exception:
            pass

    def _resolve(self):
        """Map the request path to a real file, or None if it isn't ours."""
        srv = self.server.media
        # Host pinning — see the module docstring on DNS rebinding.
        host = (self.headers.get("Host") or "").strip()
        if host != f"127.0.0.1:{srv.port}":
            return None
        parts = self.path.lstrip("/").split("/")
        # /<token>/<id>/<ignored-filename>
        if len(parts) < 2 or not secrets.compare_digest(parts[0], srv.token):
            return None
        path = srv.lookup(parts[1])
        if not path:
            return None
        p = Path(path)
        return p if p.is_file() else None

    def do_HEAD(self):
        self._serve(body=False)

    def do_GET(self):
        self._serve(body=True)

    def _serve(self, body=True):
        target = self._resolve()
        if target is None:
            self._reject(404)
            return
        try:
            size = target.stat().st_size
        except OSError:
            self._reject(404)
            return

        start, end, partial = 0, size - 1, False
        rng = self.headers.get("Range")
        if rng:
            m = _RANGE_RE.match(rng.strip())
            if m and (m.group(1) or m.group(2)):
                partial = True
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = int(m.group(2))
                else:
                    # "bytes=-N" means the LAST n bytes, not "from 0 to N".
                    # Players use this form to read the moov atom at the tail of
                    # a non-faststart MP4; getting it wrong makes those files
                    # look unplayable.
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", guess_type(target))
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        # The page is served on pywebview's port and we are on our own, so
        # every request here is cross-origin however loopback it looks — an
        # origin is scheme+host+PORT. Without this the <video> still plays, but
        # drawing it to a canvas taints the canvas and any pixel read throws.
        # Allowing it keeps a client-side frame grab possible.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        if not body:
            return
        try:
            with target.open("rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(_CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Entirely normal: every seek makes the player abandon the request
            # it had in flight. Not an error, and not worth a log line.
            pass
        except OSError:
            pass


class MediaServer:
    """
    Lazily-started loopback file server.

    One instance lives on the API object. start() is idempotent and cheap to
    call from any request path, so callers never have to reason about whether
    the server is up yet.
    """

    def __init__(self):
        self._srv = None
        self._lock = threading.Lock()
        self._ids: dict[str, str] = {}     # opaque id -> absolute path
        self._by_path: dict[str, str] = {}  # absolute path -> opaque id
        self.token = secrets.token_hex(16)
        self.port = 0

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Bring the server up if it isn't already. False if it can't bind."""
        with self._lock:
            if self._srv is not None:
                return True
            try:
                # Port 0 = let the OS pick a free one. Hardcoding a port would
                # collide with whatever else the user is running.
                srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            except OSError:
                return False
            srv.media = self
            srv.daemon_threads = True
            self.port = srv.server_address[1]
            self._srv = srv
            t = threading.Thread(target=srv.serve_forever, daemon=True,
                                 name="media-server")
            t.start()
            return True

    def stop(self):
        with self._lock:
            if self._srv is not None:
                try:
                    self._srv.shutdown()
                    self._srv.server_close()
                except Exception:
                    pass
                self._srv = None

    # ── registry ────────────────────────────────────────────────────────────

    def lookup(self, ident: str):
        return self._ids.get(ident)

    def url_for(self, path) -> str:
        """
        Register a file and return the URL that serves it. "" if unusable.

        Registration is idempotent per path, so re-rendering the history list
        doesn't grow the table without bound.
        """
        try:
            p = Path(path).resolve()
        except OSError:
            return ""
        if not p.is_file():
            return ""
        if not self.start():
            return ""

        key = str(p)
        with self._lock:
            ident = self._by_path.get(key)
            if ident is None:
                ident = secrets.token_urlsafe(9)
                self._ids[ident] = key
                self._by_path[key] = ident

        # The trailing name is decorative — the id is what resolves the file —
        # but it gives the player a sane extension to sniff and makes the URL
        # readable if it ever shows up in a debug log.
        return (f"http://127.0.0.1:{self.port}/{self.token}/{ident}/"
                f"{quote(p.name)}")
