@echo off
echo Installing/updating PyInstaller...
python -m pip install pyinstaller --quiet

echo.
echo Building Swiss Downloader...
pyinstaller ^
  --onefile ^
  --windowed ^
  --name "Swiss Downloader" ^
  --add-data "ui;ui" ^
  --collect-all pywebview ^
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
