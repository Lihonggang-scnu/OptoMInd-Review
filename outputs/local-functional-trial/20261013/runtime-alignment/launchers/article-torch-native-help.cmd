@echo off
setlocal
set "PYTHONPATH="
set "PYTHON=D:\OptoMind-Article-3\local-integration\20261013\envs\tensor\Scripts\python.exe"
pushd "%SystemRoot%"
"%PYTHON%" -B -c "import importlib.metadata as m; import torch, openai, yaml, requests, json_repair, matplotlib, optomind_optics, tmm_engine; assert not [d for d in m.distributions() if (d.metadata.get('Name') or '').lower() == 'optomind']; print('article tensor imports ok', torch.__version__)"
if errorlevel 1 exit /b %errorlevel%
"%PYTHON%" -B -m tmm_engine.cli --help
if errorlevel 1 exit /b %errorlevel%
"%PYTHON%" -B -m optomind_optics.harness.science.lifetime_cli --help
popd
exit /b 0
