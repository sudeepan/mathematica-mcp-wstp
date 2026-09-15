# Ways to get a wrong answer with no error

Every entry is a mechanism of the Wolfram Language or of this server, not a
quirk of any one package. They share one property: **nothing fails**. No
exception, no message, no timeout — just a plausible result that is wrong.

Each was observed on real work. Where a concrete instance makes the mechanism
easier to recognise it is named in brackets, but the mechanism is the point: if
you are using different packages, you will meet the same traps wearing different
clothes.

---

## 1. Naming a package's symbol in the call that loads the package

Wolfram resolves symbol names when input is **parsed**, before any of it runs. A
symbol mentioned in the same evaluation that loads its package therefore binds
to the current context — the package's own definition does not exist yet — and
the rest of the expression operates on the wrong symbol.

Two independent hits in one week, both on a package's short-form symbols: a
predicate returned `False` where the truth was `True`, and a configuration
variable came back as an unevaluated `Global`` symbol instead of its setting.

**Do:** load in one call, use in the next. If it must be one call, defer
resolution with `Symbol["Package`Name"]` or `ToExpression` after the load.

## 2. Treating `success: true` as proof that work happened

It means exactly two things: no exception was raised, and no timeout fired. Two
distinct ways that is not the same as "the cell did its job":

**An external process can fail while the cell succeeds.** A cell that shells out
— to a solver, a compiler, anything — gets whatever that process leaves behind.
If the tool aborts on a bad path, the cell still completes. Observed: an external
reduction step aborted on a stale binary path, the cell reported success, and the
chapter summary said zero failures. The results were right only because a stale
cache happened to survive.

**A cell that loads a stored result is indistinguishable from one that computes
it.** `x = Get["stage.wl"]` and a genuine computation both report success.
Observed: a chapter "ran" in 1.6 s because the compute call was commented out and
a `Get` stood in its place. The implausible timing was the only signal.

**Do:** verify an artifact — a length, a byte count, a file mtime — not a
status. Note `ValueQ` misses `DownValues`, so it will not see `f[2]`.

## 3. Warm kernels hiding missing definitions

A notebook that references a symbol it never defines works perfectly in the
kernel where you defined that symbol by hand, and returns **silent zeros** in a
fresh one.

Observed: a notebook using two symbols computed interactively but never defined
in the document. Cold replay returned `0` for two terms that should have been
non-zero. No message, no failure.

**Do:** before calling a notebook reproducible, replay it in a kernel that has
run nothing else, and compare against the warm result.

## 4. `DumpSave` of a whole context shadowing a package

`DumpSave["state.mx", "Global`"]` then `Get["state.mx"]` in a later kernel
creates **empty `Global`` symbols** for every name the dump touched. Those shadow
the package symbols of the same name, so calls return unevaluated — which looks
like a result and raises nothing.

