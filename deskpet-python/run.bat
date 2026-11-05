@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem .venv 由 uv 管理（uv sync 生成），优先用它；没有才退回全局 pythonw

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
