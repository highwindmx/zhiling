@echo off
setlocal
set "EXE=%~dp0src-tauri\target\release\deskpet-tauri.exe"
if not exist "%EXE%" set "EXE=%TEMP%\deskpet-tauri-target\release\deskpet-tauri.exe"
if not exist "%EXE%" (
  echo Release exe not found: src-tauri\target\release\deskpet-tauri.exe
  echo                      or %%TEMP%%\deskpet-tauri-target\release\deskpet-tauri.exe
  echo Build first: build.bat
  exit /b 1
)
REM Use a writable user-data folder next to the project. Some locked-down
REM sessions block WebView2 from creating its default folder under C:,
REM which surfaces as HRESULT 0x8000FFFF at startup.
if "%WEBVIEW2_USER_DATA_FOLDER%"=="" (
  set "WEBVIEW2_USER_DATA_FOLDER=%~dp0archive\wv2-profile"
)
start "" "%EXE%"
endlocal
