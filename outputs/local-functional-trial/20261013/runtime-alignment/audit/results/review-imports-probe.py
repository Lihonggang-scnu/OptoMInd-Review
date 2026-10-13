import importlib.metadata as md, json, sys
import matplotlib, openai, yaml, json_repair, requests, optomind_research, config
mods = {
    "matplotlib": matplotlib.__file__, "openai": openai.__file__,
    "yaml": yaml.__file__, "json_repair": json_repair.__file__,
    "requests": requests.__file__, "optomind_research": optomind_research.__file__,
    "config": config.__file__,
}
names = {name: md.version(name) for name in ("agentscope", "json-repair", "matplotlib", "openai", "PyYAML", "requests")}
dist = [d.metadata["Name"] for d in md.distributions() if (d.metadata.get("Name") or "").lower() == "optomind"]
print(json.dumps({"python": sys.version, "modules": mods, "versions": names, "optomind_distributions": dist}, sort_keys=True))