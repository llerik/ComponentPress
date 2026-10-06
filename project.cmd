@echo off
setlocal
pushd "%~dp0"

set "ACTION=%~1"
if not defined ACTION set "ACTION=run"

if /i "%ACTION%"=="setup" goto setup_command
if /i "%ACTION%"=="run" goto run
if /i "%ACTION%"=="cli" goto cli
if /i "%ACTION%"=="test" goto test
if /i "%ACTION%"=="release" goto release

echo Usage: project.cmd [setup^|run^|cli^|test^|release] [arguments]
echo   setup    Create the Python 3.14 environment and install dependencies
echo   run      Launch the desktop application (default)
echo   cli      Pass remaining arguments to the ComponentPress CLI
echo   test     Run the test suite
echo   release  Build and smoke-check the prepared Windows release
popd
exit /b 2

:ensure_setup
if exist ".venv\.componentpress-ready" exit /b 0
call :setup
exit /b %errorlevel%

:setup_command
call :setup
if errorlevel 1 goto failed
popd
exit /b 0

:setup
if not exist ".venv\Scripts\python.exe" (
    py -3.14 -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -r requirements\windows-py314.lock
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install --no-build-isolation --no-deps -e .
if errorlevel 1 goto failed
type nul > ".venv\.componentpress-ready"
exit /b 0

:run
call :ensure_setup
if errorlevel 1 goto failed
call :invoke_cli %*
set "RESULT=%errorlevel%"
popd
exit /b %RESULT%

:cli
call :ensure_setup
if errorlevel 1 goto failed
call :invoke_cli %*
set "RESULT=%errorlevel%"
popd
exit /b %RESULT%

:invoke_cli
shift
".venv\Scripts\python.exe" -m componentpress %1 %2 %3 %4 %5 %6 %7 %8 %9
exit /b %errorlevel%

:test
call :ensure_setup
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pytest %2 %3 %4 %5 %6 %7 %8 %9
set "RESULT=%errorlevel%"
popd
exit /b %RESULT%

:release
call :ensure_setup
if errorlevel 1 goto failed
".venv\Scripts\python.exe" packaging\build_release.py
set "RESULT=%errorlevel%"
popd
exit /b %RESULT%

:failed
set "RESULT=%errorlevel%"
if "%RESULT%"=="0" set "RESULT=1"
popd
exit /b %RESULT%
