@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    call setup_windows.bat
    if errorlevel 1 exit /b 1
)

.venv\Scripts\python.exe reddit_scraper_hybrid.py %*
exit /b %errorlevel%
