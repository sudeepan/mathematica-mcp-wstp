# Replaying a notebook

A notebook here is a `.nb` file on disk, not a window. The server opens it,
evaluates its cells in order in a persistent kernel, and can write the results
back into the document.

## Two ways to run one

`evaluate_cells` hands a span of cells to the kernel and gets one answer back.
Fewer round trips, and the right choice when nobody needs to know what happened
cell by cell.

`replay` runs each cell as its own execution. That costs a round trip per cell
and buys a request id, an evaluation token and an idempotency key for each one,
plus a manifest written to disk *before* the first cell runs. An interrupted
replay can then be resumed rather than restarted, which on a notebook that
takes an hour is the difference between losing a minute and losing the hour.

Cells are addressed by **ordinal** - 1-based, counting only `Input` and `Code`
cells - never by index. Writing an output inserts a cell and shifts every index
after it.

## Cells are evaluated as written

A notebook here is a `.nb` on disk. Cells are evaluated from their original
stored boxes and located by position in the notebook expression. They are never
rebuilt, and never retyped from a rendered preview: retyping is a transcription
step whose failure mode is silent non-evaluation, and round-tripping through a
box-to-text converter is what corrupts `\[Gamma]` and its relatives.

Only `Input` and `Code` cells run. Prose and stored output are reported as
skipped and counted separately, so a replay's success figure means what it says.

## Messages and printed output are never dropped

`Print` output and every Wolfram message arrive alongside the result:

```json
{ "output": "{1, 2}[[5]]",
  "messages": [{"name": "Part::partw", "text": "Part 5 of {1, 2} does not exist."}] }
```

A plausible-looking answer with a message attached is usually the message's
fault. Discarding them is the worst failure mode available, because the answer
still looks fine.

## Starting a fresh agent on an unfamiliar notebook

Someone meeting this for the first time, on a notebook nobody has replayed
before, mostly needs telling what *not* to do. This is the prompt we use: paste
it into a new session and replace `PATH`.

---

You have a Mathematica MCP server (`mathematica-wstp`). Its tool descriptions
and `guide(topic=...)` are accurate - read them rather than guessing. Never use
`wolframscript` or shell commands for Mathematica work.

Task: replay the notebook at **PATH**.

### Before evaluating anything

1. Work on a **copy**. Never write the original `.nb`.
2. `read_notebook_file(path, mode="wolfram")` and scan every code cell for side
   effects that leave the machine: `Export`, `Put`, `Save`, `DumpSave`,
   `DeleteFile`, `CreateDirectory`, `Run`, `SetDirectory`, anything writing a
   file. **List them with their ordinals and stop.** I will tell you which are
   safe. A live `Export` that silently overwrites a reference file is the
   failure mode I care most about.
3. Report the input-cell count and whether the notebook loads packages.

### Running

- Use `replay(action="run")`, not `evaluate_cells`, unless I say otherwise: it
  gives each cell its own identity and writes a manifest to disk before the
  first cell, so an interrupted run is resumable.
- Address cells by **ordinal** (1-based, counting only Input/Code cells), never
  by index. Writing outputs inserts cells and shifts every later index.
- The per-cell timeout defaults to 300 s. If any cell plausibly needs longer,
  tell me your estimate and the value you propose, and wait for my answer.

### When something goes wrong

This is the part I care about. **Never abandon a run silently, and never report
one as finished when it is not.**

Tell me, in this order: which ordinal, what the reply actually said, whether
the kernel survived (read the reply's `kernel` field - do not infer it), and
the manifest path.

Keep these apart; they are different facts and collapsing them wastes my time:

    the cell hit the timeout
    the cell failed
    the cell was aborted
    the kernel died
    nothing could be established

A timeout is **not** evidence that a cell needs more time - it may be a
runaway. Say which you think it is and why, before proposing a larger number.

To resume: `replay(action="reconcile")` reports what completed, what is still
running, and what never started. Restart from the next ordinal. Do **not**
re-run from cell 1 to "reset" - that can cost hours.

`success: true` means no exception and no timeout. It does not mean the work
happened. If a cell was meant to produce a file or define a symbol, check that
it did.

### Don't

- Restart the kernel to clear a problem. It destroys every definition; use
  `abort()` to interrupt a runaway instead.
- Report a conclusion you could not establish. "I cannot tell from here" is a
  useful answer; a confident wrong one is not.
