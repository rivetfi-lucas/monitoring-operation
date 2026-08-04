#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example. Add your API keys before running."
fi

echo "Setup complete."
