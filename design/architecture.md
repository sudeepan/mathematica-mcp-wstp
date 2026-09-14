# Mathematica MCP on WSTP — architecture

Design for a replacement transport and process layer under the existing
Mathematica MCP server. Every load-bearing claim here is measured; the numbers
and the method are in [`measurements.md`](measurements.md).

The starting position is that **most of the current fork is sound and should be
carried over unchanged**. The notebook layer, the tool consolidation, the
routing-probe discipline, the kernel discovery, the response filtering — none of
that is the problem. The problem is confined to one seam: wolframclient offers
exactly one primitive, *send an expression and wait for a reply*, and three
expensive limitations fall out of that single fact. This document replaces that
seam and leaves the rest alone.

---

## 1. The one-paragraph version

Replace wolframclient with a direct WSTP link as the kernel transport. That buys
abort (`$Aborted` in 2.0s, same kernel, state intact), liveness (a killed kernel
surfaces as a typed error in 0.3s instead of hanging forever), and a ~100x lower
round-trip floor (0.27ms vs ~30ms). Add a supervisor that owns the kernel
*process tree*, because the current restart path strands subkernels — 19 of them,
~7.8 GB, are orphaned on this machine right now. Add the front end back as a
**rendering service only**, driven headlessly via `UsingFrontEnd`, which works
with no display and opens a ~1000-cell notebook in 1.5s — but do **not** route
evaluation through it, because a measured 5-second job dispatched that way did
not complete in 200 seconds.

## 2. Why WSTP, stated as the three limits it removes

