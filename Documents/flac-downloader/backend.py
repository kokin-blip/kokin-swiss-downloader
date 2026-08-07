"""
Python API exposed to the pywebview JS frontend.
All public methods are callable from JS via window.pywebview.api.<method>().
Progress updates are pushed into a queue and drained by JS polling poll_updates().
"""

import os
import queue
import re
import subprocess
import threading
import traceback
from pathlib import Path
from urllib.parse import urlparse
import sys

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

import settings as cfg
import cookies as site_cookies
import convert
import history
import mediaops
import preview
import tags
from mediaserver import MediaServer
from providers import (OdesliResolver, QobuzAPI, SpotiflacProxy,
                       MusicBrainz, is_drm_error, friendly_dl_error,
                       extract_qobuz_id, fetch_spotify_metadata,
                       lookup_album_cover, fetch_spotify_album_tracks,
                       is_album_or_playlist_url, clean_url)
from utils import (app_data_dir, find_ffmpeg, tag_flac_file, flac_cover_info,
                   debug_log, debug_enabled, set_debug, debug_log_path,
                   redact_url, notify_done)
from version import __version__, GITHUB_OWNER, GITHUB_REPO

DEFAULT_OUT       = str(Path.home() / "Music"  / "Swiss Downloads")
DEFAULT_VIDEO_OUT = str(Path.home() / "Videos" / "Swiss Downloads")

# Resilience options applied to every yt-dlp call.
#
# yt-dlp's *command line* defaults these; its Python API does not, so calling
# YoutubeDL() directly gets no retries at all. That barely shows on a single
# small file, but a sniffed HLS stream is hundreds of fragments over a flaky
# CDN — one blip would abandon an otherwise complete download.
#
# continuedl is already True by default in the downloader; it's stated here so
# that resuming a .part file is an explicit, visible promise rather than an
# inherited default someone could silently change.
def validate_outtmpl(tmpl: str) -> str:
    """
    Check a user-supplied filename template. Returns "" if usable, else why not.

    Three separate hazards, none of which yt-dlp guards for us:
      - a malformed template ("%(title)") makes YoutubeDL raise at download
        time, i.e. after the user has walked away
      - an absolute path or a ".." segment writes outside the chosen output
        folder, which the user has every reason to assume is respected
      - an empty template silently produces files called ".mp4"
    """
    tmpl = (tmpl or "").strip()
    if not tmpl:
        return "Template is empty."
    if os.path.isabs(tmpl) or (len(tmpl) > 1 and tmpl[1] == ":"):
        return "Template must be relative to the output folder."
    parts = tmpl.replace("\\", "/").split("/")
    if ".." in parts:
        return "Template can't contain '..'."
    if tmpl.endswith((".", " ")):
        return "Template can't end with a dot or space."
    if yt_dlp is None:
        return ""
    # yt-dlp validates against the same template it would really use, so check
    # the assembled form rather than the fragment the user typed.
    err = yt_dlp.YoutubeDL({"quiet": True}).validate_outtmpl(tmpl + ".%(ext)s")
    return str(err) if err else ""


def _outtmpl(output_dir, tmpl: str, fallback: str) -> str:
    """Assemble a full output template, falling back if the saved one is bad."""
    if validate_outtmpl(tmpl):
        tmpl = fallback
    return str(Path(output_dir) / f"{tmpl}.%(ext)s")


# Categories offered in Settings. Deliberately a subset of yt-dlp's list: the
# rest ('music_offtopic', 'poi_highlight', …) are confusing or actively wrong
# to strip from the music this app mostly downloads.
_SPONSORBLOCK_CATS = ("sponsor", "selfpromo", "interaction",
                      "intro", "outro", "preview", "filler")


def _sub_langs(s) -> list:
    """Parse the comma-separated subtitle-language setting."""
    raw = str(s.get("sub_langs", "en") or "en")
    langs = [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
    return langs or ["en"]


def _tuning_opts(s) -> dict:
    """Throughput knobs that used to be hardcoded (or absent)."""
    opts = {}
    try:
        frags = int(s.get("concurrent_fragments", 5) or 5)
    except (TypeError, ValueError):
        frags = 5
    # Sniffed HLS streams are hundreds of small fragments and the impersonated
    # transport roughly halves per-connection throughput, so some parallelism
    # matters; too much is how you get rate-limited.
    opts["concurrent_fragment_downloads"] = max(1, min(frags, 16))
    try:
        kbps = int(s.get("rate_limit_kbps", 0) or 0)
    except (TypeError, ValueError):
        kbps = 0
    if kbps > 0:
        opts["ratelimit"] = kbps * 1024
    return opts


def _sponsorblock_pps(s) -> list:
    """
    SponsorBlock + chapter postprocessors, in the order yt-dlp expects.

    SponsorBlock only annotates; ModifyChapters is what actually cuts the
    segments out, so enabling the first without the second would download the
    sponsor and merely label it. They are always added as a pair.
    """
    pps = []
    if not s.get("sponsorblock"):
        return pps
    cats = [c for c in (s.get("sponsorblock_cats") or [])
            if c in _SPONSORBLOCK_CATS]
    if not cats:
        return pps
    # 'after_filter' matches yt-dlp's own CLI wiring: the API is queried after
    # match-filtering, so skipped videos don't cost a needless request.
    pps.append({"key": "SponsorBlock", "categories": cats,
                "when": "after_filter"})
    pps.append({"key": "ModifyChapters", "remove_sponsor_segments": cats})
    return pps


def _backoff(attempt):
    """1, 2, 4, 8, 8… seconds. Retrying a rate-limited CDN instantly is how a
    transient block becomes a lasting one."""
    return min(8, 2 ** max(0, attempt - 1))


_RETRY_OPTS = {
    "continuedl":                 True,
    "retries":                    5,
    "fragment_retries":           10,
    # NB: `retry_sleep` (the CLI's "exp=1:8" string form) is parsed by the
    # option parser and never read by the library — only this key is.
    "retry_sleep_functions":      {"http": _backoff, "fragment": _backoff},
    # A handful of dead fragments shouldn't bin a two-hour video; yt-dlp warns
    # about each one, so the gap is visible in the log rather than silent.
    "skip_unavailable_fragments": True,
}


# User-Agent presented by the headless browser during a grab. The subsequent
# yt-dlp fetch of the sniffed stream must send the same one — CDNs routinely
# reject a playlist/segment request whose UA doesn't match the session that
# asked for it (typically with 403 or 410).
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/124.0.0.0 Safari/537.36")

# Why impersonation is unavailable, if it is. Set by _impersonate_target() so
# callers can tell the user "no impersonation" apart from "it didn't help".
_impersonate_note = None


def _impersonate_target():
    """
    Return a yt-dlp ImpersonateTarget (Chrome) if curl_cffi is available.
    Many sites (PornHub, etc.) block yt-dlp by its TLS/JA3 fingerprint and
    return HTTP 403/410; impersonating a real browser's handshake bypasses that.
    Returns None if curl_cffi isn't installed (e.g. running from source),
    recording the reason in _impersonate_note.
    """
    global _impersonate_note
    try:
        import curl_cffi  # noqa: F401
    except Exception as e:
        _impersonate_note = (f"curl_cffi unavailable ({e}) — using yt-dlp's own TLS "
                             "fingerprint, which some sites block.")
        return None
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        target = ImpersonateTarget.from_str("chrome")
    except Exception as e:
        _impersonate_note = f"browser impersonation unavailable ({e})."
        return None
    _impersonate_note = None
    return target


_PLAY_SELECTORS = ("video", ".vjs-big-play-button", "button[aria-label*=play i]",
                   ".play", ".play-button", "#player", "#root", "body")


def _registrable(host):
    """eTLD+1 ('cdn.vod.example.com' -> 'example.com'). Shared with the cookie
    jars, which need the same notion of 'same site' but with real consequences
    if it's wrong."""
    return site_cookies.site_key(host)


def _best_stream(found, page_url):
    """
    Pick the most likely real stream from every .m3u8 seen on the network.

    A player page also loads ad manifests, and those frequently ARE master
    playlists — so preferring 'master' on its own can hand back a 30s preroll
    that then downloads under the page's title and reports success. Ranking by
    domain first fixes that: an ad from a third-party network loses to anything
    served by the site's own operator.

    This ranks rather than filters, because a legitimate stream is normally on
    a different *host* than the page (site.com -> cdn123.vodhost.net); dropping
    cross-domain manifests outright would break ordinary downloads. When every
    candidate scores the same we keep the original sniff order.
    """
    page_dom = _registrable(urlparse(page_url).netloc)

    def score(item):
        url, _ref = item
        same_dom = _registrable(urlparse(url).netloc) == page_dom
        return (2 if same_dom else 0) + (1 if "master" in url.lower() else 0)

    return max(found, key=score)  # max() is stable: ties keep first-seen order


def _browser_login(page_url, log, timeout=600, should_abort=None):
    """
    Open a REAL (visible) browser window so the user can sign in / clear an age
    gate, then keep the cookies that result.

    This is the counterpart to _browser_grab: the sniffer solves "the stream URL
    is hidden", this solves "the site won't serve the stream to us at all".
    Neither the login form nor the CAPTCHA is something we can or should
    automate — the user does it by hand and we simply collect what falls out.

    We finish when the user closes the window, so there is no guessing about
    whether a login "worked": whatever cookies exist at that moment are kept.
    Returns (cookie_count, error_message).
    """
    if getattr(sys, "frozen", False):
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return 0, "Playwright isn't bundled in this build."

    import time as _t
    aborted = should_abort or (lambda: False)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, args=[
                "--disable-blink-features=AutomationControlled",
            ])
            try:
                ctx = browser.new_context(
                    user_agent=_BROWSER_UA,
                    viewport={"width": 1280, "height": 800},
                )
                page = ctx.new_page()
                try:
                    page.goto(page_url, wait_until="domcontentloaded",
                              timeout=45_000)
                except Exception as e:
                    # Not fatal: the user can still navigate manually, and a
                    # slow-loading login page is exactly the normal case here.
                    log(f"(page load warning: {str(e).splitlines()[0][:90]})", "warn")

                log("Sign in, then CLOSE the browser window to save your session.",
                    "bright")
                # Snapshot as we go. Closing the window is the signal that the
                # user is done, but it also destroys the context — so reading
                # cookies only at the end would read them only after they are
                # already gone. Keep the most recent successful read instead.
                jar = []
                deadline = _t.time() + timeout
                while _t.time() < deadline and not aborted():
                    try:
                        current = ctx.cookies()
                    except Exception:
                        break          # context died => the user closed it
                    if current:
                        jar = current
                    _t.sleep(1.0)
                    if not browser.is_connected():
                        break
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        return 0, str(e).splitlines()[0][:160]

    if not jar:
        return 0, ("No cookies were set. If you closed the window before the "
                   "site finished signing you in, try again.")
    count, _path = site_cookies.save(page_url, jar)
    return count, ""


def _browser_grab(page_url, log, timeout=60, attempts=3, should_abort=None):
    """
    Load page_url in a real headless browser, let the site's own JS decrypt
    and request the stream, and return (m3u8_url, referer, page_title) sniffed
    off the network. Used as a fallback for sites whose encrypted/obfuscated
    players yt-dlp can't extract. Returns (None, None, None) on failure.

    page_title is captured because a bare m3u8 carries no metadata — without it
    the download lands as "NA - <playlist-id>".

    Lazy players are the main source of flakiness here, so we keep poking every
    plausible play target for a while instead of clicking once on whichever
    selector happens to match first (often `body`, which does nothing), and we
    retry the whole session before giving up.

    The Chromium binary is bundled into the frozen exe via PyInstaller; setting
    PLAYWRIGHT_BROWSERS_PATH=0 makes Playwright look for it inside its own
    (unpacked) package directory rather than the user's ms-playwright cache.
    """
    if getattr(sys, "frozen", False):
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        log("Browser grab unavailable (Playwright not bundled in this build).", "warn")
        return None, None, None

    import time as _t
    aborted = should_abort or (lambda: False)

    def _attempt(p, found):
        # `found` is owned by the caller so that a crash later in this function
        # (Target crashed, page closed mid-wait — the cases the retry loop
        # exists for) doesn't discard a manifest that was already sniffed.
        title = None
        browser = p.chromium.launch(headless=True, args=[
            "--autoplay-policy=no-user-gesture-required",
            "--disable-blink-features=AutomationControlled",
        ])
        try:
            ctx = browser.new_context(
                user_agent=_BROWSER_UA,
                viewport={"width": 1280, "height": 720},
            )
            page = ctx.new_page()

            def on_request(req):
                if ".m3u8" in req.url.split("?")[0].lower():
                    ref = req.headers.get("referer") or page_url
                    if (req.url, ref) not in found:
                        found.append((req.url, ref))

            page.on("request", on_request)
            # Cap the navigation itself well below the overall budget: when the
            # load fails outright the page stays blank, and waiting out the full
            # timeout clicking nothing just burns the retry budget.
            try:
                page.goto(page_url, wait_until="domcontentloaded",
                          timeout=min(30, timeout) * 1000)
            except Exception as e:
                log(f"Browser grab: page load failed ({str(e).splitlines()[0][:90]})", "warn")
                # Whatever was sniffed during navigation is already in `found`;
                # a page can fire the manifest and still fail to settle.
                return None

            deadline   = _t.time() + timeout
            stop_click = _t.time() + min(20, timeout)  # then just listen
            # Each selector is clicked at most once. Re-clicking <video> on a
            # later pass would toggle playback back off, and blind-clicking
            # body/#root repeatedly can hit an overlay and navigate away.
            pending = list(_PLAY_SELECTORS)
            while not found and _t.time() < deadline and not aborted():
                if pending and _t.time() < stop_click:
                    sel = pending.pop(0)
                    try:
                        page.click(sel, timeout=700)
                    except Exception:
                        pass
                page.wait_for_timeout(500)
            try:
                title = page.title() or None
            except Exception:
                pass
        finally:
            try:
                browser.close()
            except Exception:
                pass
        return title

    for i in range(1, attempts + 1):
        if aborted():
            return None, None, None
        found, title = [], None
        try:
            with sync_playwright() as p:
                title = _attempt(p, found)
        except Exception as e:
            # Crashes (driver spawn, "Target crashed", page closed mid-wait) are
            # exactly what the retries are for, so keep going rather than bail.
            log(f"Browser grab error: {str(e).splitlines()[0][:120]}", "warn")
        if found:
            url, ref = _best_stream(found, page_url)
            return url, ref, title
        if i < attempts and not aborted():
            log(f"Browser grab found nothing (attempt {i}/{attempts}) — retrying...", "warn")

    return None, None, None


