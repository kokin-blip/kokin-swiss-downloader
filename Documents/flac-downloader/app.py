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
    html_path  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "index.html")

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
