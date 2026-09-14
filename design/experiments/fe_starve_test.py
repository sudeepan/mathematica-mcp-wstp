import sys, time
sys.path.insert(0,"/tmp/claude-1000/-home-node-Softwares-Mathematica-MCP-WSTP/a5839c1e-76a0-4b7a-a5e4-c969bf1b5823/scratchpad")
from wstp_client import K
def codes(s): return "{" + ",".join(str(ord(c)) for c in s) + "}"
k = K(); print("pid", k.ev('$ProcessID'), flush=True)
k.ev('UsingFrontEnd[$FrontEnd]', timeout=120)
LOOP = "Do[zz=k,{k,1,60000000}]"   # measured 5.1s in-kernel
k.ev(f'UsingFrontEnd[nb = CreateDocument[{{Cell[BoxData[FromCharacterCode[{codes(LOOP)}]], "Input"]}}, Visible->False]; 0]', timeout=120)
k.ev('UsingFrontEnd[SelectionMove[nb, All, Notebook]; SelectionEvaluate[nb]; 0]', timeout=60)
print("dispatched; now sleeping 40s with ZERO link traffic", flush=True)
time.sleep(40)
print("after quiet 40s, cells =", k.ev('UsingFrontEnd[Length[Cells[nb]]]', timeout=60), flush=True)
print("  (1 = never ran; 2 = completed)", flush=True)
# Now the supported synchronous path for comparison: evaluate the cell IN the kernel
t0=time.time()
r = k.ev('ToString[ToExpression[NotebookRead[Cells[nb][[1]]][[1]] /. BoxData[b_]:>b, StandardForm]]', timeout=300)
print(f"kernel-side eval of the same cell's boxes: {r!r} in {time.time()-t0:.1f}s", flush=True)
k.close()
