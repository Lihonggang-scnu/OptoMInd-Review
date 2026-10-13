@echo off
setlocal
set "PYTHONPATH="
set "PYTHON=D:\OptoMind-Article-3\local-preparation\envs\review\Scripts\python.exe"
pushd "%SystemRoot%"
"%PYTHON%" -B -c "import importlib.metadata as m; import matplotlib, openai, yaml, json_repair, requests, optomind_research; assert not [d for d in m.distributions() if (d.metadata.get('Name') or '').lower() == 'optomind']; print('review imports ok')"
if errorlevel 1 exit /b %errorlevel%
"%PYTHON%" -B "D:\OptoMind-Article-3\repos\OptoMInd-Review\run_review_harness.py" --help
if errorlevel 1 exit /b %errorlevel%
"%PYTHON%" -B "D:\OptoMind-Article-3\repos\OptoMInd-Review\quickstart.py" --help
popd
exit /b 0
