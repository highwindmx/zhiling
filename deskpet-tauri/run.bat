@echo off
setlocal
set "PROJ=%~dp0"
set "EXE=%PROJ%src-tauri\target\release\deskpet-tauri.exe"
if not exist "%EXE%" set "EXE=%TEMP%\deskpet-tauri-target\release\deskpet-tauri.exe"
if not exist "%EXE%" (
  echo Release exe not found: src-tauri\target\release\deskpet-tauri.exe
  echo                      or %%TEMP%%\deskpet-tauri-target\release\deskpet-tauri.exe
  echo Build first: build.bat
  exit /b 1
)
REM 素材外置：exe 可能在 %TEMP%，素材固定在本目录 assets\（不存在时程序会弹红框报错，不会黑窗）
if "%DESKPET_ASSETS%"=="" (
  set "DESKPET_ASSETS=%PROJ%assets"
)
if not exist "%DESKPET_ASSETS%\clips.json" (
  echo [警告] 素材目录里没有 clips.json：%DESKPET_ASSETS%
  echo         先跑 python build\prepare_assets.py --assets
)
REM Use a writable user-data folder next to the project. Some locked-down
REM sessions block WebView2 from creating its default folder under C:,
REM which surfaces as HRESULT 0x8000FFFF at startup.
if "%WEBVIEW2_USER_DATA_FOLDER%"=="" (
  set "WEBVIEW2_USER_DATA_FOLDER=%PROJ%archive\wv2-profile"
)
start "" "%EXE%"
endlocal