Observed after one restore: **33 symbols** affected, and every call into that
package silently did nothing, because the dump had captured its short-form names
into `Global``.

This matters precisely because checkpointing is the right instinct, and this is
the wrong way to do it.

**Do:** export named values rather than a context. After any restore, check
before trusting:

```wl
Context /@ {"SomeSymbol", "AnotherSymbol"}   (* expect the package, not Global` *)
```

## 5. Trusting stored reference files

Reference files a notebook `Get[]`s can predate every fix in the toolchain that
reads them. One set was produced under a since-fixed transport deadlock, a
cell-timeout bug, and a stale solver path that made a reduction abort while the
cell reported success — and parts of the saved output referenced a different
machine's home directory.

**Do:** treat agreement with a stored file as evidence of *reproducibility*, not
of correctness. If the file predates the last toolchain fix, it is a hypothesis.

## 6. Assuming an interrupted parallel evaluation left a clean session

The probe after `abort()` verifies that the kernel answers. It cannot verify that
an interrupted `DistributeDefinitions` left the subkernels holding a consistent
set of definitions, or that a gather cut short returned everything.

**Do:** after aborting parallel work, `CloseKernels[]; LaunchKernels[]` before
computing anything you intend to keep. This is the one entry here with no
detector in the server.

## 7. Comparing fingerprints across a load and a recompute

`{Length, LeafCount}` is the cheap divergence detector this project recommends,
and it has two failure modes that look exactly like a real divergence.

**Representation.** A value that has been through a serialise/deserialise round
trip — `Export`/`Import`, `Compress`, a package's own external form — is the same
mathematics in a different structure, and the leaf count differs. Measured on one
expression: **231021** after a round trip against **291262** computed in memory;
on a later stage, **1661857** against **1661650**. So a replay that takes a
notebook's `Get[]` paths will disagree with one that recomputes, meaninglessly.

**Opaque heads.** `{Length, LeafCount}` on `Dispatch[{...}]` is `{0, 1}` whatever
it contains, because `Dispatch` hides its rules. Any head that wraps its contents
opaquely behaves this way.

**Do:** compare like with like — both loaded or both computed — and use
`ByteCount` for anything whose head conceals its contents.

## 8. Reading a self-aborted cell as a failed replay

A cell can end its own evaluation by calling `Abort[]`, directly or through
something it invokes. That is **not** the same as running too long: a timeout is
imposed from outside and reports `timed_out`, whereas an abort is the code's own
control flow and reports `aborted` with a `reason`.

A common source is a guard that refuses to do something twice — many packages
abort rather than reload — so replaying a range that re-runs an already-satisfied
setup cell will show that cell aborted while the replay continues correctly past
it. Benign. Other aborts are not: a failed assertion or an error handler calling
`Abort[]` means the cell did not do its work.

Until this was understood the same event was far worse. The abort escaped the
whole range evaluation, the helper returned a bare symbol where an encoded reply
was expected, the WSTP link desynced, and the kernel was silently replaced —
costing five full notebook replays before anyone traced it. The measurement that
isolated it: a range *excluding* the aborting cell succeeded in 6.9 s, the same
range *including* it failed in 0.3 s.

**Do:** read the `reason` on an aborted cell before concluding anything, and do
not restart the kernel to make a benign abort go away — reloading costs you the
session state that made it harmless.

## 9. Assuming a saved notebook is a notebook file

`save` writes a real notebook file: a `Content-type` header, a `CacheID`, and
the internal cache block a front end reads to find the content. A plain `Put`
of the same `Notebook[...]` expression produces none of that. Both round-trip
through the kernel, which is exactly what makes the difference easy to miss —
reopen a bare dump here and it reads back perfectly.

A front end asked to open a bare dump takes a fallback path, and on a large
document that path is where front ends have been seen to die. So a notebook
that opens fine in this server can still be unopenable on a desktop.

The check is one line, and worth running on anything you hand to a human:

```bash
head -c 60 out.nb | grep -q Content-type && echo "notebook file" || echo "BARE DUMP"
```

`save` reports which it wrote in `written_by` (`frontend` or `put`) and sets
`notebook_file`. `put` means no front end was available; the file is readable
by the kernel but is not a document.

When a file the kernel opens happily still fails on a desktop, separate format
from content before hunting the content: save the *unmodified* original both
ways and try each. If the bare dump of untouched content also fails, nothing
you generated is at fault.

## 10. Reading a cell label that no run produced

`ShowCellLabel -> True` prints whatever `CellLabel` each cell carries. A cell
that this run evaluated gets a fresh `In[n]:=`. A cell that it did *not*
evaluate keeps the label stored in the file from whenever someone last ran it
interactively — so an export can show `In[593]:=` sitting between `In[500]:=`
and `In[502]:=`.

That jump is not corruption and not a numbering bug. It is the honest record of
a cell whose output this run did not write back, which happens whenever you
evaluate a cell's content through `evaluate()` instead of `evaluate_cells(...,
write_outputs=True)` — the usual reason being that the cell had to be modified
before it would run.

Read the pairing, not the numbers: a cell with a label out of sequence and no
`Out[]` beneath it was not evaluated into the document. If you want such a cell
to carry no misleading label at all, clear its `CellLabel` with `edit_cells`
before exporting.

## 11. Expecting `abort()` to unwind a whole replay by itself

Two different things look identical from inside the kernel: you calling
`abort()`, and a cell calling `Abort[]` on its own (a package guard refusing to
load twice, a failed assertion, an error handler). Both arrive as user-initiated
aborts, and the `CheckAbort` that keeps one bad cell from desyncing the link
absorbs both the same way.

They must not have the same consequence, so the server distinguishes them by a
marker the client sets before signalling, not by anything the kernel can see:

- **`abort()`** stops the span. The reply carries `stopped_early`,
  `stopped_at`, `cells_not_attempted`, and the index to resume from.
- **A cell aborting itself** costs that cell. The span continues, and the cell
  is reported with `aborted: true`.

So a range that comes back with fewer results than you asked for has not failed
— read `stopped_at` and resume from the next index. And a range that ran to the
end containing an `aborted: true` cell is not a range that ignored your abort;
it is a cell that aborted itself.

## 12. Assuming one cell is one line number

`In[]` counts **statements**, not cells. A cell holding several statements
separated by newlines consumes one number per statement, whether or not any of
them prints, and an `Out[]` carries the number of the statement that produced
it — not the cell's, and not its position among the outputs that survived.

So a six-statement opening cell is `In[1]` and the next cell is `In[7]`; a
three-statement cell at `In[9]` whose middle statement is the only one returning
a value is followed by `Out[10]`, and the cell after it is `In[12]`.

The trap when counting statements yourself: the stored boxes interleave the
statements with the newlines between them, as
`BoxData[{stmt, "\n", stmt, "\n", stmt}]`. That list has five elements and three
statements. Counting its length over-counts; counting the outputs that appeared
under-counts, because suppressed statements still consume a number.

Measured against a real document's own stored labels: counting statements agrees
with the front end on 191 of 193 consecutive multi-statement cells, and the
remaining differences are all *larger* gaps — the author having evaluated
something else in between, which a linear replay does not do. Counting cells
agreed on none of them.

## 13. Letting two spans overlap

Writing outputs back inserts cells, so the index of everything after them moves.
Re-locating your next boundary by content is right — but land it on a cell the
previous call already ran and that cell is evaluated twice. The second run gives
it a new, higher `In[]`, and the number it held before belongs to nothing.

The record then has a hole no cell accounts for. Nothing is missing and no value
is wrong, but a reader who cannot re-run the notebook cannot tell the difference
between an abandoned number and a deleted cell.

Measured, three spans over the same six cells:

| spans | labels | result |
|---|---|---|
| `0-2` then `3-5` | 1 2 3 4 5 6 | clean |
| `0-3` then `2-5` | 1 2 **5 6 7 8** | 3 and 4 orphaned |
| boundary that drops a stale output | 1 2 3 4 | clean |

The third row matters because it is the explanation that looks right and is not:
a span that removes a stale output at its boundary numbers perfectly. Only the
overlap orphans numbers.

Worse, a re-run can produce a **duplicate** number, not just a gap. The counter
climbs past numbers frozen on cells that were never re-labelled — one run by
hand, one deliberately left alone — and lands on one of them. Two inputs then
carry the same `In[]`, which `verify` reports as its own kind, "the same line
number is used by two inputs". Gaps are cosmetic; a duplicate is a record that
contradicts itself.

`evaluate_cells` now reports the re-run rather than letting it pass — the reply
carries `cells_re_evaluated`, the old and new label of each, and a `warning`.
Resume at the cell **after** the last one the previous call reported, not at the
boundary you re-located to.

`evaluate_cells(index=N)` and `evaluate_cells(from_=N, to=N)` are the same call.
They were not: the `index` form ignored `write_outputs` entirely, so a caller
repairing one cell got a success reply and an unchanged document.

## 14. Reading an interrupted cell as a failed one

A cell stopped by `abort()` is reported with `outcome: "aborted (interrupted,
not a failure)"`, separately from cells that genuinely failed. It is not an
error, and the replay can be resumed from the next index.

