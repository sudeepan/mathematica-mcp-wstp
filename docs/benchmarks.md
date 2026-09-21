# What things cost

The numbers below are **measured, not illustrative**. They were taken with
Mathematica 15.0.1 on Linux-x86-64 with a warm kernel.

The point of the table is not to promise one universal overhead. The notebook
replay measurements show the opposite: a microbenchmark can measure the
implementation correctly while completely missing the scaling regime of the
real document.

## Round trips

| | |
|---|---|
| Round-trip floor (`1+1`) | 0.27 ms |
| Symbolic result (`Integrate[1/(1+x^3),x]`) | 2.1 ms |
| 100 KB result | 10.9 ms |
| Abort to `$Aborted`, same kernel pid, state intact | 2.0 s |
| Dead kernel reported as a typed error | 0.3 s |
| Headless front end, cold start | 1.8 s |
| Kernel shutdown with its subkernel tree closed | 0.27 s |

Splitting ordinary work across several calls is cheap at the transport level.

## Per-cell replay

`replay` gives every executable cell its own identity and a resumable manifest.
That requires one execution boundary per cell. The measured cost depends strongly
on notebook size:

| notebook | per cell |
|---|---|
| 13 trivial cells | 8.9 ms |
| adversarial, many outputs | 47 ms |
| real 994-cell physics notebook | 284 ms (median) |

On the real notebook, orchestration overhead is nearly flat while kernel work
per cell ranges from 10 ms to 134 s. That is the signature of a cost dominated
by document handling rather than by the symbolic work itself.

The lesson is deliberate: **do not extrapolate a small-notebook benchmark to a
large scientific notebook.**

## On the real workload

The controlled workload contains 275 executable cells from a two-loop symbolic
physics notebook, recomputed rather than loaded from cached results.

| | span | per-cell | per-cell, supervised |
|---|---|---|---|
| wall | 436.9 s | 513.8 s | 521.1 s |
| kernel time | 435.7 s | 433.3 s | 434.3 s |
| orchestration | ~1.2 s | 75.4 s | 83.0 s |

Kernel time agrees to within 0.2% across all three, which is how we know the
paths performed the same scientific work rather than a cheaper variant. The
per-cell path costs about 18% more wall time than handing the whole span to the
kernel; putting the kernel in its own process adds a further 1.75%.

What the per-cell path buys:

| | span | per-cell |
|---|---|---|
| same results | yes | yes |
| per-cell execution identity | no | yes |
| resumable after a client dies | no | yes |
| output traceable to the execution that wrote it | no | yes |
| source-divergence detection | no | yes |
| abort attribution | inferred | recorded |

The outputs were identical in every comparison: 989 cells and 386 outputs on
each side, 143 ordinals matching exactly, and all 15 differences
`AbsoluteTiming` values.

The performance comparison should therefore be read as a trade: additional
orchestration buys an execution record that can be reconciled and audited.
