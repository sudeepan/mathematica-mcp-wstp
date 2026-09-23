"""The MCP tool surface.

Tools are deliberately consolidated -- a handful of tools with an ``action``
argument rather than forty flat ones -- because the client pays for every tool
description in its context on every call.

Tools are defined as plain ``def`` (not ``async def``) so the framework runs
them in worker threads. That is what makes ``abort`` reachable: it arrives on a
different thread while ``evaluate`` is still blocked on the link, and the WSTP
message channel is designed to be written from exactly that position.

Every tool returns a structured result: the payload as a real object, plus a
line of text a reader can scan. Errors take that same shape -- they are
values, not exceptions, because a tool that raises tells the model nothing it
can act on, and they are not encoded differently from successes, because a
client should branch on the outcome and never on how it was serialised.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent

from . import discovery, registry
from .evaluator import evaluate_json, evaluate_text
from . import render as render_mod
from . import session
from .notebooks import get_headless_notebooks

logger = logging.getLogger("mathematica_wstp.server")

MAX_OUTPUT_CHARS = int(os.environ.get("MATHEMATICA_WSTP_MAX_CHARS", "20000"))
# Above this, vars(action="get") reports a symbol's shape instead of its value.
# A replay result of a few hundred KB is already unreadable; megabytes are common.
_VALUE_PRINT_LIMIT = int(os.environ.get("MATHEMATICA_WSTP_MAX_VALUE_BYTES", "200000"))

server = MCPServer(
    name="mathematica-wstp",
    instructions=(
        "A live Wolfram kernel over WSTP. Unlike the older socket/ZMQ servers, a "
        "running evaluation can be interrupted with abort(), a dead kernel is "
        "reported rather than hanging, and a timeout aborts the evaluation "
        "rather than discarding the session, so state normally survives both. "
        "Normally, not always: abort() probes the kernel afterwards and reports "
        "what it found, so read its 'kernel' field instead of assuming.\n\n"
        "Routing:\n"
        "- compute/solve/simplify -> evaluate(code)\n"
        "- a runaway or too-slow evaluation -> abort()\n"
        "- work with a .nb file on disk -> notebooks(action='open') then cells()/evaluate_cells()\n"
        "- kernel is wedged or you need a clean slate -> kernel(action='restart')\n\n"
        "A notebook here is a .nb FILE ON DISK, evaluated cell by cell through the "
        "kernel in document order, from the cells' original stored boxes.\n\n"
        "Prefer one compound Wolfram expression over several round trips. Load a "
        "package in its own call before using its symbols: symbols resolve when the "
        "expression is parsed, so naming a package symbol in the same evaluation "
        "that loads the package binds it to Global` and silently returns the wrong "
        "thing."
    ),
)


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]", True


def _encode(payload: dict[str, Any]) -> str:
    """The JSON a client will receive. For measuring size, not for returning."""
    return json.dumps(payload, ensure_ascii=False, default=str)


def _one_line(text: Any, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "..."


def _summary(payload: dict[str, Any]) -> str:
    """A readable line, derived from the payload and nothing else.

    It can only name fields the payload actually has, so the summary and the
    structured result beside it cannot come to disagree. The alternative --
    composing a sentence about what the tool did -- is the failure this project
    keeps finding, where a component's account of its work outlives the work.
    """
    if payload.get("success") is False:
        return f"failed: {_one_line(payload.get('error') or 'no message reported', 200)}"
    bits: list[str] = []
    for key, value in payload.items():
        if key == "success" or value is None or len(bits) >= 8:
            continue
        if isinstance(value, bool):
            if value:
                bits.append(key)
        elif isinstance(value, (int, float)):
            bits.append(f"{key}={value}")
        elif isinstance(value, str):
            if value.strip():
                bits.append(f"{key}={_one_line(value, 160)}")
        elif isinstance(value, dict) and value and all(
                isinstance(v, int) and not isinstance(v, bool) for v in value.values()):
            bits.append(f"{key} " + ", ".join(f"{k} {v}" for k, v in value.items()))
        elif isinstance(value, (list, dict)):
            bits.append(f"{key}: {len(value)}")
    return "; ".join(bits) if bits else "ok"


def _reply(payload: dict[str, Any], summary: str | None = None) -> CallToolResult:
    """One response shape for every outcome.

    The payload goes back as structured content -- a real object, not JSON
    nested inside a JSON string -- and the text block carries a line a reader
    can scan. is_error is deliberately left alone: a failure reported here is a
    value the model can act on rather than a protocol-level error, which is
    what it has always been.
    """
    return CallToolResult(
        content=[TextContent(type="text", text=summary or _summary(payload))],
        structured_content=payload,
    )


def _fail(error: str, **extra: Any) -> CallToolResult:
    return _reply({"success": False, "error": error, **extra})


# --- evaluation ------------------------------------------------------------

@server.tool(
    description=(
        "Evaluate Wolfram Language code in the persistent kernel. State carries "
        "between calls. On timeout the evaluation is ABORTED but the kernel and "
        "all its definitions survive, so you can retry a smaller piece.\n"
        "When recording is active, every call is written into the recording "
        "notebook. Pass style to control the cell style: 'Input' (default), "
        "'Chapter', 'Section', 'Subsection', 'Item', etc. A non-Input style "
        "is useful for structuring the recorded notebook with section headers "
        "that still go through the kernel."
    )
)
def evaluate(code: str, timeout: float = 60.0,
             style: str = "Input") -> dict[str, Any]:
    nb = get_headless_notebooks()
    rec = nb.record_input(code, style=style)
    result = evaluate_text(code, timeout=timeout)
    notice = session.take_kernel_change_notice()
    if not result.success:
        payload: dict[str, Any] = {
            "success": False,
            "error": result.error,
            "timed_out": result.timed_out,
            "aborted": result.aborted,
        }
        if notice:
            payload["kernel_replaced"] = True
            payload["kernel_notice"] = notice
        if result.timed_out:
            # Ask the kernel rather than inferring from aborted_cleanly, which
            # only ever meant "the evaluation released the lock".
            verdict, detail = session.verify_current_kernel()
            payload["kernel"] = verdict
            payload["kernel_state"] = {
                "alive": "intact -- the kernel answered a probe after the abort",
                "dead": f"LOST -- the kernel did not survive ({detail}); every definition is gone",
                "unverified": f"unverified -- no answer to a probe ({detail}); do not assume it survived",
                "none": "no kernel is running",
            }[verdict]
            payload["next_step"] = (
                "Retry with a smaller input, a larger timeout, or wrap the slow part "
                "in TimeConstrained. Variables from earlier calls are still defined."
            )
        if result.prints:
            payload["printed"] = result.prints
        if result.messages:
            payload["messages"] = result.messages
        if rec is not None:
            payload["recorded"] = rec.get("success", False)
        payload.update(result.extra)
        return _reply(payload)

    text, truncated = _truncate(result.text)
    payload: dict[str, Any] = {"success": True, "output": text}
    if result.abort_requested_during:
        # An abort was asked for and a value came back regardless. Whether an
        # out-of-band abort unwinds the whole expression or only the innermost
        # one is version-dependent -- measured, 15.0.1 unwinds and 14.0.0 leaves
        # the enclosing CompoundExpression to continue -- so this result may be
        # the tail of a computation whose earlier part was cut off. Saying so is
        # the whole point: a partial execution reported as a clean success is
        # indistinguishable from a real answer.
        payload["abort_requested_during"] = True
        payload["result_may_be_partial"] = True
        payload["note"] = (
            "You aborted while this was running and it returned a value anyway. "
            "On some kernel versions an abort interrupts only the innermost "
            "expression, so statements after the interrupted one still run: this "
            "output may be the tail of a partly executed computation. Re-run it "
            "if the value matters, and check any state it assigned."
        )
    if notice:
        # The case that matters: a SUCCESSFUL call against a kernel that was
        # silently swapped underneath it. Without this the reply is
        # indistinguishable from one against the session you thought you had.
        payload["kernel_replaced"] = True
        payload["kernel_notice"] = notice
    if truncated:
        payload["truncated"] = True
        payload["note"] = "Full value is still in the kernel; ask for a part of it."
    # Messages and Print output are the difference between a wrong answer you
    # can explain and one you cannot. Always surface them.
    if result.prints:
        payload["printed"] = result.prints
    if result.messages:
        payload["messages"] = result.messages
        payload["message_names"] = sorted({m["name"] for m in result.messages if m.get("name")})
    if rec is not None:
        payload["recorded"] = rec.get("success", False)
    flags = [k for k in ("truncated", "kernel_replaced", "result_may_be_partial")
             if payload.get(k)]
    if result.messages:
        flags.append(f"{len(result.messages)} message(s)")
    return _reply(payload, text + (f"\n[{', '.join(flags)}]" if flags else ""))


@server.tool(
    description=(
        "Interrupt the evaluation the kernel is running right now, and verify "
        "afterwards what survived. Normally the kernel lives and every definition "
        "with it; read the 'kernel' field rather than assuming -- alive means a "
        "probe round-tripped, dead means the session is gone, unverified means "
        "it could not be checked. Use this instead of kernel(action='restart') "
        "for a runaway computation -- restart destroys every definition. If "
        "subkernels are live the reply reports parallel_state='unverified': an "
        "interrupted parallel evaluation may leave them holding a partial set of "
        "definitions, which no probe can detect. Pass rebuild_parallel_kernels=True "
        "to close and relaunch them as part of the abort."
    )
)
def abort(rebuild_parallel_kernels: bool = False) -> dict[str, Any]:
    return _reply(session.abort_current(rebuild_parallel_kernels=rebuild_parallel_kernels))


# --- kernel administration -------------------------------------------------

@server.tool(
    description=(
        "Kernel administration. actions: state | restart | stop | abort | subkernels | "
        "close_subkernels | reap. "
        "'restart' clears ALL definitions and closes subkernels properly; prefer "
        "abort() for a merely slow evaluation. "
        "'stop' shuts the kernel down without starting a replacement -- the next "
        "evaluate() call starts a fresh one on demand. Use it to release memory "
        "when you are done computing. "
        "'close_subkernels' releases the parallel pool WITHOUT touching the master "
        "kernel or any definition in it -- an idle 20-way pool costs gigabytes, so "
        "offer it to the user once a parallel computation is finished."
    )
)
def kernel(
    action: Literal["state", "restart", "stop", "abort", "subkernels",
                    "close_subkernels", "reap"] = "state",
) -> dict[str, Any]:
    if action == "state":
        return _reply({"success": True, **session.kernel_status()})
    if action == "stop":
        return _reply(session.close_kernel())
    if action == "restart":
        return _reply(session.restart_kernel())
    if action == "abort":
        return _reply(session.abort_current())
    if action == "subkernels":
        if not session.has_kernel():
            return _reply({"success": True, "subkernels": [], "note": "no kernel running"})
        pids = session.get_kernel().subkernel_pids()
        return _reply({"success": True, "subkernels": pids, "count": len(pids)})
    if action == "close_subkernels":
        return _reply(session.close_parallel_kernels())
    if action == "reap":
        reaped = registry.reap_orphans()
        return _reply({
            "success": True,
            "reaped": [e.get("pid") for e in reaped],
            "remaining": registry.orphan_report(),
        })
    return _fail(f"unknown action: {action}")


@server.tool(description="Server, kernel and installation status, plus any orphaned kernels.")
def status() -> dict[str, Any]:
    k = session.kernel_status()
    payload = {
        "success": True,
        "transport": "WSTP",
        "kernel": k,
        "installation": discovery.summary(),
        "kernels_tracked": registry.registered_kernels(),
        "orphans": registry.orphan_report(),
        "notebooks": (get_headless_notebooks().list() or {}).get("notebooks", []),
        "recording_to": get_headless_notebooks().recording,
    }
    where = (f"K{k.get('generation')}, pid {k.get('pid')}, {k.get('link_health')}"
             if k.get("alive") else "no kernel running")
    rec = f", recording to {payload['recording_to']}" if payload["recording_to"] else ""
    return _reply(payload, "WSTP {} -- {} subkernel(s), {} notebook(s) open, {} orphan(s){}".format(
        where, len(k.get("subkernels") or []),
        len(payload["notebooks"]), len(payload["orphans"]), rec))


# --- notebooks -------------------------------------------------------------

@server.tool(
    description=(
        "Notebook sessions over .nb files on disk. actions: open(path) | create(title,path) "
        "| list | info | save(path) | close | dependencies | record | stop_recording "
        "| finalize.\n"
        "record: start recording every evaluate() call as an Input cell into this "
        "notebook. Pass record=True with create or open to start recording immediately. "
        "stop_recording: stop recording. finalize: save the notebook and run "
        "NotebookEvaluate on it so every cell gets native In[n]/Out[n] labels.\n"
        "'dependencies' reports every file the notebook reads or writes, COMMENTED "
        "CELLS INCLUDED, and classifies each one: EXTERNAL_INPUT (must exist first), "
        "ROUND_TRIP (written then read back - never skip the write), "
        "LOADS_A_STORED_RESULT (read live while the cell that would compute it is "
        "commented out), WRITES_ONLY (this run overwrites it). Run it before "
        "evaluating an unfamiliar notebook: a cell that loads a stored answer looks "
        "exactly like one that computes it, and a Get that silently returns $Failed "
        "surfaces several cells later as a physics failure."
    )
)
def notebooks(
    action: Literal["open", "create", "list", "info", "save", "close", "verify",
                    "dependencies", "record", "stop_recording", "finalize"] = "list",
    path: str | None = None,
    title: str = "Untitled",
    notebook: str | None = None,
    record: bool = False,
) -> dict[str, Any]:
    nb = get_headless_notebooks()
    if action == "open":
        if not path:
            return _fail("open requires a path")
        result = nb.open(path)
        if record and result.get("success"):
            nb.start_recording(result.get("id"))
            result["recording"] = True
        return _reply(result)
    if action == "dependencies":
        # Run this BEFORE evaluating an unfamiliar notebook. A cell that loads
        # a stored result is indistinguishable at runtime from one that
        # computes it, and a Get that silently fails shows up several cells
        # later as physics that went wrong.
        return _reply(nb.file_dependencies(notebook=notebook))
    if action == "verify":
        # No reference is not an error: a document built from scratch has none,
        # and that is exactly when the record has to stand on its own. Without a
        # path this checks the document against itself.
        if not path:
            return _reply(nb.verify_self(notebook=notebook))
        return _reply(nb.verify_against(path, notebook=notebook))
    if action == "create":
        result = nb.create(title=title, path=path)
        if record and result.get("success"):
            nb.start_recording(result.get("id"))
            result["recording"] = True
        return _reply(result)
    if action == "list":
        result = nb.list()
        rec = nb.recording
        if rec:
            result["recording_to"] = rec
        return _reply(result)
    if action == "info":
        return _reply(nb.info(notebook))
    if action == "save":
        return _reply(nb.save(notebook, path))
    if action == "close":
        if nb.recording == nb._resolve(notebook):
            nb.stop_recording()
        return _reply(nb.close(notebook))
    if action == "record":
        return _reply(nb.start_recording(notebook))
    if action == "stop_recording":
        return _reply(nb.stop_recording())
    if action == "finalize":
        return _reply(nb.finalize(notebook=notebook, timeout=600))
    return _fail(f"unknown action: {action}")


@server.tool(
    description=(
        "List or read cells of an open notebook. Use style to filter (e.g. 'Input'). "
        "Cell indices are positions in the document and are what evaluate_cells takes. "
        "Pass defines='SymbolName' to find which cells assign a symbol (accepts an "
        "indexed form like 'Amp[2]') instead of paging through previews by hand."
    )
)
def cells(
    offset: int = 0,
    limit: int = 30,
    include_content: bool = True,
    style: str = "",
    notebook: str | None = None,
    defines: str | None = None,
) -> dict[str, Any]:
    if defines:
        # Where a symbol came from is a question about the document, not the
        # kernel: the kernel holds the value but nothing about the cell that
        # produced it. Answer it here rather than making the caller page
        # through previews by hand.
        return _reply(get_headless_notebooks().find_defining(defines, notebook=notebook))
    payload = get_headless_notebooks().cells(
        offset=offset, limit=limit, include_content=include_content,
        style=style, notebook=notebook,
    )
    size = len(_encode(payload))
    if size <= MAX_REPLY_CHARS:
        return _reply(payload)
    # evaluate_cells degrades to a summary when a range is too big; this used to
    # blow the caller's token limit with a raw error instead, so asking "what is
    # in this notebook?" the obvious way returned nothing at all. Drop content
    # first -- it is the bulk -- and only then narrow the window.
    if include_content:
        trimmed = get_headless_notebooks().cells(
            offset=offset, limit=limit, include_content=False,
            style=style, notebook=notebook,
        )
        trimmed["content_omitted"] = True
        trimmed["note"] = (f"Content dropped: the full reply was {size} chars. "
                           "Ask for a narrower range to see content.")
        size = len(_encode(trimmed))
        if size <= MAX_REPLY_CHARS:
            return _reply(trimmed)
        payload = trimmed
    kept = payload.get("cells") or []
    room = max(1, len(kept) * MAX_REPLY_CHARS // max(size, 1) - 1)
    payload["cells"] = kept[:room]
    payload["truncated"] = True
    payload["note"] = (f"Showing {room} of {len(kept)} cells; the rest did not fit. "
                       f"Page with offset={offset + room}.")
    return _reply(payload)


MAX_REPLY_CHARS = int(os.environ.get("MATHEMATICA_WSTP_MAX_REPLY", "12000"))


def _condense_range(payload: dict[str, Any]) -> dict[str, Any]:
    """Reduce a cell-range result to what a caller can still act on.

    A 370-cell range returns ~50k characters of per-cell detail, nearly all of
    it "skipped: not an Input/Code cell" -- and a reply that large is refused
    outright, so the caller gets nothing at all rather than something useful.

    What survives: the counts, every cell that aborted or failed (with its
    error), every Wolfram message, anything printed, and the slowest cells.
    What is dropped is the per-cell output of the cells that worked, which is
    recoverable by re-running a narrower range.

    Executed and skipped are counted separately on purpose. Most cells in a
    real notebook are prose or stored output; folding them into a success
    count turns "994 cells evaluated" into a number that means nothing.
    """
    results = payload.get("results") or []
    executed = skipped = aborted = failed = 0
    problems: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    printed: list[dict[str, Any]] = []
    slowest: list[tuple[int, int]] = []

    for cell in results:
        idx = cell.get("index")
        if cell.get("skipped"):
            skipped += 1
        elif cell.get("timed_out"):
            aborted += 1
            problems.append({"index": idx, "style": cell.get("style"),
                             "outcome": "aborted (hit the per-cell timeout)"})
        elif cell.get("aborted"):
            # An interrupted cell is not a failed one. Without this branch it fell
            # through below and was reported as "failed" with the error text
            # "None" -- str(None) -- which reads as a real failure whose message
            # went missing, and gives a caller no way to tell an abort they asked
            # for from a cell that genuinely broke.
            aborted += 1
            problems.append({"index": idx, "style": cell.get("style"),
                             "outcome": "aborted (interrupted, not a failure)"})
        elif cell.get("success") is False:
            failed += 1
            err = cell.get("error")
            problems.append({"index": idx, "style": cell.get("style"),
                             "outcome": "failed",
                             "error": str(err)[:300] if err
                                      else "no message was reported; see this cell's own output"})
        else:
            executed += 1
            slowest.append((cell.get("timing_ms") or 0, idx))
        for msg in (cell.get("messages") or []):
            messages.append({"index": idx, **{k: msg.get(k) for k in ("name", "text") if k in msg}})
        text = cell.get("printed")
        if text:
            printed.append({"index": idx, "text": text[:400]})

    slowest.sort(reverse=True)
    out: dict[str, Any] = {
        "success": payload.get("success", True),
        "id": payload.get("id"),
        "from": payload.get("from"),
        "to": payload.get("to"),
        "detail": "summary",
        "counts": {"seen": len(results), "executed": executed, "skipped": skipped,
                   "aborted": aborted, "failed": failed},
        "note": ("Per-cell output omitted because the full reply exceeded the size limit. "
                 "Re-run a narrower range for detail. Failures, messages and printed "
                 "output are listed in full below."),
    }
    if problems:
        out["problems"] = problems[:40]
    if messages:
        out["messages"] = messages[:40]
        out["message_names"] = sorted({m["name"] for m in messages if m.get("name")})
    if printed:
        out["printed"] = printed[:20]
    if slowest:
        out["slowest_ms"] = [{"index": i, "ms": ms} for ms, i in slowest[:8]]
    # Carry everything that is not per-cell detail. This summary is built from
    # scratch, so any field not copied here disappears -- and that is how
    # indices_shifted went missing exactly when it mattered most, summarising
    # being triggered by the large ranges most likely to insert cells. An
    # allowlist of field names only moves the problem: the next field added
    # upstream vanishes in the same silent way, which happened once already.
    # Copying by exclusion means a new field is carried by default.
    detail_only = {"results"}
    for key, value in payload.items():
        if key not in detail_only and key not in out:
            out[key] = value
    return out


@server.tool(
    description=(
        "Evaluate notebook cells in document order, in the persistent kernel, with "
        "state carrying between them. Give either index, or from_+to for a range. "
        "A long replay can be interrupted with abort(). Large ranges come back "
        "summarised (counts, failures, messages, slowest cells); set detail='full' "
        "to force per-cell output, or 'summary' to force the compact form. "
        "Indices in results are all PRE-shift: edits are applied after the range "
        "finishes, so every reported index refers to the document as it was when the "
        "call started. indices_shifted warns you about your NEXT call, not this "
        "reply. outputs_written:0 means nothing needed writing (cells ending in ';' "
        "have no result), not that a write failed -- a real failure appears as "
        "success:false on a cell.\n"
        "write_outputs=True also writes each result back into the open session as an "
        "Output cell, so notebooks(action='save') or render(action='export') then "
        "records what YOU computed instead of what was stored in the file. It replaces "
        "an existing Output cell where there is one; where it must insert, later cell "
        "indices shift and the reply says so."
    )
)
def evaluate_cells(
    index: int | None = None,
    from_: int | None = None,
    to: int | None = None,
    timeout: float = 300.0,
    stop_on_error: bool = True,
    detail: Literal["auto", "full", "summary"] = "auto",
    write_outputs: bool = False,
    notebook: str | None = None,
) -> dict[str, Any]:
    nb = get_headless_notebooks()
    if index is not None:
        if write_outputs:
            # index=N used to route to a separate single-cell path that never
            # received write_outputs: the cell evaluated, the reply said success,
            # and nothing was written back. A caller fixing one cell got a silent
            # no-op while believing the record had been corrected. Treat it as the
            # one-cell range it is, so both call forms behave identically.
            from_, to = index, index
        else:
            return _reply(nb.evaluate_cell(index, notebook=notebook, timeout=int(timeout)))
    if from_ is None or to is None:
        return _fail("give either index, or both from_ and to")

    payload = nb.evaluate_range(from_, to, notebook=notebook,
                                timeout=int(timeout), stop_on_error=stop_on_error,
                                write_outputs=write_outputs)
    if detail == "summary":
        return _reply(_condense_range(payload))
    if detail == "full" or len(_encode(payload)) <= MAX_REPLY_CHARS:
        return _reply(payload)
    # Measure the real thing rather than guessing from the cell count: a
    # hundred quiet cells are small, five noisy ones are not.
    return _reply(_condense_range(payload))


@server.tool(
    description=(
        "Change the cells of an open notebook. actions: write(content,style,position) "
        "| replace(index,content) | delete(index).\n"
        "'replace' changes one cell's content in place and keeps its style and every "
        "option, addressed by the same index 'delete' takes. Use it to edit an "
        "existing cell: 'write' can only APPEND in a notebook whose cells sit inside "
        "section groups, which is most real notebooks, because insertion rewrites "
        "the top-level cell list only and will not splice into a group."
    )
)
def edit_cells(
    action: Literal["write", "replace", "delete"],
    content: str = "",
    style: str = "Input",
    index: int | None = None,
    position: str = "end",
    anchor: int | None = None,
    notebook: str | None = None,
) -> dict[str, Any]:
    nb = get_headless_notebooks()
    if action == "write":
        return _reply(nb.write_cell(content, style=style, position=position,
                                    anchor=anchor, notebook=notebook))
    if action == "replace":
        if index is None:
            return _fail("replace requires an index")
        if not content:
            return _fail("replace requires content")
        return _reply(nb.replace_cell(index, content, notebook=notebook))
    if action == "delete":
        if index is None:
            return _fail("delete requires an index")
        return _reply(nb.delete_cell(index, notebook=notebook))
    return _fail(f"unknown action: {action}")


# --- front-end rendering ---------------------------------------------------

@server.tool(
    description=(
        "Render with the Wolfram front end, headlessly: typeset an expression, "
        "rasterise a notebook cell, or export a notebook. "
        "actions: expression(code) | cell(index) | export(path) | available. "
        "This RENDERS only -- it never evaluates through the front end; use "
        "evaluate() for that. Export renders what is visible, so collapsed cell "
        "groups are OPENED by default, because a notebook saved collapsed would "
        "otherwise export with most of its content missing; pass open_groups=False "
        "for the collapsed view. Paper is A4 portrait (595 x 842 pt) by default. "
        "Content wider than the page is CLIPPED, not scaled: the reply says so and "
        "sets action_required=ASK THE USER -- put the choice to them (fit_width=True "
        "widens the page so nothing is lost, recommended; or keep A4 and lose the "
        "overflow, not recommended) rather than deciding yourself. A .md path is "
        "written as all-text Markdown by this server rather than by Export, so no "
        "output is rasterised; tex_math=True renders outputs as $$...$$."
    )
)
def render(
    action: Literal["expression", "cell", "export", "available"] = "expression",
    code: str = "",
    index: int | None = None,
    path: str = "",
    dpi: int = 96,
    open_groups: bool = True,
    tex_math: bool = False,
    paper_width: int = 595,
    paper_height: int = 842,
    fit_width: bool = False,
    notebook: str | None = None,
) -> Any:
    if action == "available":
        return _reply(render_mod.available())

    if action == "export":
        if not path:
            return _fail("export requires a path (extension picks the format)")
        paper = (paper_width, paper_height) if paper_width and paper_height else (595, 842)
        return _reply(render_mod.export_notebook(
            path, notebook=notebook, open_groups=open_groups,
            tex_math=tex_math, paper=paper, fit_width=fit_width))

    if action == "expression":
        if not code:
            return _fail("expression requires code")
        out = render_mod.render_expression(code, dpi=dpi)
    elif action == "cell":
        if index is None:
            return _fail("cell requires an index")
        out = render_mod.render_cell(index, notebook=notebook, dpi=dpi)
    else:
        return _fail(f"unknown action: {action}")

    if isinstance(out, bytes):
        # Hand back a real image block so the model can actually look at it,
        # rather than a path it cannot open.
        return ImageContent(
            type="image",
            data=base64.b64encode(out).decode("ascii"),
            mime_type="image/png",
        )
    return _reply(out)


# --- kernel variables ------------------------------------------------------

@server.tool(
    description=(
        "Inspect or change the kernel's Global` symbols. actions: list | get(name) | "
        "set(name,value) | clear(name) | clear_all. Use this to see what a notebook "
        "replay actually defined, or to clear one symbol without restarting. "
        "get measures a symbol before printing it and returns size and shape instead "
        "of the value when it is large -- a replay result can be megabytes. "
        "Pass full=True only when you genuinely need the expression itself."
    )
)
def vars(
    action: Literal["list", "get", "set", "clear", "clear_all"] = "list",
    name: str | None = None,
    value: str | None = None,
    pattern: str | None = None,
    include_system: bool = False,
    full: bool = False,
) -> dict[str, Any]:
    if action == "list":
        ctx = '"Global`*"' if not include_system else '"System`*"'
        if pattern:
            ctx = json.dumps(("Global`" if not include_system else "System`") + pattern)
        # ByteCount per symbol, so a replay that defined something enormous is
        # visible without printing it.
        code = (
            f"Module[{{ns}}, ns = Names[{ctx}]; "
            "<|\"count\" -> Length[ns], \"symbols\" -> (Function[s, "
            "<|\"name\" -> s, \"defined\" -> (ToExpression[s, InputForm, ValueQ]), "
            "\"bytes\" -> ToExpression[s, InputForm, ByteCount]|>, HoldFirst] /@ Take[ns, UpTo[200]])|>]"
        )
        result = evaluate_json(code, timeout=60)
        return _reply(result)

    if action == "get":
        if not name:
            return _fail("get requires a name")
        # Measure before printing. Pulling a multi-megabyte result across the link
        # just to truncate it here wastes the transfer and floods the caller with
        # an expression it cannot read anyway -- a replay's accumulated result is
        # routinely millions of leaves. Size and shape answer the real question
        # ("did this get defined, and is it the right magnitude?") without that.
        probe = evaluate_json(
            "Module[{v = " + name + "}, <|"
            "\"bytes\" -> ByteCount[v], \"leaves\" -> LeafCount[v], "
            "\"head\" -> ToString[Head[v]], "
            "\"length\" -> If[AtomQ[v], 0, Length[v]]|>]",
            timeout=60)
        measured = probe if isinstance(probe, dict) else {}
        size = measured.get("bytes")
        if not full and isinstance(size, int) and size > _VALUE_PRINT_LIMIT:
            return _reply({
                "success": True, "name": name, "value_omitted": True,
                "bytes": size, "leaves": measured.get("leaves"),
                "head": measured.get("head"), "length": measured.get("length"),
                "note": (
                    f"{name} is {size} bytes, too large to print, so its shape is "
                    "reported instead. Ask for a measurement of what you actually "
                    f"need (Length[{name}], a Part of it, a Count); pass full=True "
                    "only if you really need the whole expression."),
            })
        out = evaluate_text(f"{name}", timeout=60)
        if not out.success:
            return _fail(out.error)
        text, truncated = _truncate(out.text)
        return _reply({"success": True, "name": name, "value": text,
                       "truncated": truncated, "bytes": size,
                       "leaves": measured.get("leaves")})

    if action == "set":
        if not name or value is None:
            return _fail("set requires both name and value")
        out = evaluate_text(f"{name} = ({value})", timeout=60)
        return _reply({"success": out.success, "name": name,
                       "value": _truncate(out.text)[0], "error": out.error or None})

    if action == "clear":
        if not name:
            return _fail("clear requires a name")
        out = evaluate_text(f'Quiet[Clear[{name}]]; ValueQ[{name}]', timeout=60)
        return _reply({"success": out.success, "cleared": name,
                       "still_defined": out.text.strip() == "True"})

    if action == "clear_all":
        # Global` only. Clearing System` would break the kernel, and "clear
        # everything" almost always means "clear what I defined".
        out = evaluate_text(
            'Module[{n = Length[Names["Global`*"]]}, '
            'Quiet[ClearAll["Global`*"]]; {n, Length[Names["Global`*"]]}]', timeout=120)
        return _reply({"success": out.success, "result": out.text,
                       "note": "Global` only; System` and loaded packages are untouched."})

    return _fail(f"unknown action: {action}")


# --- batching --------------------------------------------------------------

@server.tool(
    description=(
        "Run several of this server's tools in one round trip. "
        "ops: [{\"tool\": \"evaluate\", \"args\": {\"code\": \"1+1\"}}, ...]. "
        "Stops at the first failure unless stop_on_error is false. Useful for a "
        "fixed setup sequence; not a substitute for one compound Wolfram expression."
    )
)
def batch(ops: list[dict[str, Any]], stop_on_error: bool = True) -> dict[str, Any]:
    dispatch = _batchable()
    results: list[dict[str, Any]] = []
    for i, op in enumerate(ops):
        tool_name = op.get("tool") or op.get("command")
        fn = dispatch.get(tool_name)
        if fn is None:
            entry = {"op": i, "tool": tool_name, "success": False,
                     "error": f"unknown tool; available: {sorted(dispatch)}"}
        else:
            try:
                raw = fn(**(op.get("args") or op.get("params") or {}))
                # render can hand back an image block instead of a result.
                entry = {"op": i, "tool": tool_name,
                         "result": raw.structured_content
                                   if isinstance(raw, CallToolResult) else "<image>"}
            except Exception as exc:
                entry = {"op": i, "tool": tool_name, "success": False,
                         "error": f"{type(exc).__name__}: {exc}"}
        results.append(entry)
        failed = entry.get("success") is False or (
            isinstance(entry.get("result"), dict) and entry["result"].get("success") is False)
        if failed and stop_on_error:
            entry["stopped_here"] = True
            break
    return _reply({"success": True, "ran": len(results), "of": len(ops), "results": results})


def _condense_replay(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep what a caller can act on when a replay is too big to return.

    A 994-cell replay carries a per-cell result each, and the outputs of a real
    symbolic notebook are measured in megabytes -- a reply that size is refused
    outright, so the caller gets nothing rather than something useful.

    What survives is what identifies the run and what went wrong: run and
    manifest identity, the counts, every cell that failed or was interrupted,
    and the slowest cells. Per-cell detail for the cells that worked is dropped,
    and it is the one part that is recoverable -- the manifest names every
    child, and a narrower range re-runs any of them.
    """
    cells = payload.get("cells") or []
    problems: list[dict[str, Any]] = []
    slowest: list[tuple[int, int]] = []
    for cell in cells:
        if cell.get("timed_out") or cell.get("aborted") or not cell.get("success"):
            problems.append({k: cell.get(k) for k in
                             ("ordinal", "child", "error", "timed_out", "aborted", "reason")
                             if cell.get(k) is not None})
        else:
            slowest.append((cell.get("timing_ms") or 0, cell.get("ordinal")))
    slowest.sort(reverse=True)

    out = {k: v for k, v in payload.items() if k != "cells"}
    out["detail"] = "summary"
    out["note"] = ("Per-cell detail omitted because the full reply exceeded the size "
                   "limit. Every child is named in the manifest; re-run a narrower "
                   "range with first/last for detail. Failures are listed in full.")
    if problems:
        out["problems"] = problems[:40]
    if slowest:
        out["slowest_ms"] = [{"ordinal": o, "ms": ms} for ms, o in slowest[:8]]
    return out


