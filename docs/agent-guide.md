# Driving this server well

For the agent, not the maintainer. `design/` explains why the transport is what
it is; this explains how to get correct answers out of it quickly.

Every claim here was measured against a real workload rather than reasoned from
the API. Where a number appears, it came from a 994-cell symbolic-algebra
notebook with 276 code cells and a 20-way `LaunchKernels[]` fan-out.

---

**Scope.** This describes the server and the Wolfram-level mechanisms it exposes
you to. It says nothing about any particular notebook or package — which chapter
defines which symbol, which cells are expensive — because that belongs with the
notebook. Measurements quoted here come from one real workload (a two-loop quantum field
theory notebook, 994 cells, heavy symbolic algebra and a 20-way `LaunchKernels[]`
fan-out). They are evidence that a mechanism is real and worth its cost. They are
also literal values from that specific document — if you are replaying it you may
well see the same numbers, and that is a coincidence of provenance, not a
generic expectation. If you need to know what a notebook
defines, read it with `cells()` — and to find *where* a particular symbol is
assigned, `cells(defines="SymbolName")` searches the open document for you
instead of making you page through previews.

## The shape of a session

**The first tool call starts a kernel.** There is no explicit "launch" step, so
`status()` before you do anything reports `running: false, generation: 0` — that
is a kernel that has not started yet, not a broken one.

**`status()`'s `orphans` list is a census, not an accusation.** It reads a
registry shared by every server process on the machine, so it lists kernels
belonging to *other* live sessions alongside genuinely stranded ones. Check
`owner_alive` and `still_ours` before concluding anything is abandoned, and read
the top-level `kernel` object — not `orphans` — for the kernel backing *your*
session.

The kernel is persistent and shared across calls. Definitions survive; so do
mistakes. Three consequences worth internalising before anything else:

- **One compound expression beats three round trips.** The round-trip floor is
  ~0.3 ms, so calls are cheap — but each one is a place for you to lose the
  thread. Ask one question well.
- **Load a package in its own call.** Symbols resolve when the expression is
  *parsed*, before any of it runs, so naming a package's symbol in the same call
  that loads the package binds it to the current context and quietly gives you
  the wrong answer. This has cost two separate sessions a wrong result in one
  week — a predicate returning `False` where the truth was `True`, and a setting
  read back as an unevaluated symbol.
- **Ask for a measurement, not the object.** `Length`, `LeafCount`, `Short`,
  `Part`. A two-loop amplitude is 291262 leaves; you do not want it in your
  context and neither does anyone reading your transcript.
  `vars(action="get")` enforces this for you: above a few hundred KB it returns
  the symbol's size, head and length with `value_omitted: true` instead of the
  expression. Pass `full=True` to override it, having decided you want that.

## Notebooks

A notebook is a `.nb` file on disk. Open it, look, then run ranges:

```
notebooks(action="open", path="/abs/path/Foo.nb")   → id, cell_count, code_cells
cells(offset=200, limit=20, include_content=False)  → indices, styles, previews
evaluate_cells(from_=1, to=72)                      → per-cell timings, printed, messages
```

Cells run from their stored boxes, so nothing is lost in retyping. Only
`Input`/`Code` cells execute; everything else comes back `skipped`. About a
quarter of a real notebook is code, so judge a replay by `executed`, never by
`seen`.

**A shift can shrink the document, not only grow it.** Writing outputs back also
deletes stale ones, so `net_cell_change` is often negative and later indices move
*down*. `cells_inserted` and `stale_outputs_removed` are reported separately;
`indices_shifted` is true for either.

**Indices inside one reply are all pre-shift.** Every edit is applied after the
whole range has finished evaluating, never during it, so the `index` on each
entry in `results` refers to the document as it was when the call started —
including entries after the point where a cell was inserted. There is no
"trustworthy up to here" boundary to find inside a reply. What `indices_shifted`
tells you is that the numbering has changed *for your next call*, so re-locate
before that one, not within this one.

