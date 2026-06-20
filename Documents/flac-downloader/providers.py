"""
Provider fallback chain for DRM-protected sources.

Flow:
  1. Odesli (song.link) — resolves any music URL to all platform equivalents (free, no auth)
  2. QobuzAPI           — direct download via Qobuz API (needs subscription + credentials)
  3. MusicBrainz        — free metadata / ISRC lookup for search fallback

Privacy:
  All connections use HTTPS.
  The source URL is sent to Odesli only when DRM fallback is triggered.
  Qobuz credentials are never stored here; they are passed in at call time.
  An optional proxy URL can be passed to every network call.

FOR LEGAL USE ONLY. Only download content you are entitled to.
"""

import urllib.request
import urllib.parse
import urllib.error
import json
import hashlib
import time
import re
import sys
from pathlib import Path
from typing import Optional

QOBUZ_BASE  = "https://www.qobuz.com/api.json/0.2"
ODESLI_BASE = "https://api.odesli.co"
MB_BASE     = "https://musicbrainz.org/ws/2"

_SPOTIFLAC_DIR = Path.home() / ".spotiflac"

# Qobuz web-app credentials (extracted from public JS bundle; not user-specific)
_QOBUZ_CRED_FILE = Path.home() / ".spotiflac" / "qobuz-api-credentials.json"
_DEFAULT_APP_ID  = "712109809"
_DEFAULT_SECRET  = "589be88e4538daea11f509d29e4a23b1"

# DRM error patterns from yt-dlp
_DRM_PATTERNS = [
    r"\[DRM\]",
    r"DRM.{0,10}protected",
    r"DRM protection",
    r"is not available",
]


def is_drm_error(msg: str) -> bool:
    return any(re.search(p, msg, re.IGNORECASE) for p in _DRM_PATTERNS)


_HTTP_ERRORS = [
    (r'HTTP Error 410',           "This content has been deleted or removed from the site."),
    (r'HTTP Error 404',           "Content not found — the URL may be wrong, or it was removed."),
    (r'HTTP Error 403',           "Access denied — the site blocked this request (may require login or region)."),
    (r'HTTP Error 401',           "Login required — this content is behind a paywall or members-only."),
    (r'HTTP Error 429',           "Rate limited — the site is blocking too many requests. Try again later."),
    (r'HTTP Error 5\d\d',         "The site returned a server error. Try again later."),
    (r'private video',            "This video is private."),
    (r'members.only|members only',  "This content is members-only."),
    (r'age.?restrict',            "Age-restricted content — yt-dlp could not access it without login."),
    (r'This video is unavailable',"This video is unavailable."),
    (r'Video unavailable',        "Video unavailable."),
    (r'confirm your age',         "Age verification required — cannot download without login cookies."),
    (r'geo.?block|not available in your country', "This content is geo-blocked in your region."),
]

def friendly_dl_error(msg: str) -> str:
    """Return a short human-readable message for common yt-dlp download errors."""
    for pattern, friendly in _HTTP_ERRORS:
        if re.search(pattern, msg, re.IGNORECASE):
            return friendly
    return None


def extract_qobuz_id(url: str) -> Optional[str]:
    """Extract numeric track ID from a Qobuz URL (open.qobuz.com/track/12345…)."""
    m = re.search(r'qobuz\.com/(?:[a-z\-]+/)?track/(\d+)', url)
    return m.group(1) if m else None


def _norm_for_match(s: str) -> str:
    """Lowercase, strip whitespace/punctuation — for fuzzy artist comparison."""
    return re.sub(r'[^a-z0-9]', '', (s or "").lower())


def _artist_matches(query_artist: str, candidate_artist: str) -> bool:
    """
    True if the candidate's artist name plausibly matches the query.
    Accepts substring matches in either direction so 'lucki' matches
    'LUCKI', 'A Boogie Wit da Hoodie' matches 'A Boogie wit da Hoodie',
    etc. — but rejects unrelated artists like 'Bob Dylan' for 'LUCKI'.
    """
    q = _norm_for_match(query_artist)
    c = _norm_for_match(candidate_artist)
    if not q or not c:
        return False
    return q in c or c in q


