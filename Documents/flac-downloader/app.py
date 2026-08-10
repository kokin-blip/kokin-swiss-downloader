"""
Kokin's Swiss Downloader — entry point.
Launches the Win95-styled pywebview UI.
"""

import os
import sys
import webview
from backend import API
from utils import WINDOW_TITLE


def selftest() -> int:
    """
    Prove the bundle is intact, then exit. Run by CI against the built exe.

    This exists because the things --collect-all breaks, it breaks *silently*:
    the exe builds, launches, and looks perfectly fine until somebody opens the
    Photo tab and finds that onnxruntime's DLLs never made it in. A size check
    cannot see that. Importing the thing can.

    The verdict goes to a file, not stdout: the shipped build is --windowed and
    has no usable stdout, so anything printed here would vanish.
    """
    lines, ok = [], True

    def check(label, fn):
        nonlocal ok
        try:
            detail = fn()
            lines.append(f"PASS  {label}{(' — ' + detail) if detail else ''}")
        except Exception as e:
            ok = False
            lines.append(f"FAIL  {label} — {type(e).__name__}: {e}")

    import convert
    import photo
    from version import __version__

    lines.append(f"Swiss Downloader {__version__} self-test")
    lines.append(f"frozen={getattr(sys, 'frozen', False)} "
                 f"meipass={getattr(sys, '_MEIPASS', '(none)')}")

    def _pillow():
        if not convert.pillow_available():
            raise RuntimeError("Pillow did not import")
        return ""

    def _ffmpeg():
        from utils import find_ffmpeg
        d = find_ffmpeg()
        if not d:
            raise RuntimeError("ffmpeg.exe not found")
        return str(d)

    def _model():
        p = photo._bundled_model()
        if not p.is_file():
            raise RuntimeError(f"not bundled at {p}")
        return f"{p.stat().st_size // 1_000_000} MB at {p}"

    def _rembg():
        ready, why = photo.available()
        if not ready:
            raise RuntimeError(why)
        return ""

    def _session():
        # The real proof: seeds the model, imports rembg, and builds a live
        # onnxruntime session out of the bundled graph. Everything the Photo tab
        # needs before it can produce a single pixel.
        ready, why = photo.warm()
        if not ready:
            raise RuntimeError(why or "session did not build")
        return f"seeded at {photo.model_path()}"

    check("Pillow imports", _pillow)
    check("ffmpeg is bundled", _ffmpeg)
    check("cutout model is bundled", _model)
    check("rembg + onnxruntime import", _rembg)
    check("onnxruntime session builds", _session)

    lines.append("RESULT: " + ("ok" if ok else "FAILED"))
    report = "\n".join(lines)
    try:
        from utils import app_data_dir
        (app_data_dir() / "selftest.txt").write_text(report, encoding="utf-8")
    except Exception:
        pass
    # Harmless when there is no console, and the only way to see this when run
    # from source.
    print(report)
    return 0 if ok else 1


def main():
    api        = API()
    # sys._MEIPASS is set by PyInstaller when running as a bundled .exe
    base_dir   = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    html_path  = os.path.join(base_dir, "ui", "index.html")

    window = webview.create_window(
        # Shared with utils.notify_done(), which finds this window by title.
        title      = WINDOW_TITLE,
        url        = html_path,
        js_api     = api,
        width      = 700,
        height     = 820,
        # The UI sizes itself to the viewport (see html/body in index.html), so
        # it survives any window size and there is no reason to pin it shut.
        # min_size stops it being dragged smaller than the layout was drawn for
        # — below this the tab strip and the Convert tab's rows start colliding.
        resizable  = True,
        min_size   = (700, 820),
        frameless  = True,
        text_select= True,
        background_color = "#008080",
    )
    api.set_window(window)
    # close_window() covers the in-app ✕. This covers Alt+F4 and the taskbar,
    # so the clipboard watcher is always stopped before the window goes away.
    window.events.closed += api.on_closed
    webview.start(debug=False)

if __name__ == "__main__":
    # Checked before anything builds a window: the self-test must not need one.
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    main()
