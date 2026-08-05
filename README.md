# Kokin's Swiss Downloader

A Windows desktop app for downloading music and video, with a Win95-styled UI.
Point it at a URL, pick a format, hit go.

Music downloads aim for **lossless FLAC** and fall back through several sources
before giving up. Video downloads run on yt-dlp, with a real-browser fallback
for sites that hide their streams behind JavaScript.

## Install

Grab **`Swiss.Downloader.exe`** from the
[latest release](https://github.com/kokin-blip/kokin-swiss-downloader/releases/latest)
and run it. There's no installer and nothing to set up — ffmpeg and everything
else is bundled.

Windows SmartScreen will likely warn you on first launch (the exe isn't code
signed). *More info → Run anyway.*

The app checks for updates on startup and can download and install a new
version itself from the popup.

> The exe is large (~400 MB) because it bundles a headless browser for the
> stream-sniffing fallback described below.

## What it does

### Audio

Paste a track or album URL and get FLAC (or MP3, M4A, OGG, Opus, WAV). Cover
art and metadata are embedded automatically.

Don't have a link? **Search by name** instead — type "radiohead creep", pick a
result, and it fills the URL box. Results come from YouTube because that's the
one source that answers free text with a usable URL, but picking one still runs
the full chain above, so you can search by name and get FLAC.

Downloads are **queued** rather than run in parallel — add as many URLs as you
like and they'll work through one at a time (bandwidth is the bottleneck, and
hammering a site is how you get blocked). The queue sits under the log: pending
items can be dropped, finished ones re-run.

Next to the queue is a **History** tab listing what actually got downloaded,
with a button to open the containing folder. It records the filename yt-dlp
really wrote, so a download that was skipped rather than fetched shows up as
what it was.

Because no single source has everything, a download walks a chain until one
works:

1. **yt-dlp** — direct extraction, if the site is supported
2. **Odesli** — resolves the link to the same track on other services
3. **Qobuz** — the lossless source, when the track is available there
4. **Spotiflac proxy** — anonymous public proxies
5. **MusicBrainz** — ISRC lookup to retry the proxies with a better identifier

Album and playlist URLs are expanded and downloaded track by track. Cover art
falls back to iTunes and Deezer when the source doesn't carry usable artwork.

### Video

Paste a video URL, choose a container (MP4, MKV, WebM) and a quality cap (up to
4K), optionally embedding thumbnails, metadata, and subtitles. Tick **List
formats only** to see what a site actually offers without downloading anything.

Three fallbacks make this work on sites plain yt-dlp fails on:

- **TLS impersonation** — many sites block yt-dlp by its TLS fingerprint and
  answer HTTP 403/410. `curl_cffi` makes the handshake look like a real Chrome.
- **Browser grab** — for players that decrypt their stream in JavaScript, the
  app loads the page in a bundled headless Chromium, lets the site's own player
  do the work, and captures the resulting HLS manifest off the network. It then
  hands that to yt-dlp with the right Referer/Origin.
