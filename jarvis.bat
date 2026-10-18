@echo off
rem Запуск Джарвиса (Ctrl+Alt+J — говорить). Окно можно свернуть.
cd /d "%~dp0"
.venv\Scripts\python.exe -u tools\jarvis.py
pause
