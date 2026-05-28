@echo off
setlocal

echo Installing/updating PyInstaller...
python -m pip install pyinstaller --quiet

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
  --collect-all pywebview ^
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
