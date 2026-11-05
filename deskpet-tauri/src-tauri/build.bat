@echo off
setlocal
set MODE=%1
if "%MODE%"=="" set MODE=release
call "C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
cd /d "%~dp0"
echo ==== MSVC ENV READY, cargo build --%MODE% ====
cargo build --%MODE% 2>&1
echo ==== CARGO BUILD EXIT %ERRORLEVEL% ====
exit /b %ERRORLEVEL%
