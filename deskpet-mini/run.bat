@echo off
chcp 65001 >nul
cd /d "%~dp0"

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
