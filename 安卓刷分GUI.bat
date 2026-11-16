@echo off
setlocal
cd /d "%~dp0"
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [ERROR] Project Python was not found: %PYTHON_EXE%
    pause
    exit /b 1
)
"%PYTHON_EXE%" "%~dp0android_score_gui.py"
if errorlevel 1 pause
exit /b %errorlevel%
