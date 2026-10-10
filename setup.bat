@echo off
rem One-time setup: create .venv and install all dependencies.
setlocal
set "HERE=%~dp0"
cd /d "%HERE%"

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating virtual environment...
  py -3.12 -m venv .venv 2>nul || python -m venv .venv
)

echo [2/3] Installing core dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt

echo [3/3] Installing MediaPipe (pose detection)...
".venv\Scripts\python.exe" -m pip install -r requirements-vision.txt

echo.
echo Done. Copy .env.example to .env and add your GEMINI_API_KEY, then run:
echo     run.bat --simulate
