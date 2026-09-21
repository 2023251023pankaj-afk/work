@echo off
REM ---------------------------------------------------------------------------
REM Build AuditMaster.exe on Windows.
REM
REM PyInstaller cannot cross-compile, so this must run on a Windows machine.
REM Requires Python 3.10+ from python.org with "Add python.exe to PATH" ticked.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python was not found on PATH.
  echo   Install it from https://www.python.org/downloads/windows/
  echo   and tick "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)

echo Creating build environment...
if not exist .venv-build\Scripts\python.exe (
  python -m venv .venv-build || goto :failed
)
.venv-build\Scripts\python -m pip install --quiet --upgrade pip || goto :failed
.venv-build\Scripts\python -m pip install --quiet -r requirements.txt pyinstaller || goto :failed

echo Building AuditMaster.exe  (this takes a minute)...
REM --add-data uses ";" as the separator on Windows (":" on macOS/Linux).
REM --console keeps the window visible so the user can read the URL and close it
REM to stop the server.
.venv-build\Scripts\pyinstaller ^
  --noconfirm --clean --onefile --console ^
  --name AuditMaster ^
  --add-data "templates;templates" ^
  --add-data "static;static" ^
  --hidden-import auditmaster ^
  --collect-submodules auditmaster ^
  launch.py || goto :failed

echo.
echo   Done.  dist\AuditMaster.exe
echo   Double-click it; your browser opens automatically.
echo.
pause
exit /b 0

:failed
echo.
echo   Build failed. See the messages above.
echo.
pause
exit /b 1
