@echo off
setlocal enableextensions

rem ============================================================
rem  ollama_agent launcher  --  Web UI (--ui) is the default.
rem
rem  Usage (after this folder is on PATH):
rem      ollama_agent                  -> start Web UI
rem      ollama_agent --port 9000      -> Web UI on port 9000
rem      ollama_agent -w D:/docs       -> Web UI rooted at D:/docs
rem      ollama_agent -p "question"    -> single shot (no UI)
rem      ollama_agent --help           -> CLI help
rem
rem  The workspace stays the directory you run it from (%CD%),
rem  the program files are resolved from the bat location.
rem
rem  Before launching it checks http://127.0.0.1:11434 and starts the
rem  Ollama server when that is not running.
rem ============================================================

set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"
set "ENTRY=%APP_DIR%\ollama_agent.py"

if not exist "%ENTRY%" (
    echo [ollama_agent] entry script not found: "%ENTRY%"
    exit /b 1
)

rem ---- pick interpreter: project venv > python on PATH > py launcher ----
set "PY="
if exist "%APP_DIR%\.venv\Scripts\python.exe" set PY="%APP_DIR%\.venv\Scripts\python.exe"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    echo [ollama_agent] Python not found. Install Python 3.10+ and add it to PATH.
    exit /b 1
)

set "FIRST=%~1"

rem ---- help/version must not touch the server ----
if /i "%FIRST%"=="-h"        goto :run_plain
if /i "%FIRST%"=="--help"    goto :run_plain
if /i "%FIRST%"=="--version" goto :run_plain

rem ---- make sure the Ollama server is up ----
call :ensure_ollama

rem ---- --ui by default; pass everything else through untouched ----
if "%FIRST%"==""             goto :run_ui
if /i "%FIRST%"=="--ui"      goto :run_plain
if /i "%FIRST%"=="-p"        goto :run_plain
if /i "%FIRST%"=="--prompt"  goto :run_plain

:run_ui
echo [ollama_agent] starting Web UI, workspace = %CD%
%PY% "%ENTRY%" --ui %*
exit /b %ERRORLEVEL%

:run_plain
%PY% "%ENTRY%" %*
exit /b %ERRORLEVEL%


rem ============================================================
rem  helpers
rem ============================================================

:ensure_ollama
call :ollama_up
if not errorlevel 1 (
    echo [ollama_agent] Ollama server OK.
    goto :eof
)

echo [ollama_agent] Ollama server not running on 127.0.0.1:11434, starting it...

set "APP_EXE="
set "OLLAMA_DIR="
for %%I in (ollama.exe) do if not "%%~dp$PATH:I"=="" set "OLLAMA_DIR=%%~dp$PATH:I"
if defined OLLAMA_DIR if exist "%OLLAMA_DIR%ollama app.exe" set "APP_EXE=%OLLAMA_DIR%ollama app.exe"
if not defined APP_EXE if exist "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe" set "APP_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"

if defined APP_EXE (
    rem desktop app: lives in the tray, no extra console window
    start "" "%APP_EXE%"
) else (
    where ollama >nul 2>nul
    if errorlevel 1 (
        echo [ollama_agent] WARNING: ollama.exe not found in PATH.
        echo [ollama_agent] Install it from https://ollama.com  or run "ollama serve" yourself.
        goto :eof
    )
    echo [ollama_agent] starting "ollama serve" in a minimized window...
    start "ollama serve" /min ollama serve
)

set /a tries=0
:ollama_wait
call :ollama_up
if not errorlevel 1 (
    echo [ollama_agent] Ollama server ready.
    goto :eof
)
set /a tries+=1
if %tries% GEQ 40 (
    echo [ollama_agent] WARNING: Ollama still not responding after ~40s.
    echo [ollama_agent] Start it manually with:  ollama serve
    goto :eof
)
ping -n 2 127.0.0.1 >nul
goto :ollama_wait

rem -- returns 0 when the server answers on 11434, non-zero otherwise --
:ollama_up
where curl >nul 2>nul
if errorlevel 1 (
    netstat -ano | findstr ":11434" >nul
    exit /b %ERRORLEVEL%
)
rem --noproxy is required: a global http_proxy would otherwise answer for us
curl -s -m 2 --noproxy "*" -o nul "http://127.0.0.1:11434/api/version" 2>nul
exit /b %ERRORLEVEL%
