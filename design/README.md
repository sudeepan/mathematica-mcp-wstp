# Mathematica-MCP-WSTP

Design work for a WSTP-based replacement of the kernel transport in the
Mathematica MCP server (`../Mathematica-MCP-Mod`, `../Mathematica-MCP-Sud`).

This directory is the design record: the architecture, the measurements behind
it, and the throwaway experiments that produced them. **The server described
here is now built** — see the top-level README and `../src/`. Kept as written so
the reasoning (and the two results that were wrong on the first pass) stays
inspectable; where the build later contradicted a claim, the measurement was
updated and says so.

- **[architecture.md](architecture.md)** — the design.
- **[measurements.md](measurements.md)** — what was measured, how, and
  what was *not* measured.
- **[experiments/](experiments/)** — a ctypes WSTP client and the experiment scripts.
  `wstp_client.py` is the seed of the link layer; the rest are throwaway probes
  kept because they are the evidence.

## The short version

Three limits in the current fork all trace to wolframclient offering exactly one
primitive — send an expression, wait for a reply. A direct WSTP link removes all
three:

| | current | WSTP | |
|---|---|---|---|
| abort a running evaluation | impossible (`SIGINT` kills the kernel, losing state) | `$Aborted` in 2.0s, same pid, state intact | measured |
| detect a dead kernel | hangs forever | typed error in 0.3s | measured |
| round-trip floor | ~30 ms | 0.27 ms | measured |

**wolframclient is dropped entirely** — not as transport, and not as a decoder
either. Results go to a language model as text, so the kernel formats them
(`ToString[expr, InputForm]`, which round-trips exactly) and Python never builds
an object it would only have to re-serialize. See
[measurements §9](measurements.md#9-results-need-no-deserialization-let-the-kernel-format).

Two further findings shape the design:

- **The front end runs fully headless here** (`-platform offscreen`, 1.8s cold),
  opens a ~1000-cell notebook in 1.5s, and rasterizes cells and exports PDF —
  restoring the GUI capability the fork dropped.
- **But it cannot be used as an evaluator.** A 5.1s job dispatched through it had
  not completed after 200s, and had not started after 40s of total link silence.
  Evaluate over the kernel link; render through the front end.

And one live number for the process leak the design has to fix: at the time of
writing this box has **37 Wolfram kernels, 7 orphaned to init, ~23.9 GB RSS**,
including 19 stranded `LaunchKernels[]` subkernels (~7.8 GB) under a dead parent.

## Running the prototype

```bash
/usr/bin/python3 experiments/wstp_abort_test.py
```

Each script launches its own kernel and closes it. Redirect to a file rather
than piping to `tail` — a pipe buffers everything until exit and makes a working
script look hung.