**`outputs_written: 0` means nothing needed writing, not that writing failed.**
A cell whose statements all end in `;` has no result to record, so a range full
of them legitimately writes nothing. A genuine failure shows up as a cell with
`success: false` in `results`, never as a silent zero. `stale_outputs_removed`
counts pre-existing Output cells that were dropped because this run produced no
result for them.

**Mixing `evaluate()` into a replay is safe.** Running a cell's code yourself —
to modify it before running, say — does not disturb the notebook's cell labels,
the write-back numbering, or the next call's index map. It simply writes nothing
back, so that cell ends up with no `Out[]` in the record. Say so in your report
rather than leaving a reader to wonder why one input has no output.

**A cheap way to watch for index drift:** `notebooks(action="info")` returns the
cell count without pulling a listing, so comparing it against the last known
count is a tripwire you can afford after every call. Reach for `cells()` only
once it tells you something moved.

**When a reply summarises**: a range switches to the compact form once the full
reply would exceed ~12000 characters, which in practice is somewhere near a
hundred evaluated cells — it depends on how noisy they are, not on the count.
`detail="full"` forces the long form, `detail="summary"` forces the short one.

**Don't ask for the whole notebook at once.** A reply that exceeds the size cap
is degraded rather than refused — content is dropped first, then the window
narrowed, and the reply says which happened — but you get far more out of
`cells(style="Input")` or a few hundred cells at a time with
`include_content=False`.

**A cell that aborts itself is reported, not fatal.** `Abort[]` — called by the
cell or by anything it invokes — ends that cell's evaluation without a timeout
and without an exception. Such a cell comes back `aborted: true` with a `reason`,
and the replay continues past it. This is distinct from `timed_out`, which means
a deadline was imposed from outside.

The common benign case is re-running a setup cell whose work is already done:
many packages guard against loading twice by aborting, so a range that re-runs
such a cell shows it aborted while everything after it is correct. Do not restart
the kernel to make that go away — you would lose the state that made it harmless.
Read the `reason`; an abort from a failed assertion or an error handler means
something quite different.

**Cell indices are the server's.** Get them from `cells()`. A raw parse of the
file can be offset by one against them.

**"Chapter boundary"**, wherever this guide uses the phrase, means a heading
cell — `Chapter`, `Section`, `Subsection`, `Subsubsection`. Enumerate them with
`cells(style="Chapter")` (or whichever level the document uses) and treat the
executable cells between two headings as one unit of work.

`evaluate_cells` gives you per-cell `timing_ms` for free. Use it — a chapter
timing table costs you nothing and tells you immediately when a "fast" chapter
was fast because it loaded a stored result instead of computing one.

## Looking up the language

This server holds no documentation. It runs a kernel; asking it what a built-in
does gets you whatever that kernel happens to return, which for a built-in is
its attributes and nothing about behaviour:

```wl
CheckAbort // Attributes = {HoldAll, Protected}
CheckAbort[___] := "<kernel function>"
```

When Wolfram's own MCP server is available, **use `WolframLanguageContext` for
anything about the language**. It is a semantic search over the real reference
pages and returns the details table and worked examples. Do not reason from
memory about a function's edge cases — options like `PropagateAborts`,
`Method`, or what a head does when its argument is unevaluated are exactly where
a confident guess is wrong, and the pages settle it in one call.

Two boundaries matter, both verified:

