#!/usr/bin/env sh
# Launch Posture Coach with the project virtualenv (no activation needed).
# Usage:  ./run.sh                 (real webcam)
#         ./run.sh --simulate      (laptop demo, no camera)
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -x "$HERE/.venv/bin/python" ]; then
  exec "$HERE/.venv/bin/python" "$HERE/run.py" "$@"
fi
echo "[posture-coach] .venv not found - run ./setup.sh first." >&2
exec python3 "$HERE/run.py" "$@"
