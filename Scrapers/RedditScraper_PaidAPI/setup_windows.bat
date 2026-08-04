@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 goto :error
)

echo Installing dependencies...
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :error
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :error

if not exist ".env" (
    copy /Y ".env.example" ".env" >nul
    echo Created .env from .env.example. Add your API keys before running.
)

echo Setup complete.
exit /b 0

:error
echo Setup failed.
exit /b 1
