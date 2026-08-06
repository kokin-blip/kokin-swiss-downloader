"""
Kokin's Swiss Downloader — entry point.
Launches the Win95-styled pywebview UI.
"""

import os
import sys
import webview
from backend import API
from utils import WINDOW_TITLE

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
    webview.start(debug=False)

if __name__ == "__main__":
    main()
