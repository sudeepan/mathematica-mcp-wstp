# Mathematica MCP over WSTP

**A Mathematica MCP server that can interrupt a running computation instead of
abandoning it.**

Your agent writes Wolfram Language; this server runs it in a persistent kernel.
When something goes wrong you can stop it, keep every definition, and carry on.
Notebooks on disk replay cell by cell, and a headless front end gives you
typeset images with no display attached.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Mathematica 14+](https://img.shields.io/badge/Mathematica-14+-red.svg)](https://www.wolfram.com/mathematica/)
[![Transport: WSTP](https://img.shields.io/badge/transport-WSTP-2b7489.svg)](https://reference.wolfram.com/language/guide/WSTPAPI.html)

---

## Why WSTP

An agent-driven session fails in ways an interactive one does not. A
simplification that will never finish looks exactly like one that needs another
minute. A crashed kernel looks exactly like a busy one. A parallel job torn
down carelessly leaves subkernels behind, hundreds of megabytes each, until the
machine runs out of memory.

WSTP fixes all three at the transport layer, because it carries an out-of-band
message channel alongside the evaluation:

- **Abort without losing the session.** `abort()` interrupts the evaluation and
  returns `$Aborted`. The kernel keeps its process and everything in it.
- **A dead kernel is an error, not a hang.** A lost link is reported in about
  0.3 s instead of a call that never returns.
- **A timeout keeps your work.** The evaluation is aborted, not the kernel, so
  earlier variables are still defined and you can retry a smaller piece.
- **Nothing is left running.** Shutdown closes subkernels over the link first,
  then signals the process group. A server killed outright is cleaned up on the
  next start.

## Quick start

You need Mathematica 14 or newer (15 recommended) and
[uv](https://docs.astral.sh/uv/). There is no compiler step and no Wolfram SDK
to build - the transport binds to the WSTP library your installation already
ships.

```bash
git clone https://github.com/sudeepan/mathematica-mcp-wstp.git
cd mathematica-mcp-wstp
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
claude mcp add --scope user mathematica-wstp -- "$PWD/.venv/bin/mathematica-wstp"
```

Restart your client and ask for an integral.

> `--scope user` matters. `claude mcp add` defaults to `--scope local`, which
> registers the server for one directory only; every other session then reports
> it as unavailable, which looks like a connection failure but is not one.
> Check from somewhere else - `cd /tmp && claude mcp list` - because checking
> from the project directory hides the mistake.

The installation, kernel and WSTP library are found automatically, including
relocated installs reachable only through a symlink on `PATH`. Override with
`MATHEMATICA_WSTP_KERNEL`, `MATHEMATICA_WSTP_INSTALL` or `MATHEMATICA_WSTP_LIB`.

## What it looks like in use

You ask in plain language; the agent picks the tool.

**"Integrate that, and stop if it takes more than ten seconds."**

```text
evaluate("Integrate[Sqrt[1 + x^4], x]", timeout=10)
=> timed_out: true
   kernel_state: "intact, the evaluation was aborted rather than the kernel"
   next_step: "Retry with a smaller input. Earlier variables are still defined."
```

**"That has gone off the rails. Stop it."**

```text
abort()
=> confirmed: true, "Evaluation interrupted; kernel state is intact."
```

**"Replay this notebook and tell me what broke."**

```text
notebooks(action="open", path="analysis.nb")   => 994 cells, 276 code cells
replay(action="run")
=> executed: 276, failed: 0, execution_timeout_seconds: 300
   every cell with its own request id and a manifest on disk
```

**"Show me what cell 39 actually looks like."**

```text
render(action="cell", index=39)
=> [typeset PNG from a headless front end, no display required]
```

Replaying a notebook nobody has run before? There is a ready-made prompt for a
fresh session in
[`docs/replaying-a-notebook.md`](docs/replaying-a-notebook.md) - it makes the
agent scan for cells that overwrite files *before* it evaluates anything.

## Tools

Sixteen consolidated tools rather than a wide flat surface, because a client
pays for every tool description in its context on every call.

| Tool | Purpose |
|------|---------|
| `evaluate` | Run Wolfram Language in the persistent kernel |
| `abort` | Interrupt the running evaluation, keeping all state |
| `kernel` | `state`, `restart`, `abort`, `subkernels`, `reap` |
| `status` | Kernel, installation and tracked-process health |
| `notebooks` | `open`, `create`, `list`, `info`, `save`, `close` |
| `cells` | List or read cells of an open notebook |
| `evaluate_cells` | Replay a span of cells, state carrying between them |
| `replay` | Per-cell replay with identity and a resumable manifest |
| `edit_cells` | Insert or delete a cell |
| `render` | Typeset an expression, rasterise a cell, export a notebook |
| `vars` | Inspect, set or clear the kernel's `Global`` symbols |
| `supervisor` | A kernel in its own process, outliving this one |
| `batch` | Run several tools in one round trip |
| `verify_derivation` | Check a chain of expressions step by step |
| `read_notebook_file` | Read a `.nb` without opening a session |
| `guide` | Usage notes by topic |

`render` drives the Wolfram front end with `-platform offscreen`: no display,
no X server, about 1.8 s to start on demand. It renders and never evaluates -
evaluation belongs on the kernel link, where abort and liveness both hold.

## Documentation

| | |
|---|---|
| [`docs/agent-guide.md`](docs/agent-guide.md) | How to drive this server well: session shape, long runs, interrupting, parallel work |
| [`docs/replaying-a-notebook.md`](docs/replaying-a-notebook.md) | Notebooks, per-cell replay, and a prompt for starting a fresh agent safely |
| [`docs/long-running-work.md`](docs/long-running-work.md) | Cells that run for hours or days, and surviving a dropped client |
| [`docs/architecture.md`](docs/architecture.md) | The two channels, a real trace, where the supervisor fits |
| [`docs/pitfalls.md`](docs/pitfalls.md) | Seventeen ways to get a wrong answer with no error, each observed on real work |
| [`docs/benchmarks.md`](docs/benchmarks.md) | What things cost, measured |

`guide(topic=...)` in the server carries the short form:
`workflow · abort · errors · notebooks · state · parallel · performance`.

### For the language itself, use Wolfram's own MCP server

This server drives **your** kernel, session and documents. It carries no
reference material and is not the place to ask what a built-in does. Run
Wolfram's own MCP server alongside it and use `WolframLanguageContext` for
that - it searches the real reference pages and comes back with options tables
and worked examples.

Note that it runs its own kernel, so it cannot see anything defined in this
one. A symbol you just assigned here comes back as "does not exist" there.

**What the language does → Wolfram's MCP. What your session contains → this
server.**

## Requirements

- Mathematica 14 or newer, with the WSTP library that ships with it
- Python 3.10+
- Linux or macOS. Windows is untested.
- One third-party Python dependency: `mcp`

**Not for** untrusted input or multi-tenant hosts. An evaluation is arbitrary
code execution.

## Tests

```bash
python3 tests/test_kernel.py                # transport and supervision
.venv/bin/python tests/test_server_mcp.py   # end to end over MCP stdio
.venv/bin/python tests/test_supervisor.py   # the out-of-process kernel
```

Point `MATHEMATICA_WSTP_TEST_NOTEBOOK` at any `.nb` to exercise the notebook
tools against a real document; those checks are skipped when it is unset.