def fetch_itunes_cover_url(artist: str, title: str,
                           proxy: Optional[str] = None) -> Optional[str]:
    """
    Search iTunes for the song and return the album-cover URL ONLY if the
    matching result's artistName actually matches the requested artist.
    This prevents iTunes returning a wrong-artist match (e.g. Bob Dylan
    for an underground rapper iTunes doesn't carry) from poisoning our
    cover art.
    """
    if not (artist and title):
        return None
    q = urllib.parse.urlencode({
        "term":   f"{artist} {title}",
        "entity": "song",
        "limit":  10,
        "media":  "music",
    })
    try:
        data = _get(f"https://itunes.apple.com/search?{q}", proxy=proxy)
    except Exception:
        return None
    for r in data.get("results", []):
        if _artist_matches(artist, r.get("artistName", "")):
            art = r.get("artworkUrl100", "")
            if art:
                return art.replace("100x100", "1000x1000")
    return None


def fetch_deezer_cover_url(artist: str, title: str,
                           proxy: Optional[str] = None) -> Optional[str]:
    """
    Search Deezer for the song and return the verified album cover URL.
    Deezer has substantially better coverage of underground / hip-hop /
    indie catalogs than iTunes, so this picks up many tracks iTunes misses.
    No auth required.
    """
    if not (artist and title):
        return None
    q = urllib.parse.urlencode({"q": f'artist:"{artist}" track:"{title}"'})
    try:
        data = _get(f"https://api.deezer.com/search?{q}&limit=10", proxy=proxy)
    except Exception:
        return None
    for r in data.get("data", []):
        cand = (r.get("artist") or {}).get("name", "")
        if _artist_matches(artist, cand):
            alb = r.get("album") or {}
            # Deezer covers: cover_xl=1000, cover_big=500, cover_medium=250
            return alb.get("cover_xl") or alb.get("cover_big") or alb.get("cover")
    return None


def lookup_album_cover(artist: str, title: str,
                       proxy: Optional[str] = None) -> Optional[str]:
    """
    Try iTunes first, then Deezer. Returns a verified album-cover URL or None.
    'Verified' means the source's artist name matches the query artist —
    never returns a wrong-artist match.
    """
    return (fetch_itunes_cover_url(artist, title, proxy=proxy)
            or fetch_deezer_cover_url(artist, title, proxy=proxy))


def is_album_or_playlist_url(url: str) -> bool:
    """True if the URL is an album / playlist / set rather than a single track."""
    url = (url or "").lower()
    return any(p in url for p in (
        "open.spotify.com/album/",
        "open.spotify.com/playlist/",
        "youtube.com/playlist",
        "youtu.be/playlist",
        "music.youtube.com/playlist",
        "bandcamp.com/album/",
        "/sets/",                    # SoundCloud sets
        "music.apple.com/" + ""      # plus "/album/" check below
    )) or ("music.apple.com" in url and "/album/" in url and "?i=" not in url)


