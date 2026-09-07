@echo off
cd /d "%~dp0"
chcp 65001 >nul
".venv\Scripts\python.exe" "scripts\test_latency.py" %*
pause
