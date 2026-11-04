@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 素材工坊：视频 → 透明帧序列。界面里改参数、调好再生成。
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" build\workshop.py 1>>build\workshop.log 2>&1
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo 找不到 Python。请先建好 .venv 并装 requirements.txt
        pause
        exit /b 1
    )
    python build\workshop.py 1>>build\workshop.log 2>&1
)
