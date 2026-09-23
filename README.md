*DISCLAIMER: The code is LLM generated, so there is always room for improvement. So treat this as a functional implementation of an architecture that enables one to use LLM agents in a fully headless containerised environment, that work with long-duration stateful Wolfram kernels for complex research calculations that may even span days.* 

*P.S. I know the README is a bit verbose, but please remember that this is meant more for the agent than for the user.*

# Mathematica MCP over WSTP

**Auditable execution for agent-driven symbolic computation in Mathematica.**

An agent can always *ask* a computer algebra system for an answer. The harder problem is to figure out what actually happened: which cell ran, in which kernel, whether an interrupt came from the user or the code itself, whether an output followed from this run or another one, and whether work survived when the client disappeared. Afterall, an agent's own report of what it did is not hard evidence for what actually transpired. Which has to do with the fact that all generative models can, and do lie.

This server is built around that problem.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Mathematica 14+](https://img.shields.io/badge/Mathematica-14+-red.svg)](https://www.wolfram.com/mathematica/)
[![Transport: WSTP](https://img.shields.io/badge/transport-WSTP-2b7489.svg)](https://reference.wolfram.com/language/guide/WSTPAPI.html)

---

## The motivating workflow

Many research workflows are not centered on one notebook or one long interactive session. A more common pattern is **hub-and-spoke work**:

```text
               ┌── computational session A
               │
main research ─┼── computational session B
session        │
               └── computational session C
```

The **hub** is the session where the larger research problem is being developed and integrated.

The **spokes** are short-term computational investigations. They do not need to communicate with one another directly. Each may have its own notebook, kernel state, assumptions, intermediate results, and failure history. Each spoke can be used for investigating specific questions that emerged naturally in the course of research in the hub, and would require a Mathematica-driven workflow to adequately address them.

A harness like Claude Code enables agentic message passing across sessions, and the user can also prompt in any session to read up the transcript of another one, and also look up the artifacts generated therein. One of these sessions could be the hub, and the others can play the role of spokes. The artifacts must be generated in a *zero trust* manner - they should not change depending on the choice of the harness, model, model-effort, etc.

A typical workflow:

1. Work in the main research session.
2. Reach a question that benefits from a separate symbolic calculation.
3. Open a separate session with its own stateful kernel and notebook.
4. Perform the calculation there, including exploratory attempts and corrections.
5. Finalize the result into a durable notebook or artifact.
6. Return to the main session.
7. Read the auxiliary session's transcript and inspect its finalized
   computational record.
8. Use the result in the larger line of reasoning.

Several such investigations may happen sequentially or concurrently.

### Role of the MCP

In this model, the computational system is not primarily a graphical notebook frontend. The primary working surface may be an agent harness, terminal, editor, or research conversation. The notebook is still important, but as a **durable scientific record**, not necessarily as the place where all interaction happens.

That makes several properties more important than GUI integration:

- a long-lived stateful kernel;
- explicit ownership of that kernel;
- the ability to interrupt runaway work without discarding useful state;
- a durable identity for each scientific execution;
- clear recovery after a client or session disappears;
- notebook records that can be inspected later from another session;
- reproducible finalization into a self-contained artifact;
- provenance strong enough that one session can rely on results produced in
  another.

The goal is therefore not merely to let an agent execute code in a computer algebra system. It is closer to let independent computational sessions behave like small scientific laboratories whose results can later be inspected, trusted, and incorporated into a larger research process.

A computational session can be thought of as a temporary laboratory:

```text
temporary laboratory
    = agent session
    + stateful kernel
    + notebook
    + execution record
    + generated artifacts
```

### Roles of the transcript and the notebook

The transcript and the notebook serve different purposes.

```text
Transcript
    explains what the auxiliary session concluded
    and why it considered the result relevant.

Notebook / artifact
    records what was actually computed.
```

The transcript is the reasoning-level handoff. The notebook or artifact is the computational evidence. A robust workflow needs both.

This becomes especially important when one session consumes a result produced by another. The receiving session should not have to trust a sentence such as "the other session found that this identity holds." It should be possible to inspect the durable calculation behind that statement.

## Advantages of a headless design

A GUI-centric workflow is excellent when the human and agent are collaborating inside one visible notebook. A headless MCP becomes attractive when the unit of work is instead:

```text
research session
    ↕
computational session
    ↕
stateful kernel
    ↕
durable notebook / artifact
```

This makes it natural to run several independent investigations without forcing them into one interface or one global kernel state. Each computational session can remain isolated, while the main research session decides which conclusions and artifacts to bring back.

That separation also reduces accidental state coupling. Rather than sharing invisible kernel state between investigations, results move between sessions explicitly through notebooks, artifacts, transcripts, and recorded execution evidence. Even if sessions stop, the results computed while they were running must be preserved as demanded by the user.

The same architecture could also be useful for a single long-running session. The hub-and-spoke workflow explains why features like persistent kernels, recovery, audit trails, and durable notebook finalization matter, but it is not a requirement for using the server.

## The execution model in one minute

There are three levels to keep apart.

1. **A persistent Wolfram kernel.** Definitions and package state survive from one tool call to the next.
2. **A notebook replay record.** `replay` runs executable cells one at a time, giving each one an identity and recording the plan in a durable manifest before execution begins.
3. **Optional supervisor ownership.** The default server owns its kernel. With the supervisor selected, a separate process owns it, so a client can disappear while the computation continues.

A few terms appear throughout the documentation:

- **ordinal**: the *nth* `Input` or `Code` cell, counting only executable cells. It is stable when output cells are inserted or deleted; raw notebook indices are not.
- **manifest**: the replay-side record written to disk before the first child runs. It records the intended run and the progress of its cells.
- **execution token / request id**: identities attached to one cell execution.
- **idempotency key**: a stable key for an intended child execution, so a reconnecting client can refer to the existing work rather than accidentally creating a duplicate.
- **reconcile**: inspect a previous replay after interruption and distinguish completed, still-running, never-submitted, changed-source, and output-state cases.
- **backend**: the component that owns execution: either this process's direct kernel or the separate supervisor-owned kernel.

The distinction between **ordinal** and **index** matters immediately. Evaluating an input can insert an output cell, shifting every later index. "The seventh input cell" remains the seventh input cell.

## What a replay buys you

For ordinary one-off evaluation, use `evaluate`. For a span where cell-by-cell identity does not matter, `evaluate_cells` is the simpler baseline. For long-running or auditable notebook work, use `replay`.

A replay provides:

- one execution identity per executable cell;
- a manifest persisted before the first cell is submitted;
- per-child timeout policy and progress;
- output-to-execution provenance;
- detection when the source cell has changed underneath an existing run;
- reconciliation after interruption instead of guessing where to resume.

With the supervisor selected, reconciliation also survives loss of the client that originally submitted the work. The original MCP call does **not** survive a client/session exit; the scientific computation can. A new client asks the ledger what happened by durable key and continues from there.

## Why use WSTP

WSTP provides an evaluation channel and a separate out-of-band message channel. That lets the server interrupt a kernel that is currently busy without destroying the kernel merely to regain control.

That gives four practical properties:

- **Interrupt a running evaluation and preserve state.** A timeout or `abort()` targets the evaluation rather than replacing the whole kernel.
- **Distinguish a dead link from a slow computation.** Link failure becomes a typed failure rather than an indefinite wait.
- **Keep process ownership explicit.** Subkernels are tracked and can be closed without throwing away the master kernel's definitions.
- **Observe control separately from scientific output.** The execution layer can record that a user requested an abort even when Wolfram code catches that interrupt and returns a normal value.

The architecture and its two channels are described in[`docs/architecture.md`](docs/architecture.md).

## Quick start

You need Mathematica 14 or newer (15 recommended) and [uv](https://docs.astral.sh/uv/). There is no compiler step and no Wolfram SDK to build; the transport binds to the WSTP library that ships with Mathematica.

```bash
git clone https://github.com/sudeepan/mathematica-mcp-wstp.git
cd mathematica-mcp-wstp
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
claude mcp add --scope user mathematica-wstp -- "$PWD/.venv/bin/mathematica-wstp"
```

Restart your client and try a small evaluation.

> `--scope user` matters. `claude mcp add` defaults to `--scope local`, which
> registers the server for one directory only. Check from somewhere else
> (`cd /tmp && claude mcp list`) if a server seems to vanish outside the project.

The installation, kernel and WSTP library are discovered automatically, including relocated installs reachable only through a symlink on `PATH`. Override them with `MATHEMATICA_WSTP_KERNEL`, `MATHEMATICA_WSTP_INSTALL`, or `MATHEMATICA_WSTP_LIB`.

## What it looks like in use

**"Integrate that, and stop if it takes more than ten seconds."**

```text
evaluate("Integrate[Sqrt[1 + x^4], x]", timeout=10)
=> timed_out: true
   kernel_state: "intact, the evaluation was aborted rather than the kernel"
```

**"Replay this notebook so I can resume if something interrupts us."**

```text
notebooks(action="open", path="analysis.nb")
replay(action="run", timeout=300)
=> run_id: ...
   executed: 276
   failed: 0
   execution_timeout_seconds: 300
   manifest: .../.mcp-replays/....json
```

The timeout is per executable cell. If one cell is expected to run for hours, set that value explicitly rather than inheriting the default.

**"The client died while a long cell was running. What happened?"**

With the supervisor selected:

```text
replay(action="reconcile")
=> c1  COMPLETE
   c2  STILL_RUNNING
   c3  NEVER_SUBMITTED
```

`STILL_RUNNING` is not an error. It means the scientific evaluation is alive without the original client attached.

**"Show me what cell 39 looks like."**

```text
render(action="cell", index=39)
=> [typeset PNG from a headless front end]
```

## Choosing the right notebook path

Use **`replay`** when the work is long, interruptible, or needs an audit trail. Use **`evaluate_cells`** when you deliberately want one span-level operation and do not need durable per-cell identity.

`replay` addresses executable cells by **ordinal**, not raw notebook index. Writing outputs mutates the document, so indices move during the run.

Before replaying an unfamiliar notebook, run `notebooks(action="dependencies")` to discover which files it reads and writes
and classify them automatically. The fresh-agent procedure in [`docs/replaying-a-notebook.md`](docs/replaying-a-notebook.md) is designed for exactly this case.

## Work that lasts hours or days

By default, this server owns its kernel. If the owning client/server process is killed, the kernel goes with it.

For work that must outlive the client, deliberately opt into the supervisor:

```text
supervisor(action="start")
supervisor(action="use")
```

Then open the notebook and start the replay. A notebook belongs to the kernel that opened it, so choose the backend **before** opening the document.

The supervisor protects against **client loss**, not against a kernel or machine failure. Domain-level checkpoints are still necessary for work whose recomputation cost is measured in hours or days.

See [`docs/long-running-work.md`](docs/long-running-work.md).

## Tools

Sixteen consolidated tools rather than a wide flat surface:

| Tool                 | Purpose                                                      |
| -------------------- | ------------------------------------------------------------ |
| `evaluate`           | Run Wolfram Language in the persistent kernel                |
| `abort`              | Interrupt the running evaluation, keeping all state          |
| `kernel`             | `state`, `restart`, `stop`, `abort`, `subkernels`, `close_subkernels`, `reap` |
| `status`             | Kernel, installation and tracked-process health              |
| `notebooks`          | `open`, `create`, `list`, `info`, `save`, `close`, `dependencies`, `verify` |
| `cells`              | List or read cells of an open notebook                       |
| `evaluate_cells`     | Replay a span of cells, state carrying between them          |
| `replay`             | Per-cell replay with identity and a resumable manifest       |
| `edit_cells`         | Insert, replace or delete a cell                             |
| `render`             | Typeset an expression, rasterise a cell, export a notebook   |
| `vars`               | Inspect, set or clear the kernel's `Global`` symbols         |
| `supervisor`         | A kernel in its own process, outliving this one              |
| `batch`              | Run several tools in one round trip                          |
| `verify_derivation`  | Check a chain of expressions step by step                    |
| `read_notebook_file` | Read a `.nb` without opening a session                       |
| `guide`              | Usage notes by topic                                         |

`render` drives the Wolfram front end with `-platform offscreen`: no display and no X server are required. Rendering never owns scientific evaluation; that stays on the kernel link where interrupt and liveness semantics are defined.

## For Wolfram Language documentation, use Wolfram's MCP

This server answers questions about **your kernel, your state, and your documents**. It is not a replacement for Wolfram's reference documentation.

Run Wolfram's own MCP server alongside it and use `WolframLanguageContext` for questions about built-ins, options, and language behavior. It uses a different kernel and cannot see definitions created here.

**What the language means → Wolfram's MCP. What your scientific session contains → this server.**

## Documentation

| Document                                                     | Start here when...                                           |
| ------------------------------------------------------------ | ------------------------------------------------------------ |
| [`docs/agent-guide.md`](docs/agent-guide.md)                 | You want to drive the server correctly and efficiently       |
| [`docs/replaying-a-notebook.md`](docs/replaying-a-notebook.md) | You are replaying a notebook or onboarding a fresh agent     |
| [`docs/long-running-work.md`](docs/long-running-work.md)     | A cell may run for hours/days or must survive a dropped client |
| [`docs/architecture.md`](docs/architecture.md)               | You want the execution, replay and supervisor model          |
| [`docs/pitfalls.md`](docs/pitfalls.md)                       | You want the observed ways a plausible result can still be wrong |
| [`docs/benchmarks.md`](docs/benchmarks.md)                   | You want measured costs and trade-offs                       |

`guide(topic=...)` carries the short form inside the server:
`workflow · abort · errors · notebooks · state · parallel · performance`.

## Requirements

- Mathematica 14 or newer, with the WSTP library that ships with it
- Python 3.10+
- Linux or macOS. Windows is untested.
- One third-party Python dependency: `mcp`

**Not for untrusted input or multi-tenant hosts.** An evaluation is arbitrary code execution.

## Tests

```bash
python3 tests/test_kernel.py
.venv/bin/python tests/test_server_mcp.py
.venv/bin/python tests/test_supervisor.py
```

Point `MATHEMATICA_WSTP_TEST_NOTEBOOK` at any `.nb` to exercise notebook tools against a real document; those checks are skipped when it is unset.
