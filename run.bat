@echo off
REM Launches the VideoAnnotations studio.
REM Edit VIDEO_DIR / ANNOTATION_FILE here if your layout differs from the defaults in config.py.

set "VIDEO_DIR=D:\NewFolder(3)\videoProject"
set "ANNOTATION_FILE=%~dp0\data\annotations.txt"

REM Use the same venv ann_rand2.1.py used (adjust if yours lives elsewhere)
set "PY=C:\Users\bergs\venv\Scripts\python.exe"

if not exist "%PY%" (
    echo Python not found at %PY%
    echo Edit run.bat to point at your Python.
    pause
    exit /b 1
)

"%PY%" -m pip install -q -r "%~dp0requirements.txt"
"%PY%" "%~dp0app.py" --host 127.0.0.1 --port 5000

pause
