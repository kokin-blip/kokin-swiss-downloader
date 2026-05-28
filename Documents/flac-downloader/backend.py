"""
Python API exposed to the pywebview JS frontend.
All public methods are callable from JS via window.pywebview.api.<method>().
Progress updates are pushed into a queue and drained by JS polling poll_updates().
"""

import queue
import threading
from pathlib import Path
import sys

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

import settings as cfg
from providers import (OdesliResolver, QobuzAPI, SpotiflacProxy,
                       MusicBrainz, is_drm_error, extract_qobuz_id,
                       fetch_spotify_metadata, clean_url)
from utils import find_ffmpeg, tag_flac_file
from version import __version__, GITHUB_OWNER, GITHUB_REPO

DEFAULT_OUT       = str(Path.home() / "Music"  / "Swiss Downloads")
DEFAULT_VIDEO_OUT = str(Path.home() / "Videos" / "Swiss Downloads")


class API:
    def __init__(self):
        self._window      = None
        self._updates:  queue.Queue = queue.Queue()
        self._downloading = False
        self._abort_flag  = False

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
                          ("proxy",        "proxy")]:
            if key in data:
                val = data[key]
                s[dest] = int(val) if dest == "qobuz_format" else val
        cfg.save(s)
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
                           url=info["url"])
        threading.Thread(target=_run, daemon=True).start()

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

    def _progress(self, pct: float):
        self._emit("progress", pct=round(float(pct), 1))

    def _provider(self, key: str, state: str):
        self._emit("provider", key=key, state=state)

    def _worker(self, url, output_dir, quality, keep, list_fmt,
                embed_thumb=True, embed_meta=True, audio_format="flac"):
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
                self._log("Converting to FLAC…", "dim")
                self._progress(100)

        class Logger:
            def __init__(self, cb):
                self.cb = cb; self.errors = []
            def debug(self, m):
                if not m.startswith("[debug]"): self.cb(m, "dim")
            def info(self, m):   self.cb(m, "dim")
            def warning(self, m): self.cb(m, "warn")
            def error(self, m):  self.cb(m, "err"); self.errors.append(m)

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
            """Tag a proxy-downloaded FLAC with metadata + optional cover art."""
            if not (embed_meta or embed_thumb):
                return
            cover_data, cover_mime = None, "image/jpeg"
            if embed_thumb:
                cover_url = ((track_info.get("album") or {})
                             .get("image", {}).get("large", ""))
                cover_data, cover_mime = fetch_cover(cover_url)
            tag_flac_file(out_file,
                          track_info if embed_meta else {},
                          cover_data, cover_mime or "image/jpeg")
            if embed_meta:
                artist = (track_info.get("performer") or {}).get("name", "")
                title  = track_info.get("title", "")
                if artist and title:
                    import re as _re
                    safe = _re.sub(r'[<>:"/\\|?*]', "_", f"{artist} - {title}")
                    renamed = out_file.parent / f"{safe}{out_file.suffix}"
                    try:
                        out_file.rename(renamed)
                        return renamed
                    except Exception:
                        pass
            return out_file

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
            self._log(f"ERROR: {exc}", "err")
        finally:
            self._downloading = False
            self._emit("done")

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
            def error(self, m):   self.cb(m, "err")

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

        try:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            self._provider("ytdlp", "active")
            q_label = "best" if quality == "best" else f"up to {quality}p"
            self._log(f"Downloading video ({video_format.upper()}, {q_label}): {url}", "ok")
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            self._provider("ytdlp", "ok")
            self._log(f"Done! Saved to: {output_dir}", "bright")
        except yt_dlp.utils.DownloadError as e:
            self._provider("ytdlp", "fail")
            err = str(e)
            if "Aborted" in err:
                self._log("Aborted.", "warn")
            elif is_drm_error(err):
                self._log("DRM-protected video — cannot bypass.", "err")
            else:
                self._log(f"ERROR: {err}", "err")
        except Exception as exc:
            self._provider("ytdlp", "fail")
            self._log(f"ERROR: {exc}", "err")
        finally:
            self._downloading = False
            self._emit("done")
