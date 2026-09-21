# Work that outlives the client

A long Wolfram evaluation has at least three different lifetimes:

```text
the MCP call
the client/session waiting on it
the scientific computation in the kernel
```

By default, those lifetimes are coupled more tightly than you may want. This
server owns its kernel, and measured behavior is that the kernel exits about a
second after its owning process is killed. For a minute-long cell that is an
annoyance. For a cell that runs for hours or days, it can destroy the expensive
part of the experiment.

The supervisor exists to break that coupling.

A separate process owns the kernel and accepts execution requests over a Unix
socket. Clients can come and go; the computation does not.

It is **opt-in**. Nothing starts it implicitly.

## Use it deliberately

Choose the backend **before opening the notebook**:

```text
supervisor(action="start")     launch one deliberately
supervisor(action="use")       route execution to it
notebooks(action="open", ...)  open the document in that kernel
```

Other supervisor actions:

```text
supervisor(action="status")    what is running and why the kernel is retained
supervisor(action="lookup", key="...")
                               retrieve an existing request without running anything
supervisor(action="stop")      shut it down
supervisor(action="use_direct")
                               return to this process's own kernel
```

A notebook session belongs to the kernel that opened it. Switching execution
backends after opening the notebook strands that live document in the old
kernel. Reopen it after choosing the backend you intend to use.

Once the supervisor is selected, `evaluate`, `vars`, notebook replay and the
rest of the execution surface use the supervisor's kernel. There is one
scientific state, not a hidden direct copy and a supervised copy.

## What survives a client/session exit

| | survives the client dying |
|---|---|
| the kernel and every definition in it | yes |
| an evaluation already running | yes, it keeps going |
| the record of what a replay intended | yes, written to disk before the first cell |
| the original MCP call that was waiting | no |
| the kernel, if the **machine** or the kernel itself dies | no |

The fourth row is easy to misunderstand. The original waiting RPC is not a
durable job handle. If the client session exits, that call is gone even though
the scientific evaluation continues.

A new client reconnects through durable identity:

```text
replay(action="reconcile")
=> c1  COMPLETE
   c2  STILL_RUNNING
   c3  NEVER_SUBMITTED
```

The reconnecting client does not "resume the old call." It reads the replay
manifest and asks the supervisor ledger what became of the existing execution.

`STILL_RUNNING` is a first-class state. The layer will not abort someone else's
science merely because no client is currently attached.

For multi-day cells, that reconnect path should be considered normal operation,
not an exceptional disaster path.

## Timeout is not the same as lifetime

A timeout is an execution policy: how long one cell is allowed to run before
control is requested.

The timeout does not need to be small. The important thing is to set it
deliberately and make it visible in the replay record. A day-long computation
with a day-scale timeout is structurally the same kind of execution as a
minute-long one.

What the supervisor changes is **ownership**, not the scientific timeout.

It also cannot rescue a computation from a kernel or host failure. For work
whose recomputation cost is measured in days, domain-level checkpoints remain
worth having.

## Reconciliation depends on two records

For a long replay, two durable records cooperate:

```text
replay manifest
    run identity, child identity, intended source and progress

supervisor ledger
    accepted request, execution identity, control and outcome
```

The notebook layer understands cells. The supervisor understands execution.
`replay(action="reconcile")` joins the two without making either component
pretend to know the other's domain.

## Letting go of an idle kernel

A long-lived kernel is useful because it may contain state that took hours to
build. It is also expensive to keep forever.

The reclaim rule, in order:

```text
work in flight             never released, at any elapsed time
a result nobody collected  never released, at any elapsed time
a client connected         never released
idle, past the window      released, and recorded before it happens
```

The window is a day by default (`SUP_RECLAIM_AFTER`). `supervisor(action=...)`
and the `RECLAIM` command report which condition is keeping a laboratory alive,
so you can inspect the rule rather than infer it.

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

That measured cost is the price of separating client lifetime from scientific
computation lifetime on this workload.
