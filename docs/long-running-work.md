# Work that outlives the client

By default this server owns its kernel, which ties the kernel's life to the
server's. Measured, a kernel exits about a second after its owning process is
killed. For a cell that runs for minutes that is an annoyance. For one that
runs for hours or days, a dropped connection destroys the work.

The supervisor is the other arrangement: a separate process owns the kernel and
takes requests over a Unix socket. Clients come and go; the computation does
not.

It is **opt-in**, and nothing starts it implicitly - a call that quietly leaves
a long-lived process on your machine is not something a tool should do as a
side effect.

## Using it

```text
supervisor(action="start")     launch one, deliberately
supervisor(action="use")       point notebook execution at it
supervisor(action="status")    is one running, and what is it doing
supervisor(action="lookup", key="...")
                               what became of a request, without running anything
supervisor(action="stop")      shut it down
supervisor(action="use_direct")
                               back to this process's own kernel
```

Two things to know before you switch:

- **Open a notebook after choosing the backend, not before.** A notebook lives
  in the kernel that opened it, so switching strands it. `use` tells you which
  notebooks were left behind, because a silent orphan looks exactly like a
  notebook that was never opened.
- Everything runs in the supervisor's kernel once selected, including
  `evaluate` and `vars`, so there is one set of definitions rather than two.

## What survives a crash, and what does not

This distinction is the whole point, so it is worth being exact.

| | survives the client dying |
|---|---|
| the kernel and every definition in it | yes |
| an evaluation already running | yes, it keeps going |
| the record of what a replay intended | yes, written to disk before the first cell |
| the original MCP call that was waiting | no |
| the kernel, if the **machine** or the kernel itself dies | no |

A new client reconnects by the key the dead one chose. It does not resume the
old call; it asks what happened to it:

```text
replay(action="reconcile")
=> c1  COMPLETE
   c2  STILL_RUNNING      found in the ledger, nobody waiting for it
   c3  NEVER_SUBMITTED
```

`STILL_RUNNING` is a first-class answer rather than an error. The computation
is alive with no client attached, and whether to wait for it, take it over or
abandon it is your decision - this layer will not end someone else's science on
its own.

For genuinely multi-day work, note the last row of that table: the supervisor
separates client lifetime from computation lifetime, and nothing more. If the
kernel or the host dies, the work is gone, and domain-level checkpoints are
still worth having.

## Letting go of an idle kernel

A kernel is worth keeping because it holds state that cost something to build,
and worth releasing because holding it costs memory. The rule, in order:

```text
work in flight             never released, at any elapsed time
a result nobody collected  never released, at any elapsed time
a client connected         never released
idle, past the window      released, and recorded before it happens
```

The window is a day by default (`SUP_RECLAIM_AFTER`). `supervisor(action=...)`
and the `RECLAIM` command both report which of these is keeping a laboratory
alive, so you can read the rule rather than infer it.

## Cost

Measured on a real 275-cell symbolic workload, against the same replay run in
this process's own kernel:

| | direct | supervisor |
|---|---|---|
| wall | 513.8 s | 521.1 s |
| kernel time | 433.3 s | 434.3 s |
| orchestration | 75.4 s | 83.0 s |

About 28 ms per cell, or 1.75% of kernel time. The outputs were identical:
989 cells and 386 outputs on both sides, 143 ordinals matching exactly, and
every one of the 15 differences an `AbsoluteTiming` value.