def fetch_spotify_album_tracks(url: str,
                               proxy: Optional[str] = None) -> list[dict]:
    """
    Scrape track entries from an open.spotify.com album OR playlist page.
    Returns a list of {"url": str, "title": str, "artist": str} dicts.
    Reads JSON-LD MusicAlbum.track / MusicPlaylist.track when present,
    falling back to all '/track/<id>' references in the HTML.
    """
    if not any(s in url for s in ("/album/", "/playlist/")):
        return []
    try:
        req = urllib.request.Request(
            clean_url(url),
            headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
        )
        with _opener(proxy).open(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return []

    tracks: list[dict] = []

    # 1. JSON-LD MusicAlbum / MusicPlaylist
    for blob in re.findall(
            r'<script[^>]+type="application/ld\+json"[^>]*>(.+?)</script>',
            html, re.DOTALL):
        try:
            data = json.loads(blob)
        except Exception:
            continue
        if isinstance(data, list):
            data = next((d for d in data if isinstance(d, dict)), {})
        if not isinstance(data, dict):
            continue
        track_list = data.get("track") or data.get("tracks") or []
        if isinstance(track_list, dict):
            track_list = [track_list]
        for t in track_list:
            if not isinstance(t, dict):
                continue
            t_url = t.get("url", "") or t.get("@id", "")
            if not t_url or "open.spotify.com/track/" not in t_url:
                continue
            ba = t.get("byArtist") or []
            if isinstance(ba, dict):
                t_artist = ba.get("name", "")
            elif isinstance(ba, list) and ba:
                t_artist = ", ".join(a.get("name", "") for a in ba if isinstance(a, dict)).strip(", ")
            else:
                t_artist = ""
            tracks.append({
                "url":    clean_url(t_url),
                "title":  t.get("name", ""),
                "artist": t_artist,
            })
        if tracks:
            break

    # 2. Fallback: pull unique track IDs out of the raw HTML
    if not tracks:
        ids = []
        for tid in re.findall(r'/track/([A-Za-z0-9]{22})', html):
            if tid not in ids:
                ids.append(tid)
        for tid in ids:
            tracks.append({
                "url":    f"https://open.spotify.com/track/{tid}",
                "title":  "",
                "artist": "",
            })

    return tracks


def clean_url(url: str) -> str:
    """Strip tracking params like Spotify's ?si=... that confuse some APIs."""
    return re.sub(r'\?(si|igsh|utm_[^=]+|fbclid)=[^&]*(&|$)', '', url).rstrip('?&')


def fetch_spotify_metadata(url: str, proxy: Optional[str] = None) -> Optional[dict]:
    """
    Scrape track metadata from an open.spotify.com track page.
    No auth required — pulls from the page's JSON-LD + og:* meta tags.

    Returns {"title", "artist", "album", "cover_url"} or None.
    """
    if "open.spotify.com/track/" not in url:
        return None
    try:
        req = urllib.request.Request(
            clean_url(url),
            headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
        )
        with _opener(proxy).open(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    title = artist = album = cover_url = ""

    # 1. JSON-LD (most reliable — structured schema.org MusicRecording)
    for blob in re.findall(
            r'<script[^>]+type="application/ld\+json"[^>]*>(.+?)</script>',
            html, re.DOTALL):
        try:
            data = json.loads(blob)
        except Exception:
            continue
        if isinstance(data, list):
            data = next((d for d in data if isinstance(d, dict)), {})
        if not isinstance(data, dict):
            continue

        if not title:
            title = data.get("name", "")
        if not artist:
            ba = data.get("byArtist") or []
            if isinstance(ba, dict):
                artist = ba.get("name", "")
            elif isinstance(ba, list) and ba:
                artist = ", ".join(a.get("name", "") for a in ba if isinstance(a, dict)).strip(", ")
        if not album:
            in_album = data.get("inAlbum") or {}
            if isinstance(in_album, dict):
                album = in_album.get("name", "")
        if not cover_url:
            img = data.get("image") or data.get("thumbnailUrl") or ""
            if isinstance(img, list):
                cover_url = img[0] if img else ""
            elif isinstance(img, dict):
                cover_url = img.get("url", "") or img.get("contentUrl", "")
            else:
                cover_url = img

        if title and artist:
            break

    # 2. og: tag fallback for any missing field
    if not (title and artist):
        m_t = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
        m_d = re.search(r'<meta\s+property="og:description"\s+content="([^"]+)"', html)
        if m_t and not title:
            title = m_t.group(1).strip()
        if m_d and not artist:
            desc  = m_d.group(1)
            parts = [p.strip() for p in re.split(r'\s+[·•]\s+', desc) if p.strip()]
            for p in parts:
                if p.lower() in ("song", "single", "ep", "album") or p.isdigit():
                    continue
                if p.lower().startswith("listen to "):
                    continue
                artist = p
                break

    if not cover_url:
        m_img = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
        if m_img:
            cover_url = m_img.group(1)

    if title and artist:
        return {
            "title":     title.strip(),
            "artist":    artist.strip(),
            "album":     album.strip(),
            "cover_url": cover_url.strip(),
        }
    return None


# ── HTTP helpers ──────────────────────────────────────────────────────────────

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _opener(proxy: str | None) -> urllib.request.OpenerDirector:
    handlers: list = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))   # disable env-var proxies
    return urllib.request.build_opener(*handlers)


def _get(url: str, headers: dict | None = None,
         proxy: str | None = None) -> dict:
    h = {"User-Agent": _UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with _opener(proxy).open(req, timeout=15) as r:
        return json.loads(r.read().decode())


def _post(url: str, data: dict,
          headers: dict | None = None,
          proxy: str | None = None) -> dict:
    h = {"User-Agent": _UA, "Content-Type": "application/x-www-form-urlencoded"}
    if headers:
        h.update(headers)
    encoded = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=encoded, headers=h)
    with _opener(proxy).open(req, timeout=15) as r:
        return json.loads(r.read().decode())


# ── Odesli (song.link) ────────────────────────────────────────────────────────