@server.tool(
    description=(
        "Per-cell replay of a notebook, and reconciliation of one that was "
        "interrupted. actions: run | reconcile | list.\n"
        "'run' evaluates each input cell as its OWN execution, so every cell "
        "gets a request id, an evaluation token and an idempotency key, and the "
        "run is written to a manifest on disk BEFORE the first cell is "
        "submitted. evaluate_cells hands a whole span to the kernel instead: "
        "fewer round trips, one identity for the lot, and nothing recoverable "
        "if it is interrupted. Cells are addressed by input ORDINAL (1-based, "
        "counting only Input/Code cells), never by index, because writing an "
        "output inserts a cell and shifts every index after it.\n"
        "'reconcile' reads a manifest and reports, per child, whether it "
        "completed, is unresolved, never ran, or no longer matches the notebook "
        "source. It needs no kernel for the execution facts.\n"
        "'list' names the manifests that exist for this notebook.\n"
        "WHAT SURVIVES A CRASH: the manifest, which is a durable record of what "
        "was attempted and what completed. NOT the computation -- the kernel "
        "belongs to this server process and dies with it, so a cell in flight "
        "when the client goes is lost. Reconciliation tells you what happened, "
        "it does not hand back a running evaluation."
    )
)
def replay(
    action: Literal["run", "reconcile", "list"] = "run",
    first: int = 1,
    last: int = -1,
    timeout: float = 300.0,
    stop_on_error: bool = True,
    write_outputs: bool = False,
    run: str | None = None,
    manifest: str | None = None,
    detail: Literal["auto", "full", "summary"] = "auto",
    notebook: str | None = None,
) -> dict[str, Any]:
    from .replay_manifest import ReplayManifest

    nb = get_headless_notebooks()

    if action == "list":
        path = nb.session_path(notebook)
        if path is None:
            return _fail("no notebook session; open one first")
        found = []
        for name in ReplayManifest.for_notebook(path):
            try:
                found.append(ReplayManifest.load(name).summary())
            except (OSError, ValueError) as exc:
                found.append({"path": name, "error": f"unreadable: {exc}"})
        return _reply({"success": True, "notebook": path, "runs": found,
                       "count": len(found)})

    if action == "reconcile":
        target = manifest
        if not target:
            # The newest run for this notebook is what "reconcile" means when
            # nobody names one, and saying which was chosen matters: silently
            # reconciling a different run than the caller meant would be a
            # confident answer to the wrong question.
            path = nb.session_path(notebook)
            if path is None:
                return _fail("no notebook session; pass manifest=<path> or open one")
            known = ReplayManifest.for_notebook(path)
            if not known:
                return _fail(f"no replay manifest found for {path}")
            target = known[-1]
        # No lookup is passed: reconcile_replay takes it from the active
        # backend, so selecting a supervisor is the whole of what makes a
        # SUBMITTED child resolvable. Passing one here would let this layer
        # decide what counts as an execution record, which is not its call.
        result = nb.reconcile_replay(target, notebook=notebook)
        if isinstance(result, dict):
            result.setdefault("manifest", target)
        return _reply(result)

    if action != "run":
        return _fail(f"unknown action: {action}")

    payload = nb.replay_cells(notebook=notebook, first=first, last=last,
                              timeout=int(timeout), stop_on_error=stop_on_error,
                              write_outputs=write_outputs, run=run)
    if not payload.get("success") and "cells" not in payload:
        return _reply(payload)
    if detail == "summary":
        return _reply(_condense_replay(payload))
    if detail == "full" or len(_encode(payload)) <= MAX_REPLY_CHARS:
        return _reply(payload)
    return _reply(_condense_replay(payload))


