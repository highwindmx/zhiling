@echo off
setlocal EnableExtensions
REM ============================================================
REM 一键构建 deskpet-tauri：同步素材 -> cargo build --release
REM 前端是零构建（ES Module），没有 npm/vite 编译步骤；
REM 但 tauri.conf.json 的 frontendDist 指向 frontend-dist，
REM 该目录由 build\prepare_assets.py 生成，所以必须先跑素材同步。
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
echo [1/2] 同步素材 + 生成 clips.json
python build\prepare_assets.py
if errorlevel 1 goto fail_assets

echo.
echo [2/2] cargo build --%MODE%
pushd src-tauri
cargo build --%MODE% --target-dir "%DESKPET_TARGET_DIR%"
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" goto fail_cargo

echo.
echo [OK] 产物：%DESKPET_TARGET_DIR%\%MODE%\deskpet-tauri.exe
for %%F in ("%DESKPET_TARGET_DIR%\%MODE%\deskpet-tauri.exe") do echo [OK] 大小：%%~zF 字节
popd
exit /b 0

:fail_assets
echo.
echo [FAIL] 第 1 步 prepare_assets.py 失败。
echo        常见原因：deskpet-mini\frames_* 不存在（帧序列不入库），
echo        先按 deskpet-mini\README.md 生成，或检查 python 是否在 PATH。
popd
exit /b 1

:fail_cargo
echo.
echo [FAIL] 第 2 步 cargo build 失败（退出码 %RC%）。
echo        若报 "拒绝访问 (os error 5)"，说明 target 目录不可写，
echo        换一个可写目录：set DESKPET_TARGET_DIR=C:\some\writable\dir
echo        若报 "link.exe not found"，安装 VS Build Tools 的 C++ 桌面开发组件。
popd
exit /b %RC%
