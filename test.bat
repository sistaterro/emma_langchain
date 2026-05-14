@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [error] Virtual environment not found.
  echo [hint] Create it with: python -m venv .venv
  exit /b 1
)

echo [test] Checking Python syntax...
".venv\Scripts\python.exe" -m py_compile server.py prompts.py tests\test_core_endpoints.py tests\test_permissions.py tests\test_rag_pipeline.py
if errorlevel 1 exit /b 1

echo [test] Running unit tests...
".venv\Scripts\python.exe" -m unittest discover tests
if errorlevel 1 exit /b 1

echo [test] OK
