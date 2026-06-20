#!/usr/bin/env python3
"""
grab.py - headless-browser stream sniffer for Swiss Downloader.

Some sites hide the real video URL behind client-side JavaScript (encrypted
or obfuscated players), so yt-dlp's generic extractor can't find it. This
script loads the page in a REAL browser, lets the site's own JS decrypt and
request the stream, captures the resulting .m3u8 (HLS) URL off the network,
and hands it to yt-dlp with the correct Referer/Origin headers.

Because the real browser does the decryption, this keeps working even when a
site rotates its crypto - and it's the stealthiest option (it behaves exactly
like a person watching the page).

One-time setup:
    pip install playwright
    playwright install chromium

Usage:
    python grab.py "https://site/watch/whatever/episode-7/"
    python grab.py --print-only "https://..."   # just print the m3u8, don't download
    python grab.py --headful "https://..."       # show the browser window (debugging)
    python grab.py -o "C:\\Users\\you\\Videos" "https://..."
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse


def find_ffmpeg():
    """Reuse the same ffmpeg the main app bundles, if present."""
    for cand in (Path("ffmpeg") / "ffmpeg.exe",
                 Path.home() / ".spotiflac" / "ffmpeg.exe"):
        if cand.exists():
            return str(cand.parent)
    return None  # fall back to PATH


def sniff_stream(page_url, headful=False, timeout=60):
    """Open page_url in a browser; return (m3u8_url, referer) or (None, None)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit(
            "Playwright isn't installed. Run these once:\n"
            "    pip install playwright\n"
            "    playwright install chromium"
        )

    found = []  # (url, referer)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headful,
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 720},
        )
        page = ctx.new_page()

        def on_request(req):
            if ".m3u8" in req.url.split("?")[0].lower():
                ref = req.headers.get("referer") or page_url
                if (req.url, ref) not in found:
                    found.append((req.url, ref))

        page.on("request", on_request)

        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=timeout * 1000)
        except Exception as e:
            print(f"(page load warning: {e})")

        # Autoplay is often blocked even with the flag - nudge it by clicking
        # the most likely play targets. Failures here are fine.
        for sel in ("video", ".vjs-big-play-button", "button[aria-label*=play i]",
                    ".play", "#root", "body"):
            try:
                page.click(sel, timeout=1500)
                break
            except Exception:
                continue

        deadline = time.time() + timeout
        while not found and time.time() < deadline:
            page.wait_for_timeout(500)

        browser.close()

    if not found:
        return None, None
    # Prefer a master playlist (lets yt-dlp pick quality); else first seen.
    for url, ref in found:
        if "master" in url.lower():
            return url, ref
    return found[0]


def download(m3u8_url, referer, out_dir="."):
    origin = f"{urlparse(referer).scheme}://{urlparse(referer).netloc}"
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--referer", referer,
        "--add-header", f"Origin:{origin}",
        "--sleep-requests", "0.75",   # be polite -> lower ban risk
        "--retries", "5",
        "-o", str(Path(out_dir) / "%(title)s.%(ext)s"),
    ]
    ff = find_ffmpeg()
    if ff:
        cmd += ["--ffmpeg-location", ff]
    cmd.append(m3u8_url)
    print("Downloading via yt-dlp...")
    return subprocess.call(cmd)


def main():
    ap = argparse.ArgumentParser(
        description="Sniff a JS-hidden HLS stream via a real browser, then download it.")
    ap.add_argument("url", help="The page / watch URL")
    ap.add_argument("--print-only", action="store_true",
                    help="Only print the .m3u8 URL; don't download")
    ap.add_argument("--headful", action="store_true",
                    help="Show the browser window (use to debug a stubborn page)")
    ap.add_argument("-o", "--out", default=".", help="Output directory")
    ap.add_argument("--timeout", type=int, default=60,
                    help="Seconds to wait for the stream to appear")
    args = ap.parse_args()

    print(f"Loading page in a browser (this runs the site's player)...")
    m3u8, referer = sniff_stream(args.url, headful=args.headful, timeout=args.timeout)

    if not m3u8:
        sys.exit(
            "No .m3u8 stream detected.\n"
            "  - Try --headful to watch what the page does.\n"
            "  - Some players need an explicit play click, or use real DRM\n"
            "    (Widevine), which this can't capture.")

    print(f"\nFound stream:\n  {m3u8}\n  (referer: {referer})\n")
    if args.print_only:
        print(m3u8)
        return
    sys.exit(download(m3u8, referer, args.out))


if __name__ == "__main__":
    main()
