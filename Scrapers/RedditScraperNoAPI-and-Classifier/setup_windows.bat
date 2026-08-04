@echo off
setlocal
cd /d "%~dp0"

echo [1/4] Preparing a clean virtual environment...
if exist ".venv" rmdir /s /q ".venv"

where py >nul 2>nul
if not errorlevel 1 (
  py -3 -m venv .venv
) else (
  python -m venv .venv
)
if errorlevel 1 goto :error

echo [2/4] Upgrading pip...
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :error

echo [3/4] Installing Python dependencies...
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo [4/4] Installing Playwright Firefox...
.venv\Scripts\python.exe -m playwright install firefox
if errorlevel 1 goto :error

echo.
echo Setup complete.
echo Run all configured sources with: run_windows.bat
echo Test one source with: run_windows.bat --source merval --days 1 --max-posts 2
exit /b 0

:error
echo.
echo [error] Setup failed. Review the message above.
exit /b 1
