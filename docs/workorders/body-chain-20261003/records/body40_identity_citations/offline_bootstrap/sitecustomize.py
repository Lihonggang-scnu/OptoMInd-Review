# Offline tests load real submodules, without unrelated AgentScope runtime facade.
import pathlib, sys, types
root = pathlib.Path.cwd()
p = root / 'optomind_research' / 'runtime'
if p.is_dir():
    module = types.ModuleType('optomind_research.runtime')
    module.__path__ = [str(p)]
    module.__package__ = 'optomind_research.runtime'
    sys.modules['optomind_research.runtime'] = module
# This optional bootstrap is exclusively for the offline record's controls.
import socket

def _deny_network(*args, **kwargs):
    raise AssertionError('Network disabled for BODY40 offline subprocess')
socket.create_connection = _deny_network
socket.socket.connect = _deny_network
