# Engineering notes

Working notes kept from the build: what was measured, what broke, and the
reasoning behind decisions that are not obvious from the code. Not the public
README — see `../README.md` for that.

A Mathematica MCP server built on WSTP instead of wolframclient. Self-contained:
no dependency on any other checkout, and no third-party Python packages.

**Status:** working server, and it passes its acceptance test. 23/23 unit tests
against a real kernel, 50/50 end-to-end over the MCP protocol (including
aborting a running evaluation from a concurrent request). It has also been run
against a ~1000-cell notebook driving a 20-way `LaunchKernels[]` fan-out and
external solver processes: every code cell replayed, one non-terminating cell
aborted and stepped over, and the process table returned to exactly its
baseline. That last part is what the previous server failed, stranding several
GB per run.

## Why

Three limits in the wolframclient-based design, all measured, all removed here:

| | before | now | |
|---|---|---|---|
| abort a running evaluation | impossible — `SIGINT` kills the kernel and loses all state | `$Aborted`, same kernel, state intact | tested |
| detect a dead kernel | hangs forever | raises in ~0.3s | tested |
| round-trip floor | ~30 ms | 0.27 ms | measured |

Plus the leak: the old restart path signalled only the master kernel, stranding
`LaunchKernels[]` subkernels. A snapshot of this machine found 37 kernels,
7 orphaned, ~23.9 GB resident. Kernels here run in their own process group with
a durable pid registry and a startup reaper.

## Layout

```
src/mathematica_wstp/
  discovery.py   find the installation, kernel binary and libWSTP
  link.py        ctypes binding to WSTP; the abort channel lives here
  registry.py    durable pid registry, orphan reaping, pid-reuse guard
  kernel.py      a supervised kernel: launch, evaluate, abort, shut down a tree
  session.py     the process-wide kernel and the evaluate_wl seam above it
  notebooks.py   .nb sessions: open, list cells, evaluate in document order
  server.py      the MCP tool surface
  helpers/*.wl   in-kernel notebook helper
tests/           unit tests (real kernel) and end-to-end MCP protocol tests
design/          the architecture and the measurements behind it
```

## Tools

`evaluate` · `abort` · `kernel` · `status` · `notebooks` · `cells` ·
`evaluate_cells` · `edit_cells` · `render` · `vars` · `batch` · `guide` ·
`verify_derivation` · `read_notebook_file`

`abort` is the one the older servers could not offer at all. It interrupts a
running evaluation and leaves every definition in place, so it — not
`kernel(action="restart")` — is the right response to a runaway computation.

`render` drives a headless front end (`WolframNB -platform offscreen`, ~1.8s
cold, no display needed) for typesetting and rasterisation, returning real image
blocks. Two limits, both measured and both deliberate:

- It **renders only** — it never evaluates through the front end. Work dispatched
  that way does not run when an external WSTP client owns the kernel's main link:
  a 5.1s job had not started after 40s of link silence. See
  [measurements §5](design/measurements.md).
- **Export renders what is visible**, as printing from the GUI does: a collapsed
  cell group exports collapsed. A notebook with most of its groups closed
  therefore exports short. Pass `open_groups=True` for the whole document.
  Pagination itself is fine — 994 synthetic cells render to 31 pages.
  See [measurements §10](design/measurements.md).

## Where this lives

This tree sits on the container's **overlay** filesystem, not on the host mount
(`/workspace/Claude_EFT` <- `/run/host_mark/home`, which is where the other git
repos on this machine live). Two consequences:

- It is not visible from the host, so it cannot be committed from there until it
  is copied across. 740 KB without `.venv`.
- Container-local storage is not the place for the only copy of anything.

```bash
cp -a /home/node/Softwares/Mathematica-MCP-WSTP /workspace/Claude_EFT/
rm -rf /workspace/Claude_EFT/Mathematica-MCP-WSTP/.venv   # rebuild on the host
```

## Install

```bash
cd /home/node/Softwares/Mathematica-MCP-WSTP
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .      # brings in mcp, adds the entry point
claude mcp add --scope user mathematica-wstp -- /home/node/Softwares/Mathematica-MCP-WSTP/.venv/bin/mathematica-wstp
```

`--scope user` is not optional. `claude mcp add` defaults to `--scope local`,
which registers the server **only for the directory you ran it in** — every
other session on the machine then reports it as unavailable, which looks like a
connection failure and is not one.

Verify from a *different* directory, which is where the mistake hides:

```bash
cd /tmp && claude mcp list      # expect: mathematica-wstp ... Connected
```

The installed entry point needs no `PYTHONPATH`, and the Wolfram installation,
kernel binary and libWSTP are discovered automatically. Override with
`MATHEMATICA_WSTP_KERNEL`, `MATHEMATICA_WSTP_INSTALL` or `MATHEMATICA_WSTP_LIB`
if discovery ever picks the wrong one, and `MATHEMATICA_WSTP_HOME` to move the
pid registry off `~/.mathematica-wstp`.

`evaluate_cells` summarises wide ranges rather than returning per-cell output
for hundreds of cells — a reply that large is refused outright, so the caller
would get nothing at all. Pass `detail='full'` to force the long form.

Running this alongside the older servers is fine — they share no port, no
socket and no state. Nothing needs uninstalling first.

## Running the tests

```bash
python3     tests/test_kernel.py       # transport + supervision; no dependencies
.venv/bin/python tests/test_server_mcp.py   # end-to-end over MCP stdio
```

```python
from mathematica_wstp.kernel import Kernel

with Kernel() as k:
    k.evaluate("1+1")                       # '2'
    k.evaluate_json('<|"n" -> 994|>')       # {'n': 994}
    k.abort()                               # from any thread, mid-evaluation
```

`design/architecture.md` has the design; `design/measurements.md` has the
numbers and the method, including what was *not* measured.

## Notes for anyone extending this

Three things here are load-bearing and were each found the hard way:

- **`WSPutString` is not byte-transparent.** It interprets backslash escapes, so
  `\[Gamma]` and every escaped quote are silently corrupted. `link.put_string`
  uses `WSPutUTF8String` instead.
- **`ExportString` returns encoded bytes rendered as characters**, so text with
  non-ASCII arrives as mojibake regardless of `CharacterEncoding`. Use
  `ExportByteArray` and decode once, in Python.
- **An aborted evaluation still sends its `$Aborted`.** If nobody reads it, it
  is handed to the next caller as their result. `Kernel.abort` drains it when no
  other thread is positioned to.
- **The kernel says more than the answer.** `Print` output and every message
  (`Part::partw`, `Power::infy`) arrive as separate WSTP packets. Keeping
  only the return packet discards them silently — the worst failure mode there
  is, because the answer still looks fine. `evaluate` returns `printed` and
  `messages` alongside `output`.
- **`WSTKSTR` is 34, not 83.** 83 is `WSTKOLDSTR`. Compile the constants out of
  `wstp.h`; do not infer them from the mnemonic letters.
- **WolframKernel ignores SIGTERM.** Measured on three orphaned kernels: SIGTERM
  left all three running; only SIGKILL removed them. `Kernel.close` therefore
  asks over the link first and escalates to SIGKILL rather than trusting a
  signal. The server's own SIGTERM handler closes the kernel and then re-raises,
  so the exit status still reports the signal.
- **A zombie answers `kill(pid, 0)`.** A kernel that has exited but not been
  waited on looks alive to a signal probe, so shutdown burned its full grace
  period and then signalled a corpse — 10.03s per close. `pid_alive` treats
  state `Z` as dead and `close` reaps its own child; now 0.27s.
