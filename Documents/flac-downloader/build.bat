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
  --add-data "version.py;." ^
  --hidden-import "pywebview.platforms.winforms" ^
  --hidden-import "clr" ^
  app.py

echo.
if exist "dist\Swiss Downloader.exe" (
  echo Done! dist\Swiss Downloader.exe is ready to share.
) else (
  echo Build may have failed. Check the output above.
)
pause
