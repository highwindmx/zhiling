@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 素材工坊 = 素材包编辑器：uv 优先；没有 uv 就用 .venv（两条路径都能跑，失败都会说原因）
where uv >nul 2>nul
if not errorlevel 1 (
    echo [工坊] 用 uv run 启动（依赖由 uv.lock 保证一致）
    uv run python build\workshop.py
    if errorlevel 1 (
        echo.
        echo [工坊] uv run 失败，常见原因：
        echo   - 依赖没装：先在本目录执行 uv sync
        echo   - 仓库在共享盘/网络盘：uv 写 uv.lock 会「拒绝访问」，把仓库拷到本地磁盘再跑
        pause
    )
    exit /b 0
)

echo [工坊] 没有找到 uv，降级到 .venv\Scripts\python.exe
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" build\workshop.py
    if errorlevel 1 (
        echo.
        echo [工坊] .venv 里的解释器跑不起来：依赖可能没装全
        echo            .venv\Scripts\pip install -r requirements.txt
        pause
    )
    exit /b 0
)

echo.
echo [工坊] 没有可用的 Python 环境，二选一：
echo   1) 装 uv 后在本目录执行：uv sync
echo   2) 用 pip：python -m venv .venv，再 .venv\Scripts\pip install -r requirements.txt
echo 详见 README.md 的「安装依赖（uv 优先）」。
pause
exit /b 1
