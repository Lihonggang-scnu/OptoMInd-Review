@echo off
setlocal
set "PYTHONPATH="
set "PYTHON=D:\OptoMind-Article-3\local-functional-trial\20261013\runtime-alignment\bridge-venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo bridge interpreter is not provisioned; run the approved alignment setup first.
  exit /b 2
)
pushd "%SystemRoot%"
"%PYTHON%" -B -c "import importlib.metadata as md, importlib.util, pathlib, sys; import optomind_optics, optomind_research, tmm_engine; p=pathlib.Path(r'D:\OptoMind-Article-3\repos\OptoMInd-Review\optomind_research\runtime\upgrade3\module4\runtime.py'); s=importlib.util.spec_from_file_location('optomind_m4_runtime_bridge', p); m=importlib.util.module_from_spec(s); sys.modules[s.name]=m; s.loader.exec_module(m); assert str(p).lower() == str(pathlib.Path(m.__file__)).lower(); assert not [d for d in md.distributions() if (d.metadata.get('Name') or '').lower() == 'optomind']; print('bridge imports ok')"
popd
exit /b %errorlevel%
