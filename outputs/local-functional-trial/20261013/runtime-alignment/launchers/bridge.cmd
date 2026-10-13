@echo off
setlocal
set "PYTHONPATH="
set "PYTHONSAFEPATH=1"
"D:\OptoMind-Article-3\local-functional-trial\20261013\runtime-alignment\bridge-venv\Scripts\python.exe" -B -X utf8 %*
exit /b %errorlevel%
