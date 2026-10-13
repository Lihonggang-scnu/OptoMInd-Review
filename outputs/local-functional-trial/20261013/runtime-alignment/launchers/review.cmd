@echo off
setlocal
set "PYTHONPATH="
set "PYTHONSAFEPATH=1"
"D:\OptoMind-Article-3\local-preparation\envs\review\Scripts\python.exe" -B -X utf8 %*
exit /b %errorlevel%
