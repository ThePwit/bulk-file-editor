@echo off
setlocal

set "APP_DIR=%~dp0"
set "APP_SCRIPT=%APP_DIR%bulk_name_edit.py"
set "PYTHON_EXE=python"
pushd "%APP_DIR%"

python -c "import tkinter as tk; root = tk.Tk(); root.withdraw(); root.destroy()" >nul 2>nul
if errorlevel 1 (
    if exist "C:\Program Files\Inkscape\bin\python.exe" (
        set "PYTHON_EXE=C:\Program Files\Inkscape\bin\python.exe"
    )
)

"%PYTHON_EXE%" "%APP_SCRIPT%"
if errorlevel 1 (
    echo.
    echo Bulk File Editor did not launch. The Python/Tkinter runtime reported an error.
    echo If this keeps happening, repair or reinstall Python and enable Tcl/Tk support.
    echo.
    pause
)
popd
