"""
Python API exposed to the pywebview JS frontend.
All public methods are callable from JS via window.pywebview.api.<method>().
Progress updates are pushed into a queue and drained by JS polling poll_updates().
"""

import os
import queue
import re
import threading
import traceback
from pathlib import Path
import sys

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

import settings as cfg
from providers import (OdesliResolver, QobuzAPI, SpotiflacProxy,
                       MusicBrainz, is_drm_error, friendly_dl_error,
                       extract_qobuz_id, fetch_spotify_metadata,
                       lookup_album_cover, fetch_spotify_album_tracks,
                       is_album_or_playlist_url, clean_url)
from utils import (find_ffmpeg, tag_flac_file, flac_cover_info,
                   debug_log, debug_enabled, set_debug, debug_log_path)
from version import __version__, GITHUB_OWNER, GITHUB_REPO

DEFAULT_OUT       = str(Path.home() / "Music"  / "Swiss Downloads")
DEFAULT_VIDEO_OUT = str(Path.home() / "Videos" / "Swiss Downloads")


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

    def _attempt(p):
        found = []  # (url, referer)
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
                # Return whatever was already sniffed during navigation — a page
                # can fire the manifest and still fail to settle.
                return found, None

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
        return found, title

    for i in range(1, attempts + 1):
        if aborted():
            return None, None, None
        found, title = [], None
        try:
            with sync_playwright() as p:
                found, title = _attempt(p)
        except Exception as e:
            # Crashes (driver spawn, "Target crashed", page closed mid-wait) are
            # exactly what the retries are for, so keep going rather than bail.
            log(f"Browser grab error: {str(e).splitlines()[0][:120]}", "warn")
        if found:
            for url, ref in found:
                if "master" in url.lower():
                    return url, ref, title
            return found[0][0], found[0][1], title
        if i < attempts and not aborted():
            log(f"Browser grab found nothing (attempt {i}/{attempts}) — retrying...", "warn")

    return None, None, None


