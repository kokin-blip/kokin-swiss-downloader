"""
GitHub Releases update checker.
Requires GITHUB_OWNER and GITHUB_REPO set in version.py.
"""

import json
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
        # Find the .exe asset for in-app download, if present.
        #
        # Since v1.11.0 the release asset is a .zip, not a .exe: the build moved
        # to PyInstaller --onedir because --onefile was re-extracting 1.45 GB to
        # %TEMP% on every launch and putting time-to-window at ~2 minutes. So
        # this loop finds nothing, asset_url stays empty, and the UI falls back
        # to opening the release page — deliberate, and handled, but it does mean
        # one-click self-update is unavailable until there is an installer or a
        # swap-the-folder path that can replace files it is currently running.
        asset_url = ""
        for asset in data.get("assets", []):
            if (asset.get("name", "").lower().endswith(".exe")):
                asset_url = asset.get("browser_download_url", "")
                break
        return {
            "version":   latest_tag.lstrip("v"),
            "notes":     notes[:2000],          # cap to avoid giant changelogs
            "url":       data.get("html_url", ""),
            "asset_url": asset_url,
        }
    return None