class OdesliResolver:
    """
    Resolves any music URL (Spotify, Apple Music, Tidal, YouTube, …)
    to equivalent URLs on every other platform via the song.link API.
    Free — no authentication required.

    Privacy: the source URL is sent to api.odesli.co over HTTPS.
    """

    PRIORITY = ["qobuz", "tidal", "soundcloud", "youtube", "bandcamp", "amazonMusic"]

    def resolve(self, url: str, proxy: str | None = None) -> dict:
        """
        Returns:
          { "title": str, "artist": str,
            "platforms": { "qobuz": "https://...", ... } }
        Raises RuntimeError on failure.
        """
        params = urllib.parse.urlencode({"url": url, "userCountry": "US"})
        try:
            data = _get(f"{ODESLI_BASE}/resolve?{params}", proxy=proxy)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Odesli HTTP {e.code}: {e.reason}")
        except Exception as e:
            raise RuntimeError(f"Odesli lookup failed: {e}")

        platforms: dict[str, str] = {
            p: info["url"]
            for p, info in data.get("linksByPlatform", {}).items()
            if "url" in info
        }

        title = artist = ""
        uid = data.get("entityUniqueId", "")
        for eid, entity in data.get("entitiesByUniqueId", {}).items():
            if eid == uid:
                title  = entity.get("title", "")
                artist = entity.get("artistName", "")
                break

        return {"title": title, "artist": artist, "platforms": platforms}

    def best_url(self, resolved: dict) -> tuple[str, str] | None:
        for p in self.PRIORITY:
            if p in resolved["platforms"]:
                return p, resolved["platforms"][p]
        return None

    def all_urls(self, resolved: dict) -> list[tuple[str, str]]:
        seen, result = set(), []
        for p in self.PRIORITY:
            if p in resolved["platforms"]:
                result.append((p, resolved["platforms"][p]))
                seen.add(p)
        for p, u in resolved["platforms"].items():
            if p not in seen:
                result.append((p, u))
        return result


# ── Qobuz API ─────────────────────────────────────────────────────────────────

class QobuzAPI:
    """
    Direct Qobuz API wrapper. Requires an active Qobuz subscription.

    App credentials (app_id / app_secret) are the Qobuz web-app's public
    credentials, read from ~/.spotiflac/qobuz-api-credentials.json when
    available, otherwise using known defaults.

    User credentials (email / password) are accepted as arguments and
    never stored by this class.

    Privacy: login and search requests are sent to qobuz.com over HTTPS.
    Your IP address will be visible to Qobuz (same as using their website).
    """

    FORMAT_MAP = {
        "FLAC_16":     6,
        "FLAC_24_96":  7,
        "FLAC_24_192": 27,
        "MP3_320":     5,
    }
    DEFAULT_FORMAT = 6

    def __init__(self):
        creds = {}
        if _QOBUZ_CRED_FILE.exists():
            try:
                creds = json.loads(_QOBUZ_CRED_FILE.read_text())
            except Exception:
                pass
        self.app_id     = creds.get("app_id",     _DEFAULT_APP_ID)
        self.app_secret = creds.get("app_secret", _DEFAULT_SECRET)

    def login(self, email: str, password: str,
              proxy: str | None = None) -> str:
        """Authenticate and return a user_auth_token."""
        try:
            data = _post(f"{QOBUZ_BASE}/user/login", {
                "username": email,
                "password": password,
                "app_id":   self.app_id,
            }, proxy=proxy)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Qobuz login failed (HTTP {e.code}) — check credentials")
        token = data.get("user_auth_token")
        if not token:
            raise RuntimeError("Qobuz login failed — no token returned")
        return token

    def search_track(self, query: str, token: str,
                     limit: int = 5, proxy: str | None = None) -> list[dict]:
        params = urllib.parse.urlencode({
            "query":           query,
            "limit":           limit,
            "app_id":          self.app_id,
            "user_auth_token": token,
        })
        data = _get(f"{QOBUZ_BASE}/track/search?{params}", proxy=proxy)
        return data.get("tracks", {}).get("items", [])

    def search_by_isrc(self, isrc: str, token: str,
                       proxy: str | None = None) -> list[dict]:
        return self.search_track(f"isrc:{isrc}", token, proxy=proxy)

    def get_file_url(self, track_id: int, token: str,
                     format_id: int | None = None,
                     proxy: str | None = None) -> str:
        fmt = format_id or self.DEFAULT_FORMAT
        ts  = str(int(time.time()))
        sig = hashlib.md5(
            f"trackgetFileUrlformat_id{fmt}intentstreamtrack_id{track_id}{ts}{self.app_secret}"
            .encode()
        ).hexdigest()
        params = urllib.parse.urlencode({
            "format_id":       fmt,
            "intent":          "stream",
            "track_id":        track_id,
            "request_ts":      ts,
            "request_sig":     sig,
            "app_id":          self.app_id,
            "user_auth_token": token,
        })
        data = _get(f"{QOBUZ_BASE}/track/getFileUrl?{params}", proxy=proxy)
        url  = data.get("url")
        if not url:
            raise RuntimeError(f"Qobuz returned no file URL (track {track_id})")
        return url

    def download_track(self, track_id: int, token: str, dest: Path,
                       format_id: int | None = None,
                       on_progress=None,
                       proxy: str | None = None) -> Path:
        fmt      = format_id or self.DEFAULT_FORMAT
        file_url = self.get_file_url(track_id, token, fmt, proxy=proxy)
        ext      = "flac" if fmt in (6, 7, 27) else "mp3"
        out      = dest / f"qobuz_{track_id}.{ext}"

        req = urllib.request.Request(file_url, headers={"User-Agent": _UA})
        with _opener(proxy).open(req) as response:
            total = int(response.headers.get("Content-Length", 0))
            done  = 0
            with open(out, "wb") as f:
                while True:
                    block = response.read(65536)
                    if not block:
                        break
                    f.write(block)
                    done += len(block)
                    if on_progress and total:
                        on_progress(done / total * 100)
        return out

    def search_track_anon(self, query: str, limit: int = 5,
                          proxy: str | None = None) -> list[dict]:
        """Search tracks with app_id only (no user token). Returns [] if auth required."""
        params = urllib.parse.urlencode({
            "query":  query,
            "limit":  limit,
            "app_id": self.app_id,
        })
        try:
            data = _get(f"{QOBUZ_BASE}/track/search?{params}", proxy=proxy)
            return data.get("tracks", {}).get("items", [])
        except Exception:
            return []

    def tag_and_rename(self, track_info: dict, file: Path, dest: Path) -> Path:
        artist = track_info.get("performer", {}).get("name", "Unknown Artist")
        title  = track_info.get("title", "Unknown Title")
        safe   = re.sub(r'[<>:"/\\|?*]', "_", f"{artist} - {title}")
        new    = dest / f"{safe}{file.suffix}"
        file.rename(new)
        return new