| Limit in the current fork | Cost paid | What WSTP does | Evidence |
|---|---|---|---|
| No abort. `recv_abortable` is socket-level only; `SIGINT` kills the kernel and loses all state. | Runaway evaluations can only be escaped by destroying the session. A `Do[...,{k,1,10^12}]` has been burning for 4h18m on this box for want of an abort. | `WSPutMessage(link, WSAbortMessage)` → `$Aborted` on the same link, same pid, state intact. | [§1](measurements.md#1-wstp-aborts-a-running-evaluation-wolframclient-cannot) |
| No liveness. A dead kernel is indistinguishable from a slow one. | A Python-side cell-count + 30s bound, and *discarding the session* on expiry. | `WSError` 1, "WSTP connection was lost", 0.3s after the kernel dies. | [§2](measurements.md#2-wstp-distinguishes-a-dead-kernel-from-a-slow-one-in-03s) |
| Undrained stdout. wolframclient opens a pipe nobody reads; >64 KB blocks the kernel in `write()`. | Fixed by routing stdout to `/dev/null`, safe only because results travel over ZMQ. | We own the launch, so we choose the disposition and can drain it properly. | — |

The abort result is the headline. It also changes what a timeout *means*: today a
timeout must discard the session, because there is nothing else to do with a
kernel you cannot interrupt. With abort, a timeout becomes "abort, keep the
state, return what we have" — which is the behaviour a 10-minute notebook replay
actually needs.

## 3. Layers

```
                    MCP tool surface  (§5 — unchanged shape, plus abort/render/jobs)
                            |
      +---------------------+---------------------+
      |                     |                     |
  notebook layer      evaluation engine      render service          (§4)
  (boxes / FE / addon)      (§3.2)           (UsingFrontEnd)
      |                     |                     |
      +---------------------+---------------------+
                            |
                    kernel supervisor  (§3.3 — process tree, reaping, watchdogs)
                            |
                      link layer  (§3.1 — ctypes WSTP)
                            |
                   WolframKernel -wstp
```

### 3.1 Link layer

A ctypes binding to `libWSTP64i4.so`. `experiments/wstp_client.py` is a working
seed of this — it is what produced every measurement in this design.

Sizing note: the SDK ships everything needed
(`SystemFiles/Links/WSTP/DeveloperKit/Linux-x86-64/`, header + `.so` + `.a`).
Interface version 4, `WSVERSION` 6. There is no maintained Python WSTP binding on
PyPI, so this layer is ours to own. It is a few hundred lines, and the surface we
need is small: open/activate/close, put and get for the wire types, `WSNextPacket`,
`WSReady`, `WSError`, `WSPutMessage`.

Two things this layer must get right, both learned the hard way:

- **Declare every `restype`/`argtypes`.** `WSLINK` and `WSENV` are pointers;
  the ctypes default of `c_int` truncates them on 64-bit and corrupts the handle
  in ways that surface much later.
- **Do not poll.** The read loop's sleep interval becomes the measured latency
  floor — a first benchmark reported a flat 20.1ms for every payload size, which
  was purely the 20ms sleep. Select on the link's file descriptor.

**Send structured expressions on the way in.** `WSPutFunction`/`WSPutSymbol` name
symbols explicitly, so a package symbol is put as that symbol rather than parsed
out of source text in whatever context happens to be current. That directly
addresses the parse-time binding trap — naming a package symbol in the same
evaluation that loads the package silently binds it to ``Global` `` and returns
the wrong thing. The structural fix is to make load-then-use two packets, and at
0.27ms a packet boundary is free; under the current transport the same split
costs an MCP round-trip at ~30ms plus JSON on both sides.

**Take strings on the way out. Do not deserialize.** Results are not consumed by
Python — they are consumed by a language model reading text, several JSON hops
downstream. Converting a Wolfram expression into Python objects only to
re-serialize it back into text is a detour that ends where it started, with a
lossy step in the middle. Let the kernel format, because the kernel is the
reference implementation for printing Wolfram expressions:

```
ToString[expr, InputForm]  ->  {1, 2.5, "abc", Sin[x], <|"k" -> {1, 2, 3}|>, 1/3}
ToExpression[%] === expr   ->  True
```

Two rules follow:

- **Results:** `ToString[expr, InputForm]` then `WSGetString`. Use `InputForm`
  specifically — the default `OutputForm` silently truncates machine reals
  (`3.1415926535897932`), while `InputForm` keeps the precision marks and round
  trips exactly.
- **Control envelope** — the fields the server branches on (success, aborted,
  message list, cell counts): have the kernel emit JSON with
  `ExportString[assoc, "RawJSON", "Compact" -> True]` and parse it with Python's
  stdlib `json`. This is what the current fork already does in
  `headless_notebook.wl`, and it was right.

**No wolframclient, in any role.** Not as transport, and not as a decoder either.
An earlier draft of this document recommended keeping it as a WXF-decoding
library; that was wrong, and the reasoning above is why. Retire
`WolframLanguageSession` and everything under `wolframclient.evaluation`, and do
not replace it with `wolframclient.deserializers`. Dropping it entirely also
disposes of the non-daemon controller thread that makes pytest pass in ~35s and
then never exit.

The one case that would justify a binary path is bulk numeric data — for 200k
machine reals, WXF is 1.80 MB against 3.56 MB of text, and 0.09s against 0.22s.
That is a real but modest win on a payload shape this server does not move: it
carries symbolic results and small control envelopes. If that ever changes, WSTP
has `WSGetReal64Array` natively, so the answer is still not wolframclient.

### 3.2 Evaluation engine

One writer per link; an event loop selecting on link fds.

- **Abort** is a first-class operation, not an error path. `abort(job)` sends
  `WSPutMessage(link, WSAbortMessage)` out of band while a read is outstanding.
- **Timeouts abort rather than discard.** On expiry: abort, wait briefly for
  `$Aborted`, return partial output plus a handle to the still-live session.
  State survives — measured.
- **Liveness** is checked from `WSError` after every read, plus an idle-link
  ping. There is no need for the cell-count heuristic the current fork carries.
- **Long jobs return handles.** `evaluate` on anything long-running returns a job
  id immediately; `poll`, `abort`, and `collect` follow. This is only safe
  *because* liveness is real — an async job over a transport that cannot detect a
  dead kernel is a hang waiting to happen, which is why the current fork is right
  not to offer one.
- **stdout** is drained by a reader thread, or sent to `/dev/null`. We own the
  launch; either is fine, but it must be a decision rather than an inherited pipe.

### 3.3 Kernel supervisor

This layer exists because of the leak, and it is the part with the most
measured-failure surface behind it.

- **Own the process tree.** Launch each kernel in its own process group
  (`setsid`). Record `(pid, pgid, link name, launch time)` in a registry file on
  disk, keyed to this server instance.
- **Shut down in three stages.** `CloseKernels[]` over WSTP first — this is the
  graceful path and it now *always* reaches the kernel, which is exactly what
  wolframclient could not guarantee. Then `SIGTERM` the process group, then
  `SIGKILL` after a grace period. Restart is shutdown followed by launch, never
  `terminate()` on the master alone.
- **Reap on startup.** Scan the registry for pids from previous instances that
  are still alive and whose parent is gone, and clean them up.
- **Key the reaper on the registry, never on a command-line pattern.** I nearly
  misattributed 19 of someone else's subkernels to my own tests because they
  matched `-wstp`, which appears in both. Command-line matching is what makes
  `pkill -f` dangerous here — it also matches the shell running the command.
- **Watchdog the launch.** `WSActivate` against a nonexistent binary blocks
  indefinitely (killed at 3m37s). Launch and activation run under a hard deadline
  on a separate thread. This is the one respect in which WSTP is worse than a
  subprocess, and it needs explicit handling rather than trust.
- **Track subkernels.** After any evaluation that may have called
  `LaunchKernels[]`, record `Kernels[]` so shutdown has something to close.

Licence starvation deserves a note: I did not exhaust the pool, so I have not
confirmed the failure signature under WSTP. The mechanism to *expect* is a failed
`WSActivate` with a real `WSErrorMessage`, which would be a strict improvement
over a cold `wolframscript` exiting with empty output and no error — a signature
that reads downstream as a code bug and has cost hours. The supervisor should
assume it can happen and label it explicitly; the cheap confirming test is to
hold N links open and open the N+1st.

## 4. The front end, and the one thing not to do with it

The current profile says the host has no front end. That is a configuration
statement, not a fact about the machine: `WolframNB` is installed, one instance
was already running when I started, and `UsingFrontEnd` brings one up in 1.8s
with `-platform offscreen` — no display, no X server, no configuration.

**What the headless front end gives you, measured:**

| Capability | Result |
|---|---|
| Flat, document-order cell list of a large notebook | `{994 cells, 255 Input}` in 1.5s |
| Typeset rasterization of any cell | 848x57 px PNG, 0.1s |
| Rasterize with real typesetting | `Style[Integrate[...], 24]` → 805x92 px |
| Notebook → PDF | real 1-page PDF 1.5, 7181 bytes |
| Cold front-end launch | 1.8s |

That is a genuine restoration of the upstream GUI capability the fork dropped —
screenshots, typeset rendering, export — on a headless box.

**What it must not be used for: evaluation.** A cell holding a job that takes
5.1s in the kernel was dispatched via `SelectionEvaluate` and had not completed
after 200s; with the link held completely silent for 40s to rule out
poll-starvation, it still had not run at all. When an external WSTP client owns
the kernel's main link, the front end has no evaluator servicing its queue.
Trivial cells (`1+1` → `Out[1]= 2` in 1.1s) succeed because they fit inside the
`UsingFrontEnd` block; real work does not.

This is the same class of constraint the current addon hits from the other side —
its socket handler occupies the kernel's main link while it polls, so it can only
ever report `evaluation_pending` and never observe completion. Moving the server
to WSTP does not dissolve that constraint; it relocates it. **The design accepts
it rather than fighting it**: evaluate over the kernel link, render through the
front end.

One consequence for the tool surface: a single `abort` tool must know which queue
its target sits in. `WSAbortMessage` reaches the kernel link's queue and does
nothing to front-end-dispatched work; `FrontEndTokenExecute["EvaluatorAbort"]` is
the front end's own channel and produces a genuine `$Aborted` output cell. Two
channels, both needed.

### Three profiles

| Profile | Topology | Use |
|---|---|---|
| **Owned** (default) | Server launches and owns the kernel over WSTP. Front end spawned lazily via `UsingFrontEnd` for rendering only. | Headless hosts, CI, containers. |
| **Attached** | Existing addon path, unchanged: probe for a front end, use it if one answers. | A user with Mathematica already open. Must stay untouched. |
| **Co-resident** | Server owns a kernel; a separate front end in `-server` mode owns its own kernel. Costs a second licence seat. | Live windows the user watches. **Untested** — §5 measures only the `UsingFrontEnd` topology. |

Routing keeps the current discipline: probe first, fall back to headless only
when nothing answers, and never disturb a working front-end setup.

## 5. Tool surface

Keep the 12 consolidated tools and their shapes. The changes are additive:

- **`abort`** — new, and the point of the exercise. Aborts the current or a named
  job, routing to the correct channel per §4.
- **`kernel(action=...)`** — add `abort`, `subkernels`, `reap`. Today the actions
  are `state | messages | restart | load_package | packages | inspect`; there is
  no abort at all, which is a fair summary of the problem.
- **`evaluate`** — gains an async form returning a job handle; timeout semantics
  change from *discard the session* to *abort and keep the state*.
- **`render`** — front-end rasterize/export, restoring `screenshot` on headless
  hosts.

## 6. What must be carried over unchanged

The notebook layer is the fork's best work and none of it should be rewritten:

- Cells evaluated from their **original stored boxes**
  (`ToExpression[content /. BoxData[b_] :> b, StandardForm]`), never retyped from
  a text preview — the failure mode of retyping is silent non-evaluation.
- Cells located by **position in the original expression**, never by rebuilding
  it, which is what corrupts `\[Gamma]` and friends.
- `NotebookDirectory[]` / `NotebookFileName[]` rebound so cells calling them work
  unpatched.
- Front-end probe-then-fallback routing.
- Kernel discovery (widest-first globbing, the `~/.mathematica-mcp/kernel-path`
  cache, and `kernel_environment()` repairing `PATH` for a minimally-inherited
  environment).
- Response filtering, pagination, and the lean-by-default response shape.

Two known traps that are not transport problems and stay the caller's
responsibility, though the docs should keep warning about them: a warm kernel
hides definitions a notebook never declares, so cold replay returns silent zeros
rather than errors; and package symbols named in the same evaluation that loads
the package bind to ``Global` `` (§3.1 makes the fix cheap, but does not make it
automatic).

## 7. Staging

1. **Link layer + supervisor.** Ship the ctypes binding, process-group ownership,
   the registry, the reaper, and the activation watchdog. Reaping alone is worth
   ~24 GB on this machine today.
2. **Evaluation engine.** Cut `evaluate` over to WSTP behind a flag; keep
   wolframclient reachable until parity is demonstrated. Add `abort`.
3. **Timeout semantics.** Switch from discard-on-expiry to abort-and-keep.
4. **Render service.** `UsingFrontEnd` rasterize/export; restore `screenshot`
   headlessly.
5. **Async jobs.** Handles, poll, collect — last, because it depends on liveness
   being trustworthy.

Parity gate for each stage: a full replay of a large real notebook (~1000 cells,
~276 of them code, `LaunchKernels[]` fanning out to 20 subkernels, ~10 minutes)
completes, and afterwards the process table returns to its pre-run state. That
second half is the one the current fork fails, and it is checked by snapshotting
`ps` to a file and grepping the file — never by trusting an exit code.

## 8. Open questions

- **Abort coverage.** §1 aborts a `Do` loop, which polls for aborts frequently.
  A kernel inside a C library call or a `RunProcess` to an external solver may
  interruptible. Measure before promising abort unconditionally.
- **The co-resident profile** is unmeasured and costs a licence seat. Worth
  testing only if live user-facing windows turn out to be a real requirement.
- **Licence starvation was a misdiagnosis.** `$MaxLicenseProcesses` and
  `$MaxLicenseSubprocesses` are both `Infinity` on this licence, so there is no
  seat limit to exhaust and the §3.3 hypothesis is moot (measurements §11). The
  symptom it was invented to explain — a cold `wolframscript` exiting with empty
  output — is far more likely OOM under the process leak. The scarce resource is
  RAM, which makes the reaper the fix for both.
- **Structured-expression ergonomics on the way in.** Putting native WSTP
  expressions is the right call for correctness (§3.1), but it is more code than
  shipping a string, and we are no longer taking wolframclient's expression
  builders. Worth prototyping a small builder against a handful of real package
  calls before committing — and worth checking how much of the surface genuinely
  needs it, since only the load-then-use split strictly requires explicit symbol
  contexts.
