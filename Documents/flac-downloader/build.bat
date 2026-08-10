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

REM ── Ensure the background-removal model is present ──────────────────────────
REM Same treatment as ffmpeg: a 168 MB binary that is fetched rather than
REM committed, then bundled with --add-data. photo._seed_model copies it out of
REM the exe into %LOCALAPPDATA% and points $U2NET_HOME at it, so the shipped app
REM never downloads a model at runtime.
if not exist "assets\models\u2net.onnx" (
  echo.
  echo Downloading the u2net background-removal model ^(168 MB^)...
  if not exist "assets\models" mkdir "assets\models"
  powershell -Command "Invoke-WebRequest -Uri https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx -OutFile assets\models\u2net.onnx"
)
REM A truncated model is the one failure the seeding logic cannot recover from,
REM and it would surface as a broken tab in someone's hands. Catch it here.
for %%F in ("assets\models\u2net.onnx") do if %%~zF LSS 150000000 (
  echo.
  echo ERROR: assets\models\u2net.onnx is only %%~zF bytes - the download was truncated.
  echo Delete it and re-run this script.
  pause
  exit /b 1
)

echo.
echo Generating icon...
python make_icon.py

echo.
echo Building Swiss Downloader (bundles ffmpeg + the cutout model, takes a while)...

REM ─────────────────────────────────────────────────────────────────────────────
REM Every comment about the build has to live up here, ABOVE the command.
REM
REM A REM line may NOT appear inside a ^-continued command. cmd's caret escapes
REM the newline *before* anything is parsed as a comment, so a REM in the middle
REM of the flag list does two silent, awful things: the word REM and the rest of
REM that line get passed to pyinstaller as positional arguments, and because the
REM REM line has no trailing caret the continuation ENDS there — every flag
REM after it then runs as its own shell command and fails with "not recognized".
REM This file used to do exactly that, which is why the flags below are now one
REM unbroken run of carets.
REM
REM --collect-all pywebview is a no-op: the import name is "webview", so it
REM matches nothing and returns empty lists. Left in place deliberately.
REM pywebview ships its own hook (webview\__pyinstaller\hook-webview.py) which
REM is what actually bundles webview\js\*.js — including api.js, which is both
REM the Python<->page bridge and the JS half of drag & drop. Verified present
REM in the built exe. Changing this to "webview" would pull the GTK/Qt/Cocoa
REM backends into the analysis for no gain.
REM
REM Photo tab, rembg and its native stack. onnxruntime matters most: its
REM inference DLLs live in onnxruntime\capi and PyInstaller's static analysis
REM does not find them. numba and llvmlite arrive through pymatting, which rembg
REM imports at module scope whether or not alpha matting is ever switched on, so
REM they are needed even though the feature they serve defaults to off.
REM (photo.py also sets NUMBA_DISABLE_JIT=1 before importing rembg, so a numba
REM that freezes but cannot JIT still works.)
REM
REM scikit-image is declared by rembg and imported nowhere in it — verified by
REM grepping every rembg source file, and by importing rembg with skimage
REM blocked outright, which succeeds. Excluding it also drops imageio, tifffile
REM and networkx, which are only there to serve it.
REM
REM --onedir, not --onefile, and this is the single most important flag here.
REM --onefile re-extracts the ENTIRE archive to %TEMP% on every launch. Measured
REM on this bundle: 3,220 files and 1,450 MB unpacked per run, which put
REM time-to-window at 114-130 seconds. --onedir leaves those files on disk, so
REM startup is the ~2s it takes to load them and nothing is written to %TEMP%.
REM The cost is that we ship a folder rather than a lone .exe — zip dist\ for
REM distribution, and note that updater.py's swap-the-exe path assumes one file.
REM
REM Keep this list in sync with .github/workflows/release.yml.
REM ─────────────────────────────────────────────────────────────────────────────
pyinstaller ^
  --onedir ^
  --windowed ^
  --name "Swiss Downloader" ^
  --add-data "ui;ui" ^
  --add-data "assets\models;assets\models" ^
  --add-binary "ffmpeg\ffmpeg.exe;ffmpeg" ^
  --collect-all pywebview ^
  --collect-all mutagen ^
  --collect-all curl_cffi ^
  --collect-all playwright ^
  --collect-all PIL ^
  --collect-all rembg ^
  --collect-all onnxruntime ^
  --collect-all numpy ^
  --collect-all cv2 ^
  --collect-all scipy ^
  --collect-all pymatting ^
  --collect-all numba ^
  --collect-all llvmlite ^
  --collect-all pooch ^
  --exclude-module skimage ^
  --exclude-module matplotlib ^
  --icon icon.ico ^
  app.py

echo.
REM --onedir puts the exe inside a folder of the same name, so the path to check
REM is one level deeper than it was under --onefile.
if exist "dist\Swiss Downloader\Swiss Downloader.exe" (
  echo ============================================
  echo  Done!  dist\Swiss Downloader\ is ready.
  echo  Run:   "dist\Swiss Downloader\Swiss Downloader.exe"
  echo  Ship:  zip the whole "dist\Swiss Downloader" folder.
  echo ============================================
) else (
  echo Build may have failed. Check the output above.
)
pause
