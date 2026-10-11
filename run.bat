@echo off
rem Launch Posture Coach with the project virtualenv (no activation needed).
rem Usage:  run.bat                 (real webcam)
rem         run.bat --simulate      (laptop demo, no camera)
setlocal
set "HERE=%~dp0"
if exist "%HERE%.venv\Scripts\python.exe" (
  "%HERE%.venv\Scripts\python.exe" "%HERE%run.py" %*
) else (
  echo [posture-coach] .venv not found - run setup.bat first.
  python "%HERE%run.py" %*
)
exit /b %ERRORLEVEL%
