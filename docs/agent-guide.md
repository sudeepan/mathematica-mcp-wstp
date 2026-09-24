# Driving this server well

This guide is for the agent using the server, not the maintainer implementing
it.

The fastest way to get a wrong scientific answer is to treat the server as a
stateless calculator. It is a persistent symbolic laboratory: kernel state,
notebook state, execution identity and file state can all outlive individual
tool calls, and they do not all have the same lifetime.

The practical rule throughout this guide is:

> **Ask what evidence you have, not what the last component claimed.**

The measurements quoted here come from a real 994-cell symbolic-algebra notebook
with 276 executable cells and a 20-way `LaunchKernels[]` fan-out. They establish
that the mechanisms are real; they are not generic expectations for every
notebook.

## The five objects you need to keep separate

**Kernel.** The persistent Wolfram process holding definitions and package state.

**Notebook session.** A live document opened inside one kernel. It is not the
same thing as the `.nb` file on disk.

**Backend.** Who owns execution: the current process's direct kernel, or the
separate supervisor-owned kernel.

**Replay manifest.** A durable sidecar record of what a per-cell replay intends
to run and what has happened so far.

**Saved notebook.** The `.nb` file after an explicit save. Session-resident
outputs are not automatically file-resident outputs.

Confusing any two of these is the source of several observed failure modes.

## Choose the execution path before you start

There are three normal ways to compute.

### `evaluate`: one Wolfram expression

Use it for targeted questions and state inspection.

The kernel is persistent, so definitions survive between calls. Load a package
in its own call: symbol names are resolved when the expression is parsed, before
the body runs. Referring to a package symbol in the same call that loads the
package can silently bind the wrong symbol.

Ask for measurements rather than enormous expressions when possible:
`Length`, `LeafCount`, `ByteCount`, `Short`, `Part`. `vars(action="get")` omits
very large values unless `full=True`.

### `evaluate_cells`: one notebook span

Use it when you deliberately want one span-level replay and do not need durable
identity for each executable cell.

The span evaluates stored notebook cells in order with one persistent kernel
state. Edits are applied after the range finishes. Indices reported in one reply
therefore refer to the pre-edit document.

### `replay`: one execution per executable cell

Use this for long, interruptible, auditable work.

`replay` gives each `Input`/`Code` cell its own execution identity, persists a
manifest before the first child is submitted, binds outputs back to the child
that wrote them, and supports reconciliation after interruption.

The cell locator is an **ordinal**: 1-based, counting only `Input` and `Code`
cells. Raw indices are unstable because output insertion/deletion shifts the
document.

For exactly the jobs where it matters what survived, prefer `replay` over
`evaluate_cells`.

## Choose direct or supervisor ownership before opening a notebook

The first ordinary tool call starts the direct kernel. There is no separate
launch step.

For work that may run for hours or days, or that must survive a dropped client,
use the supervisor deliberately:

```text
supervisor(action="start")
supervisor(action="use")
```

Then open the notebook.

A live notebook session belongs to the kernel that opened it. Switching
execution backend later does not move the document; it strands the session in
the old kernel.

The supervisor changes **lifetime**, not Mathematica semantics. An in-flight
evaluation can continue after the client disappears, and a reconnecting client
uses `replay(action="reconcile")` / lookup rather than expecting the original
MCP call to come back.

## Status and process ownership

`status()` before a kernel has ever started reports `running: false,
generation: 0`. That is an unused session, not a broken one.

Its `orphans` list is a machine-wide census, not an accusation. It can include
kernels belonging to other live server sessions. Check `owner_alive`,
`still_ours`, and the top-level `kernel` object before deciding something is
stranded.

Parallel subkernels are tracked. They follow the master kernel down, but they can
remain alive long after the parallel work is finished and continue consuming
memory.

## Opening and inspecting notebooks

```text
notebooks(action="open", path="/abs/path/Foo.nb")
cells(offset=200, limit=20, include_content=False)
cells(defines="SymbolName")
```

A notebook can contain hundreds of prose/output cells around a much smaller
number of executable cells. Judge replay progress by executable cells, not total
cells.

Before replaying an unfamiliar notebook, run
`notebooks(action="dependencies")` to discover which files it reads and writes.
The tool classifies each one (external input, round-trip data, stored result,
write-only output) so you can tell the user which side effects to expect
without reading every cell by hand.

Stored boxes are the source of execution. Do not retype a rendered preview into
Wolfram Language and assume it is equivalent.

`cells(defines=...)` is the fastest way to answer "where was this symbol
assigned?" without paging through the whole document.

To modify a cell in place - for example, commenting out an `Export` or changing
a parameter - use `edit_cells(action="replace")`. It replaces the cell's
content at its current position without inserting or deleting.

## Indices move; ordinals do not

Writing outputs can insert new cells and remove stale ones, so the document may
grow or shrink.

