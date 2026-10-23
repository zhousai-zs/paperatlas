@echo off
setlocal
set PYTHONUTF8=1
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Please install the project first: uv sync --locked
  exit /b 2
)
"%~dp0.venv\Scripts\python.exe" -m paperatlas --data-dir "%~dp0.paperatlas" %*
