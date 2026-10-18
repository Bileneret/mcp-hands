@echo off
rem Панель Джарвиса: настройка микрофона, голоса и проверки.
cd /d "%~dp0"
.venv\Scripts\python.exe -u tools\jarvis_panel.py
pause
