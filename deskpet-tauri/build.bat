@echo off
setlocal EnableExtensions
REM ============================================================
REM 一键构建 deskpet-tauri：暂存前端 -> 准备素材 -> cargo build --release
REM
REM 前端是零构建（ES Module），没有 npm/vite 编译步骤；但 tauri.conf.json 的
REM frontendDist 指向 frontend-dist，该目录由 build\prepare_assets.py --frontend 生成，
REM 只有几十 KB 的 HTML/JS（会被编译进 exe）。
REM
REM 素材（帧 + mp4）**不进 exe**：运行时从 exe 同级的 assets\ 目录读（或环境变量
REM DESKPET_ASSETS 指定）。所以素材准备失败不会中断编译，只影响运行；换素材也不
REM 需要重新编译。
REM
REM 用法：
REM   build.bat                    -> release，target 目录放 %TEMP%
REM   build.bat debug              -> debug 构建
REM   set DESKPET_TARGET_DIR=D:\t  -> 自定义 target 目录
REM ============================================================
set "PROJ=%~dp0"
set "MODE=%~1"
if "%MODE%"=="" set "MODE=release"
if not defined DESKPET_TARGET_DIR set "DESKPET_TARGET_DIR=%TEMP%\deskpet-tauri-target"

echo ============================================================
echo  deskpet-tauri build (%MODE%)
echo  target: %DESKPET_TARGET_DIR%
echo ============================================================

pushd "%PROJ%"

echo.
echo [1/3] 暂存前端（frontend -> frontend-dist）
python build\prepare_assets.py --frontend
if errorlevel 1 goto fail_frontend

echo.
echo [2/3] 准备素材（deskpet-mini -> assets\，运行期用，失败不中断编译）
python build\prepare_assets.py --assets
if errorlevel 1 echo [警告] 素材准备失败：编译继续，但运行前要自己补 assets\ 目录（见上面输出）

echo.
echo [3/3] cargo build --%MODE%
pushd src-tauri
cargo build --%MODE% --target-dir "%DESKPET_TARGET_DIR%"
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" goto fail_cargo

echo.
echo [OK] 产物 exe：%DESKPET_TARGET_DIR%\%MODE%\deskpet-tauri.exe
for %%F in ("%DESKPET_TARGET_DIR%\%MODE%\deskpet-tauri.exe") do echo [OK] exe 大小：%%~zF 字节
echo [OK] 素材目录：%PROJ%assets  （分发时与 exe 放同一目录，或用 DESKPET_ASSETS 指过去）
echo      最小可运行组合：deskpet-tauri.exe + assets\(clips.json, meta.json, state-map.json, frames_*\)
popd
exit /b 0

:fail_frontend
echo.
echo [FAIL] 第 1 步暂存前端失败。
echo        检查 python 是否在 PATH，以及 frontend\index.html、frontend\src 是否存在。
popd
exit /b 1

:fail_cargo
echo.
echo [FAIL] 第 3 步 cargo build 失败（退出码 %RC%）。
echo        若报 "拒绝访问 (os error 5)"，说明 target 目录不可写，
echo        换一个可写目录：set DESKPET_TARGET_DIR=C:\some\writable\dir
echo        若报 "link.exe not found"，安装 VS Build Tools 的 C++ 桌面开发组件。
popd
exit /b %RC%
