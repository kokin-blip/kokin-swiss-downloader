"""
GitHub Releases update checker.
Requires GITHUB_OWNER and GITHUB_REPO set in version.py.
"""

import json
import sys
import urllib.request
import urllib.error
from typing import Optional

from version import __version__, GITHUB_OWNER, GITHUB_REPO

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def _parse(tag: str) -> tuple:
    """'v1.2.3' or '1.2.3' → (1, 2, 3)"""
    try:
        return tuple(int(x) for x in tag.lstrip("v").split(".")[:3])
    except Exception:
        return (0, 0, 0)


def check(proxy: Optional[str] = None) -> Optional[dict]:
    """
    Query the GitHub Releases API for the latest release.

    Returns:
        {"version": str, "notes": str, "url": str}  — if a newer version exists
        None  — if already up-to-date, not configured, or the check fails
    """
    if not (GITHUB_OWNER and GITHUB_REPO):
        return None

    url = (f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
           f"/releases/latest")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _UA,
                 "Accept":     "application/vnd.github+json"},
    )
    handlers: list = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)

    try:
        with opener.open(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None

    latest_tag = data.get("tag_name", "")
    if not latest_tag:
        return None

    if _parse(latest_tag) > _parse(__version__):
        notes = data.get("body", "").strip()
        # Find this platform's asset for in-app download, if present.
        #
        # This MUST be platform-aware. Since v1.13.0 a release carries both a
        # Windows Setup .exe and a macOS .dmg, and the old version of this loop
        # took the first asset ending in ".exe" — which on a Mac means happily
        # handing backend.install_update() the *Windows installer* to run.
        #
        # (History, since the previous comment here was stale: v1.11.0 shipped a
        # .zip, so this loop found nothing and the UI fell back to opening the
        # release page. v1.12.0 moved to an Inno Setup installer, which is a
        # single .exe again, restoring one-click update on Windows.)
        _WANT = {"win32": ".exe", "darwin": ".dmg"}.get(sys.platform)
        asset_url = ""
        if _WANT:
            for asset in data.get("assets", []):
                if asset.get("name", "").lower().endswith(_WANT):
                    asset_url = asset.get("browser_download_url", "")
                    break

        # Only Windows can install its own update. Replacing a running .app out
        # of a mounted DMG is a Sparkle-sized job and is deliberately not built
        # yet, so macOS gets the release-page fallback the UI already has: blank
        # the URL rather than offering a button that cannot work.
        if sys.platform != "win32":
            asset_url = ""
        return {
            "version":   latest_tag.lstrip("v"),
            "notes":     notes[:2000],          # cap to avoid giant changelogs
            "url":       data.get("html_url", ""),
            "asset_url": asset_url,
        }
    return None
