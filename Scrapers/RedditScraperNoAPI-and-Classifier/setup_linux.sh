#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/4] Preparing a clean virtual environment..."
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate

echo "[2/4] Upgrading pip..."
python -m pip install --upgrade pip

echo "[3/4] Installing Python dependencies..."
python -m pip install -r requirements.txt

echo "[4/4] Installing Playwright Firefox and Linux dependencies..."
python -m playwright install --with-deps firefox

echo "Setup complete. Run: ./run_linux.sh"
