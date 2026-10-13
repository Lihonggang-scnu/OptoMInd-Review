import importlib.metadata as md, importlib.util, json, pathlib, sys
import optomind_optics, optomind_research, tmm_engine
path = pathlib.Path(r"D:\OptoMind-Article-3\repos\OptoMInd-Review\optomind_research\runtime\upgrade3\module4\runtime.py")
spec = importlib.util.spec_from_file_location("optomind_m4_runtime_bridge", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
dist = [d.metadata["Name"] for d in md.distributions() if (d.metadata.get("Name") or "").lower() == "optomind"]
print(json.dumps({"python": sys.version, "runtime": str(path), "loaded": module.__file__, "article": optomind_optics.__file__, "review": optomind_research.__file__, "tmm": tmm_engine.__file__, "optomind_distributions": dist}, sort_keys=True))