For `evaluate_cells`, every index in one reply is relative to the document as it
stood when that range began. `indices_shifted` means "re-locate before the next
call," not "some indices in this reply are already new."

For `replay`, use executable-cell ordinals and let the replay layer resolve the
current position.

A cheap span-mode tripwire is `notebooks(action="info")`: compare the current
cell count with the last known count before pulling another full listing.

## What success means

`success: true` is not proof that the scientific work occurred. It means the
tool did not report a timeout/exception on that path.

Examples observed in real work:

- an external solver failed while the Wolfram cell itself completed;
- a cell loaded cached data instead of recomputing it;
- a warm kernel supplied a definition missing from the notebook.

Verify the intended artifact or state: a file, a symbol, a size, a digest, a
chapter fingerprint.

This is why `pitfalls.md` exists.

## Long computations and timeout policy

Calls longer than the client's foreground window may be moved to a background
task. That does not make the original RPC durable across a client/session exit.

For a replay, set the per-cell timeout deliberately. The agent-facing replay
surface defaults to 300 s per cell. If one cell is expected to take hours or
days, choose the larger value explicitly and report it.

The execution timeout is not a kernel-lifetime limit. Under supervisor ownership
the computation can outlive the client that submitted it.

Before expensive work, checkpoint at scientifically meaningful boundaries.
Export named values rather than dumping an entire context; `DumpSave` of
`Global`` can shadow package symbols when restored.

## Interrupting

`abort()` is for a running evaluation; `kernel(action="restart")` is for a
kernel you are deliberately willing to destroy.

After an abort, read the reported kernel state rather than inferring recovery:

| state | meaning |
|---|---|
| `alive` | a round trip succeeded after the abort |
| `dead` | the kernel is gone and in-memory state is lost |
| `unverified` | the server could not establish readiness; assume nothing |

A cell that calls `Abort[]` itself is not the same event as a user requesting an
abort. Span mode uses an out-of-band marker to distinguish those cases.
Supervisor-backed per-cell execution records the caller's control intent
directly.

If a helper catches the interrupt with `CheckAbort`, the request may return a
normal payload even though the caller did request an abort. That is why control
history and result value are separate facts.

## Reconciliation after interruption

For per-cell replay:

```text
replay(action="reconcile")
```

can report what is complete, what is still running, what was never submitted,
whether source changed, and whether a completed output is still present.

Do not automatically restart from cell 1.

If the source digest no longer matches the planned run, stop automatic
continuation. One edited definition can change every downstream cell in a
stateful notebook.

If a supervised child is still running with no client attached, treat that as
live science. Decide explicitly whether to wait, take control, or abandon it.

## Session state is not file durability

Writing an output into the live notebook does **not** save the `.nb` file.

Keep these separate:

```text
output present in live session
manifest persisted on disk
notebook saved on disk
```

When file durability matters:

```text
notebooks(action="save", path="/a/new/path.nb")
```

Never overwrite the original notebook you opened.

Check `written_by`. `frontend` means a real notebook file was written. `put`
means the kernel wrote a bare expression dump; that may round-trip through the
kernel while still being unsuitable as a desktop notebook.

## Recording every evaluation into a notebook

```text
notebooks(action="create", title="Computation Log", path="/tmp/log.nb", record=True)
```

Or on an already-open notebook: `notebooks(action="record", notebook="hnb1")`.

Recording requires **zero pre-existing executable cells** in the notebook.
This guarantees the recorder ledger is the sole authority for what was
computed. Resuming a partially-filled notebook is a separate operation.

Every `evaluate(code)` call writes `code` as a cell before evaluating it.
While recording, `write_cell`, `edit_cells`, `execute_in_notebook` are blocked
on that notebook.

Pass `style` to write structure cells:

```text
evaluate("Diagrams", style="Chapter")
evaluate("Common setup", style="Section")
evaluate("GraphGen[1]")
```

Only `Input` and `Code` are accepted as scientific styles. Narrative styles
(Title, Subtitle, Section, Subsection, Subsubsection, Text, Item,
ItemNumbered, ItemParagraph) write notebook structure but **skip scientific
execution entirely**. No ledger record is created, so the scientific ledger
contains only cells that actually computed. Any other style is rejected.

### What is blocked during recording

While an integrity recorder is active, the following are refused to prevent
unrecorded kernel state changes:

- `evaluate_cells`, `replay(action="run")`
- `vars(action="set/clear/clear_all")`
- `kernel(action="restart/stop")`

All scientific work must go through `evaluate()`, which routes through the
recorder. Inspection tools (`vars list/get`, `cells`, `status`) remain
available.

### Fail-closed recording

If any recording step fails - a cell write, a read-back verification, a
source digest mismatch, an annotation failure, a session loss, or a
post-evaluation integrity check - the recorder **faults the entire run**.
Once faulted, all further scientific dispatch and finalization are refused for
that recording. The fault is written into the recorder ledger on disk, so it
survives process restarts.

Before every scientific dispatch, the recorder:

1. verifies the written cell matches the intended source (digest comparison)
2. verifies the cell style, executable flag, and Evaluatable state
3. verifies exactly one cell carries the new tag
4. runs full bidirectional ledger/notebook verification

Two separate phases can trigger a fault:

- **Pre-dispatch**: the cell was written but its identity, content, or
  notebook state could not be verified before execution. Science did not run.
- **Post-evaluation**: science ran and produced a result, but the notebook
  no longer matches the ledger. The execution outcome is preserved and
  returned, but the recording is marked integrity-faulted.

The server dispatch gate is **positive**: science is dispatched only when the
recorder returned a verified record with a sequence number. Missing keys,
malformed results, or `None` refuse dispatch. A post-eval fault is surfaced
in the tool reply so the caller knows immediately.

### Finalization and structural verification

Finalize **before** stopping the recorder:

```text
notebooks(action="save")
notebooks(action="finalize")       # while recorder is still active
notebooks(action="stop_recording") # only after finalization
```

`stop_recording` without prior finalization is refused (pass `force=True`
to abandon the run). If you force-stop first, finalization falls through to
the legacy path and never checks the recorder ledger.

`notebooks(action="finalize")` re-runs every cell in a fresh kernel
via `NotebookEvaluate` so the notebook gets native `In[n]`/`Out[n]` labels.

A successful finalization **seals** the recorder: no further scientific
cells or narrative writes are accepted, and a second finalize is refused.
The finalized artifact is named `<base>-<run_id>-finalized.nb`.

After finalization, the server opens the finalized `.nb` as a temporary
session and runs a structural comparison against the recorder ledger:
tags, source digests, styles, annotation state, annotation reasons, and cell
order. A mismatch means the finalized artifact does not faithfully represent
the recorded computation.

## Parallel work

A finished parallel computation can leave a large subkernel pool resident even
though the master kernel is healthy and still contains valuable state.

Measured on one replay, 20 subkernels held 5.2 GB, 69% of all Wolfram memory on
the machine, an hour after the work ended.

Release just the pool with:

```text
kernel(action="close_subkernels")
```

Definitions in the master survive. If the user may immediately continue
parallel work, ask whether to keep the pool rather than deciding for them.

After interrupting parallel work, remember that a successful transport probe
does not prove the algebra or distributed definitions are coherent. Rebuilding
the parallel pool is a precaution the user can choose.

## Releasing the kernel when done

When you are finished computing and want to free all kernel memory:

```text
kernel(action="stop")
```

This shuts the kernel down without starting a replacement. The next
`evaluate()` call starts a fresh one on demand. Use it to release memory at
the end of a session rather than leaving a large kernel resident.

## External processes

Managed processes and shell-detached processes have different lifetimes.

Prefer `StartProcess`, which returns a `ProcessObject` owned by the kernel.
`Run["... &"]` or `setsid` can create processes that survive the kernel and fall
outside the server's process-group cleanup.

If a detached process must outlive its caller, record its pid, start time and
command somewhere durable first.

## Looking up Wolfram Language behavior

This server has no reference corpus. For language semantics, options and
built-ins, use Wolfram's own MCP server and `WolframLanguageContext`.

That server uses a different kernel. It cannot inspect symbols or notebook state
created here.

**The language → Wolfram's MCP. Your live state/document → this server.**

## Checking the record

```text
notebooks(action="verify", path="/path/to/original.nb")   # against a reference
notebooks(action="verify")                                  # self-consistency
```

Neither proves scientific correctness. For mathematical divergence, use
`Length`/`LeafCount` on transparent expressions and `ByteCount` on opaque
ones (pitfall 7).

## Exporting for a human

`render(action="export", ...)` uses the offscreen front end and opens cell groups
by default.

Wide content is clipped, not automatically scaled. If export reports
`action_required: "ASK THE USER"`, stop and present the choice rather than
silently truncating scientific content.

`fit_width=True` widens the page so content survives, at the cost of a
non-standard page size.

## The short operating checklist

Before a serious replay:

```text
1. run notebooks(action="dependencies") to find side effects
2. choose direct vs supervisor ownership
3. open the notebook in that backend
4. choose replay vs span deliberately
5. make the effective per-cell timeout explicit
6. checkpoint expensive scientific stages
7. run
8. reconcile rather than guess after interruption
9. verify the notebook record
10. save explicitly when file durability matters
```

For a from-scratch computation:

```text
1. notebooks(action="create", ..., record=True)
2. evaluate(code) - every call is recorded as a cell
3. notebooks(action="save")
4. notebooks(action="finalize") - while recorder is still active;
   re-evaluates in a fresh kernel, then verifies against the ledger
5. notebooks(action="stop_recording")
```

For the observed failure modes behind these rules, read
[`pitfalls.md`](pitfalls.md).
