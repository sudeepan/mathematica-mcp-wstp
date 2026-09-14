import sys, time
sys.path.insert(0,"/tmp/claude-1000/-home-node-Softwares-Mathematica-MCP-WSTP/a5839c1e-76a0-4b7a-a5e4-c969bf1b5823/scratchpad")
from wstp_client import K
def codes(s): return "{" + ",".join(str(ord(c)) for c in s) + "}"
k = K(); print("pid", k.ev('$ProcessID'), flush=True)
k.ev('UsingFrontEnd[$FrontEnd]', timeout=120)

print("\n[0] baseline: how long does the loop take in the kernel directly?", flush=True)
t0=time.time(); print("   ", k.ev('Do[zz=k,{k,1,60000000}]; AbsoluteTiming[1]', timeout=300), f"wall={time.time()-t0:.1f}s", flush=True)

LOOP = "Do[zz=k,{k,1,60000000}]"
print("\n[1] dispatch that loop to the FE; is our link free meanwhile?", flush=True)
k.ev(f'UsingFrontEnd[nb = CreateDocument[{{Cell[BoxData[FromCharacterCode[{codes(LOOP)}]], "Input"]}}, Visible->False]; 0]', timeout=120)
t0=time.time()
k.ev('UsingFrontEnd[SelectionMove[nb, All, Notebook]; SelectionEvaluate[nb]; 0]', timeout=60)
print(f"   dispatch returned at t={time.time()-t0:.2f}s", flush=True)
t1=time.time(); pid=k.ev('$ProcessID', timeout=30)
print(f"   our link mid-eval: $ProcessID={pid} answered in {time.time()-t1:.2f}s", flush=True)
t2=time.time(); n="1"
while time.time()-t0 < 200:
    n = k.ev('UsingFrontEnd[Length[Cells[nb]]]', timeout=60).strip()
    if n not in ("1",""): break
    time.sleep(0.5)
print(f"   FE eval completed at t={time.time()-t0:.1f}s (cells={n})", flush=True)

print("\n[2] ABORT an FE-dispatched eval via WSAbortMessage on OUR link", flush=True)
LONG = "Do[yy=k,{k,1,10^12}]"
k.ev(f'UsingFrontEnd[nb2 = CreateDocument[{{Cell[BoxData[FromCharacterCode[{codes(LONG)}]], "Input"]}}, Visible->False]; 0]', timeout=120)
k.ev('UsingFrontEnd[SelectionMove[nb2, All, Notebook]; SelectionEvaluate[nb2]; 0]', timeout=60)
time.sleep(3)
t0=time.time()
# our link is idle; abort must be delivered while the FE eval occupies the kernel
r = k.ev('UsingFrontEnd[Length[Cells[nb2]]]', timeout=30, abort_after=0.5)
print(f"   probe w/ WSAbortMessage -> {r!r} ({time.time()-t0:.1f}s)", flush=True)
time.sleep(2)
print("   nb2 cells:", k.ev('UsingFrontEnd[Length[Cells[nb2]]]', timeout=60), flush=True)
print("   nb2 last:", k.ev('UsingFrontEnd[StringTake[ToString[NotebookRead[Cells[nb2][[-1]]]],UpTo[100]]]', timeout=60), flush=True)

print("\n[3] ABORT via FrontEndTokenExecute[EvaluatorAbort]", flush=True)
t0=time.time()
print("   sent ->", k.ev('UsingFrontEnd[FrontEndTokenExecute["EvaluatorAbort"]; 0]', timeout=60), f"({time.time()-t0:.1f}s)", flush=True)
time.sleep(4)
print("   nb2 cells:", k.ev('UsingFrontEnd[Length[Cells[nb2]]]', timeout=60), flush=True)
print("   nb2 last:", k.ev('UsingFrontEnd[StringTake[ToString[NotebookRead[Cells[nb2][[-1]]]],UpTo[100]]]', timeout=60), flush=True)
print("   kernel pid:", k.ev('$ProcessID', timeout=60), flush=True)
k.close()
