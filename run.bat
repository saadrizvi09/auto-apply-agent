@echo off
REM ============================================================
REM  AutoApply - normal-use launcher (NO auto-reload)
REM  Use this for everyday applying. The dashboard's Auto-Apply
REM  runs live inside this server, and WITHOUT --reload nothing
REM  restarts mid-run, so long apply batches finish cleanly.
REM
REM  Use dev.bat instead only when editing the code.
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
echo  Mode: NORMAL (no auto-reload - apply runs won't be interrupted)
echo.

REM Open the dashboard in the default browser (server starts right after).
start "" "http://127.0.0.1:8000"

REM Local-only bind. No --reload: dashboard apply runs survive to completion.
%PY% -m uvicorn app.main:app --host 127.0.0.1 --port 8000

REM Keep the window open if uvicorn exits or errors out.
echo.
echo  Server stopped.
pause
