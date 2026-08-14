#!/usr/bin/env bash
set -euo pipefail
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e '.[dev]'
cp -n .env.example .env || true
fstec-monitor init
printf '\nInstalled. Edit .env, then run: . .venv/bin/activate && fstec-monitor baseline\n'
