@echo off
setlocal

echo Installing/updating PyInstaller...
python -m pip install pyinstaller --quiet

REM ── Bundle a browser for the in-app "browser grab" fallback ──────────────────
REM PLAYWRIGHT_BROWSERS_PATH=0 installs Chromium INTO the playwright package so
REM PyInstaller's --collect-all picks it up. Runtime sets the same env var.
echo Installing Playwright + bundled Chromium...
set PLAYWRIGHT_BROWSERS_PATH=0
python -m pip install playwright --quiet
python -m playwright install chromium

REM ── Ensure ffmpeg.exe is present ─────────────────────────────────────────────
if not exist "ffmpeg\ffmpeg.exe" (
  echo.
  echo ffmpeg.exe not found in .\ffmpeg\
  if exist "%USERPROFILE%\.spotiflac\ffmpeg.exe" (
    echo Copying from %USERPROFILE%\.spotiflac\...
    if not exist "ffmpeg" mkdir ffmpeg
    copy "%USERPROFILE%\.spotiflac\ffmpeg.exe" "ffmpeg\ffmpeg.exe" >nul
  ) else (
    echo Downloading ffmpeg from gyan.dev...
    if not exist "ffmpeg" mkdir ffmpeg
    powershell -Command "Invoke-WebRequest -Uri https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip -OutFile ffmpeg.zip; Expand-Archive ffmpeg.zip -DestinationPath ffmpeg-tmp -Force; Get-ChildItem -Path ffmpeg-tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1 | Copy-Item -Destination ffmpeg\ffmpeg.exe; Remove-Item -Recurse -Force ffmpeg-tmp; Remove-Item ffmpeg.zip"
  )
)

echo.
echo Generating icon...
python make_icon.py

echo.
echo Building Swiss Downloader (this bundles ffmpeg, will take a minute)...
pyinstaller ^
  --onefile ^
  --windowed ^
  --name "Swiss Downloader" ^
  --add-data "ui;ui" ^
  --add-binary "ffmpeg\ffmpeg.exe;ffmpeg" ^
REM --collect-all pywebview is a no-op: the import name is "webview", so it
REM matches nothing and returns empty lists. Left in place deliberately.
REM pywebview ships its own hook (webview\__pyinstaller\hook-webview.py) which
REM is what actually bundles webview\js\*.js — including api.js, which is both
REM the Python<->page bridge and the JS half of drag & drop. Verified present
REM in the built exe. Changing this to "webview" would pull the GTK/Qt/Cocoa
REM backends into the analysis for no gain.
  --collect-all pywebview ^
  --collect-all mutagen ^
  --collect-all curl_cffi ^
  --collect-all playwright ^
  --collect-all PIL ^
  --icon icon.ico ^
  app.py

echo.
if exist "dist\Swiss Downloader.exe" (
  echo ============================================
  echo  Done!  dist\Swiss Downloader.exe is ready.
  echo ============================================
) else (
  echo Build may have failed. Check the output above.
)
pause
