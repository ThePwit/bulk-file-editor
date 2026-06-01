@echo off
setlocal

set "APP_DIR=%~dp0"
set "APP_SCRIPT=%APP_DIR%bulk_name_edit.py"
set "PYTHON_EXE=python"
set "LOG_FILE=%APP_DIR%launch_error.log"
pushd "%APP_DIR%"

if exist "%LOG_FILE%" del "%LOG_FILE%"
echo Bulk File Editor launch log>"%LOG_FILE%"
echo Started: %DATE% %TIME%>>"%LOG_FILE%"
echo App dir: %APP_DIR%>>"%LOG_FILE%"

python -c "import tkinter as tk; root = tk.Tk(); root.withdraw(); root.destroy()" >>"%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo Default python cannot start Tkinter; trying fallback.>>"%LOG_FILE%"
    if exist "C:\Program Files\Inkscape\bin\python.exe" (
        set "PYTHON_EXE=C:\Program Files\Inkscape\bin\python.exe"
    ) else (
        echo Inkscape fallback Python was not found.>>"%LOG_FILE%"
    )
)

echo Using Python: %PYTHON_EXE%>>"%LOG_FILE%"
"%PYTHON_EXE%" "%APP_SCRIPT%" %* >>"%LOG_FILE%" 2>&1
set "APP_EXIT=%ERRORLEVEL%"

if "%APP_EXIT%"=="0" (
    del "%LOG_FILE%" >nul 2>nul
)

if not "%APP_EXIT%"=="0" (
    echo.
    echo Bulk File Editor did not launch. The Python/Tkinter runtime reported an error.
    echo Details were saved to:
    echo %LOG_FILE%
    echo If this keeps happening, repair or reinstall Python and enable Tcl/Tk support.
    echo.
    pause
)
popd