# ── MusicBrainz ───────────────────────────────────────────────────────────────

class MusicBrainz:
    """
    Free, open music metadata database — no authentication required.
    Rate-limited to ~1 req/sec by MusicBrainz policy.

    Privacy: title and artist are sent to musicbrainz.org over HTTPS.
    MusicBrainz is a non-profit and does not sell user data.
    """

    _HEADERS = {
        "User-Agent": "flac-downloader/1.0",
        "Accept":     "application/json",
    }

    def search_recording(self, title: str, artist: str,
                         limit: int = 5,
                         proxy: str | None = None) -> list[dict]:
        q      = f'recording:"{title}" AND artist:"{artist}"'
        params = urllib.parse.urlencode({"query": q, "limit": limit, "fmt": "json"})
        try:
            data = _get(f"{MB_BASE}/recording?{params}",
                        headers=self._HEADERS, proxy=proxy)
            return data.get("recordings", [])
        except Exception as e:
            raise RuntimeError(f"MusicBrainz search failed: {e}")

    def get_isrc(self, mbid: str, proxy: str | None = None) -> str | None:
        try:
            data = _get(f"{MB_BASE}/recording/{mbid}?inc=isrcs&fmt=json",
                        headers=self._HEADERS, proxy=proxy)
            isrcs = data.get("isrcs", [])
            return isrcs[0] if isrcs else None
        except Exception:
            return None

    def best_isrc(self, title: str, artist: str,
                  proxy: str | None = None) -> str | None:
        recordings = self.search_recording(title, artist, limit=3, proxy=proxy)
        for rec in recordings:
            mbid = rec.get("id")
            if mbid:
                isrc = self.get_isrc(mbid, proxy=proxy)
                if isrc:
                    return isrc
        return None


# ── SpotiFlac Proxy ───────────────────────────────────────────────────────────

