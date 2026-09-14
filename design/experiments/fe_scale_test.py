import sys, time
sys.path.insert(0,"/tmp/claude-1000/-home-node-Softwares-Mathematica-MCP-WSTP/a5839c1e-76a0-4b7a-a5e4-c969bf1b5823/scratchpad")
from wstp_client import K
import os
NB = os.environ.get("MATHEMATICA_WSTP_TEST_NOTEBOOK", "")  # any large .nb
k = K()
print("pid", k.ev('$ProcessID'), flush=True)

print("\n[A] open the real 994-cell notebook through the offscreen FE", flush=True)
t0=time.time()
r = k.ev(f'UsingFrontEnd[nb = NotebookOpen["{NB}", Visible->False]; {{Length[Cells[nb]], Length[Cells[nb, CellStyle->"Input"]]}}]', timeout=300)
print(f"    {{total cells, Input cells}} = {r}   ({time.time()-t0:.1f}s)", flush=True)

print("\n[B] same file via kernel-only parse (no FE), for comparison", flush=True)
t0=time.time()
r2 = k.ev(f'nbx = Get["{NB}"]; {{Length[First[nbx]], Count[First[nbx], Cell[_, "Input", ___], Infinity]}}', timeout=300)
print(f"    {{total, Input}} = {r2}   ({time.time()-t0:.1f}s)", flush=True)

print("\n[C] typeset render of one real cell (headless 'screenshot')", flush=True)
t0=time.time()
r3 = k.ev('UsingFrontEnd[img = Rasterize[NotebookRead[Cells[nb][[3]]], "Image", ImageResolution->96]; ImageDimensions[img]]', timeout=300)
print(f"    cell 3 raster dims = {r3}  ({time.time()-t0:.1f}s)", flush=True)
r3b = k.ev('UsingFrontEnd[Export["/tmp/claude-1000/-home-node-Softwares-Mathematica-MCP-WSTP/a5839c1e-76a0-4b7a-a5e4-c969bf1b5823/scratchpad/cell3.png", img]]', timeout=120)
print(f"    exported: {r3b}", flush=True)

print("\n[D] ABORT an evaluation dispatched through the front end", flush=True)
t0=time.time()
r4 = k.ev('UsingFrontEnd[nb2 = CreateDocument[{Cell["Do[zz=k,{k,1,10^12}]; \\"DONE\\"","Input"]}, Visible->False]; '
          'SelectionMove[nb2, All, Notebook]; SelectionEvaluate[nb2]; "returned"]', timeout=40, abort_after=3.0)
print(f"    result after {time.time()-t0:.1f}s: {r4!r}", flush=True)
print("    kernel alive? pid =", k.ev('$ProcessID', timeout=30), flush=True)
print("    FE still alive? ->", k.ev('UsingFrontEnd[ToString[$FrontEnd]]', timeout=60), flush=True)
print("    notebook still there? cells =", k.ev('UsingFrontEnd[Length[Cells[nb]]]', timeout=60), flush=True)
k.close()