- **It is a different kernel.** It cannot see a symbol defined in this session,
  and this session cannot see one defined there. Its per-session sandboxing puts
  symbols in a `Sessions`<id>`` context, so even `Global`x` will not resolve to
  your `x`. Anything about *your* state — what a replay defined, what a variable
  holds now — is a question for this server: `vars`, `evaluate`, `cells`.
- **Its `SymbolDefinition` is not a documentation tool.** It reads back
  definitions in its own kernel. Useful for inspecting something you defined
  there; useless for a built-in.

So: **the language → Wolfram's MCP; your kernel and your document → here.**
Neither substitutes for the other, and mixing them up wastes a call and can
produce a confidently wrong answer about a symbol that was never in scope.

## Long computations

Calls over ~120 s are moved to a background task by the client. This is normal;
plan chapters around it rather than shrinking timeouts to avoid it. `abort()`
works from a second call while the first is still in flight.

Before anything long, **checkpoint**. Well-written notebooks already contain
`Export["stage-N.wl", result]` cells at stage boundaries, often commented out so
that a replay does not overwrite reference data. For real work, leave them on: a
lost kernel then costs one stage instead of the whole session, and reloading is
seconds against minutes of recomputation. Export named values, not a whole
context — see `docs/pitfalls.md` #4 for why a `DumpSave` of `Global`` is a trap.

