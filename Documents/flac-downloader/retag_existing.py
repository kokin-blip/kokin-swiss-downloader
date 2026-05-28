"""
One-shot fix-up: walk a folder of FLAC files that came out of an older
build, look each one up on iTunes by tags, embed the proper album cover,
and delete the leftover .jpg sidecars + folder.jpg next to them.

Usage:
    python retag_existing.py "C:\\Users\\erick\\Music\\Swiss Downloads"
"""

import sys
import urllib.request
from pathlib import Path

# Make sibling modules importable when run from anywhere
sys.path.insert(0, str(Path(__file__).parent))

from providers import fetch_itunes_cover_url
from utils     import tag_flac_file, flac_cover_info


_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def fetch_bytes(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read()
    except Exception as e:
        print(f"    ! fetch failed: {e}")
        return None


def parse_filename(stem: str) -> tuple[str, str]:
    """'Artist - Title' → (artist, title). Returns ('', '') if no separator."""
    if " - " not in stem:
        return "", ""
    artist, _, title = stem.partition(" - ")
    return artist.strip(), title.strip()


def process(folder: Path) -> None:
    if not folder.is_dir():
        print(f"Not a directory: {folder}")
        return

    flacs = sorted(folder.glob("*.flac"))
    if not flacs:
        print("No .flac files found here.")
        return

    print(f"Found {len(flacs)} FLAC file(s) in {folder}")
    print()

    for flac in flacs:
        print(f"-> {flac.name}")
        info = flac_cover_info(flac)
        if info.get("present"):
            print(f"   already has cover ({info['width']}×{info['height']}); skipping embed.")
        else:
            artist, title = parse_filename(flac.stem)
            if not (artist and title):
                print("   filename isn't 'Artist - Title'; skipping iTunes lookup.")
                continue
            print(f"   iTunes lookup: {artist} — {title}")
            url = fetch_itunes_cover_url(artist, title)
            if not url:
                print("   no iTunes match.")
                continue
            data = fetch_bytes(url)
            if not data:
                continue
            ti = {
                "title":     title,
                "performer": {"name": artist},
            }
            ok, err = tag_flac_file(flac, ti, data)
            if ok:
                info = flac_cover_info(flac)
                print(f"   [ok] embedded {info.get('width')}x{info.get('height')}, "
                      f"{info.get('size')} bytes")
            else:
                print(f"   [fail] {err}")

        # Clean up sidecar JPGs the old build left behind
        sidecar = flac.with_suffix(".jpg")
        if sidecar.exists():
            sidecar.unlink()
            print(f"   removed sidecar {sidecar.name}")

    # Remove folder.jpg if present
    fj = folder / "folder.jpg"
    if fj.exists():
        fj.unlink()
        print(f"\nRemoved {fj}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python retag_existing.py "C:\\path\\to\\folder"')
        sys.exit(1)
    process(Path(sys.argv[1]))
