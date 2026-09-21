# What things cost

Measured, not estimated. Mathematica 15.0.1, Linux-x86-64, warm kernel.

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

Splitting work across several calls costs essentially nothing.

## Per-cell replay

`replay` gives every cell its own identity and a resumable manifest, at the
price of a round trip each. What that costs depends on the notebook, and the
spread is the interesting part:

| notebook | per cell |
|---|---|
| 13 trivial cells | 8.9 ms |
| adversarial, many outputs | 47 ms |
| real 994-cell physics notebook | 284 ms (median) |

The overhead is nearly flat while kernel work per cell ranges from 10 ms to
134 s, which is the signature of a fixed cost that scales with *document size*
rather than with the work being done. A microbenchmark on a small notebook
measures the implementation correctly and still tells you almost nothing about
the regime you will actually run in.

## On the real workload

275 cells of a two-loop symbolic physics notebook, recomputed rather than
loaded from cached results.

| | span | per-cell | per-cell, supervised |
|---|---|---|---|
| wall | 436.9 s | 513.8 s | 521.1 s |
| kernel time | 435.7 s | 433.3 s | 434.3 s |
| orchestration | ~1.2 s | 75.4 s | 83.0 s |

Kernel time agrees to within 0.2% across all three, which is how we know the
same work was done rather than a cheaper variant of it. The per-cell path costs
about 18% more wall time than handing the whole span to the kernel; putting the
kernel in its own process adds a further 1.75%.

What that buys:

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
