@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [error] .venv is missing. Run setup_windows.bat first.
  exit /b 1
)

echo Installing Playwright Firefox...
.venv\Scripts\python.exe -m playwright install firefox
if errorlevel 1 exit /b 1

echo Firefox installed successfully.
exit /b 0
