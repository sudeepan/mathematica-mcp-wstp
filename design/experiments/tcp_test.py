import ctypes, sys
sys.path.insert(0,"/tmp/claude-1000/-home-node-Softwares-Mathematica-MCP-WSTP/a5839c1e-76a0-4b7a-a5e4-c969bf1b5823/scratchpad")
import wstp_client as W
w = W.w
env = w.WSInitialize(None)
for spec in [b"-linkmode listen -linkprotocol TCPIP -linkname 21345",
             b"-linkmode listen -linkprotocol SharedMemory -linkname mcptest1",
             b"-linkmode listen -linkname 21346"]:
    e = ctypes.c_int(0)
    l = w.WSOpenString(env, spec, ctypes.byref(e))
    print(f"{spec.decode():62s} -> link={'OK' if l else 'NULL'} err={e.value}", flush=True)
    if l: w.WSClose(l)
