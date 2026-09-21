# How it works

Two paths carry information between the server and the kernel, and keeping them
apart is the whole design. Everything this server can do that an ordinary
request/reply socket cannot comes from the second one.

## The two channels

```mermaid
flowchart TB
    A["AI agent<br/><i>MCP client</i>"]
    S["server.py<br/><i>tool surface</i>"]
    E["session.py<br/><i>owns one kernel</i>"]
    L["link.py<br/><i>ctypes to libWSTP</i>"]
    K["WolframKernel<br/><i>own process group</i>"]
    P["subkernels<br/><i>LaunchKernels[]</i>"]
    F["WolframNB<br/><i>-platform offscreen</i>"]
    X["external processes<br/><i>RunProcess[...]</i>"]
    R[("registry<br/><i>pids on disk</i>")]

    A <-->|"JSON-RPC over stdio"| S
    S --> E
    E --> L
    L ==>|"evaluation channel"| K
    L -.->|"message channel (abort)"| K
    K --> P
    K --> F
    K --> X
    E -.->|"records pid + pgid"| R
    R -.->|"reaps what a crash left"| K

    classDef ours fill:#e8f0fe,stroke:#4a76c7,color:#123
    classDef theirs fill:#f6f6f6,stroke:#999,color:#333
    class S,E,L,R ours
    class K,P,F,X theirs
```

The thick arrow into the kernel is the evaluation: one expression down, one
result back. The dotted arrow beside it is WSTP's out-of-band message channel,
which stays writable while the evaluation channel is blocked. Everything this
server does that a request/reply socket cannot comes from that second arrow.

## A real trace

Replaying a notebook in which one cell never terminates:

```mermaid
sequenceDiagram
    participant A as Agent
    participant S as server.py
    participant K as Kernel
    participant P as Subkernels

    A->>S: notebooks(open, "analysis.nb")
    S->>K: Get[...] then cell positions
    K-->>S: 994 cells, 276 code

    A->>S: evaluate_cells(from_=0, to=200)
    S->>K: cell 1 boxes to ToExpression
    K->>P: LaunchKernels[] spawns 20
    K-->>S: Print output (text packet)
    K-->>S: result (return packet)
    Note over S,K: repeats per cell, state carrying across

    A->>S: evaluate_cells(from_=920, to=930)
    S->>K: cell 925 boxes
    activate K
    Note over K: simplification that<br/>will not terminate
    S-->>S: per-cell deadline expires
    S-->>K: WSAbortMessage (message channel)
    K-->>S: $Aborted
    deactivate K
    Note over S,K: kernel alive, same pid,<br/>every definition intact
    S-->>A: aborted: 1, executed: 9, failed: 0

    A->>S: evaluate_cells(from_=931, to=993)
    Note over S,K: replay continues

    A->>S: kernel(restart)
    S->>K: CloseKernels[] over the link
    K->>P: closes all 20
    S->>K: SIGTERM process group, then SIGKILL
    Note over S,P: nothing left running
```

Three moments in that trace are the point of the project.

1. **The abort lands on a busy kernel.** The deadline fires in Python, the
   message goes out of band, and the evaluation returns `$Aborted`. The caller
   does not wait forever and the kernel is not destroyed.
2. **The replay carries on.** Cell 925 failing costs cell 925, not the session.
   Everything the first 924 cells defined is still in the kernel.
3. **Shutdown reaches the whole tree.** The kernel is asked to close its
   subkernels over the link while it can still answer. Only then is the process
   group signalled, so nothing is left to be found later.

---

## Where the supervisor fits

Everything above describes the default arrangement, where this server owns the
kernel. That makes kernel lifetime equal to client lifetime: measured, a kernel
exits about a second after its owner is killed. For a cell that runs for
minutes that is a nuisance; for one that runs for days a dropped connection
destroys the work.

The supervisor is the other arrangement. A separate process owns the kernel and
takes requests over a Unix socket, so a client can come and go while the
science continues.

```mermaid
flowchart LR
    C1["client<br/><i>comes and goes</i>"]
    SUP["supervisor<br/><i>own process group</i>"]
    K["WolframKernel"]
    LED[("ledger + manifest<br/><i>on disk</i>")]

    C1 <-->|"Unix socket"| SUP
    SUP --> K
    SUP --> LED
    LED -.->|"LOOKUP by the key<br/>the caller chose"| C1

    classDef ours fill:#e8f0fe,stroke:#4a76c7,color:#123
    classDef theirs fill:#f6f6f6,stroke:#999,color:#333
    class SUP,LED ours
    class K,C1 theirs
```

It is opt-in and never starts by itself. See
[`long-running-work.md`](long-running-work.md).
