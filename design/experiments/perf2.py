import sys, time, statistics
sys.path.insert(0,"/tmp/claude-1000/-home-node-Softwares-Mathematica-MCP-WSTP/a5839c1e-76a0-4b7a-a5e4-c969bf1b5823/scratchpad")
from wstp_client import K
k = K(); k.ev('1')
for label, expr in [("trivial 1+1","1+1"),("symbolic Integrate","Integrate[1/(1+x^3),x]"),("100KB output",'StringRepeat["x",100000]')]:
    ts=[(time.time(), k.ev(expr, timeout=120)) and 0 or 0 for _ in range(0)]
    ts=[]
    for _ in range(20):
        t0=time.perf_counter(); k.ev(expr, timeout=120); ts.append((time.perf_counter()-t0)*1000)
    print(f"{label:22s} p50={statistics.median(ts):6.2f}ms min={min(ts):6.2f}ms", flush=True)
k.close()
