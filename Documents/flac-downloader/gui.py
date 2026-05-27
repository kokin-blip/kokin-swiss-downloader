"""
FLAC Downloader — Retro Terminal GUI
⚠  FOR LEGAL USE ONLY
   Only download content you are legally entitled to:
     • Music you have purchased where the platform permits local copies
     • Creative Commons / open-licence releases
     • Your own uploads or content you own outright
   Downloading copyrighted music without authorisation is illegal
   and may violate the DMCA, EU Copyright Directive, and similar laws.
"""

import tkinter as tk
from tkinter import filedialog
import threading
import queue
import sys
import re
import random
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    print("Run: python -m pip install yt-dlp")
    sys.exit(1)

import settings as cfg
from providers import OdesliResolver, QobuzAPI, MusicBrainz, is_drm_error
from utils import find_ffmpeg

# ── Palette ───────────────────────────────────────────────────────────────────
BG       = "#0a0a0a"
GREEN    = "#00ff41"
G_BRIGHT = "#afffbe"
G_MID    = "#00cc34"
G_DIM    = "#007d20"
G_DARK   = "#001a08"
AMBER    = "#ffc107"
RED      = "#ff4d4d"
GREY     = "#333333"
BLUE     = "#4fc3f7"

GLOW_SEQ = [
    "#002a0e","#003d14","#00551c","#006e24","#00882d",
    "#00a336","#00be40","#00da4a","#00ff41","#00da4a",
    "#00be40","#00a336","#00882d","#006e24","#00551c",
]

FONT       = ("Courier New", 11)
FONT_BOLD  = ("Courier New", 11, "bold")
FONT_BIG   = ("Courier New", 14, "bold")
FONT_SMALL = ("Courier New", 9)

DEFAULT_OUT = Path.home() / "Music" / "FLAC Downloads"

TITLE_LINES = [
    " ███████╗██╗      █████╗  ██████╗",
    " ██╔════╝██║     ██╔══██╗██╔════╝",
    " █████╗  ██║     ███████║██║     ",
    " ██╔══╝  ██║     ██╔══██║██║     ",
    " ██║     ███████╗██║  ██║╚██████╗",
    " ╚═╝     ╚══════╝╚═╝  ╚═╝ ╚═════╝",
    "",
    "    ──  D O W N L O A D E R  ──  ",
    "    ──  v 1 . 0   [ R E T R O ]  ──  ",
]

PRIVACY_NOTICE = """\
Your activity is NOT sent anywhere beyond what is strictly required:

  Odesli (api.odesli.co)    HTTPS   Source URL, to resolve DRM alternatives
  Qobuz  (qobuz.com)        HTTPS   Search query + track ID, if Qobuz is used
  MusicBrainz (mb.org)      HTTPS   Title + artist, for ISRC lookup only
  yt-dlp                    HTTPS   Requests go directly to the source site

Credentials are stored in your OS secure store — never written to disk:
  Windows  →  Credential Manager
  macOS    →  Keychain
  Linux    →  Secret Service (GNOME Keyring / KWallet)

No analytics, telemetry, or crash reporting is collected by this app.
All traffic can be routed through a proxy (see Proxy URL below).\
"""