- **Site sign-in** — when a site simply won't serve the stream to a stranger
  (login, age gate, region check), *Settings → Site sign-in* opens a real
  browser window. Sign in by hand, close it, and the session is reused for
  later downloads from that site. See [Privacy](#privacy) — those cookies are
  credentials.

DRM-protected video (Widevine) is not supported and won't be.

### Settings

- **Output folders** for audio and video (defaults: `~/Music/Swiss Downloads`,
  `~/Videos/Swiss Downloads`)
- **Qobuz quality** — FLAC 16-bit, 24/96, or 24/192
- **Proxy** — routes provider lookups through an HTTP or SOCKS5 proxy
- **Auto-fallback** — toggles the provider chain above
- **Filenames** — output templates for audio and video in yt-dlp's template
  syntax (`%(artist)s`, `%(title)s`, `%(track_number)02d`, …). Use `/` to build
  subfolders. The extension is appended for you, and a template that's malformed
  or tries to escape the output folder is rejected when you save it, not
  silently at 3am mid-download
- **Subtitle languages** — comma-separated, or `all`
- **Parallel fragments / speed limit** — streams arrive as hundreds of small
  fragments, so fetching a few at once is much faster; too many gets you
  rate-limited
- **SponsorBlock** — optionally cut sponsor spots, self-promo, intros and
  similar out of YouTube downloads, per category. Enabling it sends the video ID
  to sponsor.ajay.app
- **Embed chapter markers**
- **Chime when downloads finish** — flashes the taskbar and plays the system
  sound once the whole queue is done, so you can leave it running. On by default
- **Site sign-in** — see above
- **Debug log** — off by default; see below

Interrupted downloads resume from the partial file rather than starting over,
and transient failures are retried with a backoff (a sniffed HLS stream is
hundreds of fragments, and one blip shouldn't cost you the whole video).

## Privacy

No analytics, no telemetry, no accounts. Settings are stored locally.

**Site sign-ins are the one exception.** If you use *Settings → Site sign-in*,
the cookies from that browser session are saved to
`%LOCALAPPDATA%\flac-downloader\flac-downloader\cookies\` as one file per site.
A session cookie is as good as a password for as long as it lasts, and these
files are **not encrypted** — the app has no master password to derive a key
from, and pretending otherwise would be security theatre. They're readable by
anything running as you, which is equally true of your browser's own cookie
database. Settings lists every saved session and can forget them individually
or all at once; do that when you're finished with a site.

The **debug log** is opt-in and off by default. When enabled it writes to
`%LOCALAPPDATA%\flac-downloader\flac-downloader\logs\swiss-downloader.log` so failures can be
diagnosed from a `--windowed` build that has no console.

Because that file is meant to be shared in bug reports, URLs written to it are
reduced to their host (`https://example.com/...`), and query strings are
stripped from any URL that appears anywhere in the file — including inside
tracebacks, where signed CDN tokens would otherwise land. The log records which
*sites* were used, not which pages. Have a look before sending it if you like.

## Building from source

Requires Python 3.11+ on Windows.

```bat
pip install -r requirements.txt
build.bat
```

`build.bat` fetches ffmpeg, installs the Playwright Chromium, generates the
icon, and produces `dist\Swiss Downloader.exe`.

To run without building:

```bat
pip install -r requirements.txt
playwright install chromium
python app.py
```

Note that `PLAYWRIGHT_BROWSERS_PATH=0` is required at build time — it installs
Chromium *inside* the playwright package so PyInstaller's `--collect-all` picks
it up. The app sets the same variable at runtime when frozen.

`grab.py` is a standalone CLI version of the browser-grab sniffer, useful for
debugging a stubborn site:

```bat
python grab.py --headful "https://site/watch/whatever/"
```

## Layout

| File | |
|---|---|
| `app.py` | entry point; creates the pywebview window |
| `backend.py` | the API exposed to JS — download workers, provider chain, browser grab |
| `providers.py` | Qobuz, Odesli, MusicBrainz, spotiflac, cover-art lookups |
| `ui/index.html` | the entire front end |
| `utils.py` | ffmpeg discovery, FLAC tagging, debug log, notifications |
| `settings.py` | persisted settings |
| `cookies.py` | per-site sign-in jars (credentials — read the header) |
| `history.py` | the download history file |
| `updater.py` | GitHub release check + in-app update |

Standalone scripts, not imported by the app:

| File | |
|---|---|
| `grab.py` | the browser-grab sniffer as a CLI, for debugging a site |
| `retag_existing.py` | one-shot repair for FLACs from an older build |

Dead code, kept only for reference: `gui.py` (a retro-terminal Tkinter UI from
before the pywebview rewrite) and `downloader.py` (the original CLI). Nothing
imports either, and both hold stale copies of logic that now lives in
`backend.py` — don't fix bugs there expecting the app to change.

## Notes

- Windows only in practice. Nothing is hard-coded to Windows in the Python, but
  the build, the bundled ffmpeg, and the UI chrome all assume it.
- The in-app updater needs this repo to stay **public** — it calls the GitHub
  releases API unauthenticated, and a private repo would return 404 and
  silently stop offering updates to everyone.
- Downloading copyrighted material you don't have rights to may be illegal
  where you live. That's on you.
