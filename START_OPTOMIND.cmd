@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 quickstart.py ui
) else (
  python quickstart.py ui
)
if errorlevel 1 pause
endlocal
