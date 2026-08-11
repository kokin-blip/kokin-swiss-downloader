"""
Per-site cookie jars captured from a real browser sign-in.

Some content is gated behind a login, an age confirmation, or a region check
rather than behind an extractor yt-dlp lacks. None of that is fixable by the
browser-grab sniffer — the site never serves the stream in the first place. So
we let the user sign in once in a real Chromium window (the same one already
bundled for stream sniffing), keep the resulting cookies, and hand them to
yt-dlp on later downloads for that site.

The macOS build has no bundled Chromium (PyInstaller cannot ad-hoc sign
Playwright's nested browser .app — see backend._chromium_bundled), so nothing
here is ever populated there. Existing jars are still read and used; there is
just no way to capture a new one on that platform.

UNLIKE THE REST OF THIS APP'S STORAGE, THESE FILES ARE CREDENTIALS. A session
cookie is as good as a password for as long as it lives, so:
  - they live in their own directory, one file per site, never in settings.json
  - they are written with owner-only permissions where the OS supports it
  - nothing here is ever written to the debug log
  - the user can see what is stored and delete it from the Settings tab
They are stored unencrypted: the app has no password to derive a key from, and
inventing one would be security theatre. Anyone with read access to the user's
profile can read them — which is equally true of the browser's own cookie DB.
"""

import os
import time
from pathlib import Path
from urllib.parse import urlparse

from utils import app_data_dir


def _cookie_dir() -> Path:
    d = app_data_dir() / "cookies"
    d.mkdir(parents=True, exist_ok=True)
    return d


# Second-level registry labels: under a two-letter country TLD these are part
# of the public suffix, not the registrable name. Without this, bbc.co.uk keys
# as "co.uk" and every other .co.uk site would be handed the same cookie jar —
# a cross-site leak, not just a mis-grouping. This is a pragmatic subset of the
# public suffix list, which is far too large to vendor for one filename.
_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "edu", "ac",
                 "or", "ne", "in", "gr", "gob", "nom", "mil"}


def site_key(url_or_host: str) -> str:
    """
    Registrable domain used as the filename ('www.site.co.uk/x' -> 'site.co.uk').

    Cookies are stored per site rather than per host so a login on www.site.com
    still applies to the CDN-ish subdomains the player uses.
    """
    raw = str(url_or_host or "").strip()
    host = urlparse(raw).netloc if "//" in raw else raw
    host = host.split("@")[-1].split(":")[0].lower().strip("/")
    labels = [l for l in host.split(".") if l]
    if len(labels) < 2:
        return host
    if (len(labels) >= 3 and labels[-2] in _SECOND_LEVEL
            and len(labels[-1]) == 2):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def jar_path(url_or_host: str) -> Path:
    key = site_key(url_or_host)
    # Defensive: a site key comes from a URL, so keep it to filename-safe chars
    safe = "".join(c for c in key if c.isalnum() or c in ".-_")
    return _cookie_dir() / f"{safe}.txt"


def has_cookies(url_or_host: str) -> bool:
    return bool(site_key(url_or_host)) and jar_path(url_or_host).exists()


def cookie_file_for(url_or_host: str):
    """Path as a str for yt-dlp's `cookiefile`, or None if we have none."""
    p = jar_path(url_or_host)
    return str(p) if p.exists() else None


def to_netscape(cookies: list) -> str:
    """
    Render Playwright's cookie dicts as a Netscape cookie file.

    yt-dlp insists on the magic header line and on tab separators; the columns
    are domain, include-subdomains, path, secure, expiry, name, value.
    """
    lines = ["# Netscape HTTP Cookie File",
             "# Written by Kokin's Swiss Downloader. Treat as a password."]
    for c in cookies:
        domain = c.get("domain") or ""
        if not domain or not c.get("name"):
            continue
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        expires = c.get("expires", -1)
        # Playwright uses -1 for a session cookie; Netscape uses 0, and yt-dlp
        # reads 0 as "no expiry recorded" rather than "already expired".
        expiry = 0 if not expires or expires < 0 else int(expires)
        lines.append("\t".join([
            domain, include_sub, c.get("path") or "/",
            "TRUE" if c.get("secure") else "FALSE",
            str(expiry), c["name"], c.get("value") or "",
        ]))
    return "\n".join(lines) + "\n"


def save(url_or_host: str, cookies: list) -> tuple[int, Path]:
    """Write a jar for this site. Returns (cookies written, path)."""
    usable = [c for c in cookies if c.get("name") and c.get("domain")]
    p = jar_path(url_or_host)
    p.write_text(to_netscape(usable), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass  # best-effort; Windows ACLs don't map onto this
    return len(usable), p


def listing() -> list:
    """Saved sites for the Settings UI: name, cookie count, age in days."""
    out = []
    for p in sorted(_cookie_dir().glob("*.txt")):
        try:
            body = p.read_text(encoding="utf-8").splitlines()
            count = sum(1 for l in body if l and not l.startswith("#"))
            age = max(0, int((time.time() - p.stat().st_mtime) // 86400))
        except OSError:
            continue
        out.append({"site": p.stem, "count": count, "age_days": age})
    return out


def forget(site: str) -> bool:
    p = jar_path(site)
    try:
        p.unlink()
        return True
    except OSError:
        return False


def forget_all() -> int:
    n = 0
    for p in _cookie_dir().glob("*.txt"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n
