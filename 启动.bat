@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" app.py
    exit /b 0
)

set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% app.py
if errorlevel 1 pause