@server.tool(
    description=(
        "A kernel owned by a separate process, so it outlives this one. "
        "actions: status | start | stop | use | use_direct | lookup.\n"
        "WHY: this server's own kernel dies with it -- measured, about a second "
        "after its owning client is killed. A cell that runs for hours or days "
        "is therefore only as safe as this process. A supervisor owns the "
        "kernel instead, keeps a ledger of every request, and can be asked "
        "afterwards what became of one.\n"
        "'start' launches one deliberately; nothing starts it implicitly, and "
        "'use' refuses rather than starting one. 'use' points NOTEBOOK "
        "execution at it -- evaluate() and vars() keep talking to this "
        "process's own kernel, so while a supervisor is selected those and "
        "notebook replay are two different kernels with different definitions. "
        "'lookup' asks what became of an idempotency key without submitting "
        "anything, which is how a client that lost its answer recovers. "
        "'running' names the evaluation in the supervisor's kernel and 'abort' "
        "interrupts it by its token -- USE THESE, not abort() or "
        "kernel(action='abort'), which reach THIS process's kernel and will "
        "cheerfully report success about an idle one while the supervised run "
        "continues.\n"
        "'stop' and the idle reclaim both refuse while work is in flight or a "
        "result has not been collected."
    )
)
def supervisor(
    action: Literal["status", "start", "stop", "use", "use_direct", "lookup",
                    "running", "abort"] = "status",
    socket_path: str | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    from .evaluator import get_evaluator
    from .supervisor import SupervisorUnavailable, lifecycle
    from .supervisor import use as use_supervisor
    from .supervisor import use_direct as use_own

    def described(info) -> dict[str, Any]:
        return {"success": True, "running": info.running, "socket": info.socket_path,
                "pid": info.pid, "session": info.session,
                "kernel_state": info.kernel_state, "stale_socket": info.stale_socket,
                "detail": info.detail, "backend_in_use": get_evaluator().name}

    if action == "status":
        return _reply(described(lifecycle.probe(socket_path)))

    if action == "start":
        info = lifecycle.start(
            lifecycle.SupervisorConfig.from_env() if socket_path is None
            else _config_for(socket_path))
        payload = described(info)
        if not info.running:
            payload["success"] = False
            payload["error"] = f"the supervisor did not come up: {info.detail}"
        return _reply(payload)

    if action == "stop":
        info = lifecycle.stop(socket_path)
        payload = described(info)
        if info.running:
            payload["success"] = False
            payload["error"] = f"not stopped: {info.detail}"
        return _reply(payload)

    if action == "use":
        try:
            evaluator = use_supervisor(socket_path)
        except SupervisorUnavailable as exc:
            return _fail(str(exc))
        stranded = getattr(evaluator, "stranded_notebooks", [])
        payload = {
            "success": True, "backend_in_use": evaluator.name,
            "socket": evaluator.socket_path,
            "note": ("Notebook execution now runs in the supervisor's kernel. "
                     "evaluate() and vars() still use this process's own kernel, "
                     "so the two hold different definitions."),
        }
        if stranded:
            # A notebook lives in the kernel that opened it. Saying so here is
            # the difference between a caller reopening it and a caller reading
            # "no such session" later and not knowing why.
            payload["stranded_notebooks"] = stranded
            payload["action_required"] = (
                f"{len(stranded)} notebook(s) were opened in the previous kernel and "
                "are not reachable from this one. Reopen them before using them.")
        return _reply(payload)

    if action == "use_direct":
        return _reply({"success": True, "backend_in_use": use_own().name,
                       "note": "Notebook execution is back in this process's kernel."})

    if action in ("running", "abort"):
        # abort() and kernel(action="abort") reach THIS process's kernel. When
        # a supervisor owns the kernel they are aimed at the wrong one and
        # report success about an idle kernel, which is how a run became
        # unstoppable: the handle that could abort it belonged to a replay loop
        # that had already gone.
        try:
            from .supervisor.backend import connect
            backend = connect(socket_path)
            current = backend.running()
        except SupervisorUnavailable as exc:
            return _fail(str(exc))
        if action == "running":
            if current is None:
                return _reply({"success": True, "running": False,
                               "note": "the supervisor's kernel is not evaluating anything"})
            return _reply({"success": True, "running": True, **current})
        if current is None:
            return _fail("nothing is running in the supervisor's kernel",
                         hint="an abort sent to an idle kernel leaves an interrupt "
                              "pending and wedges it, so this refuses instead")
        outcome = backend.abort_running()
        return _reply({
            "success": outcome.startswith("ABORT_ISSUED"),
            "request_id": current["request_id"], "token": current["token"],
            "outcome": outcome, "state_before": current["detail"],
            "note": ("Issued, not confirmed. Read supervisor(action='status') to see "
                     "whether it landed: an evaluation blocked on parallel subkernels "
                     "may not answer an abort at all, and the reply will say "
                     "abort=UNCONFIRMED rather than pretend otherwise."),
        })

    if action == "lookup":
        if not key:
            return _fail("lookup requires a key (the idempotency key you submitted under)")
        try:
            from .supervisor.backend import connect
            found = connect(socket_path).lookup(key)
        except SupervisorUnavailable as exc:
            return _fail(str(exc))
        if found is None:
            return _reply({"success": True, "key": key, "found": False,
                           "note": "never admitted; nothing ran under this key"})
        return _reply({"success": True, "key": key, "found": True, "record": found})

    return _fail(f"unknown action: {action}")


def _config_for(socket_path: str):
    from .supervisor import SupervisorConfig

    base = SupervisorConfig.from_env()
    base.sock = socket_path
    return base


# --- reading a notebook without opening a session --------------------------

@server.tool(
    description=(
        "Read a .nb file from disk without opening a kernel session for it. "
        "modes: outline (headings only) | markdown | wolfram (code cells only) | "
        "plain | json. Use notebooks(action='open') instead when you intend to "
        "evaluate anything."
    )
)
def read_notebook_file(
    path: str,
    mode: Literal["outline", "markdown", "wolfram", "plain", "json"] = "outline",
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    nb = get_headless_notebooks()
    # open() de-duplicates by path, so a file the caller already has open comes
    # back as THEIR session id, not a scratch one. Closing that in the finally
    # below silently destroys a session they are still using -- every later call
    # then fails with "No headless notebook matches ...". Only close what we made.
    borrowed = nb.is_open(path)
    opened = nb.open(path)
    if not opened.get("success"):
        return _reply(opened)
    scratch_id = opened.get("id")
    try:
        listing = nb.cells(offset=offset, limit=limit, include_content=True,
                           style="", notebook=scratch_id)
        if not listing.get("success"):
            return _reply(listing)
        cells_out = listing.get("cells") or []

        if mode == "json":
            payload: dict[str, Any] = {"success": True, "path": path, **listing}
        elif mode == "outline":
            heads = {"Title", "Chapter", "Section", "Subsection", "Subsubsection"}
            payload = {"success": True, "path": path,
                       "cell_count": opened.get("cell_count"),
                       "code_cells": opened.get("code_cells"),
                       "outline": [{"index": c.get("index"), "style": c.get("style"),
                                    "text": str(c.get("content", ""))[:120]}
                                   for c in cells_out if c.get("style") in heads]}
        elif mode == "wolfram":
            payload = {"success": True, "path": path,
                       "code": [{"index": c.get("index"), "source": c.get("content")}
                                for c in cells_out if c.get("style") in ("Input", "Code")]}
        else:  # markdown / plain
            lines = []
            for c in cells_out:
                style, text = c.get("style", ""), str(c.get("content", ""))
                if mode == "markdown" and style in ("Title", "Chapter", "Section",
                                                    "Subsection", "Subsubsection"):
                    depth = {"Title": 1, "Chapter": 2, "Section": 3,
                             "Subsection": 4, "Subsubsection": 5}[style]
                    lines.append(f"{'#' * depth} {text}")
                elif mode == "markdown" and style in ("Input", "Code"):
                    lines.append(f"```wolfram\n{text}\n```")
                else:
                    lines.append(text)
            body, truncated = _truncate("\n\n".join(lines))
            payload = {"success": True, "path": path, "mode": mode,
                       "text": body, "truncated": truncated}
        payload["note"] = (
            f"Read-only view; the session already open on this file ({scratch_id}) "
            "was left untouched." if borrowed
            else "Read-only view; no session was left open.")
        return _reply(payload)
    finally:
        if not borrowed:
            nb.close(scratch_id)


# --- derivation checking ---------------------------------------------------

@server.tool(
    description=(
        "Check a chain of expressions step by step: each step must equal the one "
        "before it. Returns the first step that does not follow. steps are Wolfram "
        "expressions as strings, in order."
    )
)
def verify_derivation(steps: list[str], timeout: float = 120.0,
                      assumptions: str = "") -> dict[str, Any]:
    if len(steps) < 2:
        return _fail("give at least two steps to compare")
    checks: list[dict[str, Any]] = []
    first_break: int | None = None

    for i in range(len(steps) - 1):
        a, b = steps[i], steps[i + 1]
        # FullSimplify of the difference, bounded: a step that cannot be decided
        # within the budget is reported as "unproven", never as "equal".
        assume = f", Assumptions -> ({assumptions})" if assumptions else ""
        code = (f"TimeConstrained[TrueQ[FullSimplify[({a}) - ({b}) == 0{assume}]], "
                f"{max(5.0, timeout / max(1, len(steps)))}, $TimedOut]")
        out = evaluate_text(code, timeout=timeout)
        verdict = out.text.strip() if out.success else "$Failed"
        entry = {"step": i + 1, "from": a[:120], "to": b[:120]}
        if verdict == "True":
            entry["equal"] = True
        elif verdict == "$TimedOut":
            entry["equal"] = None
            entry["note"] = "could not be decided within the budget -- not a disproof"
        else:
            entry["equal"] = False
            if first_break is None:
                first_break = i + 1
        if out.messages:
            entry["messages"] = [m.get("name") for m in out.messages]
        checks.append(entry)

    return _reply({
        "success": True,
        "steps": len(steps),
        "all_verified": first_break is None and all(c.get("equal") for c in checks),
        "first_failing_step": first_break,
        "checks": checks,
        "note": ("equal=None means undecided within the time budget, which is not "
                 "the same as unequal."),
    })


# --- guidance --------------------------------------------------------------

_GUIDE: dict[str, str] = {
    "workflow": (
        "Compute: evaluate(code). State persists between calls.\n"
        "Prefer one compound expression over several round trips.\n"
        "Load a package in its OWN call before using its symbols -- symbols resolve "
        "when the expression is parsed, so loading and using in one call binds the "
        "symbol to Global` and silently returns the wrong thing.\n"
        "Notebooks: notebooks(action='open', path=...) then cells() to look, "
        "evaluate_cells(from_=, to=) to run. Cells run from their stored boxes.\n"
        "Documentation for the LANGUAGE is not here. This server runs your kernel; "
        "it carries no reference material. When Wolfram's own MCP server is "
        "available, use its WolframLanguageContext for what a built-in does and "
        "what its options mean -- do not reason from memory about edge cases. Note "
        "it runs a SEPARATE kernel: it cannot see symbols defined in this session, "
        "and its SymbolDefinition reports on its kernel, not yours.\n"
        "Long-running external tools: use StartProcess, not Run[\"cmd &\"]. A "
        "shell-detached child SURVIVES the death of the kernel and of this server "
        "(measured), is not a Wolfram kernel so the orphan reaper never sees it, "
        "and after setsid cannot be reached by process-group signalling either."
    ),
    "abort": (
        "abort() interrupts the running evaluation and then PROBES the kernel, so "
        "read the 'kernel' field instead of assuming: alive means a round trip "
        "succeeded and your definitions are there, dead means the session is gone, "
        "unverified means it could not be checked. It is still the right response "
        "to a runaway computation -- it just does not promise survival.\n"
        "Aborting a large parallel evaluation has been seen to kill a kernel "
        "outright (once, unreproduced, Wolfram-side). Checkpoint expensive results "
        "to disk BEFORE interrupting anything long.\n"
        "kernel(action='restart') destroys all state -- use it only for a wedged "
        "kernel, not a slow one.\n"
        "A timeout on evaluate() aborts for you and then probes the same way; its "
        "reply carries the same 'kernel' field. State usually survives a timeout, "
        "but read the field rather than assuming it.\n"
        "Abort may not land while the kernel is inside an external process or a "
        "long library call; the reply says 'did not confirm' when that happens."
    ),
    "errors": (
        "evaluate() returns 'messages' (Part::partw and friends) and 'printed' "
        "alongside 'output'. A plausible-looking answer with a message attached is "
        "usually the message's fault -- read it.\n"
        "timed_out=true carries a 'kernel' field from a real probe: alive means "
        "your earlier definitions are still there, dead means they are not, "
        "unverified means nobody knows yet.\n"
        "success:true means no exception was raised and no timeout fired. It does "
        "NOT mean the cell did its work: a cell that shells out to an external tool "
        "still succeeds when that tool fails, and a cell that Get[]s a stored result "
        "looks identical to one that computed it. Check an artifact -- a length, a "
        "byte count, a file mtime -- not a status.\n"
        "A cell can also end itself with Abort[]: that returns aborted:true with a "
        "reason, is distinct from timed_out, and does not stop the rest of a range. "
        "Packages that refuse to load twice abort, so re-running a setup cell shows "
        "a benign abort -- read the reason before concluding anything.\n"
        "'kernel died' means the link dropped; the next call builds a fresh kernel "
        "and all state is gone."
    ),
    "notebooks": (
        "A notebook is a .nb FILE ON DISK. Cells are evaluated from their original "
        "stored boxes, so nothing is lost retyping.\n"
        "Only Input/Code cells run; Text, Output and Print cells come back "
        "'skipped'. Roughly a quarter of a real notebook's cells run code, so judge "
        "a replay by 'executed', not by 'seen'.\n"
        "Large ranges return a summary; pass detail='full' for per-cell output. "
        "cells() degrades the same way rather than failing: it drops content first, "
        "then narrows the window, and says which.\n"
        "The first tool call starts the kernel, so status() before that reports "
        "running:false, generation:0 -- 'not started', not 'broken'. The orphans "
        "list is a machine-wide census that includes other sessions' live kernels; "
        "check owner_alive before calling anything abandoned.\n"
        "read_notebook_file() reads a .nb without opening a session."
    ),
    "state": (
        "The kernel is persistent and shared: everything you define stays until the "
        "kernel dies or is restarted. That cuts both ways.\n"
        "A notebook that depends on a symbol only YOU defined will replay perfectly "
        "here and return silent zeros in a fresh kernel. Before calling a notebook "
        "reproducible, replay it in a kernel that has run nothing else.\n"
        "Checkpoint expensive results by exporting NAMED VALUES: "
        "Export[\"stage.wl\", value]. Do NOT DumpSave a whole context: restoring a "
        "Global` dump creates empty Global` symbols that shadow the same-named "
        "symbols of any loaded package, so calls into it return unevaluated and "
        "raise nothing -- 33 symbols affected in one observed case. After any "
        "restore check Context /@ {\"SomeSymbol\"} names the package, not Global`.\n"
        "Stored .wl files may be older than the code that reads them; agreement "
        "with one proves reproducibility, not correctness."
    ),
    "parallel": (
        "LaunchKernels[] subkernels are tracked, listed by status(), and closed with "
        "the kernel. They also self-terminate within a few seconds if the master "
        "dies, so they do not accumulate.\n"
        "Interrupting parallel work is the risky case: an abort landing while large "
        "expressions are in flight to subkernels can leave the session subtly wrong "
        "even when the kernel survives and answers a probe. A probe verifies the "
        "transport, never the algebra. abort() reports parallel_state='unverified' "
        "when subkernels are live; rebuild_parallel_kernels=True closes and "
        "relaunches them. Offered, not automatic -- and the risk is reasoned rather "
        "than measured, so treat it as a cheap precaution, not a known fault.\n"
        "An expensive operation applied to a whole collection at once is the usual "
        "bottleneck: cost is rarely spread evenly, so a few elements hold the rest "
        "hostage with no partial result. Map it per element under TimeConstrained. "
        "Measured: a canonicalisation received all 21 elements as one argument, of "
        "which 18 finished in seconds and 3 were the entire bottleneck."
    ),
    "performance": (
        "Round trip floor is ~0.3ms, so extra calls are cheap; huge results are "
        "not. Ask for Length/Short/Part rather than printing a large expression.\n"
        "LaunchKernels[] subkernels are tracked and closed with the kernel; "
        "status() lists them.\n"
        "Exporting a notebook: paper is A4 portrait by default and cell groups are "
        "opened, so a collapsed document still exports in full. If the reply comes "
        "back with action_required='ASK THE USER', content is wider than the page "
        "and has been silently cut off -- do NOT quietly re-export with fit_width, "
        "and do NOT leave it clipped. Put both options to the user: widen the page "
        "so nothing is lost (recommended, non-standard page size), or keep A4 and "
        "accept the missing content (not recommended). It is their document.\n"
        "render() drives a headless front end for typeset images. It RENDERS STORED "
        "CONTENT: rasterising a cell shows what is in the file, not what you just "
        "computed -- evaluating cells never writes results back into the document. "
        "For a fresh result use render(action='expression') on the live value.\n"
        "{Length, LeafCount} is a cheap fingerprint for spotting divergence between "
        "runs, with two traps: a value that has been through a serialise/deserialise "
        "round trip (Export/Import, Compress, a package's external form) counts "
        "differently from the same value computed in memory, so a replay that loads "
        "and one that recomputes disagree meaninglessly; and it is {0,1} for any "
        "head that hides its contents, such as Dispatch[] -- use ByteCount there."
    ),
}


@server.tool(
    description=("Usage notes for this server. topics: workflow | abort | errors | "
                 "notebooks | performance.")
)
def guide(topic: Literal["workflow", "abort", "errors", "notebooks", "state", "parallel",
                         "performance"] = "workflow") -> dict[str, Any]:
    return _reply({"success": True, "topic": topic,
                   "guidance": _GUIDE.get(topic, _GUIDE["workflow"]),
                   "topics": sorted(_GUIDE)})


# --- batch dispatch --------------------------------------------------------

# Every tool except batch itself, which would recurse. Built from the module
# rather than hand-listed inside batch(): the first version of that list was
# written by hand and silently omitted verify_derivation, so batch rejected a
# tool its own description advertised. test_server_mcp asserts this set matches
# the registered tools, so the two cannot drift apart again.
_BATCH_EXCLUDED = {"batch"}


def _batchable() -> dict[str, Any]:
    names = ("evaluate", "abort", "kernel", "status", "notebooks", "cells",
             "evaluate_cells", "edit_cells", "render", "vars", "guide",
             "verify_derivation", "read_notebook_file", "replay", "supervisor")
    return {n: globals()[n] for n in names if n not in _BATCH_EXCLUDED}


# --- entry point -----------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=os.environ.get("MATHEMATICA_WSTP_LOGLEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,   # stdout is the MCP transport; never log to it
    )
    hooked = session.install_signal_handlers()
    logger.info("shutdown handlers installed for: %s", ", ".join(hooked) or "none")
    reaped = session.reap_on_startup()
    if reaped:
        logger.info("reaped %d kernel(s) stranded by a previous run", len(reaped))
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
