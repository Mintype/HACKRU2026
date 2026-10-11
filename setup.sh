#!/usr/bin/env sh
# One-time setup: create .venv and install all dependencies.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

if [ ! -x ".venv/bin/python" ]; then
  echo "[1/3] Creating virtual environment..."
  python3 -m venv .venv
fi

echo "[2/3] Installing core dependencies..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

echo "[3/3] Installing MediaPipe (pose detection)..."
.venv/bin/python -m pip install -r requirements-vision.txt

echo
echo "Done. Copy .env.example to .env and add your GEMINI_API_KEY, then run:"
echo "    ./run.sh --simulate"