class API:
    def __init__(self):
        self._window          = None
        self._updates: queue.Queue = queue.Queue()
        self._downloading     = False
        self._abort_flag      = False
        # Synchronous-prompt support: worker thread blocks on _prompt_event until JS replies
        self._prompt_event    = threading.Event()
        self._prompt_response = True
        set_debug(cfg.load().get("debug_log", False))
        debug_log(f"--- Swiss Downloader {__version__} started "
                  f"(log: {debug_log_path()}) ---")

    def set_window(self, window):
        self._window = window

    # ── Window controls ───────────────────────────────────────────────────────

    def minimize_window(self):
        if self._window: self._window.minimize()

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
            "autoFallback":     s.get("auto_fallback", True),
            "qobuzFormat":      s.get("qobuz_format", 6),
            "proxy":            s.get("proxy", ""),
            "debugLog":         s.get("debug_log", False),
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
                          ("debugLog",     "debug_log")]:
            if key in data:
                val = data[key]
                s[dest] = int(val) if dest == "qobuz_format" else val
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

    # ── Download ──────────────────────────────────────────────────────────────

    def start_download(self, url: str, output_dir: str,
                       quality: int, keep_original: bool,
                       list_formats: bool,
                       embed_thumb: bool = True,
                       embed_meta:  bool = True,
                       audio_format: str = "flac") -> dict:
        if self._downloading:
            return {"ok": False, "msg": "Already downloading."}
        url = url.strip()
        if not url:
            return {"ok": False, "msg": "No URL provided."}
        if yt_dlp is None:
            return {"ok": False, "msg": "yt-dlp not installed."}

        self._downloading = True
        self._abort_flag  = False
        threading.Thread(
            target=self._worker,
            args=(url, output_dir, int(quality), bool(keep_original),
                  bool(list_formats), bool(embed_thumb), bool(embed_meta),
                  str(audio_format).lower()),
            daemon=True,
        ).start()
        return {"ok": True}

    def start_video_download(self, url: str, output_dir: str,
                             video_format: str, quality: str,
                             embed_thumb: bool = True,
                             embed_meta:  bool = True,
                             write_subs:  bool = False) -> dict:
        if self._downloading:
            return {"ok": False, "msg": "Already downloading."}
        url = url.strip()
        if not url:
            return {"ok": False, "msg": "No URL provided."}
        if yt_dlp is None:
            return {"ok": False, "msg": "yt-dlp not installed."}

        self._downloading = True
        self._abort_flag  = False
        threading.Thread(
            target=self._worker_video,
            args=(url, output_dir, str(video_format).lower(),
                  str(quality), bool(embed_thumb), bool(embed_meta),
                  bool(write_subs)),
            daemon=True,
        ).start()
        return {"ok": True}

    def abort_download(self) -> dict:
        self._abort_flag = True
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
        self._updates.put({"kind": kind, **kwargs})

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
        finally:
            self._downloading = False
            self._emit("done")

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
            tpl = str(Path(output_dir) / "%(artist,uploader)s - %(title)s.%(ext)s")
            pps = [_audio_postproc()]
            if embed_meta:
                pps.append({"key": "FFmpegMetadata", "add_metadata": True})
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
            }
            if ffmpeg_dir: opts["ffmpeg_location"] = str(ffmpeg_dir)
            if proxy:      opts["proxy"] = proxy
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
                debug_log(f"audio error for {url}\n{traceback.format_exc()}")
            self._log(friendly_dl_error(msg) or f"ERROR: {msg}", "err")
        # NOTE: _downloading flag + 'done' event are reset by the outer _worker
        # so album loops can keep going across multiple track downloads.

    def _video_browser_fallback(self, page_url, opts, output_dir):
        """When yt-dlp can't extract a site, drive a real browser to sniff the
        stream, then download that m3u8 with the correct Referer/Origin."""
        self._log("Trying browser grab (loading the page in a real browser)...", "warn")
        m3u8, ref, title = _browser_grab(page_url, self._log,
                                         should_abort=lambda: self._abort_flag)
        debug_log(f"browser grab {page_url} -> m3u8={m3u8} ref={ref} title={title!r}")
        if self._abort_flag:
            self._log("Aborted.", "warn")
            return True   # nothing failed; the user stopped it
        if not m3u8:
            self._log("Browser grab found no downloadable stream "
                      "(the player may use real DRM).", "err")
            return False
        from urllib.parse import urlparse
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
        # name). Without this, the second video would collide with the first and
        # yt-dlp would skip it while still reporting success.
        if title and Path(output_dir).exists():
            stem, n = safe, 2
            while any(p.stem == safe for p in Path(output_dir).glob(f"{stem}*")):
                safe = f"{stem} ({n})"
                n += 1
        opts2["outtmpl"] = str(Path(output_dir) / f"{safe}.%(ext)s")
        # Sniffed streams are HLS with hundreds of small fragments, and the
        # impersonated transport roughly halves per-connection throughput.
        # Fetching a few at a time turns a multi-hour download into minutes.
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
                debug_log(f"browser-grab download failed for {m3u8}\n{traceback.format_exc()}")
            self._log(friendly_dl_error(str(e)) or f"ERROR: {e}", "err")
            return False

    def _worker_video(self, url, output_dir, video_format, quality,
                      embed_thumb, embed_meta, write_subs):
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
        if embed_meta:  pps.append({"key": "FFmpegMetadata", "add_metadata": True})
        if embed_thumb: pps.append({"key": "EmbedThumbnail"})

        tpl  = str(Path(output_dir) / "%(uploader)s - %(title)s.%(ext)s")
        opts = {
            "format":         fmt,
            "outtmpl":        tpl,
            "postprocessors": pps,
            "writethumbnail": embed_thumb,
            "writesubtitles": write_subs,
            "subtitleslangs": ["en", "en-US"] if write_subs else [],
            "progress_hooks": [ydl_hook],
            "logger":         Logger(self._log),
        }
        if merge:      opts["merge_output_format"] = merge
        if ffmpeg_dir: opts["ffmpeg_location"]     = str(ffmpeg_dir)
        if proxy:      opts["proxy"]               = proxy
        imp = _impersonate_target()
        if imp:        opts["impersonate"]         = imp
        elif _impersonate_note:
            self._log(f"Note: {_impersonate_note}", "warn")

        try:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
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
                debug_log(f"video DownloadError for {url}\n{traceback.format_exc()}")
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
                debug_log(f"video error for {url}\n{traceback.format_exc()}")
            self._log(friendly_dl_error(msg) or f"ERROR: {msg}", "err")
        finally:
            self._downloading = False
            self._emit("done")
