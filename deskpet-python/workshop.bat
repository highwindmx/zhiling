@echo off
cd /d "%~dp0"
rem Material workshop (pack editor). Prefer uv, fall back to .venv.
rem All visible text is ASCII on purpose: stored as ASCII bytes, so it renders
rem identically under cmd (GBK/936), PowerShell, and after any git clone.
where uv >nul 2>nul
if not errorlevel 1 (
    echo [workshop] starting: uv run python workshop\workshop.py %*
    uv run python workshop\workshop.py %*
    if not errorlevel 1 exit /b 0
    echo.
    echo [workshop] uv run failed, falling back to .venv\Scripts\python.exe
)

if exist ".venv\Scripts\python.exe" (
    echo [workshop] starting: .venv\Scripts\python.exe workshop\workshop.py %*
    ".venv\Scripts\python.exe" workshop\workshop.py %*
    if errorlevel 1 (
        echo.
        echo [workshop] the .venv interpreter failed: dependencies may be incomplete
        echo            .venv\Scripts\pip install -r requirements.txt
        pause
    )
    exit /b 0
)

echo.
echo [workshop] no Python environment found. Choose one:
echo   1) install uv, then run: uv sync
echo   2) pip: python -m venv .venv, then .venv\Scripts\pip install -r requirements.txt
echo See README.md section "Build from scratch".
pause
exit /b 1
