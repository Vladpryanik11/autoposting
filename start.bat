@echo off
rem Запуск на Windows: бот + рабочий процесс. Остановить — Ctrl+C в этом окне.
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" -X utf8 main.py serve
pause