# ── Media operations ─────────────────────────────────────────────────────────
#
# One queued job kind per entry. Each function takes the same shape —
# (ffmpeg, src, out_dir, params, on_pct=, should_abort=, on_proc=) — and
# returns mediaops' {'ok', 'path', 'msg'}, so API._worker_mediaop can drive
# all of them without knowing which is which. Adding an operation is this
# function plus a line in _MEDIAOP_VERB, not another worker and another flag.

# on_stage is offered to every op but only wanted by the multi-stage ones, so
# the single-stage wrappers swallow it rather than each op growing a parameter
# it ignores.

def _op_clip(exe, src, out_dir, params, on_stage=None, **cb) -> dict:
    return mediaops.clip(exe, src, params.get("start", 0.0),
                         params.get("end", 0.0), out_dir,
                         fast=params.get("fast", False), **cb)


def _op_sheet(exe, src, out_dir, params, on_stage=None, **cb) -> dict:
    return mediaops.contact_sheet(exe, src, out_dir,
                                  cols=params.get("cols", 4),
                                  rows=params.get("rows", 4),
                                  width=params.get("width",
                                                   mediaops.SHEET_TILE_WIDTH),
                                  fmt=params.get("fmt", "jpg"), **cb)


def _op_fit(exe, src, out_dir, params, **cb) -> dict:
    return mediaops.fit_under(exe, src, out_dir,
                              target_mb=params.get("target_mb", 25.0), **cb)


def _op_loudnorm(exe, src, out_dir, params, on_stage=None, **cb) -> dict:
    return mediaops.normalize(exe, src, out_dir,
                              preset=params.get("preset", "streaming"),
                              on_stage=on_stage, **cb)


_MEDIAOPS = {"clip": _op_clip, "sheet": _op_sheet, "fit": _op_fit,
             "loudnorm": _op_loudnorm}

_MEDIAOP_VERB = {"clip": "Cutting", "sheet": "Building a contact sheet for",
                 "fit": "Shrinking", "loudnorm": "Levelling"}


