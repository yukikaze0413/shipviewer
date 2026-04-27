@echo off
setlocal EnableExtensions EnableDelayedExpansion
title ShipViewer - Launcher

set "VENV_PATH=.venv"
set "PYTHON_EXE=%VENV_PATH%\Scripts\python.exe"
set "CONDA_ENVS_ROOT=G:\Tools\miniconda\envs"
set "BOOTSTRAP_PYTHON="
set "BOOTSTRAP_SOURCE="

echo Checking environment...
call :find_bootstrap_python
if errorlevel 1 exit /b 1

echo Selected bootstrap interpreter: %BOOTSTRAP_SOURCE%
call :print_python_version "%BOOTSTRAP_PYTHON%"

if exist "%PYTHON_EXE%" (
    call :is_supported_python "%PYTHON_EXE%"
    if errorlevel 1 (
        echo [INFO] Existing .venv uses an unsupported Python version. Recreating it...
        rmdir /s /q "%VENV_PATH%"
    )
)

if not exist "%PYTHON_EXE%" (
    echo [INFO] Creating virtual environment...
    "%BOOTSTRAP_PYTHON%" -m venv "%VENV_PATH%"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment with "%BOOTSTRAP_PYTHON%".
        pause
        exit /b 1
    )
)

echo [INFO] Ensuring pip is available in the virtual environment...
"%PYTHON_EXE%" -m ensurepip --upgrade >nul 2>&1
if errorlevel 1 (
    echo [WARN] ensurepip did not complete successfully. Trying to continue...
)

"%PYTHON_EXE%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] pip is not available in the virtual environment.
    pause
    exit /b 1
)

set "VENV_VERSION=unknown"
for /f "tokens=2 delims= " %%i in ('""%PYTHON_EXE%" -V 2^>^&1"') do set "VENV_VERSION=%%i"
echo Using virtual environment Python %VENV_VERSION%

echo Upgrading pip...
"%PYTHON_EXE%" -m pip install --upgrade pip --quiet
if errorlevel 1 (
    echo [ERROR] Failed to upgrade pip.
    pause
    exit /b 1
)

echo Installing dependencies from requirements.txt...
"%PYTHON_EXE%" -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [WARN] Some packages from requirements.txt could not be installed.
    echo [WARN] Retrying core dependencies without optional 3DM support...
    "%PYTHON_EXE%" -m pip install "PySide6>=6.5.0" "vtk>=9.2.0" "numpy>=1.24.0" --quiet
    if errorlevel 1 (
        echo [ERROR] Failed to install required dependencies.
        pause
        exit /b 1
    )

    echo Installing optional 3DM support (rhino3dm)...
    "%PYTHON_EXE%" -m pip install --only-binary=:all: "rhino3dm>=8.0.0" --quiet
    if errorlevel 1 (
        echo [WARN] Optional package rhino3dm could not be installed.
        echo [WARN] ShipViewer will still start, but .3dm files will not open in this environment.
    )
)

echo.
echo ==========================================
echo        Starting ShipViewer...
echo ==========================================
echo.

"%PYTHON_EXE%" main.py

if errorlevel 1 (
    echo.
    echo [INFO] The application exited unexpectedly.
    pause
)

exit /b 0

:find_bootstrap_python
if exist "%CONDA_ENVS_ROOT%" (
    for %%V in (3.12 3.11 3.10) do (
        for /d %%D in ("%CONDA_ENVS_ROOT%\*") do (
            if exist "%%~fD\python.exe" (
                call :matches_version "%%~fD\python.exe" %%V
                if not errorlevel 1 (
                    set "BOOTSTRAP_PYTHON=%%~fD\python.exe"
                    set "BOOTSTRAP_SOURCE=Conda env %%~nxD (Python %%V)"
                    exit /b 0
                )
            )
        )
    )
)

for %%V in (3.12 3.11 3.10) do (
    for /f "usebackq delims=" %%i in (`py -%%V -c "import sys; print(sys.executable)" 2^>nul`) do (
        if not defined BOOTSTRAP_PYTHON (
            set "BOOTSTRAP_PYTHON=%%i"
            set "BOOTSTRAP_SOURCE=Python Launcher py -%%V"
            exit /b 0
        )
    )
)

for /f "usebackq delims=" %%i in (`python -c "import sys; print(sys.executable)" 2^>nul`) do (
    call :is_supported_python "%%i"
    if not errorlevel 1 (
        set "BOOTSTRAP_PYTHON=%%i"
        set "BOOTSTRAP_SOURCE=System python"
        exit /b 0
    )
)

echo [ERROR] No compatible Python 3.10, 3.11, or 3.12 interpreter was found.
echo [ERROR] Checked Conda environments under %CONDA_ENVS_ROOT% and the Python launcher.
pause
exit /b 1

:matches_version
"%~1" -c "import sys; raise SystemExit(0 if sys.version.startswith('%~2.') else 1)" >nul 2>&1
exit /b %errorlevel%

:is_supported_python
"%~1" -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] <= (3, 12) else 1)" >nul 2>&1
exit /b %errorlevel%

:print_python_version
"%~1" -V 2>nul
exit /b 0
