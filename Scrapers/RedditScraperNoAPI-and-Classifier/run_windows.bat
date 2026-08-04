@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [notice] Virtual environment is missing. Running setup...
  call setup_windows.bat
  if errorlevel 1 exit /b 1
)

.venv\Scripts\python.exe -c "import playwright, bs4, yaml" >nul 2>nul
if errorlevel 1 (
  echo [notice] Dependencies are incomplete. Rebuilding setup...
  call setup_windows.bat
  if errorlevel 1 exit /b 1
)

.venv\Scripts\python.exe main.py %*
exit /b %errorlevel%
