@echo off
cd /d "%~dp0"
rem pythonw from .venv (managed by uv) first; fall back to global pythonw / python.
rem This script itself still opens one cmd window for a few ms. For a completely
rem silent start (no black window at all), double-click run.vbs instead.
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" pet.py %*
) else (
    where pythonw >nul 2>nul
    if errorlevel 1 (
        start "" python pet.py %*
    ) else (
        start "" pythonw pet.py %*
    )
)
rem exit immediately: keep the console window alive no longer than necessary
exit /b 0
