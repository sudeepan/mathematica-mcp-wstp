# Replaying a notebook

A notebook here is a `.nb` file on disk that becomes a **live document in a
kernel session** when opened. Evaluating cells changes that in-memory document.
Nothing reaches the `.nb` on disk until you explicitly save it.

That distinction is central to replay and reconciliation.

## Two ways to run notebook cells

### `evaluate_cells`: one span-level execution

`evaluate_cells` hands a span to the kernel and gets one span-level answer back.
It is the simpler choice when you want stateful replay but do not need durable
identity for every individual cell.

### `replay`: one execution per executable cell

`replay` runs each `Input` or `Code` cell as its own execution. Each child gets:

- a replay child id;
- a request id / execution token from the selected backend;
- an idempotency key;
- source identity;
- timeout policy;
- output provenance.

Before the first child is submitted, the replay plan is persisted to a sidecar
**manifest**. That makes the run itself recoverable even if the client that
created it disappears.

For long, interruptible or auditable work, prefer `replay`.

## Ordinal means "the nth executable cell"

Replay does **not** identify a cell by raw notebook index.

An **ordinal** is 1-based and counts only `Input` and `Code` cells:

```text
ordinal 1 = first Input/Code cell
ordinal 2 = second Input/Code cell
...
```

Why? Writing an output inserts a new cell. Every later raw index can move, while
"the seventh input cell" remains the seventh input cell.

This is not terminology for its own sake; it prevents a replay from silently
evaluating the wrong cell after write-back changes the document.

## Cells are evaluated as written

Executable cells are evaluated from their stored notebook content. They are not
retyped from a rendered preview.

Only `Input` and `Code` cells execute. Prose and stored output are skipped and
counted separately.

That preserves notebook fidelity and keeps a replay's execution count meaningful.

## Messages and printed output are part of the record

`Print` output and Wolfram messages are returned alongside the value:

```json
{
  "output": "{1, 2}[[5]]",
  "messages": [
    {"name": "Part::partw", "text": "Part 5 of {1, 2} does not exist."}
  ]
}
```

A plausible result with a message attached can be more important than a clean
status flag. Do not discard messages merely because a value was returned.

## What the manifest does — and does not do

The manifest records the intended replay and each child's progress on disk
before execution can outrun the record.

It is not the notebook file.

Three states must stay separate:

```text
session-resident
    output exists in the live notebook document in the kernel

persisted manifest
    replay identity/progress exists on disk

saved notebook
    the .nb file itself contains the current document
```

Per-cell write-back makes progress visible in the live session. If the kernel
survives client loss under the supervisor, that session-resident progress can be
recovered. It is not file durability until `notebooks(action="save", ...)`
succeeds.

## Output provenance

A replay does not infer that an output belongs to a child merely because it is
located under the same input. The output is tagged so reconciliation can identify
which replay child wrote it even after other outputs are deleted and positions
shift.

That lets reconciliation distinguish:

```text
execution completed, output present
execution completed, output missing or overwritten
execution never completed
```

## Source divergence

The replay records source identity for each executable cell. On reconciliation,
the current notebook source is checked against the planned source.

If a cell was edited underneath an existing run, the run does **not** silently
continue as though it were the same scientific program. The divergence is
reported and automatic continuation of that replay is blocked.

For a stateful notebook this is important: one changed definition may alter every
later cell.

## Reconciliation

After an interruption or reconnect:

```text
replay(action="reconcile")
```

can distinguish states such as:

```text
COMPLETE
STILL_RUNNING
NEVER_SUBMITTED
source diverged
output present / missing
```

With the supervisor, execution facts come from its ledger even if the kernel is
currently busy with the evaluation being reconciled.

The reconnecting client does not need to reproduce the old waiting MCP call. It
recovers the run from the manifest and looks up the existing execution.

## Direct versus supervised replay

The replay machinery works with either backend, but lifetime guarantees differ.

**Direct backend:** the current process owns the kernel. If that owner dies, the
kernel dies with it.

**Supervisor backend:** a separate process owns the kernel. If the MCP client
dies, an in-flight cell can keep running and a later client can reconcile it.

Choose the backend before opening the notebook. A live notebook session belongs
to the kernel that opened it.

## Starting a fresh agent on an unfamiliar notebook

Paste the following into a fresh session and replace `PATH`.

---

You have a Mathematica MCP server (`mathematica-wstp`). Its tool descriptions
and `guide(topic=...)` are accurate; read them instead of guessing. Never use
`wolframscript` or shell commands for Mathematica work.

Task: replay the notebook at **PATH**.

### Before evaluating anything

1. Work on a **copy**. Never write the original `.nb`.
2. `read_notebook_file(path, mode="wolfram")` and scan every code cell for side
   effects that leave the machine: `Export`, `Put`, `Save`, `DumpSave`,
   `DeleteFile`, `CreateDirectory`, `Run`, `SetDirectory`, or anything else
   that writes a file. **List them with their ordinals and stop.** I will tell
   you which are safe.
3. Report the executable-cell count and whether the notebook loads packages.
4. If the work is expected to outlive this client/session, ask whether to use
   the supervisor **before opening the notebook**.

### Running

- Use `replay(action="run")`, not `evaluate_cells`, unless I say otherwise.
  `replay` gives each executable cell its own identity and persists a manifest
  before the first cell is submitted.
- Address executable cells by **ordinal**: 1-based, counting only `Input` and
  `Code` cells.
- The per-cell timeout defaults to 300 s. If any cell plausibly needs longer,
  tell me the timeout you propose and wait for my answer.
- Report the run id / manifest path when the replay begins.

### When something goes wrong

Never abandon a run silently and never report one as finished when it is not.

Tell me, in this order:

1. the ordinal;
2. what the reply actually said;
3. whether the kernel survived (use the reported state; do not infer it);
4. the manifest path;
5. whether reconciliation reports the child as complete, still running, never
   submitted, or divergent.

Keep these apart:

```text
the cell hit its timeout
the cell failed
the cell was aborted
the kernel died
the client disappeared but the computation is still running
nothing could be established
```

A timeout is not proof that a cell merely needed more time. It may be runaway
work. Explain why before increasing the timeout.

To resume after interruption, use:

```text
replay(action="reconcile")
```

Do not restart from ordinal 1 merely to "reset" the run.

`success: true` means the tool call did not report an exception/timeout. It does
not prove the intended scientific side effect occurred. If a cell was meant to
write a file or define a symbol, verify that artifact/state.

### Don't

- Restart the kernel just to clear a slow calculation; a restart destroys every
  in-memory definition.
- Switch execution backend after opening the notebook and expect the session to
  follow you.
- Treat a manifest as proof that notebook outputs were saved to disk.
- Report a conclusion you could not establish. "I cannot tell from here" is a
  useful answer.
