@echo off
chcp 65001 >nul
rem Один раз: создаёт задачу Планировщика «WDA tunnel» (туннель к iPhone с правами администратора).
rem Потом start.py и прогон Edits поднимают связь сами, без окна «Да / Нет».
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','%~dp0tunnel-task.ps1'"
if exist "%~dp0tunnel-task.ok" (echo Готово: задача WDA tunnel создана.) else (echo Не создана - нажми Да в окне Windows.)
pause