If you see `outcome: "failed"` with no error text, the reply says so in words
rather than printing a null. That means the cell reported no message of its own,
which is a reason to look at its output — not evidence of an abort.

## 15. Treating a close request as a close

`CloseKernels[]` returns when the request has been sent, not when the processes
are gone. Count the subkernels immediately afterwards and you will still see all
of them, and conclude nothing happened — while the memory does in fact come back
a moment later.

The same shape applies to any shutdown: asking is not observing. Wait for the
processes to actually leave, with a deadline, and report what is still there
rather than what you asked for. `kernel(action="close_subkernels")` does this,
which is why it can say `closed: 20, still_open: 0` and mean it.

A subkernel that survives the wait is the one worth knowing about —
`kernel(action="reap")` clears strays.

## 16. Starting a span one cell late

After `indices_shifted` you re-locate your next boundary by content. Land one
cell late and you skip a definition — and nothing fails. An undefined function
stays unevaluated, `Coefficient` of an unevaluated head is `0`, and every cell
downstream reports `success: true` while recording zeros. Measured: one missed
definition silently emptied ~700 results, and the record looked complete.

`evaluate_cells` now reports `executable_cells_skipped` and
`skipped_input_numbers` when a span begins after executable cells no span in
this session has run. It informs rather than refuses, because a skip is
sometimes deliberate — a cell run by hand, a cell you were told to avoid.

If it fires and you did not mean it, do not just re-run the missed cell: every
cell that already consumed its result computed from an unevaluated symbol, so
re-run the whole dependent stretch. Check a value you can recognise afterwards.
A page of zeros is what this failure looks like, and zeros are hard to
distinguish from a legitimate result.

## 17. Launching a long-lived external process through the shell

A kernel's descendants do not all share its fate. Measured by starting a tree,
`SIGKILL`ing the process that owns the link, and watching `/proc` for 60 s:

| descendant | survives the owner's death? |
|---|---|
| the master kernel itself | no — gone in ~1–3 s |
| `LaunchKernels[4]` workers | no — gone in ~2 s |
| `StartProcess[{"sleep", "..."}]` | no — gone in ~3 s |
| `Run["sleep ... &"]` | **yes — still alive at 60 s** |
| `Run["setsid sh -c '...' &"]` | **yes — still alive at 60 s** |

The boundary is not "Wolfram process versus external program". It is **managed
versus shell-detached**. Anything the kernel owns as a process object follows it
down; anything handed to a shell with `&`, or moved into its own session with
`setsid`, outlives the whole tree and is reparented to init.

That matters for a long solver run — a reduction, an integral table, anything
started for its side effects and left going. Start it with `Run["... &"]` and a
crash leaves it burning CPU with nothing tracking it: it is not a Wolfram
kernel, so the orphan reaper does not know about it, and once `setsid` has moved
it out of the process group it cannot be reached by group signalling either.

**Prefer `StartProcess`.** It returns a `ProcessObject`, the kernel owns the
lifecycle, and cleanup is free. Use `Run["... &"]` only for something short
enough that you would not mind it finishing unattended — and if a detached
process must outlive its caller, record its pid, start time and command
somewhere durable first, because nothing in this server will do it for you.
