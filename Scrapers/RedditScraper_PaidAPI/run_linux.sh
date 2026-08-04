#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  ./setup_linux.sh
fi

exec .venv/bin/python reddit_scraper_hybrid.py "$@"
