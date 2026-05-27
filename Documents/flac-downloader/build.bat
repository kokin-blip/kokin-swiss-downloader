@echo off
echo Installing/updating PyInstaller...
python -m pip install pyinstaller --quiet

echo.
echo Generating icon...
python make_icon.py

echo.
echo Building Swiss Downloader...
pyinstaller ^
  --onefile ^
  --windowed ^
  --name "Swiss Downloader" ^
  --add-data "ui;ui" ^
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
