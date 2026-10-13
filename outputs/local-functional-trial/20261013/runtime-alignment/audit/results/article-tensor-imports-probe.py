import importlib.metadata as md, json, sys
import torch, matplotlib, openai, yaml, requests, json_repair, optomind_optics, tmm_engine
mods = {
    "torch": torch.__file__, "matplotlib": matplotlib.__file__, "openai": openai.__file__,
    "yaml": yaml.__file__, "requests": requests.__file__, "json_repair": json_repair.__file__,
    "optomind_optics": optomind_optics.__file__, "tmm_engine": tmm_engine.__file__,
}
names = {name: md.version(name) for name in ("torch", "json-repair", "matplotlib", "openai", "PyYAML", "requests")}
dist = [d.metadata["Name"] for d in md.distributions() if (d.metadata.get("Name") or "").lower() == "optomind"]
print(json.dumps({"python": sys.version, "modules": mods, "versions": names, "optomind_distributions": dist}, sort_keys=True))