# ── Retro Button ──────────────────────────────────────────────────────────────
class RetroButton(tk.Canvas):
    def __init__(self, parent, text, command=None, width=160, height=36,
                 color=GREEN, font=FONT_BOLD, **kwargs):
        super().__init__(parent, width=width, height=height,
                         bg=BG, highlightthickness=0, **kwargs)
        self.text     = text
        self.command  = command
        self.color    = color
        self.font     = font
        self.w        = width
        self.h        = height
        self._glow_i  = 0
        self._glowing = False
        self._after   = None
        self._enabled = True
        self._draw(G_DIM, BG)
        self.bind("<Enter>",           self._on_enter)
        self.bind("<Leave>",           self._on_leave)
        self.bind("<Button-1>",        self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _draw(self, border, bg, text_color=None):
        self.delete("all")
        tc = text_color or self.color
        self.create_rectangle(1, 1, self.w-2, self.h-2,
                              outline=border, fill=bg, width=2)
        for x, y in [(4,4),(self.w-6,4),(4,self.h-6),(self.w-6,self.h-6)]:
            self.create_rectangle(x, y, x+2, y+2, fill=border, outline="")
        self.create_text(self.w//2, self.h//2,
                         text=f"[ {self.text} ]", fill=tc, font=self.font)

    def _on_enter(self, _=None):
        if not self._enabled: return
        self._glowing = True
        self._glow_i  = 0
        self._animate()

    def _on_leave(self, _=None):
        self._glowing = False
        if self._after: self.after_cancel(self._after)
        self._draw(G_DIM, BG)

    def _animate(self):
        if not self._glowing: return
        c = GLOW_SEQ[self._glow_i % len(GLOW_SEQ)]
        self._draw(c, G_DARK, G_BRIGHT)
        self._glow_i += 1
        self._after = self.after(50, self._animate)

    def _on_press(self, _=None):
        if not self._enabled: return
        self._glowing = False
        self._draw(G_BRIGHT, GREEN, BG)

    def _on_release(self, _=None):
        if not self._enabled: return
        self._on_enter()
        if self.command: self.command()

    def set_enabled(self, state: bool):
        self._enabled = state
        if not state:
            self._glowing = False
            if self._after: self.after_cancel(self._after)
            self._draw(GREY, BG, GREY)
        else:
            self._draw(G_DIM, BG)


# ── Retro Progress Bar ────────────────────────────────────────────────────────
class RetroProgress(tk.Canvas):
    FILLED = "▓"
    EMPTY  = "░"

    def __init__(self, parent, width=520, height=22, **kwargs):
        super().__init__(parent, width=width, height=height,
                         bg=BG, highlightthickness=0, **kwargs)
        self.w = width; self.h = height
        self._pct = 0.0; self._pulse_job = None; self._pulse_i = 0
        self._set(0.0)

    def _set(self, pct):
        self.delete("all")
        cols   = 46
        filled = int(pct / 100 * cols)
        bar    = self.FILLED * filled + self.EMPTY * (cols - filled)
        self.create_text(4, self.h//2, anchor="w",
                         text=bar, fill=G_MID, font=("Courier New", 9))
        self.create_text(self.w-4, self.h//2, anchor="e",
                         text=f"{pct:5.1f}%", fill=GREEN, font=FONT_BOLD)

    def set(self, pct):
        self._pct = max(0.0, min(100.0, pct))
        self._set(self._pct)

    def pulse(self):
        if self._pulse_job: return
        self._pulse_i = 0; self._do_pulse()

    def _do_pulse(self):
        self.delete("all")
        cols = 46; offset = self._pulse_i % cols
        bar  = ""
        for i in range(cols):
            bar += self.FILLED if min(abs(i-offset), cols-abs(i-offset)) < 6 else self.EMPTY
        self.create_text(4, self.h//2, anchor="w",
                         text=bar, fill=G_MID, font=("Courier New", 9))
        self.create_text(self.w-4, self.h//2, anchor="e",
                         text=" ···· ", fill=AMBER, font=FONT_BOLD)
        self._pulse_i += 1
        self._pulse_job = self.after(60, self._do_pulse)

    def stop_pulse(self):
        if self._pulse_job:
            self.after_cancel(self._pulse_job)
            self._pulse_job = None


# ── Terminal Log ──────────────────────────────────────────────────────────────
class TerminalLog(tk.Frame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, bg=BG, **kwargs)
        self._text = tk.Text(
            self, bg=G_DARK, fg=GREEN, insertbackground=GREEN,
            font=FONT, relief="flat", bd=0, wrap="word", state="disabled",
            selectbackground=G_DIM, selectforeground=G_BRIGHT,
        )
        sb = tk.Scrollbar(self, orient="vertical", command=self._text.yview,
                          bg=G_DARK, troughcolor=BG, activebackground=G_MID)
        self._text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._text.pack(side="left", fill="both", expand=True)
        for tag, color in [("prompt",G_MID),("ok",GREEN),("warn",AMBER),
                            ("err",RED),("bright",G_BRIGHT),("dim",G_DIM),("blue",BLUE)]:
            self._text.tag_config(tag, foreground=color)
        self._queue: list[tuple] = []
        self._typing = False

    def _append(self, text, tag):
        self._text.configure(state="normal")
        self._text.insert("end", text, tag)
        self._text.configure(state="disabled")
        self._text.see("end")

    def write(self, message, tag="ok", instant=False):
        self._queue.append((message, tag, instant))
        if not self._typing: self._flush_next()

    def _flush_next(self):
        if not self._queue:
            self._typing = False; return
        self._typing = True
        message, tag, instant = self._queue.pop(0)
        self._append("\n> ", "prompt")
        if instant:
            self._append(message, tag); self._flush_next()
        else:
            self._type_chars(message, tag, 0)

    def _type_chars(self, message, tag, idx):
        if idx < len(message):
            self._append(message[idx], tag)
            self.after(random.randint(10, 30),
                       lambda: self._type_chars(message, tag, idx+1))
        else:
            self.after(40, self._flush_next)

    def clear(self):
        self._queue.clear(); self._typing = False
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.configure(state="disabled")


# ── Blinking cursor ───────────────────────────────────────────────────────────
class BlinkLabel(tk.Label):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self._v = True; self._blink()

    def _blink(self):
        self.configure(fg=GREEN if self._v else BG)
        self._v = not self._v
        self.after(530, self._blink)


# ── Provider Status Bar ───────────────────────────────────────────────────────
class ProviderBar(tk.Frame):
    CHAIN = [("ytdlp","yt-dlp"),("odesli","Odesli"),
             ("qobuz","Qobuz"),("musicbrainz","MusicBrainz")]
    STATES = {
        "idle":   ("●", G_DIM),
        "active": ("●", AMBER),
        "ok":     ("●", GREEN),
        "fail":   ("●", RED),
        "skip":   ("○", G_DIM),
    }

    def __init__(self, parent, **kwargs):
        super().__init__(parent, bg=BG, **kwargs)
        self._dots: dict[str, tk.Label] = {}
        tk.Label(self, text="PROVIDER CHAIN  ", bg=BG, fg=G_DIM,
                 font=FONT_SMALL).pack(side="left")
        for key, label in self.CHAIN:
            dot = tk.Label(self, text="●", bg=BG, fg=G_DIM, font=FONT_SMALL)
            dot.pack(side="left")
            self._dots[key] = dot
            tk.Label(self, text=f" {label}  ", bg=BG, fg=G_DIM,
                     font=FONT_SMALL).pack(side="left")

    def set(self, key, state):
        sym, color = self.STATES.get(state, self.STATES["idle"])
        if key in self._dots:
            self._dots[key].configure(text=sym, fg=color)

    def reset(self):
        for key in self._dots: self.set(key, "idle")


# ── Settings Window ───────────────────────────────────────────────────────────
class SettingsWindow(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Settings")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()
        self._s = cfg.load()
        self._build()

    def _label(self, parent, text, color=None):
        tk.Label(parent, text=text, bg=BG, fg=color or G_DIM,
                 font=FONT_SMALL, anchor="w").pack(fill="x", padx=14, pady=(8,0))

    def _styled_entry(self, parent, var, show=None, width=None):
        kw = {}
        if width: kw["width"] = width
        e = tk.Entry(parent, textvariable=var, show=show,
                     bg=G_DARK, fg=GREEN, insertbackground=GREEN,
                     font=FONT, relief="flat", bd=2,
                     highlightthickness=1, highlightcolor=G_DIM,
                     highlightbackground=G_DIM, **kw)
        e.pack(fill="x", padx=14, ipady=4)
        return e

    def _sep(self):
        tk.Frame(self, bg=G_DARK, height=1).pack(fill="x", padx=14, pady=6)

    def _section(self, text):
        tk.Label(self, text=f"  {text}", bg=BG, fg=AMBER,
                 font=FONT_BOLD).pack(anchor="w", padx=14, pady=(8,0))

    def _build(self):
        tk.Label(self, text="  ► SETTINGS", bg=BG, fg=GREEN,
                 font=FONT_BIG).pack(anchor="w", padx=14, pady=(14,4))
        self._sep()

        # ── Qobuz credentials ──
        self._section("QOBUZ ACCOUNT")
        kr_ok = cfg.keyring_available()
        kr_note = ("Stored in OS keychain  ✓" if kr_ok
                   else "⚠  keyring unavailable — credentials stored in memory only")
        tk.Label(self, text=f"  {kr_note}", bg=BG,
                 fg=GREEN if kr_ok else AMBER, font=FONT_SMALL).pack(anchor="w", padx=14)

        email, password = cfg.get_credentials()
        self._label(self, "  Email")
        self._email_var = tk.StringVar(value=email)
        self._styled_entry(self, self._email_var)

        self._label(self, "  Password")
        self._pass_var = tk.StringVar(value=password)
        self._styled_entry(self, self._pass_var, show="●")

        cred_row = tk.Frame(self, bg=BG)
        cred_row.pack(padx=14, pady=(8,0), anchor="w")
        RetroButton(cred_row, "TEST LOGIN", command=self._test_qobuz,
                    width=140, height=32, color=AMBER).pack(side="left", padx=(0,8))
        RetroButton(cred_row, "CLEAR CREDS", command=self._clear_creds,
                    width=150, height=32, color=RED).pack(side="left")

        self._cred_status = tk.Label(self, text="", bg=BG, fg=G_MID, font=FONT_SMALL)
        self._cred_status.pack(anchor="w", padx=14, pady=(4,0))

        self._sep()

        # ── Fallback options ──
        self._section("FALLBACK OPTIONS")
        self._fallback_var = tk.BooleanVar(value=self._s.get("auto_fallback", True))
        tk.Checkbutton(
            self, text="  Auto-activate fallback chain on DRM error",
            variable=self._fallback_var,
            bg=BG, fg=G_MID, selectcolor=G_DARK,
            activebackground=BG, activeforeground=GREEN,
            font=FONT_SMALL,
        ).pack(anchor="w", padx=14, pady=(6,0))

        self._label(self, "  Qobuz download quality")
        fmt_row = tk.Frame(self, bg=BG)
        fmt_row.pack(fill="x", padx=14, pady=(2,0))
        self._fmt_var = tk.StringVar(value=str(self._s.get("qobuz_format", 6)))
        for label, val in [("16-bit FLAC (CD quality)", "6"),
                            ("24-bit / 96 kHz", "7"),
                            ("24-bit / 192 kHz", "27")]:
            tk.Radiobutton(fmt_row, text=label, variable=self._fmt_var, value=val,
                           bg=BG, fg=G_MID, selectcolor=G_DARK,
                           activebackground=BG, activeforeground=GREEN,
                           font=FONT_SMALL).pack(side="left", padx=(0,12))

        self._sep()

        # ── Network & Privacy ──
        self._section("NETWORK & PRIVACY")

        self._label(self, "  Proxy URL  (optional — routes ALL traffic through this proxy)")
        self._label(self, "  e.g.  http://127.0.0.1:8080   or   socks5://127.0.0.1:1080")
        self._proxy_var = tk.StringVar(value=self._s.get("proxy", ""))
        self._styled_entry(self, self._proxy_var)

        # Privacy disclosure box
        self._label(self, "  Privacy disclosure", color=GREEN)
        notice_frame = tk.Frame(self, bg=G_DARK, bd=0)
        notice_frame.pack(fill="x", padx=14, pady=(4,0))
        notice_text = tk.Text(
            notice_frame, bg=G_DARK, fg=G_DIM, font=FONT_SMALL,
            relief="flat", bd=0, wrap="word", state="normal",
            height=10, cursor="arrow",
        )
        notice_text.insert("1.0", PRIVACY_NOTICE)
        notice_text.configure(state="disabled")
        notice_text.pack(fill="x", padx=8, pady=6)

        self._sep()

        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(pady=12)
        RetroButton(btn_row, "SAVE", command=self._save,
                    width=110, height=34, color=GREEN).pack(side="left", padx=8)
        RetroButton(btn_row, "CANCEL", command=self.destroy,
                    width=110, height=34, color=G_DIM).pack(side="left", padx=8)

    def _save(self):
        s = cfg.load()
        s["auto_fallback"] = self._fallback_var.get()
        s["qobuz_format"]  = int(self._fmt_var.get())
        s["proxy"]         = self._proxy_var.get().strip()
        cfg.save(s)

        email  = self._email_var.get().strip()
        passwd = self._pass_var.get()
        if email or passwd:
            ok = cfg.set_credentials(email, passwd)
            if not ok:
                self._cred_status.configure(
                    text="⚠  Could not save to keychain — credentials held in memory only.",
                    fg=AMBER)
        self._cred_status.configure(text="✓ Saved.", fg=GREEN)
        self.after(1500, self.destroy)

    def _test_qobuz(self):
        email  = self._email_var.get().strip()
        passwd = self._pass_var.get()
        if not email or not passwd:
            self._cred_status.configure(text="Enter email + password first.", fg=AMBER)
            return
        self._cred_status.configure(text="Testing…", fg=AMBER)
        self.update()
        proxy = self._proxy_var.get().strip() or None
        try:
            QobuzAPI().login(email, passwd, proxy=proxy)
            self._cred_status.configure(text="✓ Qobuz login successful!", fg=GREEN)
        except Exception as e:
            self._cred_status.configure(text=f"✗ {e}", fg=RED)

    def _clear_creds(self):
        cfg.delete_credentials()
        self._email_var.set("")
        self._pass_var.set("")
        self._cred_status.configure(text="Credentials cleared from keychain.", fg=AMBER)


# ── Main Application ──────────────────────────────────────────────────────────
class FlacDownloaderApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("FLAC Downloader  ─  Retro Edition")
        self.configure(bg=BG)
        self.resizable(False, False)

        self._msg_queue:  queue.Queue = queue.Queue()
        self._downloading = False
        self._ydl_abort   = False
        self._ffmpeg_dir  = find_ffmpeg()

        self._build_ui()
        self._poll_queue()
        self._boot_sequence()

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg=BG)
        hdr.pack(fill="x", padx=12, pady=(12,2))
        tk.Label(hdr, text="\n".join(TITLE_LINES), bg=BG, fg=GREEN,
                 font=("Courier New", 9, "bold"), justify="left").pack(side="left")
        BlinkLabel(hdr, text="█", bg=BG, fg=GREEN,
                   font=("Courier New", 24, "bold")).pack(side="right", anchor="n", padx=6)

        self._sep()

        # URL
        self._row_label("SOURCE URL  ( Spotify · YouTube · SoundCloud · Bandcamp · … )")
        url_row = tk.Frame(self, bg=BG)
        url_row.pack(fill="x", padx=14, pady=(2,6))
        self._url_var  = tk.StringVar()
        self._url_entry = tk.Entry(
            url_row, textvariable=self._url_var,
            bg=G_DARK, fg=GREEN, insertbackground=GREEN,
            font=FONT, relief="flat", bd=2,
            highlightthickness=1, highlightcolor=G_DIM, highlightbackground=G_DIM,
        )
        self._url_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self._url_entry.bind("<Control-v>", lambda e: self.after(10, self._flash_url))

        # Output
        self._row_label("OUTPUT FOLDER")
        out_row = tk.Frame(self, bg=BG)
        out_row.pack(fill="x", padx=14, pady=(2,6))
        self._out_var = tk.StringVar(value=str(DEFAULT_OUT))
        tk.Entry(out_row, textvariable=self._out_var,
                 bg=G_DARK, fg=G_MID, insertbackground=GREEN,
                 font=FONT, relief="flat", bd=2,
                 highlightthickness=1, highlightcolor=G_DIM,
                 highlightbackground=G_DIM,
                 ).pack(side="left", fill="x", expand=True, ipady=5)
        RetroButton(out_row, "BROWSE", command=self._browse,
                    width=110, height=32, color=G_MID).pack(side="left", padx=(8,0))

        # Quality
        self._row_label("FLAC COMPRESSION  ( 0 = fastest · 9 = smallest )")
        q_row = tk.Frame(self, bg=BG)
        q_row.pack(fill="x", padx=14, pady=(2,6))
        self._quality_var = tk.IntVar(value=0)
        tk.Scale(
            q_row, from_=0, to=9, orient="horizontal",
            variable=self._quality_var, bg=BG, fg=GREEN, troughcolor=G_DARK,
            highlightthickness=0, activebackground=G_MID,
            font=FONT_SMALL, length=280, showvalue=True,
        ).pack(side="left")

        # Options
        opt_row = tk.Frame(self, bg=BG)
        opt_row.pack(fill="x", padx=14, pady=(2,8))
        self._keep_var    = tk.BooleanVar(value=False)
        self._listfmt_var = tk.BooleanVar(value=False)
        for text, var in [("KEEP ORIGINAL", self._keep_var),
                           ("LIST FORMATS ONLY", self._listfmt_var)]:
            f = tk.Frame(opt_row, bg=BG)
            tk.Checkbutton(f, text=f"  {text}", variable=var,
                           bg=BG, fg=G_MID, selectcolor=G_DARK,
                           activebackground=BG, activeforeground=GREEN,
                           font=FONT_SMALL, cursor="hand2").pack(side="left")
            f.pack(side="left", padx=(0,18))

        self._sep()

        # Buttons
        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(pady=10)
        self._dl_btn = RetroButton(btn_row, "DOWNLOAD", command=self._start_download,
                                   width=180, height=40, color=GREEN, font=FONT_BIG)
        self._dl_btn.pack(side="left", padx=8)
        self._abort_btn = RetroButton(btn_row, "ABORT", command=self._abort,
                                      width=120, height=40, color=AMBER)
        self._abort_btn.set_enabled(False)
        self._abort_btn.pack(side="left", padx=8)
        RetroButton(btn_row, "CLEAR", command=self._clear,
                    width=110, height=40, color=G_DIM).pack(side="left", padx=8)
        RetroButton(btn_row, "SETTINGS", command=self._open_settings,
                    width=130, height=40, color=BLUE).pack(side="left", padx=8)

        self._sep()

        # Provider bar
        self._provider_bar = ProviderBar(self)
        self._provider_bar.pack(fill="x", padx=14, pady=(4,6))

        self._sep()

        # Terminal log
        self._row_label("TERMINAL OUTPUT")
        self._log = TerminalLog(self)
        self._log.pack(fill="both", expand=True, padx=14, pady=(2,8))
        self._log.configure(height=160)

        self._sep()

        # Progress
        prog_row = tk.Frame(self, bg=BG)
        prog_row.pack(fill="x", padx=14, pady=6)
        tk.Label(prog_row, text="PROGRESS  ", bg=BG, fg=G_DIM,
                 font=FONT_SMALL).pack(side="left")
        self._progress = RetroProgress(prog_row)
        self._progress.pack(side="left")

        self._sep()

        # Footer
        tk.Label(
            self,
            text="⚠  FOR LEGAL USE ONLY  —  only download content you are entitled to.",
            bg=BG, fg=AMBER, font=FONT_SMALL,
        ).pack(padx=14, pady=(4,10), anchor="w")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _sep(self):
        tk.Frame(self, bg=G_DARK, height=1).pack(fill="x", padx=14, pady=2)

    def _row_label(self, text):
        tk.Label(self, text=f"  ► {text}", bg=BG, fg=G_DIM,
                 font=FONT_SMALL).pack(anchor="w", padx=14)

    def _flash_url(self):
        self._url_entry.configure(highlightbackground=GREEN, highlightcolor=GREEN)
        self.after(300, lambda: self._url_entry.configure(
            highlightbackground=G_DIM, highlightcolor=G_DIM))

    def _browse(self):
        d = filedialog.askdirectory(title="Output folder", initialdir=self._out_var.get())
        if d: self._out_var.set(d)

    def _clear(self):
        self._log.clear()
        self._progress.stop_pulse()
        self._progress.set(0)
        self._provider_bar.reset()

    def _abort(self):
        self._log.write("Abort requested…", "warn")
        self._ydl_abort = True

    def _open_settings(self):
        SettingsWindow(self)

    # ── Boot ──────────────────────────────────────────────────────────────────

    def _boot_sequence(self):
        s      = cfg.load()
        email, _ = cfg.get_credentials()
        ffmpeg_status = (str(self._ffmpeg_dir / "ffmpeg.exe")
                         if self._ffmpeg_dir else "NOT FOUND — install ffmpeg and add to PATH")
        qobuz_status  = (f"configured ✓ ({email})"
                         if email else "not configured — open Settings")
        proxy_status  = s.get("proxy") or "none"
        msgs = [
            ("Initialising FLAC Downloader v1.0…",                   "ok",    False),
            (f"ffmpeg         : {ffmpeg_status}",                     "dim",   True),
            (f"Output         : {DEFAULT_OUT}",                       "dim",   True),
            (f"Qobuz account  : {qobuz_status}",                      "dim",   True),
            (f"Proxy          : {proxy_status}",                      "dim",   True),
            ("DRM fallback   : Odesli → Qobuz API → MusicBrainz",    "dim",   True),
            ("Credentials    : OS keychain (never written to disk)",  "dim",   True),
            ("Status         : READY",                                "bright",False),
            ("Paste a URL and press [ DOWNLOAD ].",                   "ok",    False),
            ("Spotify · Apple Music · Tidal → resolved via song.link.", "blue",False),
            ("⚠  Legal: only download content you own or have permission to copy.",
             "warn", False),
        ]
        if not self._ffmpeg_dir:
            msgs.append(("⚠  ffmpeg not found! Install it or place ffmpeg.exe next to this script.", "err", False))

        delay = 200
        for msg, tag, instant in msgs:
            self.after(delay, lambda m=msg, t=tag, i=instant: self._log.write(m, t, i))
            delay += 100 if instant else 500

    # ── Download ──────────────────────────────────────────────────────────────

    def _start_download(self):
        url = self._url_var.get().strip()
        if not url:
            self._log.write("ERROR: No URL provided.", "err"); return
        if self._downloading:
            self._log.write("Already downloading — wait or press ABORT.", "warn"); return

        self._downloading = True
        self._ydl_abort   = False
        self._dl_btn.set_enabled(False)
        self._abort_btn.set_enabled(True)
        self._progress.stop_pulse()
        self._progress.pulse()
        self._provider_bar.reset()

        output_dir = Path(self._out_var.get())
        output_dir.mkdir(parents=True, exist_ok=True)

        threading.Thread(
            target=self._download_worker,
            args=(url, output_dir,
                  str(self._quality_var.get()),
                  self._keep_var.get(),
                  self._listfmt_var.get()),
            daemon=True,
        ).start()

    def _download_worker(self, url, output_dir, quality, keep, list_fmt):
        def put(msg, tag="ok"):
            self._msg_queue.put(("log", msg, tag))

        def set_prov(key, state):
            self._msg_queue.put(("provider", key, state))

        def on_progress(pct):
            self._msg_queue.put(("progress", pct, ""))

        s     = cfg.load()
        proxy = s.get("proxy") or None

        def ydl_hook(d):
            if self._ydl_abort:
                raise yt_dlp.utils.DownloadError("Aborted by user")
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done  = d.get("downloaded_bytes", 0)
                on_progress((done / total * 100) if total else 0)
            elif d["status"] == "finished":
                put(f"Downloaded: {Path(d['filename']).name}", "bright")
                put("Converting to FLAC…", "dim")
                on_progress(100)

        class YDLLogger:
            def __init__(self, cb):
                self.cb = cb; self.errors = []
            def debug(self, msg):
                if not msg.startswith("[debug]"): self.cb(msg, "dim")
            def info(self, msg): self.cb(msg, "dim")
            def warning(self, msg): self.cb(msg, "warn")
            def error(self, msg):
                self.cb(msg, "err"); self.errors.append(msg)

        def ydl_opts(target_url):
            tpl  = str(output_dir / "%(uploader)s - %(title)s.%(ext)s")
            opts = {
                "format": "bestaudio/best",
                "outtmpl": tpl,
                "postprocessors": [
                    {"key": "FFmpegExtractAudio",
                     "preferredcodec": "flac", "preferredquality": quality},
                    {"key": "FFmpegMetadata"},
                    {"key": "EmbedThumbnail"},
                ],
                "writethumbnail": True,
                "keepvideo":      keep,
                "progress_hooks": [ydl_hook],
            }
            if self._ffmpeg_dir:
                opts["ffmpeg_location"] = str(self._ffmpeg_dir)
            if proxy:
                opts["proxy"] = proxy
            return opts

        try:
            if list_fmt:
                put("Fetching available formats…", "dim")
                with yt_dlp.YoutubeDL({"listformats": True, "quiet": True,
                                       **({"proxy": proxy} if proxy else {})}) as ydl:
                    info = ydl.extract_info(url, download=False)
                    for f in (info or {}).get("formats", []):
                        put(f"  {f.get('format_id','?'):12s}  "
                            f"{f.get('ext','?'):6s}  {f.get('format_note','')}", "dim")
                put("Format listing complete.", "ok")
                return

            # ── 1. yt-dlp ────────────────────────────────────────────────────
            set_prov("ytdlp", "active")
            put(f"Trying yt-dlp: {url}", "ok")
            logger  = YDLLogger(put)
            opts    = ydl_opts(url)
            opts["logger"] = logger

            drm_hit = False
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                set_prov("ytdlp", "ok")
                put(f"Done! Saved to: {output_dir}", "bright")
                return
            except yt_dlp.utils.DownloadError as e:
                err = str(e)
                if is_drm_error(err) or any(is_drm_error(m) for m in logger.errors):
                    drm_hit = True
                    set_prov("ytdlp", "skip")
                    put("DRM detected — activating fallback chain…", "warn")
                elif "Aborted" in err:
                    set_prov("ytdlp", "fail"); put("Aborted.", "warn"); return
                else:
                    set_prov("ytdlp", "fail"); raise

            if not drm_hit or not s.get("auto_fallback", True):
                if not s.get("auto_fallback", True):
                    put("Auto-fallback is disabled in Settings.", "warn")
                return

            # ── 2. Odesli resolution ─────────────────────────────────────────
            set_prov("odesli", "active")
            put("Resolving via Odesli (song.link)…", "blue")
            resolved = title = artist = None
            try:
                odesli   = OdesliResolver()
                resolved = odesli.resolve(url, proxy=proxy)
                title    = resolved["title"]
                artist   = resolved["artist"]
                put(f"Found: {artist} — {title}", "bright")
                for platform, purl in odesli.all_urls(resolved):
                    put(f"  {platform:<14s}: {purl}", "dim")
                set_prov("odesli", "ok")
            except Exception as e:
                set_prov("odesli", "fail")
                put(f"Odesli failed: {e}", "warn")

            # ── 3. Retry with each resolved URL via yt-dlp ───────────────────
            if resolved:
                for platform, alt_url in odesli.all_urls(resolved):
                    if platform in ("spotify", "appleMusic"):
                        continue
                    put(f"Trying {platform}: {alt_url}", "blue")
                    logger2 = YDLLogger(put)
                    opts2   = ydl_opts(alt_url)
                    opts2["logger"] = logger2
                    try:
                        with yt_dlp.YoutubeDL(opts2) as ydl:
                            ydl.download([alt_url])
                        put(f"Done via {platform}! Saved to: {output_dir}", "bright")
                        set_prov("ytdlp", "ok")
                        return
                    except Exception as e2:
                        put(f"  {platform} failed: {e2}", "warn")

            # ── 4. Qobuz API direct ──────────────────────────────────────────
            set_prov("qobuz", "active")
            email, passwd = cfg.get_credentials()
            if not email or not passwd:
                set_prov("qobuz", "skip")
                put("Qobuz skipped — no credentials (open Settings).", "warn")
            else:
                try:
                    put("Connecting to Qobuz API…", "blue")
                    qobuz = QobuzAPI()
                    token = qobuz.login(email, passwd, proxy=proxy)
                    put("Qobuz auth OK.", "ok")

                    query  = f"{artist} {title}" if (artist and title) else url
                    put(f"Searching Qobuz: {query}", "blue")
                    tracks = qobuz.search_track(query, token, limit=5, proxy=proxy)
                    if not tracks:
                        raise RuntimeError("No results found on Qobuz")

                    track    = tracks[0]
                    track_id = track["id"]
                    t_title  = track.get("title", "?")
                    t_artist = track.get("performer", {}).get("name", "?")
                    put(f"Best match: {t_artist} — {t_title}  (id {track_id})", "bright")

                    fmt = s.get("qobuz_format", 6)
                    put(f"Downloading (Qobuz format {fmt})…", "blue")
                    out_file = qobuz.download_track(
                        track_id, token, output_dir,
                        format_id=fmt,
                        on_progress=on_progress,
                        proxy=proxy,
                    )
                    renamed = qobuz.tag_and_rename(track, out_file, output_dir)
                    put(f"Saved: {renamed.name}", "bright")
                    set_prov("qobuz", "ok")
                    put(f"Done! Saved to: {output_dir}", "bright")
                    return
                except Exception as qe:
                    set_prov("qobuz", "fail")
                    put(f"Qobuz API failed: {qe}", "warn")

            # ── 5. MusicBrainz ISRC → Qobuz retry ───────────────────────────
            set_prov("musicbrainz", "active")
            if not (artist and title):
                set_prov("musicbrainz", "skip")
                put("MusicBrainz skipped — no title/artist metadata.", "warn")
            else:
                try:
                    put(f"Looking up ISRC: {artist} — {title}", "blue")
                    mb   = MusicBrainz()
                    isrc = mb.best_isrc(title, artist, proxy=proxy)
                    if isrc:
                        put(f"ISRC: {isrc}", "bright")
                        set_prov("musicbrainz", "ok")
                        email, passwd = cfg.get_credentials()
                        if email and passwd:
                            qobuz  = QobuzAPI()
                            token  = qobuz.login(email, passwd, proxy=proxy)
                            tracks = qobuz.search_by_isrc(isrc, token, proxy=proxy)
                            if tracks:
                                track    = tracks[0]
                                track_id = track["id"]
                                fmt      = s.get("qobuz_format", 6)
                                put(f"Downloading via ISRC match (id {track_id})…", "blue")
                                out_file = qobuz.download_track(
                                    track_id, token, output_dir,
                                    format_id=fmt, on_progress=on_progress, proxy=proxy,
                                )
                                renamed = qobuz.tag_and_rename(track, out_file, output_dir)
                                put(f"Saved: {renamed.name}", "bright")
                                put(f"Done! Saved to: {output_dir}", "bright")
                                return
                            put("No Qobuz results for ISRC.", "warn")
                    else:
                        put("No ISRC found in MusicBrainz.", "warn")
                        set_prov("musicbrainz", "fail")
                except Exception as me:
                    set_prov("musicbrainz", "fail")
                    put(f"MusicBrainz failed: {me}", "warn")

            put("All providers exhausted — could not download this track.", "err")

        except Exception as exc:
            put(f"ERROR: {exc}", "err")
        finally:
            self._msg_queue.put(("done", None, None))

    # ── Queue polling ─────────────────────────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                item = self._msg_queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._log.write(item[1], item[2])
                elif kind == "progress":
                    self._progress.stop_pulse()
                    self._progress.set(item[1])
                elif kind == "provider":
                    self._provider_bar.set(item[1], item[2])
                elif kind == "done":
                    self._downloading = False
                    self._dl_btn.set_enabled(True)
                    self._abort_btn.set_enabled(False)
                    self._progress.stop_pulse()
        except queue.Empty:
            pass
        self.after(80, self._poll_queue)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = FlacDownloaderApp()
    app.mainloop()