def _clock(secs: float) -> str:
    """m:ss for a job label. Hours are rare enough to spell out in full."""
    secs = max(0.0, float(secs or 0))
    h, m, s = int(secs // 3600), int(secs % 3600 // 60), int(secs % 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class API:
    def __init__(self):
        self._window          = None
        self._updates: queue.Queue = queue.Queue()
        self._downloading     = False
        self._abort_flag      = False
        # Job queue. Downloads run one at a time on a single runner thread:
        # they are bandwidth-bound, the UI has one progress bar and one row of
        # provider lights, and the sleep_requests/politeness settings elsewhere
        # exist to avoid bans — running several at once would undo all three.
        self._jobs: list[dict] = []
        self._jobs_lock       = threading.Lock()
        self._job_seq         = 0
        self._runner          = None
        self._job_ok          = False   # set by _emit when a job reports success
        self._logging_in      = False
        self._last_file       = ""      # last path yt-dlp reported writing
        self._skip_history    = False   # set by probe-only jobs (list formats)
        # Live ffmpeg process for a conversion or a frame extraction, so Abort
        # can kill it outright rather than waiting for the next progress line
        # (a stalled encode emits none, and the read would block indefinitely).
        self._convert_proc    = None
        # Serves local files to the webview over loopback. Started lazily on
        # the first preview — a user who never opens History never binds a port.
        self._media           = MediaServer()
        self._extracting      = False
        self._maximized       = False
        # Synchronous-prompt support: worker thread blocks on _prompt_event until JS replies
        self._prompt_event    = threading.Event()
        self._prompt_response = True
        # API() is the first thing app.py runs, before any window exists, and
        # the shipped build is --windowed with no console — so anything that
        # raises here exits the exe with no window and no traceback. Settings
        # loading touches disk (app_data_dir() creates it), which can fail on a
        # redirected or locked-down %LOCALAPPDATA%. Losing the debug-log
        # preference is survivable; losing the app is not.
        try:
            set_debug(cfg.load().get("debug_log", False))
            debug_log(f"--- Swiss Downloader {__version__} started "
                      f"(log: {debug_log_path()}) ---")
        except Exception:
            pass

    def set_window(self, window):
        self._window = window

    # ── Window controls ───────────────────────────────────────────────────────

    def minimize_window(self):
        if self._window: self._window.minimize()

    def toggle_maximize(self) -> dict:
        """
        Maximise / restore, for the title bar's □ button.

        The state is tracked here rather than read back from the window: the
        title bar is our own HTML, so nothing else can change it behind our
        back, and pywebview's own `state` is not consistently reported across
        backends.
        """
        if not self._window:
            return {"ok": False, "maximized": False}
        try:
            if self._maximized:
                self._window.restore()
            else:
                self._window.maximize()
            self._maximized = not self._maximized
        except Exception:
            # Never let a window-chrome button raise into the UI; the worst
            # outcome should be a button that appears not to work.
            return {"ok": False, "maximized": self._maximized}
        return {"ok": True, "maximized": self._maximized}

    def close_window(self):
        if self._window: self._window.destroy()

    # ── Initialisation ────────────────────────────────────────────────────────

    def get_init_data(self) -> dict:
        s      = cfg.load()
        ffmpeg = find_ffmpeg()
        sf     = SpotiflacProxy()
        return {
            "defaultOutput":      DEFAULT_OUT,
            "defaultVideoOutput": DEFAULT_VIDEO_OUT,
            "ffmpegFound":        ffmpeg is not None,
            "ffmpegPath":       str(ffmpeg / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")) if ffmpeg else "",
            "pillowFound":        convert.pillow_available(),
            "autoFallback":     s.get("auto_fallback", True),
            "qobuzFormat":      s.get("qobuz_format", 6),
            "proxy":            s.get("proxy", ""),
            "debugLog":         s.get("debug_log", False),
            "notifyDone":       s.get("notify_done", True),
            "outtmplAudio":     s.get("outtmpl_audio", cfg.DEFAULTS["outtmpl_audio"]),
            "outtmplVideo":     s.get("outtmpl_video", cfg.DEFAULTS["outtmpl_video"]),
            "subLangs":         s.get("sub_langs", "en"),
            "sponsorblock":     s.get("sponsorblock", False),
            "sponsorblockCats": s.get("sponsorblock_cats", []),
            "sponsorblockAll":  list(_SPONSORBLOCK_CATS),
            "embedChapters":    s.get("embed_chapters", False),
            "concurrentFragments": s.get("concurrent_fragments", 5),
            "rateLimitKbps":       s.get("rate_limit_kbps", 0),
            "debugLogPath":     str(debug_log_path()),
            "spotiflacFound":   sf.found(),          # always True (built-in fallback)
            "spotiflacLocalDb": sf.has_local_db(),   # true if user has ~/.spotiflac/
            "spotiflacSvcs":    [name for name, _ in sf.services()],
            "appVersion":       __version__,
            "updateConfigured": bool(GITHUB_OWNER and GITHUB_REPO),
        }

    # ── Settings ──────────────────────────────────────────────────────────────

    def save_settings(self, data: dict) -> dict:
        s = cfg.load()
        for key, dest in [("autoFallback", "auto_fallback"),
                          ("qobuzFormat",  "qobuz_format"),
                          ("proxy",        "proxy"),
                          ("debugLog",     "debug_log"),
                          ("notifyDone",   "notify_done"),
                          ("outtmplAudio", "outtmpl_audio"),
                          ("outtmplVideo", "outtmpl_video"),
                          ("subLangs",     "sub_langs"),
                          ("sponsorblock", "sponsorblock"),
                          ("sponsorblockCats", "sponsorblock_cats"),
                          ("embedChapters",    "embed_chapters"),
                          ("concurrentFragments", "concurrent_fragments"),
                          ("rateLimitKbps",      "rate_limit_kbps")]:
            if key in data:
                val = data[key]
                if dest in ("qobuz_format", "concurrent_fragments",
                            "rate_limit_kbps"):
                    try:
                        val = int(val)
                    except (TypeError, ValueError):
                        continue      # keep the previous value over a bad one
                s[dest] = val

        # Reject a broken template here rather than at download time, when the
        # user has walked away and the only symptom is a failed job.
        for dest, fallback in (("outtmpl_audio", "%(artist,uploader)s - %(title)s"),
                               ("outtmpl_video", "%(uploader)s - %(title)s")):
            err = validate_outtmpl(s.get(dest))
            if err:
                s[dest] = cfg.DEFAULTS[dest]
                cfg.save(s)
                return {"ok": False,
                        "msg": f"Filename template rejected ({err}) — reset to default."}
        cfg.save(s)
        # Apply immediately: API is constructed once, so without this the
        # toggle would not take effect until the app was restarted.
        set_debug(s.get("debug_log", False))
        return {"ok": True, "msg": "Settings saved."}

    def check_for_updates(self) -> None:
        """Non-blocking: starts a background thread that pushes 'update_available' if newer."""
        import updater
        proxy = cfg.load().get("proxy") or None
        def _run():
            info = updater.check(proxy=proxy)
            if info:
                self._emit("update_available",
                           version=info["version"],
                           notes=info["notes"],
                           url=info["url"],
                           asset_url=info.get("asset_url", ""))
        threading.Thread(target=_run, daemon=True).start()

    def install_update(self, asset_url: str) -> dict:
        """Download the new .exe in the background, then swap + relaunch on exit."""
        import os, tempfile, subprocess
        import urllib.request

        if not getattr(sys, "frozen", False):
            return {"ok": False,
                    "msg": "Auto-install only works in the built .exe. "
                           "Running from source — pull the latest code instead."}
        if not asset_url:
            return {"ok": False,
                    "msg": "This release has no downloadable .exe attached."}

        current = sys.executable  # path to the running Swiss Downloader.exe

        def _run():
            try:
                self._emit("update_progress", pct=0, msg="Connecting…")
                tmp_new = os.path.join(tempfile.gettempdir(),
                                       "SwissDownloader_update.exe")
                req = urllib.request.Request(
                    asset_url, headers={"User-Agent":
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    total = int(resp.headers.get("Content-Length", 0))
                    done  = 0
                    with open(tmp_new, "wb") as f:
                        while True:
                            chunk = resp.read(262144)
                            if not chunk:
                                break
                            f.write(chunk)
                            done += len(chunk)
                            pct = (done / total * 100) if total else 0
                            self._emit("update_progress", pct=pct,
                                       msg=f"Downloading… {done//1048576} MB"
                                           + (f" / {total//1048576} MB" if total else ""))

                self._emit("update_progress", pct=100,
                           msg="Download complete. Restarting to install…")

                # Batch waits for this exe to unlock, swaps it, relaunches, self-deletes.
                bat = os.path.join(tempfile.gettempdir(),
                                   "SwissDownloader_update.bat")
                with open(bat, "w") as f:
                    f.write(
                        "@echo off\r\n"
                        ":retry\r\n"
                        f'move /Y "{tmp_new}" "{current}" >nul 2>&1\r\n'
                        "if errorlevel 1 (\r\n"
                        "  timeout /t 1 /nobreak >nul\r\n"
                        "  goto retry\r\n"
                        ")\r\n"
                        f'start "" "{current}"\r\n'
                        'del "%~f0"\r\n'
                    )
                subprocess.Popen(["cmd", "/c", bat],
                                 creationflags=0x08000000)  # CREATE_NO_WINDOW
                self._emit("update_ready")  # tells JS to close the window
            except Exception as e:
                self._emit("update_progress", pct=-1,
                           msg=f"Update failed: {e}")

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}

    def browse_folder(self) -> str:
        import webview
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        return result[0] if result else ""

    # ── Convert ───────────────────────────────────────────────────────────────

    # Dialog filters per input category. "All files" must stay last so someone
    # with an unusual extension is never locked out of their own file.
    _FILE_TYPES = {
        "audio": ("Audio (*.mp3;*.flac;*.wav;*.m4a;*.aac;*.ogg;*.opus;*.wma)",
                  "All files (*.*)"),
        "video": ("Video (*.mp4;*.mkv;*.webm;*.mov;*.avi;*.flv;*.wmv;*.m4v)",
                  "All files (*.*)"),
        "image": ("Images (*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.gif;*.ico;*.tif)",
                  "All files (*.*)"),
        "all":   ("Media (*.mp3;*.flac;*.wav;*.m4a;*.aac;*.ogg;*.opus;*.wma;"
                  "*.mp4;*.mkv;*.webm;*.mov;*.avi;*.flv;*.wmv;*.m4v;"
                  "*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.gif;*.ico)",
                  "All files (*.*)"),
    }

    def pick_files(self, kind: str = "all") -> list:
        """Multi-select file picker. Returns absolute paths, [] if cancelled."""
        import webview
        types = self._FILE_TYPES.get(kind, self._FILE_TYPES["all"])
        try:
            result = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True, file_types=types)
        except Exception:
            # Some GTK/Qt backends reject file_types they can't parse; a
            # picker with no filter beats no picker at all.
            result = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True)
        return [str(p) for p in result] if result else []

    def scan_convert_folder(self, folder: str, target: str,
                            recursive: bool = False,
                            out_dir: str = "", force: bool = False) -> dict:
        """Collect convertible files from a folder for the Convert tab."""
        folder = (folder or "").strip()
        if not folder or not Path(folder).is_dir():
            return {"ok": False, "msg": "That folder doesn't exist."}
        if target not in convert.TARGETS:
            return {"ok": False, "msg": "Pick a format to convert to first."}

        r = convert.scan_dir(folder, target, recursive=bool(recursive),
                             out_dir=out_dir, force=bool(force))
        if r["over_cap"]:
            return {"ok": False, "msg": (
                f"Found more than {convert.SCAN_CAP} files to convert. "
                f"Pick a sub-folder"
                f"{', or turn off sub-folders' if recursive else ''}.")}
        if not r["files"]:
            if r["same_fmt"]:
                return {"ok": False, "msg": (
                    f"All {r['same_fmt']} file(s) there are already "
                    f"{target.upper()}. Tick 'Re-encode same format' to "
                    f"convert them anyway.")}
            return {"ok": False, "msg": "No convertible files in that folder."}
        return {"ok": True, "files": r["files"], "same_fmt": r["same_fmt"],
                "total": r["total"]}

    def start_convert(self, files: list, out_dir: str, target: str,
                      quality, opts: dict = None) -> dict:
        """Queue one conversion job covering every file in `files`."""
        opts = dict(opts or {})          # comes from the renderer — copy it
        files = [str(f) for f in (files or []) if str(f).strip()]
        if not files:
            return {"ok": False, "msg": "No files selected."}
        if target not in convert.TARGETS:
            return {"ok": False, "msg": "Pick a format to convert to."}

        cat = convert.category_for_target(target)
        if cat == "image":
            if not convert.pillow_available():
                return {"ok": False, "msg": "Image conversion needs Pillow "
                                            "(pip install Pillow)."}
        elif find_ffmpeg() is None:
            return {"ok": False, "msg": "ffmpeg not found — conversion needs it."}

        # Catch the impossible direction up front rather than failing every
        # file individually once the job is already running.
        src_cats = {convert.CATEGORY_OF_EXT.get(Path(f).suffix.lower(), "")
                    for f in files}
        if cat == "video" and src_cats and src_cats <= {"audio"}:
            return {"ok": False, "msg": "Can't turn audio into video."}
        if cat == "audio" and src_cats and src_cats <= {"image"}:
            return {"ok": False, "msg": "Can't turn an image into audio."}

        if out_dir:
            try:
                Path(out_dir).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return {"ok": False, "msg": f"Can't use that output folder: {e}"}

        label = (Path(files[0]).name if len(files) == 1
                 else f"{len(files)} files → {target.upper()}")
        return self._enqueue("convert", label,
                             (files, out_dir, target, quality, opts))

    # ── Download ──────────────────────────────────────────────────────────────

    def start_download(self, url: str, output_dir: str,
                       quality: int, keep_original: bool,
                       list_formats: bool,
                       embed_thumb: bool = True,
                       embed_meta:  bool = True,
                       audio_format: str = "flac") -> dict:
        url = url.strip()
        if not url:
            return {"ok": False, "msg": "No URL provided."}
        if yt_dlp is None:
            return {"ok": False, "msg": "yt-dlp not installed."}

        return self._enqueue("audio", url, (
            url, output_dir, int(quality), bool(keep_original),
            bool(list_formats), bool(embed_thumb), bool(embed_meta),
            str(audio_format).lower()))

    def start_video_download(self, url: str, output_dir: str,
                             video_format: str, quality: str,
                             embed_thumb: bool = True,
                             embed_meta:  bool = True,
                             write_subs:  bool = False,
                             list_formats: bool = False) -> dict:
        url = url.strip()
        if not url:
            return {"ok": False, "msg": "No URL provided."}
        if yt_dlp is None:
            return {"ok": False, "msg": "yt-dlp not installed."}

        return self._enqueue("video", url, (
            url, output_dir, str(video_format).lower(),
            str(quality), bool(embed_thumb), bool(embed_meta),
            bool(write_subs), bool(list_formats)))

    def abort_download(self) -> dict:
        """Stop everything: the running job and anything still queued.

        'Abort' has always meant "stop", so cancelling only the current item
        and silently starting the next one would surprise people. Individual
        pending items can be dropped with remove_job() instead.
        """
        self._abort_flag = True
        # A conversion only notices the flag between ffmpeg progress lines, and
        # a stalled encode emits none — so kill the process directly instead of
        # leaving Abort looking like it did nothing.
        proc = self._convert_proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        with self._jobs_lock:
            for j in self._jobs:
                if j["status"] == "pending":
                    j["status"] = "cancelled"
        self._push_queue()
        return {"ok": True}

    # ── Search ───────────────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 8) -> dict:
        """
        Find something by name instead of making the user go and fetch a URL.

        Results come from YouTube because it is the one source that reliably
        answers a free-text query WITH a URL attached, and a URL is what the
        rest of the app runs on. That is not a downgrade in quality: picking a
        result only fills in the URL box, so the normal provider chain still
        runs and Odesli/Qobuz can still resolve it to lossless.

        extract_flat keeps this to a single fast metadata call — no formats are
        resolved and nothing is downloaded.
        """
        query = (query or "").strip()
        if not query:
            return {"ok": False, "msg": "Type something to search for."}
        if yt_dlp is None:
            return {"ok": False, "msg": "yt-dlp not installed."}

        limit = max(1, min(int(limit or 8), 20))
        opts = {
            "quiet": True, "no_warnings": True,
            "skip_download": True, "extract_flat": True,
        }
        proxy = cfg.load().get("proxy") or None
        if proxy:
            opts["proxy"] = proxy
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        except Exception as e:
            if debug_enabled():
                debug_log(f"search failed\n{traceback.format_exc()}")
            return {"ok": False, "msg": friendly_dl_error(str(e)) or f"Search failed: {e}"}

        results = []
        for e in (info or {}).get("entries") or []:
            url = e.get("url") or (f"https://www.youtube.com/watch?v={e['id']}"
                                   if e.get("id") else "")
            if not url:
                continue
            results.append({
                "title":    e.get("title") or "(untitled)",
                "url":      url,
                "uploader": e.get("uploader") or e.get("channel") or "",
                "duration": int(e["duration"]) if e.get("duration") else 0,
            })
        if not results:
            return {"ok": False, "msg": f"Nothing found for “{query}”."}
        return {"ok": True, "results": results}

    # ── History ──────────────────────────────────────────────────────────────

    def get_history(self) -> list:
        return self._history_entries()

    @staticmethod
    def _history_entries(limit: int = 100) -> list:
        """History plus a liveness flag. Both the poll and the push go through
        here so a freshly-finished row renders the same as a reloaded one."""
        entries = history.listing(limit)
        # Tell the UI whether each file is still where we left it, so a moved
        # or deleted download doesn't offer a dead "open folder" button.
        for e in entries:
            p = Path(e["path"]) if e.get("path") else None
            e["exists"] = bool(p) and p.exists()
            # 'video' | 'audio' | 'image' | '' — decided from the extension, so
            # the list can be rendered without probing 100 files off disk. A
            # frame-extraction row's path is a folder, which has no extension
            # and correctly comes out as ''.
            e["media"] = preview.kind_of(p) if (p and p.is_file()) else ""
        return entries

    def clear_history(self) -> dict:
        return {"ok": True, "removed": history.clear()}

    def reveal_file(self, path: str) -> dict:
        """Open the folder containing a downloaded file and select it."""
        p = Path(path or "")
        if not p.exists():
            # Fall back to the folder: post-processing renames the file (e.g.
            # .webm -> .flac), so the recorded path can be stale while the
            # download itself was fine.
            if p.parent.exists():
                p = p.parent
            else:
                return {"ok": False, "msg": "That file is no longer there."}
        try:
            if sys.platform == "win32":
                if p.is_dir():
                    os.startfile(str(p))            # noqa: S606
                else:
                    subprocess.Popen(["explorer", "/select,", str(p)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p if p.is_dir() else p.parent)])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ── Preview / frames ─────────────────────────────────────────────────────

    def media_info(self, path: str) -> dict:
        """
        Everything the History tab needs to preview one file.

        `url` points at the loopback media server rather than the file itself.
        The page is served over http (pywebview's own bottle server), and an
        http page may not load file: subresources — an inline
        <video src="file://..."> is rejected outright. See mediaserver.py.
        """
        p = Path(path or "")
        if not path or not p.is_file():
            return {"ok": False, "msg": "That file is no longer there."}

        kind = preview.kind_of(p)
        if not kind:
            return {"ok": False, "msg": "That isn't a media file."}

        url = self._media.url_for(p)
        if not url:
            return {"ok": False, "msg": "Couldn't start the preview server."}

        # Images are probed too — dimensions are the thing you actually want to
        # know about a picture. The UI drops the fields that mean nothing for a
        # still (ffmpeg reports every JPEG as a 25 fps mjpeg "video").
        exe = self._ffmpeg_exe()
        info = convert.probe_full(exe, p) if exe else {}
        if kind == "image":
            # Pillow overrides ffmpeg here: ffmpeg cannot read an animated
            # WebP's size at all, and Pillow knows the frame count, which is
            # what makes "animated" showable.
            pil = convert.probe_image(p)
            if pil.get("width"):
                info.update(width=pil["width"], height=pil["height"],
                            frames=pil["frames"], animated=pil["animated"])
                info["format"] = pil["format"] or info.get("format", "")
        return {
            "ok":     True,
            "url":    url,
            "kind":   kind,
            "name":   p.name,
            "path":   str(p),
            "size":   p.stat().st_size,
            "ext":    p.suffix.lower().lstrip("."),
            # Everything below is best-effort: probe_full degrades each field
            # to 0 or '' rather than failing, and the UI simply omits the rows
            # it has no value for.
            **{k: info.get(k, d) for k, d in (
                ("duration", 0.0), ("format", ""), ("bitrate", 0),
                ("has_cover", False), ("vcodec", ""), ("vprofile", ""),
                ("width", 0), ("height", 0), ("fps", 0.0), ("vbitrate", 0),
                ("pix_fmt", ""), ("acodec", ""), ("aprofile", ""),
                ("sample_rate", 0), ("channels", 0), ("abitrate", 0),
                ("frames", 0), ("animated", False))},
        }

    def thumb_for(self, path: str) -> dict:
        """
        A thumbnail URL for one history row, generated on demand and cached.

        {"ok": False} is an ordinary answer — an audio file with no cover art
        has no thumbnail and the UI shows a placeholder. It is not an error.
        """
        exe = self._ffmpeg_exe()
        if not exe:
            return {"ok": False}
        thumb = preview.thumbnail(exe, path)
        if not thumb:
            return {"ok": False}
        url = self._media.url_for(thumb)
        return {"ok": bool(url), "url": url}

    def save_frame(self, path: str, seconds: float, fmt: str = "png",
                   out_dir: str = "") -> dict:
        """Write the frame the user paused on. Fast enough to stay synchronous."""
        try:
            secs = float(seconds)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "Bad timestamp."}

        dst, err = preview.save_frame(self._ffmpeg_exe(), path, secs,
                                      out_dir, fmt)
        if err:
            self._log(f"Frame not saved: {err}", "err")
            return {"ok": False, "msg": err}
        self._log(f"Saved frame → {dst}", "bright")
        return {"ok": True, "path": dst, "name": Path(dst).name}

    def plan_frames(self, path: str, mode: str, value: float) -> dict:
        """How many files an extraction would write, asked before running it."""
        try:
            val = float(value)
        except (TypeError, ValueError):
            val = 1.0
        return preview.plan_extraction(self._ffmpeg_exe(), path, mode, val)

    def extract_frames(self, path: str, mode: str = "every", value: float = 1.0,
                       fmt: str = "png", width: int = 0,
                       out_dir: str = "") -> dict:
        """
        Start a bulk frame extraction on a worker thread.

        Refuses to run alongside a download or conversion: all three share one
        progress bar, one log and one _convert_proc handle, so overlapping them
        would make Abort ambiguous and the progress bar meaningless.
        """
        if self._downloading:
            return {"ok": False, "msg": "Wait for the current job to finish."}
        if self._extracting:
            return {"ok": False, "msg": "Already extracting frames."}
        p = Path(path or "")
        if not p.is_file():
            return {"ok": False, "msg": "That file is no longer there."}
        if not self._ffmpeg_exe():
            return {"ok": False, "msg": "ffmpeg wasn't found."}

        try:
            val = float(value)
        except (TypeError, ValueError):
            val = 1.0

        self._extracting = True
        # A previous Abort leaves the flag set; inheriting it would kill this
        # run before it started. Same reset the job runner does per job.
        self._abort_flag = False
        threading.Thread(
            target=self._extract_worker,
            args=(str(p), mode, val, fmt, int(width or 0), out_dir),
            daemon=True).start()
        return {"ok": True}

    def _extract_worker(self, path, mode, value, fmt, width, out_dir):
        try:
            name = Path(path).name
            self._log(f"Extracting frames from {name}…", "bright")
            self._progress(0)
            result = preview.extract_frames(
                self._ffmpeg_exe(), path, out_dir, mode, value, fmt, width,
                on_pct=lambda p: self._progress(p if p is not None else 0),
                should_abort=lambda: self._abort_flag,
                on_proc=lambda pr: setattr(self, "_convert_proc", pr))

            self._log(result["msg"], "bright" if result["ok"] else "warn")
            if result["ok"]:
                self._progress(100)
                self._log(f"Frames are in {result['folder']}", "info")
                try:
                    history.add("frames", f"{result['count']} frames ← {name}",
                                "done", title=f"{result['count']} frames from {name}",
                                path=result["folder"])
                except Exception:
                    pass   # history is a convenience; never fail a job over it
            else:
                self._progress(0)
            # The History tab hides the shared log, so the outcome has to
            # travel with the event or the user sees the bar vanish and
            # nothing else.
            self._emit("frames_done", ok=result["ok"], msg=result["msg"],
                       folder=result.get("folder", ""),
                       count=result.get("count", 0))
        except Exception as e:
            if debug_enabled():
                debug_log(f"frame extraction failed\n{traceback.format_exc()}")
            self._log(f"Frame extraction failed: {e}", "err")
            self._emit("frames_done", ok=False, msg=f"Failed: {e}",
                       folder="", count=0)
        finally:
            self._extracting = False
            self._convert_proc = None
            self._emit("history", entries=self._history_entries(50))
            self._emit("done")

    def clear_thumb_cache(self) -> dict:
        return {"ok": True, "removed": preview.clear_cache()}

    # ── Tag editor ───────────────────────────────────────────────────────────
    #
    # The only feature that modifies files the user already owns rather than
    # producing new ones, which is why tags.py copies, tags the copy, and only
    # then replaces the original.

    def read_tags(self, path: str) -> dict:
        r = tags.read_tags(path)
        r["fields"] = list(tags.FIELDS)
        r["labels"] = dict(tags.LABELS)
        return r

    def save_tags(self, path: str, values: dict = None, clear=None,
                  cover_path: str = "", remove_cover: bool = False) -> dict:
        """
        Write tag changes, having first let go of the file.

        The preview holds the file open through the media server, and Windows
        will not let us replace a file with a live handle on it. The page drops
        the <video> src before calling this; forgetting the id here closes the
        other half, so a request already in flight 404s rather than re-opening
        the file underneath us.
        """
        p = Path(path or "")
        self._media.forget(p)
        r = tags.write_tags(p, values or {}, clear or [], cover_path,
                            bool(remove_cover))
        if r["ok"]:
            self._log(f"{p.name}: {r['msg']}", "bright")
            # The thumbnail cache keys on mtime and size, so new artwork
            # invalidates itself — nothing to clear here.
            self._emit("history", entries=self._history_entries(50))
        return r

    def pick_cover(self) -> dict:
        """Choose an image for cover art. Returns a path — never bytes."""
        if not self._window:
            return {"ok": False, "msg": "No window."}
        try:
            import webview
            sel = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=("Images (*.jpg;*.jpeg;*.png;*.webp;*.bmp)",))
        except Exception as e:
            return {"ok": False, "msg": str(e)}
        if not sel:
            return {"ok": False, "msg": ""}      # cancelled is not an error
        path = sel[0] if isinstance(sel, (list, tuple)) else sel
        return {"ok": True, "path": str(path), "name": Path(path).name}

    def fetch_tags(self, query: str) -> dict:
        """
        Look up track metadata to fill the editor from.

        Returns candidates for the user to choose between — never auto-applies.
        A search is a guess, and silently overwriting somebody's tags with a
        guess is how a library gets quietly wrecked.
        """
        query = (query or "").strip()
        if not query:
            return {"ok": False, "msg": "Type something to search for.",
                    "results": []}
        s = cfg.load()
        proxy = s.get("proxy") or None
        try:
            items = QobuzAPI().search_track_anon(query, limit=5, proxy=proxy)
        except Exception as e:
            return {"ok": False, "msg": f"Search failed: {e}", "results": []}

        results = []
        for it in items or []:
            album = it.get("album") or {}
            artist = ((it.get("performer") or {}).get("name")
                      or (album.get("artist") or {}).get("name") or "")
            released = str(album.get("release_date_original") or "")
            results.append({
                "title":       it.get("title") or "",
                "artist":      artist,
                "album":       album.get("title") or "",
                "albumartist": (album.get("artist") or {}).get("name") or "",
                "date":        released[:4],
                "genre":       (album.get("genre") or {}).get("name") or "",
                "tracknumber": str(it.get("track_number") or ""),
                "discnumber":  str(it.get("media_number") or ""),
                "cover":       ((album.get("image") or {}).get("large") or ""),
                "label":       f"{artist} — {it.get('title') or ''}"
                               f"{' · ' + album.get('title') if album.get('title') else ''}"
                               f"{' · ' + released[:4] if released else ''}",
            })
        if not results:
            return {"ok": False, "msg": "Nothing found for that.", "results": []}
        return {"ok": True, "msg": f"{len(results)} matches", "results": results}

    def fetch_cover(self, url: str) -> dict:
        """
        Download a chosen cover to a temp file and hand back the path.

        Same rule as pick_cover: artwork never crosses the JS bridge as bytes.
        """
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "msg": "That isn't a usable image link."}
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read(12 * 1024 * 1024)
            if not data:
                return {"ok": False, "msg": "That image was empty."}
            d = app_data_dir() / "tmp"
            d.mkdir(parents=True, exist_ok=True)
            dst = d / "cover-fetch.jpg"
            dst.write_bytes(data)
            return {"ok": True, "path": str(dst), "name": dst.name}
        except Exception as e:
            return {"ok": False, "msg": f"Couldn't fetch that cover: {e}"}

    # ── Media operations (clip, and later: sheet / fit / normalise) ───────────
    #
    # These go on the job queue rather than running on their own thread the way
    # extract_frames does. Anything that runs ffmpeg to completion over a whole
    # file shares _abort_flag and _convert_proc with the runner, and every such
    # feature that opts out needs its own mutual-exclusion flag — four of them
    # and Abort no longer knows which process it is killing. The queue already
    # gives us single-process safety, an unambiguous Abort, history and
    # progress; the only thing it lacked was a per-tab completion event, and
    # that is what mediaop_done is.

    def export_clip(self, path: str, start: float, end: float,
                    fast: bool = False, out_dir: str = "") -> dict:
        """Queue a cut of path[start:end] as its own file."""
        p = Path(path or "")
        if not p.is_file():
            return {"ok": False, "msg": "That file is no longer there."}
        try:
            start, end = float(start), float(end)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "Bad in/out points."}
        if end - start < 0.05:
            return {"ok": False, "msg": "The out point has to come after the in point."}

        label = f"clip {_clock(start)}–{_clock(end)} ← {p.name}"
        return self._enqueue("clip", label,
                             ("clip", str(p), out_dir,
                              {"start": start, "end": end, "fast": bool(fast)}))

    def export_sheet(self, path: str, cols: int = 4, rows: int = 4,
                     fmt: str = "jpg", out_dir: str = "") -> dict:
        """Queue a contact sheet: the whole video as one grid of stills."""
        p = Path(path or "")
        if not p.is_file():
            return {"ok": False, "msg": "That file is no longer there."}
        cols, rows = max(1, int(cols or 1)), max(1, int(rows or 1))
        if cols * rows > mediaops.SHEET_MAX_TILES:
            return {"ok": False,
                    "msg": f"That's {cols * rows} tiles — keep it to "
                           f"{mediaops.SHEET_MAX_TILES}."}
        return self._enqueue("sheet", f"{cols}×{rows} sheet ← {p.name}",
                             ("sheet", str(p), out_dir,
                              {"cols": cols, "rows": rows, "fmt": fmt}))

    def plan_fit(self, path: str, target_mb: float) -> dict:
        """
        What a size target would cost, asked before running it.

        Shares its arithmetic with fit_under, so the preview and the refusal
        can never disagree about whether a target is possible.
        """
        return mediaops.plan_fit(self._ffmpeg_exe(), path, target_mb)

    def export_fit(self, path: str, target_mb: float,
                   out_dir: str = "") -> dict:
        """Queue a re-encode that lands under `target_mb` decimal megabytes."""
        p = Path(path or "")
        if not p.is_file():
            return {"ok": False, "msg": "That file is no longer there."}
        # Refuse an impossible target here rather than after a long encode.
        plan = mediaops.plan_fit(self._ffmpeg_exe(), p, target_mb)
        if not plan["ok"]:
            return {"ok": False, "msg": plan["msg"]}
        if plan["fits_already"]:
            return {"ok": False, "msg": plan["msg"]}
        return self._enqueue("fit", f"under {target_mb} MB ← {p.name}",
                             ("fit", str(p), out_dir,
                              {"target_mb": float(target_mb)}))

    def export_normalize(self, path: str, preset: str = "streaming",
                         out_dir: str = "") -> dict:
        """Queue a two-pass loudness normalisation."""
        p = Path(path or "")
        if not p.is_file():
            return {"ok": False, "msg": "That file is no longer there."}
        preset = str(preset or "streaming").lower()
        if preset not in convert.LOUDNORM_PRESETS:
            return {"ok": False, "msg": "Unknown loudness target."}
        return self._enqueue("loudnorm", f"level ← {p.name}",
                             ("loudnorm", str(p), out_dir, {"preset": preset}))

    def loudness_presets(self) -> dict:
        """The presets and their targets, so the UI never hardcodes a number."""
        return {"ok": True,
                "presets": [{"name": n, "i": v[0], "tp": v[1], "lra": v[2]}
                            for n, v in convert.LOUDNORM_PRESETS.items()]}

    def _worker_mediaop(self, op: str, src: str, out_dir: str, params: dict):
        """
        Run one queued media operation. Shared by every kind in _MEDIAOPS.

        The History tab hides the shared log, so the outcome has to travel with
        the mediaop_done event as well as being logged — otherwise the user
        watches the progress bar vanish and is told nothing.
        """
        name = Path(src).name
        fn = _MEDIAOPS.get(op)
        if fn is None:                     # only reachable via a coding error
            self._log(f"Unknown media operation: {op}", "err")
            self._emit("mediaop_done", op=op, ok=False,
                       msg="That operation isn't available.", path="")
            return

        self._log(f"{_MEDIAOP_VERB.get(op, 'Working on')} {name}…", "bright")
        self._progress(0)
        result = fn(
            self._ffmpeg_exe(), src, out_dir, params,
            on_pct=lambda p: self._progress(p if p is not None else 0),
            should_abort=lambda: self._abort_flag,
            on_proc=lambda pr: setattr(self, "_convert_proc", pr),
            # A multi-stage op (analyse, encode, tighten) would otherwise show
            # one bar restarting for no visible reason.
            on_stage=lambda msg: self._emit("job_status", msg=msg))

        self._convert_proc = None
        self._log(result["msg"], "bright" if result["ok"] else "warn")
        if result["ok"]:
            self._progress(100)
            self._last_file = result["path"]
            # Without this the runner records the job as failed no matter what
            # the worker returned.
            self._emit("success", path=result["path"])
            # "The right thing to do was nothing" is a success, but there is no
            # file to point a history row at.
            if result.get("skip"):
                self._skip_history = True
        else:
            self._progress(0)
            # Same rule as a frame extraction: History records the files these
            # produced, so an attempt that produced none leaves no row. A
            # failed *download* is worth keeping because it can be retried
            # from the URL; a clip that failed is just redone from the player.
            self._skip_history = True
        self._emit("mediaop_done", op=op, ok=result["ok"], msg=result["msg"],
                   path=result.get("path", ""))

    @staticmethod
    def _ffmpeg_exe() -> str:
        d = find_ffmpeg()
        if not d:
            return ""
        return str(d / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"))

    # ── Site sign-in (cookies) ───────────────────────────────────────────────

    def browser_login(self, url: str) -> dict:
        """Open a visible browser so the user can sign in to `url`'s site."""
        url = (url or "").strip()
        if not url:
            return {"ok": False, "msg": "Enter a URL from the site first."}
        # site_key() echoes back anything dot-less, so "notaurl" would sail
        # through a plain truthiness check and open a browser on nothing.
        if "." not in site_cookies.site_key(url):
            return {"ok": False, "msg": "That doesn't look like a site URL."}
        if self._logging_in:
            return {"ok": False, "msg": "A sign-in window is already open."}

        self._logging_in = True
        threading.Thread(target=self._login_worker, args=(url,),
                         daemon=True).start()
        return {"ok": True}

    def list_cookies(self) -> list:
        return site_cookies.listing()

    def forget_cookies(self, site: str) -> dict:
        ok = site_cookies.forget(site)
        return {"ok": ok, "sites": site_cookies.listing()}

    def forget_all_cookies(self) -> dict:
        n = site_cookies.forget_all()
        return {"ok": True, "removed": n, "sites": site_cookies.listing()}

    def _login_worker(self, url):
        try:
            self._emit("login_state", busy=True)
            self._log(f"Opening a browser to sign in to {site_cookies.site_key(url)}…",
                      "info")
            count, err = _browser_login(url, self._log,
                                        should_abort=lambda: self._abort_flag)
            if err and not count:
                self._log(f"Sign-in failed: {err}", "err")
            else:
                self._log(f"Saved {count} cookies for "
                          f"{site_cookies.site_key(url)}. Downloads from this "
                          f"site will use them.", "bright")
        except Exception as e:
            self._log(f"Sign-in failed: {e}", "err")
        finally:
            self._logging_in = False
            self._emit("login_state", busy=False, sites=site_cookies.listing())

    # ── Queue ────────────────────────────────────────────────────────────────

    def get_queue(self) -> list:
        return self._queue_snapshot()

    def remove_job(self, job_id: int) -> dict:
        """Drop a queued item. The running one has to go through abort."""
        with self._jobs_lock:
            for j in self._jobs:
                if j["id"] == int(job_id):
                    if j["status"] == "running":
                        return {"ok": False, "msg": "That one is running — use Abort."}
                    self._jobs.remove(j)
                    break
        self._push_queue()
        return {"ok": True}

    def retry_job(self, job_id: int) -> dict:
        """Re-queue a finished item using the settings it was created with."""
        with self._jobs_lock:
            for j in self._jobs:
                if j["id"] == int(job_id):
                    if j["status"] in ("running", "pending"):
                        return {"ok": False, "msg": "That one hasn't finished yet."}
                    j["status"] = "pending"
                    break
            else:
                return {"ok": False, "msg": "No such item."}
        self._push_queue()
        self._ensure_runner()
        return {"ok": True}

    def clear_finished(self) -> dict:
        with self._jobs_lock:
            self._jobs = [j for j in self._jobs
                          if j["status"] in ("pending", "running")]
        self._push_queue()
        return {"ok": True}

    def prompt_response(self, answer: bool) -> dict:
        """Called from JS to deliver the user's Yes/No answer to a pending prompt."""
        self._prompt_response = bool(answer)
        self._prompt_event.set()
        return {"ok": True}

    # ── Internal helpers for worker → UI prompts ─────────────────────────────

    def _ask_user(self, question: str, default: bool = True, timeout: float = 120.0) -> bool:
        """Block the worker thread until the UI returns a Yes/No answer."""
        self._prompt_response = default
        self._prompt_event.clear()
        self._emit("prompt", question=question)
        if not self._prompt_event.wait(timeout=timeout):
            return default
        return self._prompt_response

    def poll_updates(self) -> list:
        """Drain and return all pending UI updates for JS to process."""
        batch = []
        try:
            while True:
                batch.append(self._updates.get_nowait())
        except queue.Empty:
            pass
        return batch

    # ── Internal ──────────────────────────────────────────────────────────────

    def _emit(self, kind: str, **kwargs):
        # The workers report a finished download by emitting "success" rather
        # than returning a status (they catch and log their own errors), so
        # this is where the queue learns whether a job actually worked.
        if kind == "success":
            self._job_ok = True
        self._updates.put({"kind": kind, **kwargs})

    # ── Queue internals ──────────────────────────────────────────────────────

    def _enqueue(self, kind: str, url: str, args: tuple) -> dict:
        # A frame extraction is an ffmpeg run that shares _abort_flag and
        # _convert_proc with the job runner. Letting a download start on top of
        # one would make Abort kill an unpredictable half of the two.
        if self._extracting:
            return {"ok": False,
                    "msg": "Wait for the frame extraction to finish."}
        with self._jobs_lock:
            self._job_seq += 1
            job = {"id": self._job_seq, "kind": kind, "url": url,
                   "status": "pending", "args": args}
            self._jobs.append(job)
            # Count the running job too — by the time a second URL is added the
            # first has usually left 'pending', and ignoring it would report the
            # newcomer as starting immediately when it is in fact waiting.
            ahead = sum(1 for j in self._jobs
                        if j["status"] in ("pending", "running")) - 1
        self._push_queue()
        self._ensure_runner()
        if ahead > 0:
            self._log(f"Queued behind {ahead} other download"
                      f"{'s' if ahead > 1 else ''}: {url}", "info")
        return {"ok": True, "id": job["id"], "queued": ahead > 0}

    def _queue_snapshot(self) -> list:
        """Job list for the UI — without `args`, which holds no display value
        and would be pushed on every poll."""
        with self._jobs_lock:
            return [{k: v for k, v in j.items() if k != "args"} for j in self._jobs]

    def _push_queue(self):
        self._emit("queue", jobs=self._queue_snapshot())

    def _ensure_runner(self):
        """Start the drain thread if it isn't already going."""
        with self._jobs_lock:
            if self._runner is not None and self._runner.is_alive():
                return
            self._runner = threading.Thread(target=self._runner_loop, daemon=True)
            runner = self._runner
        runner.start()

    def _runner_loop(self):
        """Run queued jobs one at a time until nothing is pending."""
        ran = 0
        all_ok = True
        try:
            while True:
                with self._jobs_lock:
                    job = next((j for j in self._jobs
                                if j["status"] == "pending"), None)
                    if job is None:
                        return
                    job["status"] = "running"
                # A fresh job must not inherit the previous one's abort, but a
                # user who hit Abort cancelled the whole queue — so anything
                # still pending was already marked cancelled above.
                self._abort_flag  = False
                self._downloading = True
                self._job_ok       = False
                self._last_file    = ""
                self._skip_history = False
                self._push_queue()
                # Every media operation shares one worker; the op name travels
                # in args[0]. An unknown kind still raises, which is what we
                # want — it can only mean _enqueue was called with a typo.
                fn = {**{k: self._worker_mediaop for k in _MEDIAOPS},
                      "audio":   self._worker,
                      "video":   self._worker_video,
                      "convert": self._worker_convert}[job["kind"]]
                try:
                    fn(*job["args"])
                except Exception:
                    # Workers handle their own errors; anything reaching here is
                    # a bug in the worker itself and must not kill the queue.
                    if debug_enabled():
                        debug_log(f"job {job['id']} crashed\n{traceback.format_exc()}")
                    self._log("That download failed unexpectedly.", "err")
                status = ("cancelled" if self._abort_flag
                          else "done" if self._job_ok else "failed")
                ran += 1
                all_ok = all_ok and status == "done"
                with self._jobs_lock:
                    job["status"] = status
                try:
                    if not self._skip_history:
                        history.add(job["kind"], job["url"], status,
                                    title=Path(self._last_file).stem if self._last_file else "",
                                    path=self._last_file)
                except Exception:
                    pass   # history is a convenience; never fail a job over it
                self._push_queue()
                self._emit("history", entries=self._history_entries(50))
        finally:
            self._downloading = False
            self._emit("done")
            # Only when work actually happened: a runner that starts and finds
            # nothing pending (a race with remove_job, say) shouldn't chime.
            if ran:
                try:
                    if cfg.load().get("notify_done", True):
                        notify_done(ok=all_ok)
                except Exception:
                    pass

    def _log(self, msg: str, level: str = "ok"):
        self._emit("log", msg=str(msg), level=level)

    def _progress(self, pct: float, speed: float = 0, eta: float = 0,
                  done: int = 0, total: int = 0):
        self._emit("progress",
                   pct=round(float(pct), 1),
                   speed=speed, eta=eta, done=done, total=total)

    def _provider(self, key: str, state: str):
        self._emit("provider", key=key, state=state)

    def _worker(self, url, output_dir, quality, keep, list_fmt,
                embed_thumb=True, embed_meta=True, audio_format="flac"):
        """Dispatch: album/playlist → loop, single track → just call _download_one."""
        url = url.strip()
        try:
            # Album / playlist detection
            if is_album_or_playlist_url(url) and not list_fmt:
                self._log(f"Detected album/playlist URL — expanding…", "info")
                s     = cfg.load()
                proxy = s.get("proxy") or None
                track_urls: list[str] = []
                if "open.spotify.com/" in url and ("/album/" in url or "/playlist/" in url):
                    tracks = fetch_spotify_album_tracks(url, proxy=proxy)
                    track_urls = [t["url"] for t in tracks if t.get("url")]
                # For YouTube/Bandcamp/SoundCloud playlists, yt-dlp handles natively —
                # pass the URL through but disable noplaylist below.

                if track_urls:
                    self._log(f"Album/playlist contains {len(track_urls)} tracks.", "bright")
                    for i, t_url in enumerate(track_urls, 1):
                        if self._abort_flag:
                            self._log("Aborted.", "warn")
                            break
                        self._log(f"\n=== Track {i}/{len(track_urls)} ===", "bright")
                        self._emit("album_progress", current=i, total=len(track_urls))
                        # Reset provider dots between tracks
                        for k in ("ytdlp","odesli","proxy","musicbrainz"):
                            self._provider(k, "idle")
                        self._download_one(t_url, output_dir, quality, keep, False,
                                           embed_thumb, embed_meta, audio_format)
                    self._log(f"\nAlbum/playlist complete: {len(track_urls)} tracks processed.", "bright")
                    return
                # Fallthrough: let yt-dlp's native playlist support handle it
            # Single track / native-playlist URL
            self._download_one(url, output_dir, quality, keep, list_fmt,
                               embed_thumb, embed_meta, audio_format)
        except Exception:
            # _download_one logs its own failures; a leak to here is a bug, and
            # the runner needs it to mark the job failed rather than swallow it.
            if debug_enabled():
                debug_log(f"audio worker crashed\n{traceback.format_exc()}")
            raise

    def _download_one(self, url, output_dir, quality, keep, list_fmt,
                      embed_thumb=True, embed_meta=True, audio_format="flac"):
        s          = cfg.load()
        proxy      = s.get("proxy") or None
        ffmpeg_dir = find_ffmpeg()

        # Reported once per track rather than inside make_opts(), which the
        # provider chain calls repeatedly.
        if _impersonate_target() is None and _impersonate_note:
            self._log(f"Note: {_impersonate_note}", "warn")

        def ydl_hook(d):
            if self._abort_flag:
                raise yt_dlp.utils.DownloadError("Aborted by user")
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done  = d.get("downloaded_bytes", 0)
                speed = d.get("speed") or 0
                eta   = d.get("eta") or 0
                pct   = (done / total * 100) if total else 0
                self._progress(pct, speed=speed, eta=eta, done=done, total=total)
            elif d["status"] == "finished":
                # Remember what was really written — history records the actual
                # file, which is the only way a skipped-but-reported-successful
                # download becomes visible.
                self._last_file = d.get("filename") or ""
                self._log(f"Downloaded: {Path(d['filename']).name}", "bright")
                self._log("Converting to FLAC…", "dim")
                self._progress(100)

        class Logger:
            def __init__(self, cb):
                self.cb = cb; self.errors = []
            def debug(self, m):
                if not m.startswith("[debug]"): self.cb(m, "dim")
            def info(self, m):   self.cb(m, "dim")
            def warning(self, m): self.cb(m, "warn")
            def error(self, m):
                self.errors.append(m)
                # Suppress raw dump if a friendly message will be shown instead
                if not friendly_dl_error(m): self.cb(m, "err")

        # Map UI format → (yt-dlp preferredcodec, preferredquality)
        # For FLAC the quality param is the compression level (0–12).
        # For MP3/M4A/Opus it's bitrate in kbps (or "0" for VBR-V0 on MP3).
        # For OGG/vorbis it's quality level 0–10.
        # For WAV it's ignored.
        def _audio_postproc():
            f = audio_format
            if f == "mp3":
                bitrates = ["128", "192", "256", "320", "0"]   # last is VBR-V0
                q = bitrates[max(0, min(int(quality), 4))]
                return {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": q}
            if f == "m4a":
                bitrates = ["128", "192", "256", "320", "320"]
                q = bitrates[max(0, min(int(quality), 4))]
                return {"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": q}
            if f == "ogg":
                return {"key": "FFmpegExtractAudio", "preferredcodec": "vorbis", "preferredquality": str(quality)}
            if f == "opus":
                bitrates = ["96", "128", "160", "192", "256"]
                q = bitrates[max(0, min(int(quality), 4))]
                return {"key": "FFmpegExtractAudio", "preferredcodec": "opus", "preferredquality": q}
            if f == "wav":
                return {"key": "FFmpegExtractAudio", "preferredcodec": "wav"}
            # default flac
            return {"key": "FFmpegExtractAudio", "preferredcodec": "flac",
                    "preferredquality": str(quality)}

        def make_opts(target_url):
            # %(artist,uploader)s = use the 'artist' field if present (Bandcamp, etc.),
            # otherwise fall back to 'uploader' (YouTube channel name).
            tpl = _outtmpl(output_dir, s.get("outtmpl_audio"),
                           "%(artist,uploader)s - %(title)s")
            pps = [_audio_postproc()]
            if embed_meta:
                pps.append({"key": "FFmpegMetadata", "add_metadata": True,
                            "add_chapters": bool(s.get("embed_chapters"))})
            pps.extend(_sponsorblock_pps(s))
            if embed_thumb and audio_format != "wav":
                # WAV doesn't support embedded album art
                pps.append({"key": "EmbedThumbnail"})
            opts = {
                "format":          "bestaudio/best",
                "outtmpl":         tpl,
                "postprocessors":  pps,
                "writethumbnail":  embed_thumb and audio_format != "wav",
                "keepvideo":       keep,
                "progress_hooks":  [ydl_hook],
                **_RETRY_OPTS,
                **_tuning_opts(s),
            }
            if ffmpeg_dir: opts["ffmpeg_location"] = str(ffmpeg_dir)
            if proxy:      opts["proxy"] = proxy
            # Keyed on the target rather than the URL the user pasted: Odesli
            # hands us the same track on a different service, and the cookies
            # that matter are the ones for wherever we actually fetch from.
            # (No log line here — make_opts runs once per provider attempt.)
            jar = site_cookies.cookie_file_for(target_url)
            if jar:        opts["cookiefile"] = jar
            imp = _impersonate_target()
            if imp:        opts["impersonate"] = imp
            return opts

        def fetch_cover(cover_url: str):
            """Download cover art, return (bytes, mime) or (None, None)."""
            if not cover_url:
                return None, None
            try:
                import urllib.request as _ur
                req = _ur.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
                from providers import _opener
                with _opener(proxy).open(req, timeout=10) as r:
                    data = r.read()
                    mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
                    return data, mime
            except Exception:
                return None, None

        def tag_proxy_file(out_file: Path, track_info: dict):
            """
            Tag a downloaded FLAC with album metadata + cover art.
            Source priority for the cover: primary URL (Spotify/Qobuz) → iTunes
            album-cover lookup as fallback.  Also drops a same-name sidecar JPG
            and folder.jpg as backups for Windows Explorer thumbnails (which are
            unreliable for FLAC files even when art is properly embedded).
            """
            if not (embed_meta or embed_thumb):
                return out_file

            artist = (track_info.get("performer") or {}).get("name", "")
            title  = track_info.get("title", "")

            cover_data, cover_mime = None, "image/jpeg"

            if embed_thumb:
                # 1) Primary cover URL (already in track_info if scraping worked)
                cover_url = (track_info.get("album") or {}) \
                                .get("image", {}).get("large", "")
                if cover_url:
                    self._log(f"  Fetching album cover from {cover_url[:60]}…", "dim")
                    cover_data, cover_mime = fetch_cover(cover_url)
                    if cover_data:
                        self._log(f"  Cover fetched: {len(cover_data)} bytes ({cover_mime})", "dim")
                    else:
                        self._log("  Primary cover fetch failed.", "warn")

                # 2) iTunes + Deezer fallback — both verify the result's artistName
                # matches the query so we never embed a wrong-artist cover
                if not cover_data and artist and title:
                    self._log("  Looking up verified album cover (iTunes + Deezer)…", "dim")
                    alt_url = lookup_album_cover(artist, title, proxy=proxy)
                    if alt_url:
                        self._log(f"  Match: {alt_url[:60]}…", "dim")
                        cover_data, cover_mime = fetch_cover(alt_url)
                        if cover_data:
                            self._log(f"  Cover fetched: {len(cover_data)} bytes", "dim")
                    else:
                        self._log("  No verified match for this artist + title.", "warn")

                if not cover_data:
                    self._log("  No album cover available — file will have no thumbnail.", "warn")

            # Write FLAC tags + embedded picture (verbose failure reasons)
            ok, err = tag_flac_file(out_file,
                                    track_info if embed_meta else {},
                                    cover_data, cover_mime or "image/jpeg")
            if not ok:
                self._log(f"  ⚠ Tagging failed: {err}", "warn")

            # Rename to clean "Artist - Title.flac" if we have both
            final = out_file
            if embed_meta and artist and title:
                import re as _re
                safe    = _re.sub(r'[<>:"/\\|?*]', "_", f"{artist} - {title}")
                renamed = out_file.parent / f"{safe}{out_file.suffix}"
                try:
                    if out_file != renamed:
                        if renamed.exists():
                            renamed.unlink()
                        out_file.rename(renamed)
                    final = renamed
                except Exception:
                    pass

            # Verify the embed actually landed in the file
            if final.suffix.lower() == ".flac":
                info = flac_cover_info(final)
                if info.get("present"):
                    self._log(
                        f"  ✓ Cover embedded: {info['width']}×{info['height']}, "
                        f"{info['size']} bytes, {info['mime']}", "dim")
                else:
                    self._log(f"  ⚠ No cover in final file ({info.get('reason','unknown')})", "warn")

            return final

        try:
            Path(output_dir).mkdir(parents=True, exist_ok=True)

            if list_fmt:
                self._log("Fetching available formats…", "dim")
                with yt_dlp.YoutubeDL({"listformats": True, "quiet": True,
                                       **({"proxy": proxy} if proxy else {})}) as ydl:
                    info = ydl.extract_info(url, download=False)
                    for f in (info or {}).get("formats", []):
                        self._log(
                            f"  {f.get('format_id','?'):12s}  "
                            f"{f.get('ext','?'):6s}  {f.get('format_note','')}", "dim")
                self._log("Format listing complete.", "ok")
                # As above: a successful probe, but nothing was downloaded.
                self._skip_history = True
                self._emit("success")
                return

            # 1. yt-dlp
            self._provider("ytdlp", "active")
            self._log(f"Trying yt-dlp: {url}", "ok")
            logger = Logger(self._log)
            opts   = make_opts(url)
            opts["logger"] = logger
            drm_hit = False
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                self._provider("ytdlp", "ok")
                self._log(f"Done! Saved to: {output_dir}", "bright")
                self._emit("success")
                return
            except yt_dlp.utils.DownloadError as e:
                err = str(e)
                if is_drm_error(err) or any(is_drm_error(m) for m in logger.errors):
                    drm_hit = True
                    self._provider("ytdlp", "skip")
                    self._log("DRM detected — activating fallback chain…", "warn")
                elif "Aborted" in err:
                    self._provider("ytdlp", "fail"); self._log("Aborted.", "warn"); return
                else:
                    self._provider("ytdlp", "fail"); raise

            if not drm_hit or not s.get("auto_fallback", True):
                if not s.get("auto_fallback", True):
                    self._log("Auto-fallback disabled in Settings.", "warn")
                return

            # 2. Odesli
            self._provider("odesli", "active")
            self._log("Resolving via Odesli (song.link)…", "info")
            resolved = title = artist = None
            odesli   = OdesliResolver()
            try:
                resolved = odesli.resolve(clean_url(url), proxy=proxy)
                title    = (resolved.get("title") or "").strip()
                artist   = (resolved.get("artist") or "").strip()
                if title and artist:
                    self._log(f"Found: {artist} — {title}", "bright")
                    for plat, purl in odesli.all_urls(resolved):
                        self._log(f"  {plat:<14s}: {purl}", "dim")
                    self._provider("odesli", "ok")
                else:
                    self._log("Odesli returned no metadata for this track.", "warn")
                    self._provider("odesli", "fail")
            except Exception as e:
                self._provider("odesli", "fail")
                self._log(f"Odesli failed: {e}", "warn")

            # 2b. If Odesli has no metadata and source is Spotify, scrape directly
            if (not (title and artist)) and "open.spotify.com/track/" in url:
                self._log("Reading metadata from Spotify page directly…", "info")
                meta = fetch_spotify_metadata(url, proxy=proxy)
                if meta:
                    title  = meta["title"]
                    artist = meta["artist"]
                    self._log(f"Spotify says: {artist} — {title}", "bright")
                else:
                    self._log("Could not parse Spotify page either.", "warn")

            # 2c. Duplicate detection — once we know artist+title, check if the
            # destination file already exists and ask the user before downloading again.
            if artist and title:
                import re as _re
                safe = _re.sub(r'[<>:"/\\|?*]', "_", f"{artist} - {title}")
                existing = Path(output_dir) / f"{safe}.{audio_format}"
                if existing.exists():
                    self._log(f"File already exists: {existing.name}", "warn")
                    if not self._ask_user(
                        f"\"{existing.name}\" already exists in this folder. "
                        f"Download it again and overwrite?"):
                        self._log("Skipped (user kept existing file).", "ok")
                        self._provider("ytdlp", "skip")
                        return

            # 3. yt-dlp retry on resolved URLs
            if resolved:
                for plat, alt_url in odesli.all_urls(resolved):
                    if plat in ("spotify", "appleMusic"): continue
                    self._log(f"Trying {plat}: {alt_url}", "info")
                    lg2   = Logger(self._log)
                    opts2 = make_opts(alt_url)
                    opts2["logger"] = lg2
                    try:
                        with yt_dlp.YoutubeDL(opts2) as ydl:
                            ydl.download([alt_url])
                        self._provider("ytdlp", "ok")
                        self._log(f"Done via {plat}! Saved to: {output_dir}", "bright")
                        self._emit("success")
                        return
                    except Exception as e2:
                        self._log(f"  {plat} failed: {e2}", "warn")

            # 3.5 YouTube search — uses artist+title from Odesli or Spotify scrape,
            # bypasses DRM entirely. Uses our scraped metadata for the filename
            # (avoids "(Official Music Video)" bloat) and embeds the Spotify cover
            # art instead of the YouTube video thumbnail.
            if artist and title:
                import re as _re
                safe_name = _re.sub(r'[<>:"/\\|?*\n\r\t]', "_", f"{artist} - {title}").strip()
                search    = f"ytsearch1:{artist} - {title}"
                self._log(f"Searching YouTube: {artist} — {title}", "info")

                # If source was Spotify, fetch the album + cover URL now
                spotify_meta = None
                if "open.spotify.com" in url:
                    spotify_meta = fetch_spotify_metadata(url, proxy=proxy)

                # Strip yt-dlp's metadata + thumbnail postprocessors here — we'll do
                # both ourselves below with the clean Spotify data, and that avoids
                # any chance of a postprocessor crash hiding a successful download.
                tpl = str(Path(output_dir) / f"{safe_name}.%(ext)s")
                pps = [_audio_postproc()]
                opts3 = {
                    "format":         "bestaudio/best",
                    "outtmpl":        tpl,
                    "postprocessors": pps,
                    "writethumbnail": False,
                    "noplaylist":     True,
                    "progress_hooks": [ydl_hook],
                    "logger":         Logger(self._log),
                }
                if ffmpeg_dir: opts3["ffmpeg_location"] = str(ffmpeg_dir)
                if proxy:      opts3["proxy"]           = proxy

                yt_ok = False
                try:
                    with yt_dlp.YoutubeDL(opts3) as ydl:
                        ydl.download([search])
                    yt_ok = True
                except Exception as e3:
                    # Even if yt-dlp raised, check if the audio file actually
                    # exists — postprocessor errors fire after the file is saved.
                    ext = audio_format if audio_format != "ogg" else "ogg"
                    if (Path(output_dir) / f"{safe_name}.{ext}").exists():
                        yt_ok = True
                    else:
                        self._log(f"  YouTube search failed: {e3}", "warn")

                # 3.6 SoundCloud search fallback — if YouTube didn't produce a file
                if not yt_ok and artist and title:
                    self._log(f"Searching SoundCloud: {artist} — {title}", "info")
                    sc_search = f"scsearch1:{artist} - {title}"
                    opts_sc = dict(opts3)
                    opts_sc["logger"] = Logger(self._log)
                    try:
                        with yt_dlp.YoutubeDL(opts_sc) as ydl:
                            ydl.download([sc_search])
                        yt_ok = True
                    except Exception as e_sc:
                        ext_check = audio_format
                        if (Path(output_dir) / f"{safe_name}.{ext_check}").exists():
                            yt_ok = True
                        else:
                            self._log(f"  SoundCloud search failed: {e_sc}", "warn")

                if yt_ok:
                    # Find the produced file (account for ogg→.ogg naming)
                    candidates = list(Path(output_dir).glob(f"{safe_name}.*"))
                    audio_exts = {"flac", "mp3", "m4a", "ogg", "opus", "wav"}
                    final = next(
                        (p for p in candidates if p.suffix.lstrip(".").lower() in audio_exts),
                        None,
                    )

                    # Clean up leftover thumbnail files yt-dlp may have created
                    for p in candidates:
                        if p.suffix.lstrip(".").lower() in ("webp", "jpg", "jpeg", "png"):
                            try: p.unlink()
                            except Exception: pass

                    # Tag with mutagen using clean Spotify metadata + cover (FLAC only)
                    if final and final.suffix.lower() == ".flac" and (embed_meta or embed_thumb):
                        ti = {"title": title, "performer": {"name": artist}}
                        if spotify_meta:
                            ti["album"] = {
                                "title": spotify_meta.get("album", ""),
                                "image": {"large": spotify_meta.get("cover_url", "")},
                            }
                        tag_proxy_file(final, ti)

                    self._provider("ytdlp", "ok")
                    self._log(f"Done via YouTube! Saved: {final.name if final else safe_name}", "bright")
                    self._emit("success")
                    return

            # 4. SpotiFlac proxy (anonymous — no account needed)
            self._provider("proxy", "active")
            qobuz_id = None

            # Try to extract Qobuz track ID from Odesli result
            if resolved:
                qobuz_url = resolved.get("platforms", {}).get("qobuz", "")
                if qobuz_url:
                    qobuz_id = extract_qobuz_id(qobuz_url)

            # Fall back to anonymous Qobuz search
            qobuz_track_info = {}
            if not qobuz_id and artist and title:
                self._log("Looking up Qobuz track ID (anonymous search)…", "dim")
                try:
                    tracks = QobuzAPI().search_track_anon(f"{artist} {title}", proxy=proxy)
                    if tracks:
                        qobuz_track_info = tracks[0]
                        qobuz_id = str(tracks[0]["id"])
                        t_title  = tracks[0].get("title", "?")
                        t_artist = (tracks[0].get("performer") or {}).get("name", "?")
                        self._log(f"Qobuz match: {t_artist} — {t_title} (id {qobuz_id})", "dim")
                except Exception:
                    pass
            elif qobuz_id and artist and title:
                # We got the ID from Odesli URL — try to also fetch track metadata
                try:
                    tracks = QobuzAPI().search_track_anon(f"{artist} {title}", proxy=proxy)
                    if tracks:
                        qobuz_track_info = tracks[0]
                except Exception:
                    pass

            if qobuz_id:
                self._log(f"Trying SpotiFlac proxies (track {qobuz_id})…", "info")
                sf = SpotiflacProxy()
                fmt = s.get("qobuz_format", 6)
                out_file, svc = sf.try_download(
                    qobuz_id, Path(output_dir),
                    fmt_id=fmt, on_progress=self._progress, proxy=proxy)
                if out_file:
                    self._provider("proxy", "ok")
                    result = tag_proxy_file(out_file, qobuz_track_info)
                    final  = result if isinstance(result, Path) else out_file
                    self._log(f"Downloaded via {svc}: {final.name}", "bright")
                    self._log(f"Done! Saved to: {output_dir}", "bright")
                    self._emit("success")
                    return
                else:
                    self._provider("proxy", "fail")
                    self._log("All proxy services failed.", "warn")
            else:
                self._provider("proxy", "skip")
                self._log("No Qobuz track ID found — proxy skipped.", "warn")

            # 5. MusicBrainz ISRC
            self._provider("musicbrainz", "active")
            if not (artist and title):
                self._provider("musicbrainz", "skip")
                self._log("MusicBrainz skipped — no metadata available.", "warn")
            else:
                try:
                    self._log(f"MusicBrainz ISRC lookup: {artist} — {title}", "info")
                    mb   = MusicBrainz()
                    isrc = mb.best_isrc(title, artist, proxy=proxy)
                    if isrc:
                        self._log(f"ISRC: {isrc}", "bright")
                        self._provider("musicbrainz", "ok")
                        # Try anonymous Qobuz search by ISRC → proxy
                        try:
                            tracks = QobuzAPI().search_track_anon(f"isrc:{isrc}", proxy=proxy)
                            if tracks:
                                isrc_track = tracks[0]
                                isrc_id    = str(isrc_track["id"])
                                self._log(f"Proxy download via ISRC match (id {isrc_id})…", "info")
                                self._provider("proxy", "active")
                                sf  = SpotiflacProxy()
                                fmt = s.get("qobuz_format", 6)
                                out_file, svc = sf.try_download(
                                    isrc_id, Path(output_dir),
                                    fmt_id=fmt, on_progress=self._progress, proxy=proxy)
                                if out_file:
                                    self._provider("proxy", "ok")
                                    result = tag_proxy_file(out_file, isrc_track)
                                    final  = result if isinstance(result, Path) else out_file
                                    self._log(f"Saved: {final.name}", "bright")
                                    self._log(f"Done! Saved to: {output_dir}", "bright")
                                    self._emit("success")
                                    return
                                else:
                                    self._provider("proxy", "fail")
                                    self._log("Proxy failed on ISRC match.", "warn")
                            else:
                                self._log("No Qobuz results for ISRC.", "warn")
                        except Exception as qe:
                            self._log(f"ISRC→proxy failed: {qe}", "warn")
                    else:
                        self._log("No ISRC found in MusicBrainz.", "warn")
                        self._provider("musicbrainz", "fail")
                except Exception as me:
                    self._provider("musicbrainz", "fail")
                    self._log(f"MusicBrainz failed: {me}", "warn")

            self._log("All providers exhausted — could not download this track.", "err")

        except Exception as exc:
            msg = str(exc)
            if debug_enabled():
                debug_log(f"audio error for {redact_url(url)}\n{traceback.format_exc()}")
            self._log(friendly_dl_error(msg) or f"ERROR: {msg}", "err")
        # NOTE: _downloading flag + 'done' event are reset by the outer _worker
        # so album loops can keep going across multiple track downloads.

    def _video_browser_fallback(self, page_url, opts, output_dir):
        """When yt-dlp can't extract a site, drive a real browser to sniff the
        stream, then download that m3u8 with the correct Referer/Origin."""
        self._log("Trying browser grab (loading the page in a real browser)...", "warn")
        m3u8, ref, title = _browser_grab(page_url, self._log,
                                         should_abort=lambda: self._abort_flag)
        debug_log(f"browser grab {redact_url(page_url)} -> m3u8={redact_url(m3u8)} "
                  f"ref={redact_url(ref)} found={bool(m3u8)}")
        if self._abort_flag:
            self._log("Aborted.", "warn")
            return True   # nothing failed; the user stopped it
        if not m3u8:
            self._log("Browser grab found no downloadable stream "
                      "(the player may use real DRM).", "err")
            return False
        origin = f"{urlparse(ref).scheme}://{urlparse(ref).netloc}"
        opts2 = dict(opts)
        # Merge, don't replace: the original headers (and the impersonation
        # target, still on opts2) are what got us past the site's bot check.
        hdrs = {**opts.get("http_headers", {}), "Referer": ref, "Origin": origin}
        # Only pin the browser's UA when we are NOT impersonating. yt-dlp strips
        # a User-Agent only when it matches std_headers exactly, so forcing one
        # here would ship our UA over curl_cffi's unrelated TLS fingerprint —
        # the exact mismatch that makes strict CDNs answer 403/410.
        if not opts2.get("impersonate"):
            hdrs["User-Agent"] = _BROWSER_UA
        opts2["http_headers"] = hdrs
        # A bare m3u8 has no uploader/title of its own, so the default template
        # would yield "NA - <playlist-id>". Prefer the page title, else the slug.
        slug = urlparse(page_url).path.rstrip("/").rsplit("/", 1)[-1] or "video"
        safe = yt_dlp.utils.sanitize_filename(title or slug, restricted=False)
        # Plenty of player pages share one constant <title> ("Watch", the site
        # name), and the slug fallback collides just as easily (every series has
        # an "episode-7"). Without this, the second video would collide with the
        # first and yt-dlp would skip it while still reporting success.
        #
        # Compare against real filenames rather than a glob pattern: titles keep
        # their brackets through sanitize_filename, and glob would read the
        # "[1080p]" in "Watch [1080p] Ep 7" as a character class and match
        # nothing — silently disabling this guard on exactly the sites that need
        # it most.
        if Path(output_dir).exists():
            try:
                taken = {p.stem for p in Path(output_dir).iterdir() if p.is_file()}
            except OSError:
                taken = set()
            stem, n = safe, 2
            while safe in taken:
                safe = f"{stem} ({n})"
                n += 1
        opts2["outtmpl"] = str(Path(output_dir) / f"{safe}.%(ext)s")
        # Sniffed streams are HLS with hundreds of small fragments, and the
        # impersonated transport roughly halves per-connection throughput.
        # Fetching a few at a time turns a multi-hour download into minutes.
        # The value is inherited from opts (Settings); this only covers callers
        # that built opts without it.
        opts2.setdefault("concurrent_fragment_downloads", 5)
        # Inherited from the page-level opts but meaningless for a bare
        # manifest: there is no thumbnail or subtitle track to fetch.
        opts2["writethumbnail"] = False
        opts2["writesubtitles"] = False
        opts2["subtitleslangs"] = []
        opts2["postprocessors"] = [pp for pp in opts.get("postprocessors", [])
                                   if pp.get("key") != "EmbedThumbnail"]
        try:
            self._log("Found stream via browser - downloading...", "ok")
            self._provider("ytdlp", "active")
            with yt_dlp.YoutubeDL(opts2) as ydl:
                ydl.download([m3u8])
            self._provider("ytdlp", "ok")
            self._log(f"Done! Saved to: {output_dir}", "bright")
            self._emit("success")
            return True
        except Exception as e:
            self._provider("ytdlp", "fail")
            if debug_enabled():
                debug_log(f"browser-grab download failed for {redact_url(m3u8)}\n"
                          f"{traceback.format_exc()}")
            self._log(friendly_dl_error(str(e)) or f"ERROR: {e}", "err")
            return False

    def _worker_video(self, url, output_dir, video_format, quality,
                      embed_thumb, embed_meta, write_subs, list_formats=False):
        s          = cfg.load()
        proxy      = s.get("proxy") or None
        ffmpeg_dir = find_ffmpeg()

        def ydl_hook(d):
            if self._abort_flag:
                raise yt_dlp.utils.DownloadError("Aborted by user")
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done  = d.get("downloaded_bytes", 0)
                self._progress((done / total * 100) if total else 0)
            elif d["status"] == "finished":
                # Remember what was really written — history records the actual
                # file, which is the only way a skipped-but-reported-successful
                # download becomes visible.
                self._last_file = d.get("filename") or ""
                self._log(f"Downloaded: {Path(d['filename']).name}", "bright")
                self._progress(100)

        class Logger:
            def __init__(self, cb):
                self.cb = cb
            def debug(self, m):
                if not m.startswith("[debug]"): self.cb(m, "dim")
            def info(self, m):    self.cb(m, "dim")
            def warning(self, m): self.cb(m, "warn")
            def error(self, m):
                if not friendly_dl_error(m): self.cb(m, "err")

        # Build format string + merge container
        height = "" if quality == "best" else f"[height<={quality}]"
        if video_format == "mp4":
            fmt = (f"bestvideo{height}[ext=mp4]+bestaudio[ext=m4a]"
                   f"/bestvideo{height}+bestaudio"
                   f"/best{height}[ext=mp4]/best{height}/best")
            merge = "mp4"
        elif video_format == "mkv":
            fmt   = f"bestvideo{height}+bestaudio/best{height}/best"
            merge = "mkv"
        elif video_format == "webm":
            fmt = (f"bestvideo{height}[ext=webm]+bestaudio[ext=webm]"
                   f"/bestvideo{height}+bestaudio"
                   f"/best{height}[ext=webm]/best{height}/best")
            merge = "webm"
        else:  # best
            fmt   = f"bestvideo{height}+bestaudio/best{height}/best"
            merge = None

        pps = []
        if embed_meta:  pps.append({"key": "FFmpegMetadata", "add_metadata": True,
                                    "add_chapters": bool(s.get("embed_chapters"))})
        if embed_thumb: pps.append({"key": "EmbedThumbnail"})
        pps.extend(_sponsorblock_pps(s))

        tpl  = _outtmpl(output_dir, s.get("outtmpl_video"),
                        "%(uploader)s - %(title)s")
        opts = {
            "format":         fmt,
            "outtmpl":        tpl,
            "postprocessors": pps,
            "writethumbnail": embed_thumb,
            "writesubtitles": write_subs,
            "subtitleslangs": _sub_langs(s) if write_subs else [],
            "progress_hooks": [ydl_hook],
            "logger":         Logger(self._log),
            **_RETRY_OPTS,
            **_tuning_opts(s),
        }
        if merge:      opts["merge_output_format"] = merge
        if ffmpeg_dir: opts["ffmpeg_location"]     = str(ffmpeg_dir)
        if proxy:      opts["proxy"]               = proxy
        jar = site_cookies.cookie_file_for(url)
        if jar:
            opts["cookiefile"] = jar
            self._log(f"Using your saved sign-in for "
                      f"{site_cookies.site_key(url)}.", "info")
        imp = _impersonate_target()
        if imp:        opts["impersonate"]         = imp
        elif _impersonate_note:
            self._log(f"Note: {_impersonate_note}", "warn")

        try:
            Path(output_dir).mkdir(parents=True, exist_ok=True)

            if list_formats:
                self._log("Fetching available formats…", "dim")
                self._provider("ytdlp", "active")
                probe = {"listformats": True, "quiet": True,
                         **({"proxy": proxy} if proxy else {})}
                if jar:
                    probe["cookiefile"] = jar   # gated videos need it to probe
                with yt_dlp.YoutubeDL(probe) as ydl:
                    info = ydl.extract_info(url, download=False)
                for f in (info or {}).get("formats", []):
                    size = f.get("filesize") or f.get("filesize_approx") or 0
                    self._log(
                        f"  {str(f.get('format_id','?')):>6s}  "
                        f"{str(f.get('ext','?')):5s}  "
                        f"{str(f.get('resolution') or f.get('format_note') or ''):12s}  "
                        f"{(str(round(size/1048576)) + ' MB') if size else ''}", "dim")
                self._provider("ytdlp", "ok")
                self._log("Format listing complete — nothing downloaded.", "ok")
                # Counts as a successful job so the queue doesn't mark it
                # failed, but it is NOT a download and must not appear in the
                # history — a history full of things that fetched nothing is
                # exactly the noise history exists to avoid.
                self._skip_history = True
                self._emit("success")
                return

            self._provider("ytdlp", "active")
            q_label = "best" if quality == "best" else f"up to {quality}p"
            self._log(f"Downloading video ({video_format.upper()}, {q_label}): {url}", "ok")
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            self._provider("ytdlp", "ok")
            self._log(f"Done! Saved to: {output_dir}", "bright")
            self._emit("success")
        except yt_dlp.utils.DownloadError as e:
            self._provider("ytdlp", "fail")
            err = str(e)
            if debug_enabled():
                debug_log(f"video DownloadError for {redact_url(url)}\n{traceback.format_exc()}")
            if "Aborted" in err:
                self._log("Aborted.", "warn")
            elif is_drm_error(err):
                self._log("DRM-protected video — cannot bypass.", "err")
            # 410 belongs here too: sites that hide the stream behind JS and
            # fingerprint their edge answer the page fetch that way, and the
            # browser grab is the only thing that gets past it. 403 is
            # deliberately NOT here — on supported sites it almost always means
            # age-gate/login/region/expired-fragment, which a browser grab
            # cannot fix and would only stall on for minutes.
            elif re.search(r"Unsupported URL|Cloudflare|No video formats|Unable to extract"
                           r"|HTTP Error 410",
                           err, re.I):
                if not self._video_browser_fallback(url, opts, output_dir):
                    self._log(friendly_dl_error(err) or f"ERROR: {err}", "err")
            else:
                self._log(friendly_dl_error(err) or f"ERROR: {err}", "err")
        except Exception as exc:
            self._provider("ytdlp", "fail")
            msg = str(exc)
            if debug_enabled():
                debug_log(f"video error for {redact_url(url)}\n{traceback.format_exc()}")
            self._log(friendly_dl_error(msg) or f"ERROR: {msg}", "err")

    # ── Convert worker ───────────────────────────────────────────────────────

    def _worker_convert(self, files, output_dir, target, quality, opts):
        """
        Convert every file in one batch.

        Deliberately one job for the whole batch rather than one per file: a
        200-file folder would otherwise evict the entire download history
        (history.MAX is 300), fill a queue widget that shows six rows, and fire
        200 success banners. The cost is no per-file retry, so a failure here
        is logged and counted and the batch carries on — only an entirely
        fruitless batch is reported as failed.
        """
        ffmpeg_dir = find_ffmpeg()
        ffmpeg_exe = str(ffmpeg_dir / ("ffmpeg.exe" if sys.platform == "win32"
                                       else "ffmpeg")) if ffmpeg_dir else ""
        cat        = convert.category_for_target(target)
        collision  = str(opts.get("collision") or "rename")
        force      = bool(opts.get("force"))
        total      = len(files)
        default_q  = convert.DEFAULT_QUALITY.get(target)
        try:
            quality_is_default = int(quality) == default_q
        except (TypeError, ValueError):
            quality_is_default = True

        # Ask once, before any work, when the batch is big enough that somebody
        # may have pointed this at the wrong folder. The UI must already be
        # polling by now (startConvert calls beforeDownload first), or this
        # prompt would sit unanswered until the 120s timeout.
        if total > 25 and not self._ask_user(
                f"Convert {total} files to {target.upper()}? "
                f"This may take a while."):
            self._log("Cancelled.", "warn")
            self._abort_flag = True
            return

        plural = "s" if total != 1 else ""
        self._log(f"Converting {total} file{plural} to {target.upper()}…",
                  "bright")

        done = failed = skipped = 0
        for idx, src in enumerate(files, 1):
            if self._abort_flag:
                self._log("Aborted.", "warn")
                break
            src = Path(src)
            self._emit("job_status",
                       msg=f"Converting {idx} of {total}: {src.name}")

            if not src.exists():
                self._log(f"{src.name}: file is gone — skipped.", "warn")
                skipped += 1
                continue
            if convert.is_same_format(src, target) and not force:
                self._log(f"{src.name} is already {target.upper()} — skipped.",
                          "dim")
                skipped += 1
                continue

            try:
                dst = convert.resolve_output(src, output_dir, target, collision)
            except convert.SameFileError:
                self._log(f"{src.name}: that would overwrite the file itself. "
                          f"Choose a different output folder.", "err")
                failed += 1
                continue
            if dst is None:
                self._log(f"{src.name}: output exists — skipped.", "dim")
                skipped += 1
                continue

            tmp = convert.temp_path(dst)
            try:
                ok = self._convert_one(ffmpeg_exe, src, dst, tmp, cat, target,
                                       quality, opts, quality_is_default,
                                       idx, total)
            except Exception as exc:
                if debug_enabled():
                    debug_log(f"convert crashed on {src.name}\n"
                              f"{traceback.format_exc()}")
                self._log(f"{src.name}: {exc}", "err")
                ok = False
            finally:
                # Never leave a half-written file wearing a real name.
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass

            if ok:
                done += 1
                self._last_file = str(dst)
            elif self._abort_flag:
                break
            else:
                failed += 1

        if self._abort_flag:
            self._progress(0)
            return

        bits = [f"{done} converted"]
        if failed:
            bits.append(f"{failed} failed")
        if skipped:
            bits.append(f"{skipped} skipped")
        summary = ", ".join(bits)
        if done:
            self._progress(100)
            self._log(f"Done: {summary}.", "ok")
            # Marks the job 'done' for the queue; without it the runner records
            # the whole batch as failed.
            self._emit("success", path=self._last_file)
        elif not failed:
            # Everything was skipped and nothing went wrong — that is the
            # correct outcome for "convert this folder to mp3" on a folder
            # that is already mp3, so it must not be reported as a failure.
            self._progress(100)
            self._log(f"Nothing to do — {summary}.", "info")
            self._emit("success", path="")
            self._skip_history = True
        else:
            self._log(f"Nothing converted ({summary}).", "err")

    def _convert_one(self, ffmpeg_exe, src, dst, tmp, cat, target, quality,
                     opts, quality_is_default, idx, total) -> bool:
        """Convert a single file. Returns True on success."""
        # Images never touch ffmpeg — Pillow handles ICO and animation, which
        # ffmpeg either can't do or does badly.
        if cat == "image":
            self._progress((idx - 1) / total * 100)
            note = convert.convert_image(src, tmp, target, quality, opts)
            os.replace(tmp, dst)
            self._log(f"{src.name} → {dst.name}"
                      + (f" ({note})" if note else ""), "ok")
            self._progress(idx / total * 100)
            return True

        # Normalising needs the source's sample rate and bit depth, which
        # probe() doesn't carry — without the rate, loudnorm's -af omits
        # aresample and writes a 192 kHz file roughly 4x the size. Same
        # subprocess cost either way; the difference is only in the parsing.
        normalising = bool(convert.loudnorm_preset(opts))
        info = (convert.probe_full(ffmpeg_exe, src) if normalising
                else convert.probe(ffmpeg_exe, src))
        copy_v, copy_a = convert.can_stream_copy(target, info, opts,
                                                 quality_is_default)

        def on_pct(p):
            # Scale this file's progress into the batch's share of the bar, so
            # a 40-file batch fills the bar once rather than 40 times.
            base = (idx - 1) / total * 100
            span = 100 / total
            self._progress(base + (span * (p / 100) if p is not None else 0))

        if normalising:
            self._log(f"{src.name}: measuring loudness…", "info")
            measured = convert.measure_loudness(
                ffmpeg_exe, src, convert.loudnorm_preset(opts),
                info.get("duration", 0),
                on_pct=lambda p: on_pct(p * 0.3 if p is not None else None),
                should_abort=lambda: self._abort_flag,
                on_proc=lambda p: setattr(self, "_convert_proc", p))
            if self._abort_flag:
                self._log("Aborted.", "warn")
                return False
            if not measured:
                self._log(f"{src.name}: couldn't measure it — levelling by "
                          f"estimate instead.", "warn")
            # One opts dict serves all 40 files in a batch. Rebinding rather
            # than assigning into it keeps this file's measurement out of the
            # next file's command.
            opts = {**opts, "loudnormMeasured": measured}

        cmd = convert.build_cmd(ffmpeg_exe, src, tmp, target, quality, opts,
                                info, copy_v, copy_a)
        rc, err = convert.run_ffmpeg(
            cmd, info.get("duration", 0), on_pct=on_pct,
            should_abort=lambda: self._abort_flag,
            on_proc=lambda p: setattr(self, "_convert_proc", p))

        # Any failure of a stream copy is retried as a full re-encode. ffmpeg
        # words container/codec rejections too many ways to pattern-match, and
        # the worst case is one wasted fast attempt.
        if rc not in (0, -1) and (copy_v or copy_a) and not self._abort_flag:
            self._log(f"{src.name}: fast copy not possible — re-encoding…",
                      "warn")
            cmd = convert.build_cmd(ffmpeg_exe, src, tmp, target, quality,
                                    opts, info, False, False)
            rc, err = convert.run_ffmpeg(
                cmd, info.get("duration", 0), on_pct=on_pct,
                should_abort=lambda: self._abort_flag,
                on_proc=lambda p: setattr(self, "_convert_proc", p))

        if rc == -1 or self._abort_flag:
            self._log("Aborted.", "warn")
            return False
        if rc != 0 or not tmp.exists():
            tail = (err or "").strip().splitlines()
            detail = tail[-1] if tail else f"ffmpeg exited {rc}"
            self._log(f"{src.name}: {detail}", "err")
            if debug_enabled():
                debug_log(f"convert failed: {' '.join(cmd)}\n{err}")
            return False

        os.replace(tmp, dst)
        how = ("fast copy" if copy_v and copy_a
               else "copied audio" if copy_a and cat == "audio"
               else "")
        self._log(f"{src.name} → {dst.name}" + (f" ({how})" if how else ""),
                  "ok")
        return True
