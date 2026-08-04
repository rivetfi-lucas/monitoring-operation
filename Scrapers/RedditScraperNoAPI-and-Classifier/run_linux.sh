#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  echo "[notice] Virtual environment is missing. Running setup..."
  ./setup_linux.sh
fi

if ! .venv/bin/python -c "import playwright, bs4, yaml" >/dev/null 2>&1; then
  echo "[notice] Dependencies are incomplete. Rebuilding setup..."
  ./setup_linux.sh
fi

exec .venv/bin/python main.py "$@"
