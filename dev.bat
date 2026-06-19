@echo off
REM ============================================================
REM  AutoApply - local dev launcher
REM  Starts the FastAPI server bound to 127.0.0.1 only.
REM  Open http://127.0.0.1:8000 in your browser.
REM ============================================================

REM Always run from the project root (folder this script lives in)
cd /d "%~dp0"

REM Prefer the project venv; fall back to the Python 3.11 launcher.
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=py -3.11"
)

echo.
echo  AutoApply - starting local server with: %PY%
echo  URL: http://127.0.0.1:8000   (Ctrl+C to stop)
echo.

REM Open the dashboard in the default browser (server starts right after).
start "" "http://127.0.0.1:8000"

REM Local-only bind. --reload restarts on code changes during development.
%PY% -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

REM Keep the window open if uvicorn exits or errors out.
echo.
echo  Server stopped.
pause
