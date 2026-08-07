"""
Watch the Windows clipboard for a URL worth offering to download.

GUI-free and dependency-injected, like preview.py and mediaops.py: the callback
is passed in, so the whole thing is exercisable from a bare REPL with
`ClipboardWatcher(print).start()` and no window anywhere.

Why polling GetClipboardSequenceNumber rather than a format listener: the
"proper" route (AddClipboardFormatListener) needs an HWND, a registered window
class, a WNDPROC that crashes the process if it is ever garbage collected, and
its own message pump — inside an app whose main thread is already running
pywebview's WinForms loop. That is ~120 lines whose failure mode is "works in
dev, crashes in the exe". The sequence number is one syscall, is exact, and
lets us open the clipboard only when it actually changed.

Nothing here logs the clipboard's contents. utils.redact_url's docstring
explains why: the debug log is effectively published, and a clipboard is
somebody's passwords and private messages as often as it is a link.
"""

from __future__ import annotations

import re
import sys
import threading
from typing import Callable
from urllib.parse import urlparse

CF_UNICODETEXT = 13
MAX_URL_LEN = 2048
# A URL is never this big. Refuse to page a 50 MB clipboard into the process
# just to discover it is a spreadsheet.
MAX_CLIP_BYTES = 65536
POLL_SECONDS = 0.7


def available() -> bool:
    """Whether clipboard watching can work at all on this platform."""
    return sys.platform == "win32"


def sequence_number() -> int:
    """
    A counter Windows bumps on every clipboard change. 0 means "couldn't ask".

    Comparing this is one syscall and needs no clipboard lock, so an idle app
    costs essentially nothing per tick.
    """
    try:
        import ctypes
        return int(ctypes.windll.user32.GetClipboardSequenceNumber())
    except Exception:
        return 0


def read_text(attempts: int = 6, delay: float = 0.03) -> str:
    """
    The clipboard's text, or '' — never raises.

    Retries because the clipboard is a single global lock: any other app
    polling it (and plenty do) holds it briefly and OpenClipboard fails. Giving
    up silently is fine; the sequence number will still be different next tick,
    so the same content gets another chance.
    """
    if not available():
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        # Every one of these signatures is load-bearing on 64-bit Python.
        # ctypes defaults a return type to c_int, so GetClipboardData's HANDLE
        # comes back truncated to 32 bits and sign-extended — 0x29c97abb750
        # arrives as -1750354096 — and every GlobalSize/GlobalLock on it then
        # fails. The symptom is not an error: read_text simply returns '' for
        # ever, so the watcher looks like it is running and never fires.
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
        kernel32.GlobalSize.restype = ctypes.c_size_t

        opened = False
        for _ in range(max(1, attempts)):
            if user32.OpenClipboard(None):
                opened = True
                break
            threading.Event().wait(delay)
        if not opened:
            return ""

        try:
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return ""
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            if kernel32.GlobalSize(handle) > MAX_CLIP_BYTES:
                return ""
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    except Exception:
        return ""


# No trailing-punctuation trimming and no "find a URL inside this text": see
# the whitespace rule below.
_URL_RE = re.compile(r"^https?://[^\s<>\"'`]+$", re.IGNORECASE)


def url_from_text(text: str) -> str:
    """
    The URL worth offering, or ''. Deliberately strict.

    Each rejection is here for a reason:

    * whitespace anywhere — a copied paragraph that happens to contain a link
      is not a "download this" gesture. Without this the bar would appear
      constantly while somebody writes an email.
    * anything but http(s) — file:, data: and javascript: are never
      downloadable, and echoing a file: URL into a visible bar leaks a local
      path.
    * a host with no dot, or loopback — mediaserver.py hands out
      http://127.0.0.1:<port>/f/<id>?t=<per-run token> URLs for the preview
      player, and the UI is text_select=True. Surfacing one of those in a bar
      is exactly the leak utils.redact_url exists to prevent.
    * length — a copied base64 blob.

    There is no host allowlist. yt-dlp supports ~1800 sites; restricting to a
    list would silently drop the long tail, and the bar costs one glance to
    ignore.
    """
    t = (text or "").strip()
    if not t or len(t) > MAX_URL_LEN:
        return ""
    if any(c.isspace() for c in t):
        return ""
    if not _URL_RE.match(t):
        return ""
    try:
        p = urlparse(t)
    except ValueError:
        return ""
    if p.scheme.lower() not in ("http", "https"):
        return ""
    host = (p.hostname or "").lower()
    if not host or "." not in host:
        return ""
    if host in ("localhost", "::1", "0.0.0.0") or host.startswith("127."):
        return ""
    return t


class ClipboardWatcher:
    """
    Calls `on_url(url)` when a new URL shows up on the clipboard.

    The callback runs on this watcher's own thread, so it must not block for
    long and must not touch a GUI toolkit directly.
    """

    def __init__(self, on_url: Callable[[str], None],
                 interval: float = POLL_SECONDS):
        self._on_url = on_url
        self._interval = max(0.2, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seq = 0
        self._last = ""

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        """
        Begin watching. Idempotent — calling it twice does not double up.

        Priming the sequence number *before* the thread exists is what stops
        whatever happened to be on the clipboard at startup being offered as
        though the user had just copied it.
        """
        if not available():
            return False
        if self.running():
            return True
        self._seq = sequence_number()
        self._last = ""
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="clipwatch")
        self._thread.start()
        return True

    def stop(self, timeout: float = 1.0) -> None:
        """Stop watching. Returns once the thread is gone (or timed out)."""
        self._stop.set()
        t, self._thread = self._thread, None
        if t and t.is_alive():
            t.join(timeout=timeout)

    def _run(self) -> None:
        # wait() is both the sleep and the shutdown signal, so stop() takes
        # effect immediately instead of up to one interval later.
        while not self._stop.wait(self._interval):
            try:
                seq = sequence_number()
                if seq == self._seq:
                    continue          # nothing changed: one syscall, no lock
                self._seq = seq

                url = url_from_text(read_text())
                if not url or url == self._last:
                    continue
                # A single value, not a set of everything ever seen: copying A,
                # then B, then A again is somebody asking for A a second time.
                self._last = url
                self._on_url(url)
            except Exception:
                # One bad tick must never end the watch. There is nothing
                # useful to log here that would not be the clipboard itself.
                pass
