@echo off
setlocal enabledelayedexpansion
rem 看海量化回测平台（开源版）本地打包：PyInstaller -> Inno Setup
rem 需要 Python 3.11，并已安装 requirements.lock 与 requirements-build.lock

cd /d "%~dp0"

for /f "usebackq delims=" %%v in (`python -c "from version import get_version; print(get_version())"`) do set APP_VERSION=%%v
if "%APP_VERSION%"=="" (
    echo Error: cannot read version from version.py
    exit /b 1
)
echo Building khQuantOS %APP_VERSION% ...

if exist "dist" rd /s /q "dist"
if exist "build" rd /s /q "build"

pyinstaller --noconfirm khQuant_inno.spec
if !ERRORLEVEL! NEQ 0 (
    echo Error: PyInstaller build failed
    exit /b 1
)

set ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" set ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe
"%ISCC%" /DMyAppVersion=%APP_VERSION% installer.iss
if !ERRORLEVEL! NEQ 0 (
    echo Error: Inno Setup compilation failed
    exit /b 1
)

echo Done: Output\khQuantOS_Setup_V%APP_VERSION%.exe
exit /b 0
