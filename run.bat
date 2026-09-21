@echo off
REM ---------------------------------------------------------------------------
REM Run Audit Master on Windows without building an executable.
REM Creates the virtualenv and installs Flask on first run.
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

if not exist .venv\Scripts\python.exe (
  echo Creating virtualenv...
  python -m venv .venv || goto :failed
  .venv\Scripts\python -m pip install --quiet --upgrade pip
  .venv\Scripts\python -m pip install --quiet -r requirements.txt || goto :failed
)

.venv\Scripts\python launch.py %*
exit /b 0

:failed
echo.
echo   Setup failed. See the messages above.
echo.
pause
exit /b 1
