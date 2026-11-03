@echo off
setlocal enableextensions

rem ============================================================
rem  ollama_agent_ui.bat  --  Web UI launcher for external models
rem
rem  Difference from ollama_agent.bat:
rem    1. does NOT probe http://127.0.0.1:11434
rem    2. does NOT start "ollama serve" or the desktop app
rem    3. does NOT warm up the model (passes --no-warm)
rem  => no waiting on a local Ollama you may not even have installed,
rem     and no 1-2 min weight loading before the page opens.
rem  Measured on this machine: page reachable in ~3.4s (2.2s of that is
rem  plain Python import). For comparison, README records a 73s cold start
rem  when the model is warmed up -- that wait is what this script removes.
rem
rem  Usage (this folder is on PATH, so the bare name works anywhere):
rem      ollama_agent_ui                 -> Web UI, workspace = %CD%
rem      ollama_agent_ui --port 9000     -> Web UI on port 9000
rem      ollama_agent_ui -w D:/docs      -> Web UI rooted at D:/docs
rem      ollama_agent_ui --host https://xxx/v1 --provider openai ^
rem                      --api-key sk-xxx --model gpt-4o
rem                                      -> go straight at an external model
rem
rem  Typical flow for external-model users:
rem      run this once -> Settings / Model management / New
rem      fill the /v1 address + Key -> Test -> Save
rem  The saved profile is restored on every later launch, so no flags
rem  are needed after the first time.
rem
rem  Port check: only when --port is NOT given (default 8888).
rem  It has to be done here because Windows lets a SECOND process bind
rem  an already-listening port (HTTPServer.allow_reuse_address == 1),
rem  so python exits with no error while all real traffic still goes to
rem  the old instance -- the browser would show the old window and look
rem  like the new launch did nothing.
rem ============================================================

set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"
set "ENTRY=%APP_DIR%\ollama_agent.py"

if not exist "%ENTRY%" (
    echo [ollama_agent_ui] entry script not found: "%ENTRY%"
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
    echo [ollama_agent_ui] Python not found. Install Python 3.10+ and add it to PATH.
    exit /b 1
)

echo [ollama_agent_ui] Web UI only -- no local Ollama check, no model warmup.
echo [ollama_agent_ui] workspace = %CD%

rem ---- refuse to start on an occupied default port (see header note) ----
rem  Walk the args with shift instead of `echo %* | findstr`: expanding %*
rem  into a command line lets an "&" inside a path/key split the line, and
rem  "findstr ':8888 '" treats the space as an OR separator (matches 88880).
rem  --help / --version never bind a port, so they skip the check entirely.
set "HAS_PORT="
set "NO_SERVE="
:scan_args
if "%~1"=="" goto :scan_done
if /i "%~1"=="--port"     set "HAS_PORT=1"
if /i "%~1"=="-h"         set "NO_SERVE=1"
if /i "%~1"=="--help"     set "NO_SERVE=1"
if /i "%~1"=="--version"  set "NO_SERVE=1"
shift
goto :scan_args
:scan_done

if defined NO_SERVE goto :port_ok
if defined HAS_PORT goto :port_ok
netstat -ano | findstr /c:":8888 " | findstr /c:"LISTENING" >nul
if errorlevel 1 goto :port_ok

echo.
echo [ollama_agent_ui] Port 8888 is already in use.
echo [ollama_agent_ui] Most likely an older Web UI is still running: on Windows the
echo [ollama_agent_ui] new process would start "fine" but the browser would show the
echo [ollama_agent_ui] OLD one instead. Pick one of these:
echo [ollama_agent_ui]   1) close the old window / kill that python process, retry
echo [ollama_agent_ui]   2) ollama_agent_ui --port 9000
echo.
exit /b 1

:port_ok
echo [ollama_agent_ui] first time? open Settings / Model management / New to add a model.

rem ---- --ui is always on; --no-warm skips weight loading; %* passes the rest ----
%PY% "%ENTRY%" --ui --no-warm %*
exit /b %ERRORLEVEL%
