"""
Kokin's Swiss Downloader — entry point.
Launches the Win95-styled pywebview UI.
"""

import os
import sys
import webview
from backend import API

def main():
    api        = API()
    # sys._MEIPASS is set by PyInstaller when running as a bundled .exe
    base_dir   = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    html_path  = os.path.join(base_dir, "ui", "index.html")

    window = webview.create_window(
        title      = "Kokin's Swiss Downloader",
        url        = html_path,
        js_api     = api,
        width      = 700,
        height     = 820,
        resizable  = False,
        frameless  = True,
        text_select= True,
        background_color = "#008080",
    )
    api.set_window(window)
    webview.start(debug=False)

if __name__ == "__main__":
    main()
