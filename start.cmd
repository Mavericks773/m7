@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -c "import m7manager, docker, PySide6" >nul 2>nul
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -e ".[desktop]"
    if errorlevel 1 goto fail
)
start "" ".venv\Scripts\pythonw.exe" -m m7manager
exit /b 0
:fail
echo Startup failed. Please read the error above.
pause
exit /b 1
