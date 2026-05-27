"""
FLAC Music Downloader
=====================
LEGAL DISCLAIMER
----------------
This tool is intended ONLY for downloading music you are legally entitled to,
such as:
  - Content you have purchased and the site permits downloading
  - Music released under Creative Commons or other open licenses
  - Your own uploads or content you have explicit rights to
  - Content explicitly offered as a free/legal download by the rights holder

Downloading copyrighted music without authorization is ILLEGAL in most
countries and may violate the Digital Millennium Copyright Act (DMCA),
the EU Copyright Directive, and equivalent laws worldwide. The author of
this tool accepts NO responsibility for any misuse.

By using this tool you confirm that you have the legal right to download
the requested content.
"""

import sys
import os
import argparse
from pathlib import Path

# ── dependency check ────────────────────────────────────────────────────────
try:
    import yt_dlp
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm
    from rich import print as rprint
except ImportError:
    print("Missing dependencies. Run:  python -m pip install yt-dlp rich")
    sys.exit(1)

console = Console()

FFMPEG_PATH = Path(r"C:\Users\erick\.spotiflac\ffmpeg.exe")
DEFAULT_OUTPUT = Path.home() / "Music" / "FLAC Downloads"

DISCLAIMER = """[bold yellow]⚠  LEGAL DISCLAIMER[/bold yellow]

This tool may only be used to download content you are [bold]legally entitled[/bold] to:
  • Music you have purchased where the platform permits local copies
  • Content under Creative Commons or other open licences
  • Your own uploads / content you own outright
  • Files the rights holder explicitly offers as free downloads

[bold red]Downloading copyrighted music without authorisation is illegal[/bold red]
and may violate the DMCA, EU Copyright Directive, and similar laws.
The author bears NO responsibility for misuse of this tool."""


def show_disclaimer() -> bool:
    console.print(Panel(DISCLAIMER, title="Before you continue", border_style="yellow"))
    return Confirm.ask("[yellow]I confirm I have the legal right to download this content[/yellow]")


def build_ydl_opts(output_dir: Path, quality: str, keep_original: bool) -> dict:
    output_template = str(output_dir / "%(uploader)s - %(title)s.%(ext)s")

    postprocessors = [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "flac",
            "preferredquality": quality,
        },
        {"key": "FFmpegMetadata"},
        {"key": "EmbedThumbnail"},
    ]

    opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "postprocessors": postprocessors,
        "writethumbnail": True,
        "keepvideo": keep_original,
        "noplaylist": False,
        "quiet": False,
        "no_warnings": False,
        "progress_hooks": [progress_hook],
        "ffmpeg_location": str(FFMPEG_PATH.parent) if FFMPEG_PATH.exists() else None,
    }

    # strip None values
    return {k: v for k, v in opts.items() if v is not None}


def progress_hook(d: dict) -> None:
    if d["status"] == "finished":
        filename = Path(d["filename"]).name
        console.print(f"[green]✓[/green] Downloaded: [cyan]{filename}[/cyan] — converting to FLAC…")
    elif d["status"] == "error":
        console.print(f"[red]✗ Error during download[/red]")


def download(urls: list[str], output_dir: Path, quality: str, keep_original: bool, skip_disclaimer: bool) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    if not skip_disclaimer:
        if not show_disclaimer():
            console.print("[red]Aborted.[/red]")
            sys.exit(0)

    if not FFMPEG_PATH.exists():
        console.print(
            f"[yellow]Warning:[/yellow] ffmpeg not found at {FFMPEG_PATH}\n"
            "Install ffmpeg and set FFMPEG_PATH in the script, or place ffmpeg.exe in the same folder."
        )

    opts = build_ydl_opts(output_dir, quality, keep_original)

    console.print(f"\n[bold]Output folder:[/bold] {output_dir}")
    console.print(f"[bold]Downloading {len(urls)} URL(s)…[/bold]\n")

    with yt_dlp.YoutubeDL(opts) as ydl:
        results = ydl.download(urls)

    if results == 0:
        console.print(f"\n[bold green]Done![/bold green] Files saved to [cyan]{output_dir}[/cyan]")
    else:
        console.print(f"\n[yellow]Finished with some errors (exit code {results}).[/yellow]")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="flac-dl",
        description="Download audio from a URL and save as FLAC.\n\n"
                    "FOR LEGAL USE ONLY — only download content you have the right to.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("urls", nargs="+", metavar="URL", help="One or more URLs to download")
    parser.add_argument(
        "-o", "--output",
        default=str(DEFAULT_OUTPUT),
        metavar="DIR",
        help=f"Output directory (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "-q", "--quality",
        default="0",
        metavar="0-9",
        help="FLAC compression level 0 (fastest/largest) to 9 (slowest/smallest). Default: 0",
    )
    parser.add_argument(
        "--keep-original",
        action="store_true",
        help="Keep the original downloaded file alongside the FLAC",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip the legal disclaimer prompt (you still accept the terms)",
    )
    parser.add_argument(
        "--list-formats",
        action="store_true",
        help="List all available formats for the given URL(s) without downloading",
    )

    args = parser.parse_args()

    if args.list_formats:
        with yt_dlp.YoutubeDL({"listformats": True}) as ydl:
            for url in args.urls:
                ydl.download([url])
        return

    download(
        urls=args.urls,
        output_dir=Path(args.output),
        quality=args.quality,
        keep_original=args.keep_original,
        skip_disclaimer=args.yes,
    )


if __name__ == "__main__":
    main()