See `docs/pitfalls.md` for how *not* to checkpoint — `DumpSave` of `Global`` is
a trap that silently disables the package you are using.

## Interrupting

`abort()` interrupts the running evaluation and then probes the kernel. Read the
`kernel` field:

| value | meaning | what to do |
|---|---|---|
| `alive` | a round trip succeeded after the abort | carry on; definitions are there |
| `dead` | the kernel is gone | everything in memory is lost; reload from disk |
| `unverified` | no answer within the window | assume nothing; `status()` will keep showing `link_health: uncertain` until it resolves |

`unverified` is sticky on purpose. A warning that appears once and vanishes is a
warning nobody acts on, so it stays in `status()` until a watchdog or a normal
evaluation settles it.

`abort()` during an `evaluate_cells` span stops the span. The reply carries
`stopped_early`, `stopped_at`, and `cells_not_attempted`, and names the index to
resume from — the kernel and everything it has already computed are untouched,
so resuming is just another `evaluate_cells` from the next cell.

A cell that calls `Abort[]` *itself* is a different thing and does not stop the
span: it is reported with `aborted: true` and the replay continues. So a span
that ran to the end containing an aborted cell did not ignore your abort — see
pitfall 11.

Prefer `abort()` to `kernel(action="restart")` — restart destroys every
definition, and is for a wedged kernel, not a slow one.

**Aborting a large parallel evaluation has been seen to kill a kernel outright.**
Once, unreproduced, and traced to Wolfram's own abort handling rather than to
anything this server sends — it sends only the standard out-of-band
`WSAbortMessage`, the same one the front end uses. Nothing here can prevent it.
Checkpoint first.

## Parallel work

Subkernels are tracked, listed by `status()`, closed with the kernel, and
self-terminate within a few seconds if the master dies. They do not leak.

They do, however, **outlive the work that needed them**. A pool is released when
the kernel goes, and not before — so a finished computation keeps its fan-out
until someone closes the session. Measured here: one replay's 20 subkernels held
5.2 GB, 69% of all Wolfram memory on the machine, an hour after they had
anything left to do. Nothing reclaims it, because the master kernel is healthy
and still holds every result the caller wants; restarting would free the memory
and throw those results away.

```
kernel(action="close_subkernels")
```

releases the pool and nothing else. Definitions survive — this is not a restart.
The reply says how many closed, how many did not, and how much memory came back
(about 265 MB per subkernel). Work that needs parallelism again just calls
`LaunchKernels[]` and pays the startup cost then.

**When a parallel computation finishes, ask the user whether to keep the pool
open.** Do not decide it yourself: keeping it costs gigabytes, and closing it
costs a minute of relaunch time if they were about to continue. Only they know
which. Ask once, plainly — "the 20-way pool is holding ~5 GB; keep it for
further work in this kernel, or close it?" — and they may also ask you to close
it at any later point, which is the same single call.

The risk is not leakage, it is coherence. An abort landing while large
expressions are in flight to subkernels can leave the session subtly wrong even
though the kernel survives and answers a probe. **A probe verifies the
transport; it never verifies the algebra.**

`abort()` reports `parallel_state: "unverified"` whenever subkernels are live,
and `rebuild_parallel_kernels=True` closes and relaunches them as part of the
call. It is offered rather than done automatically, for two reasons. Most aborts
involve no parallel work, and closing twenty subkernels unasked is its own
unannounced side effect — the kind this server exists to avoid. And the risk is
**reasoned, not measured**: it follows from what an interrupted
`DistributeDefinitions` could leave behind, but no corruption of that kind has
actually been observed here. Treat it as a cheap precaution after aborting
parallel work, not as a known failure you are repairing.

**An expensive operation applied to a whole collection at once is the usual
bottleneck**, and it is usually avoidable. Cost is rarely spread evenly: a few
elements dominate and the rest are cheap, so handing the entire collection to one
call means the slow elements hold everything hostage with no partial result and
no way to bound the damage.

Measured on one such step: an earlier transformation had inflated the expression
forty-fold, and a canonical-form call then received all 21 elements as a single
argument. 18 finished in seconds; 3 were the entire bottleneck.

Two mitigations, buying different things:

- **Map it per element under `TimeConstrained`.** You keep the result wherever it
  is affordable, the expensive elements fail individually, and the cost is
  bounded. This is normally what you want.
- **Replace the expensive operation with `Identity`.** Instant, and buys nothing:
  structurally correct, unsimplified, much larger. Use it only when you need the
  chain to reach a later stage and do not care about the form.

Reach for the first unless you know you want the second.

## Checking the record against the notebook

Do not judge a replay by page counts and success flags. Every defect this
write-back has had was found by a person opening the PDF and recognising that it
did not look like a notebook — results collapsed into a list where there should
have been three, an output left over from an earlier run, values typeset in the
wrong form. Three consecutive runs verified page counts and missed all of it.

The reference is on disk. Compare against it:

```
notebooks(action="verify", path="/path/to/the/original.nb")
```

It reports, per input cell, where the number of outputs differs from the
original and whether the output form matches, and it separates the expected
differences — a stale output dropped for a cell that is commented out — from
real ones. Run it before exporting, and quote the verdict in your report.

It checks **shape, not values**: it catches a malformed record, not a changed
result. Fingerprints are still how you compare the mathematics.

## Saving the document

```
notebooks(action="save", path="/a/new/path.nb")
```

Never save over the file you opened; a replay is a new document, not an edit of
the original.

`read_notebook_file(mode="outline")` counts `limit` and `offset` in **cells
scanned**, not entries returned — `limit=400` reads the first 400 cells and
returns however few headings are among them. `cells()` counts entries, so the
two differ; page with `offset` until the outline stops growing.

`render(action="cell")` rasterises a cell **without** its `In[]`/`Out[]` label;
only `render(action="export")` sets `ShowCellLabel`. Use an export, or read the
label from `cells()`, when the label is what you are checking.

`timeout` has no ceiling — the schema's default (300s for a span) is a default,
not a limit, and a long chapter can be given an hour without complaint. The
per-cell timeout bounds each cell; the transport is given room for the whole
span on top of it.

Cell labels are rewritten as the front end would: `In[]` advances once per
*statement*, so a cell holding six statements consumes six numbers and an
`Out[]` carries the number of the statement that produced it. A gap between
consecutive `In[]` numbers is normal and is not a numbering fault — see
pitfall 12.

**Check `written_by` in the reply.** `frontend` means a real notebook file was
written — the header, the CacheID, the block a front end reads to find the
content. `put` means no front end was available and the file is a bare
expression dump: the kernel reads it back perfectly, and a desktop front end may
refuse it or die on it. `notebook_file` says the same thing as a boolean.

This distinction is invisible from in here — a dump reopens flawlessly through
these tools — so the reply is the only signal you get. Do not report a saved
document as delivered without looking at it. See pitfall 9.

## Exporting a record

`render(action="export", path=...)` writes the open session to PDF (or to
Markdown for a `.md` path). Two defaults are deliberate: **cell groups are
opened**, because a notebook saved collapsed would otherwise export with most of
its content missing; and **paper is A4 portrait**, because a defined page is what
makes clipping detectable at all.

**Content wider than the page is cut off, not scaled**, and the front end gives
no sign of it. Anything with an explicit `ImageSize` wider than the printable
area is at risk — layout code that sizes its own output commonly produces this.
Measured on a row of 12 graphics forced to 2400 pt: 4 survived on A4 portrait, 5
on A4 landscape, 12 on a page wide enough to hold them.

The export detects this and returns `action_required: "ASK THE USER"`. When you
see it, **stop and put the choice to the user**:

- `fit_width=True` widens the page so nothing is lost — **recommended**, at the
  cost of a non-standard page size.
- Keep A4 and accept that content is missing — **not recommended**.

Do not quietly pick one. A silently truncated record is worse than either, and
which trade-off is acceptable depends on what the document is for.

Do not try to fix it by editing the graphics either. Setting an oversized
`ImageSize` to `Automatic`, or scaling it down, both leave a `GraphicsBox` of
absolutely positioned insets unable to render — the export comes back with a
pink placeholder where each picture should be. `Magnification` avoids that but
does not help: it is a view scale, and printing clips at the same width.

## Seeing things

`render()` drives a headless front end — no display required, `WolframNB` runs
as a child of the kernel. Rasterise a cell or typeset an expression rather than
dumping boxes when the point is for a human to look at it.

```
render(action="expression", code="Column[{...}]", dpi=110)
render(action="cell", index=927)
render(action="export", path="out.pdf", open_groups=True)
```

## Comparing against an interactive run

The usual reference for a headless replay is the same notebook run by a human
with a front end. What matters there is **fidelity** — that this run is the same
computation — not correctness in the abstract.

Emit a fingerprint at each chapter boundary so a divergence is visible
immediately rather than after a full manual replay:

```wl
{Length[#], LeafCount[#]} & /@ {result1, result2, result3}
```

Leaf count is a tripwire, not physics: it proves something changed, never that
anything is right, and two different expressions can share one.

**Two ways it misleads, both seen on real runs:**

*It depends on representation, not just content.* A value that has been through a
serialise/deserialise round trip — `Export`/`Import`, `Compress`, a package's own
external form — is the same mathematics in a different structure, and counts
differently. Measured: 231021 after a round trip against 291262 computed in
memory, and 1661857 against 1661650 at a later stage. So a replay that takes a
notebook's `Get[]` paths and one that recomputes *will* disagree, meaninglessly.
Compare like with like.

*It is degenerate for opaque heads.* `{Length, LeafCount}` on a `Dispatch[]` is
`{0, 1}` however many rules it holds, and any head that conceals its contents
behaves the same way. Use `ByteCount` for those.

## Checking a record that has no reference

A computation built from scratch has nothing to compare against, and that is
exactly when the exported record carries the whole weight: a reader who cannot
run the notebook judges the work by what the page says.

```
notebooks(action="verify")          # no path: checks the document against itself
```

It audits the document's own cell labels — `In[]` advancing by each cell's
statement count, every `Out[]` inside its input's range, no number used twice —
and refuses to vouch for a record that misnumbers itself.

Two limits worth knowing, because they decide what a clean verdict means:

- **It cannot detect a shift of the whole sequence.** Closing a front end or
  quitting a kernel deregisters labels, so absolute numbers are only meaningful
  within one session. Self-consistency is a claim about the record's internal
  arithmetic, not about which session produced it.
- **It cannot detect a wrong counting rule.** The check and the writer both
  count statements the same way, so a rule that is wrong agrees with itself.
  Only `verify` *with* a reference catches that, by comparing how the line
  numbers advance against labels a real front end wrote — reported under
  `labels.increments_vs_reference`.

It is gated on authorship. A record this server numbered is held to the rule; a
notebook somebody evaluated interactively is not, because re-running cells and
working out of order legitimately produce gaps and repeats. Records this server
writes are marked, so the check still applies after the file is reopened in a
later session.