class SpotiflacProxy:
    """
    Anonymous Qobuz proxy download via third-party services in ~/.spotiflac/.
    No Qobuz account required.

    Privacy: only the numeric Qobuz track ID is sent to the proxy servers over HTTPS.
    These are third-party services; use at your own discretion.
    """

    _DB_PATH  = _SPOTIFLAC_DIR / "provider_priority.db"

    # Hardcoded fallback if DB is absent or unreadable
    _FALLBACK: list[tuple[str, str]] = [
        ("dab.yeet.su",  "https://dab.yeet.su/api/stream?trackId="),
        ("dabmusic.xyz", "https://dabmusic.xyz/api/stream?trackId="),
        ("musicdl.me",   "https://www.musicdl.me/api/qobuz/download"),
    ]

    def found(self) -> bool:
        """
        Always True — the proxy fallback URLs are hardcoded so the service is
        available regardless of whether ~/.spotiflac/ exists on this machine.
        """
        return True

    def has_local_db(self) -> bool:
        """True if a ~/.spotiflac/provider_priority.db is present locally."""
        return self._DB_PATH.exists()

    def services(self) -> list[tuple[str, str]]:
        """Return [(name, base_url)] parsed from DB, or the hardcoded fallback list."""
        if not self._DB_PATH.exists():
            return self._FALLBACK
        try:
            text = self._DB_PATH.read_bytes().decode("latin-1", errors="replace")
            urls = re.findall(r'https://[^\x00-\x1f\x7f-\xff\s"\'<>\\]{15,}', text)
            api_urls = [u for u in urls
                        if any(k in u for k in ("/api/", "/stream", "/download"))]
            seen: set[str] = set()
            result = []
            for u in api_urls:
                m = re.match(r'https://([^/]+)', u)
                if m and m.group(1) not in seen:
                    seen.add(m.group(1))
                    result.append((m.group(1), u))
            return result or self._FALLBACK
        except Exception:
            return self._FALLBACK

    def try_download(self, track_id: str, dest: Path,
                     fmt_id: int = 27,
                     on_progress=None,
                     proxy: str | None = None) -> tuple[Optional[Path], str]:
        """
        Try each proxy service in sequence.
        Returns (file_path, service_name) on first success; (None, '') if all fail.
        """
        for name, base_url in self.services():
            if "stream?trackId=" in base_url:
                out = self._try_stream(base_url + track_id, track_id, dest,
                                       on_progress, proxy)
            elif "musicdl.me" in name:
                out = self._try_musicdl(track_id, fmt_id, dest, on_progress, proxy)
            else:
                out = self._try_stream(base_url + track_id, track_id, dest,
                                       on_progress, proxy)
            if out:
                return out, name
        return None, ""

    # ── internals ─────────────────────────────────────────────────────────────

    def _try_stream(self, url: str, track_id: str, dest: Path,
                    on_progress, proxy) -> Optional[Path]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*"})
            with _opener(proxy).open(req, timeout=30) as resp:
                if resp.getcode() != 200:
                    return None
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype:
                    body = json.loads(resp.read().decode())
                    dl   = body.get("url") or body.get("stream") or body.get("link")
                    return self._fetch_url(dl, track_id, dest, on_progress, proxy) if dl else None
                if not any(t in ctype for t in ("audio", "octet", "flac", "mpeg")):
                    return None
                return self._drain(resp, track_id, dest, on_progress)
        except Exception:
            return None

    def _try_musicdl(self, track_id: str, fmt_id: int, dest: Path,
                     on_progress, proxy) -> Optional[Path]:
        try:
            payload = json.dumps({"trackId": track_id, "quality": fmt_id}).encode()
            req = urllib.request.Request(
                "https://www.musicdl.me/api/qobuz/download",
                data=payload,
                headers={"User-Agent": _UA, "Content-Type": "application/json"},
            )
            with _opener(proxy).open(req, timeout=30) as resp:
                if resp.getcode() != 200:
                    return None
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype:
                    body = json.loads(resp.read().decode())
                    dl   = (body.get("url") or body.get("downloadUrl")
                            or body.get("stream_url"))
                    return self._fetch_url(dl, track_id, dest, on_progress, proxy) if dl else None
                return self._drain(resp, track_id, dest, on_progress)
        except Exception:
            return None

    def _fetch_url(self, url: str, track_id: str, dest: Path,
                   on_progress, proxy) -> Optional[Path]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with _opener(proxy).open(req, timeout=60) as resp:
                return self._drain(resp, track_id, dest, on_progress)
        except Exception:
            return None

    def _drain(self, resp, track_id: str, dest: Path, on_progress) -> Path:
        ctype = resp.headers.get("Content-Type", "audio/flac")
        ext   = "mp3" if "mpeg" in ctype else "flac"
        total = int(resp.headers.get("Content-Length", 0))
        done  = 0
        out   = dest / f"proxy_{track_id}.{ext}"
        with open(out, "wb") as f:
            while True:
                block = resp.read(65536)
                if not block:
                    break
                f.write(block)
                done += len(block)
                if on_progress and total:
                    on_progress(done / total * 100)
        return out
