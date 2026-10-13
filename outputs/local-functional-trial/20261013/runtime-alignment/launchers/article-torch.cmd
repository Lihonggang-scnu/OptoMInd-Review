@echo off
setlocal
set "PYTHONPATH="
set "PYTHONSAFEPATH=1"
"D:\OptoMind-Article-3\local-integration\20261013\envs\tensor\Scripts\python.exe" -B -X utf8 %*
exit /b %errorlevel%
