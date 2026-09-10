"""The MCP tool surface.

Tools are deliberately consolidated -- a handful of tools with an ``action``
argument rather than forty flat ones -- because the client pays for every tool
description in its context on every call.

Tools are defined as plain ``def`` (not ``async def``) so the framework runs
them in worker threads. That is what makes ``abort`` reachable: it arrives on a
different thread while ``evaluate`` is still blocked on the link, and the WSTP
message channel is designed to be written from exactly that position.

Every tool returns a JSON string. Errors are values, not exceptions -- a tool
that raises tells the model nothing it can act on.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent

from . import discovery, registry
from . import render as render_mod
from . import session
from .notebooks import get_headless_notebooks

logger = logging.getLogger("mathematica_wstp.server")

MAX_OUTPUT_CHARS = int(os.environ.get("MATHEMATICA_WSTP_MAX_CHARS", "20000"))

server = MCPServer(
    name="mathematica-wstp",
    instructions=(
        "A live Wolfram kernel over WSTP. Unlike the older socket/ZMQ servers, a "
        "running evaluation can be interrupted with abort(), a dead kernel is "
        "reported rather than hanging, and a timeout aborts the evaluation "
        "WITHOUT losing kernel state.\n\n"
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


def _reply(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _fail(error: str, **extra: Any) -> str:
    return _reply({"success": False, "error": error, **extra})


# --- evaluation ------------------------------------------------------------

@server.tool(
    description=(
        "Evaluate Wolfram Language code in the persistent kernel. State carries "
        "between calls. On timeout the evaluation is ABORTED but the kernel and "
        "all its definitions survive, so you can retry a smaller piece."
    )
)
def evaluate(code: str, timeout: float = 60.0) -> str:
    result = session.evaluate_wl(code, timeout=timeout)
    if not result.success:
        payload: dict[str, Any] = {
            "success": False,
            "error": result.error,
            "timed_out": result.timed_out,
            "aborted": result.aborted,
        }
        if result.timed_out:
            payload["kernel_state"] = (
                "intact -- the evaluation was aborted, not the kernel"
                if result.extra.get("aborted_cleanly")
                else "uncertain -- the kernel did not confirm the abort"
            )
            payload["next_step"] = (
                "Retry with a smaller input, a larger timeout, or wrap the slow part "
                "in TimeConstrained. Variables from earlier calls are still defined."
            )
        if result.prints:
            payload["printed"] = result.prints
        if result.messages:
            payload["messages"] = result.messages
        payload.update(result.extra)
        return _reply(payload)

    text, truncated = _truncate(result.text)
    payload: dict[str, Any] = {"success": True, "output": text}
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
    return _reply(payload)


@server.tool(
    description=(
        "Interrupt the evaluation the kernel is running right now. The kernel "
        "survives with all state intact. Use this instead of kernel(action='restart') "
        "for a runaway computation -- restart destroys every definition."
    )
)
def abort() -> str:
    return _reply(session.abort_current())


# --- kernel administration -------------------------------------------------

@server.tool(
    description=(
        "Kernel administration. actions: state | restart | abort | subkernels | reap. "
        "'restart' clears ALL definitions and closes subkernels properly; prefer "
        "abort() for a merely slow evaluation."
    )
)
def kernel(
    action: Literal["state", "restart", "abort", "subkernels", "reap"] = "state",
) -> str:
    if action == "state":
        return _reply({"success": True, **session.kernel_status()})
    if action == "restart":
        return _reply(session.restart_kernel())
    if action == "abort":
        return _reply(session.abort_current())
    if action == "subkernels":
        if not session.has_kernel():
            return _reply({"success": True, "subkernels": [], "note": "no kernel running"})
        pids = session.get_kernel().subkernel_pids()
        return _reply({"success": True, "subkernels": pids, "count": len(pids)})
    if action == "reap":
        reaped = registry.reap_orphans()
        return _reply({
            "success": True,
            "reaped": [e.get("pid") for e in reaped],
            "remaining": registry.orphan_report(),
        })
    return _fail(f"unknown action: {action}")


@server.tool(description="Server, kernel and installation status, plus any orphaned kernels.")
def status() -> str:
    return _reply({
        "success": True,
        "transport": "WSTP",
        "kernel": session.kernel_status(),
        "installation": discovery.summary(),
        "kernels_tracked": registry.registered_kernels(),
        "orphans": registry.orphan_report(),
        "notebooks": (get_headless_notebooks().list() or {}).get("notebooks", []),
    })


# --- notebooks -------------------------------------------------------------

@server.tool(
    description=(
        "Notebook sessions over .nb files on disk. actions: open(path) | create(title,path) "
        "| list | info | save(path) | close. Cells are evaluated from their original "
        "stored boxes, so nothing is lost in translation."
    )
)
def notebooks(
    action: Literal["open", "create", "list", "info", "save", "close"] = "list",
    path: str | None = None,
    title: str = "Untitled",
    notebook: str | None = None,
) -> str:
    nb = get_headless_notebooks()
    if action == "open":
        if not path:
            return _fail("open requires a path")
        return _reply(nb.open(path))
    if action == "create":
        return _reply(nb.create(title=title, path=path))
    if action == "list":
        return _reply(nb.list())
    if action == "info":
        return _reply(nb.info(notebook))
    if action == "save":
        return _reply(nb.save(notebook, path))
    if action == "close":
        return _reply(nb.close(notebook))
    return _fail(f"unknown action: {action}")


@server.tool(
    description=(
        "List or read cells of an open notebook. Use style to filter (e.g. 'Input'). "
        "Cell indices are positions in the document and are what evaluate_cells takes."
    )
)
def cells(
    offset: int = 0,
    limit: int = 30,
    include_content: bool = True,
    style: str = "",
    notebook: str | None = None,
) -> str:
    return _reply(get_headless_notebooks().cells(
        offset=offset, limit=limit, include_content=include_content,
        style=style, notebook=notebook,
    ))


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
        elif cell.get("success") is False:
            failed += 1
            problems.append({"index": idx, "style": cell.get("style"),
                             "outcome": "failed", "error": str(cell.get("error"))[:300]})
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
    return out


@server.tool(
    description=(
        "Evaluate notebook cells in document order, in the persistent kernel, with "
        "state carrying between them. Give either index, or from_+to for a range. "
        "A long replay can be interrupted with abort(). Large ranges come back "
        "summarised (counts, failures, messages, slowest cells); set detail='full' "
        "to force per-cell output, or 'summary' to force the compact form."
    )
)
def evaluate_cells(
    index: int | None = None,
    from_: int | None = None,
    to: int | None = None,
    timeout: float = 300.0,
    stop_on_error: bool = True,
    detail: Literal["auto", "full", "summary"] = "auto",
    notebook: str | None = None,
) -> str:
    nb = get_headless_notebooks()
    if index is not None:
        return _reply(nb.evaluate_cell(index, notebook=notebook, timeout=int(timeout)))
    if from_ is None or to is None:
        return _fail("give either index, or both from_ and to")

    payload = nb.evaluate_range(from_, to, notebook=notebook,
                                timeout=int(timeout), stop_on_error=stop_on_error)
    if detail == "summary":
        return _reply(_condense_range(payload))
    full = _reply(payload)
    if detail == "full" or len(full) <= MAX_REPLY_CHARS:
        return full
    # Measure the real thing rather than guessing from the cell count: a
    # hundred quiet cells are small, five noisy ones are not.
    return _reply(_condense_range(payload))


@server.tool(description="Insert or delete a cell. actions: write(content,style,position) | delete(index).")
def edit_cells(
    action: Literal["write", "delete"],
    content: str = "",
    style: str = "Input",
    index: int | None = None,
    position: str = "end",
    anchor: int | None = None,
    notebook: str | None = None,
) -> str:
    nb = get_headless_notebooks()
    if action == "write":
        return _reply(nb.write_cell(content, style=style, position=position,
                                    anchor=anchor, notebook=notebook))
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
        "groups export collapsed; pass open_groups=True for the whole document."
    )
)
def render(
    action: Literal["expression", "cell", "export", "available"] = "expression",
    code: str = "",
    index: int | None = None,
    path: str = "",
    dpi: int = 96,
    open_groups: bool = False,
    notebook: str | None = None,
) -> Any:
    if action == "available":
        return _reply(render_mod.available())

    if action == "export":
        if not path:
            return _fail("export requires a path (extension picks the format)")
        return _reply(render_mod.export_notebook(
            path, notebook=notebook, open_groups=open_groups))

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
        "replay actually defined, or to clear one symbol without restarting."
    )
)
def vars(
    action: Literal["list", "get", "set", "clear", "clear_all"] = "list",
    name: str | None = None,
    value: str | None = None,
    pattern: str | None = None,
    include_system: bool = False,
) -> str:
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
        result = session.evaluate_wl_json(code, timeout=60)
        return _reply(result)

    if action == "get":
        if not name:
            return _fail("get requires a name")
        out = session.evaluate_wl(f"{name}", timeout=60)
        if not out.success:
            return _fail(out.error)
        text, truncated = _truncate(out.text)
        return _reply({"success": True, "name": name, "value": text, "truncated": truncated})

    if action == "set":
        if not name or value is None:
            return _fail("set requires both name and value")
        out = session.evaluate_wl(f"{name} = ({value})", timeout=60)
        return _reply({"success": out.success, "name": name,
                       "value": _truncate(out.text)[0], "error": out.error or None})

    if action == "clear":
        if not name:
            return _fail("clear requires a name")
        out = session.evaluate_wl(f'Quiet[Clear[{name}]]; ValueQ[{name}]', timeout=60)
        return _reply({"success": out.success, "cleared": name,
                       "still_defined": out.text.strip() == "True"})

    if action == "clear_all":
        # Global` only. Clearing System` would break the kernel, and "clear
        # everything" almost always means "clear what I defined".
        out = session.evaluate_wl(
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
def batch(ops: list[dict[str, Any]], stop_on_error: bool = True) -> str:
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
                # render can hand back an image block rather than JSON text.
                entry = {"op": i, "tool": tool_name,
                         "result": json.loads(raw) if isinstance(raw, str) else "<image>"}
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
) -> str:
    nb = get_headless_notebooks()
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
        payload["note"] = "Read-only view; no session was left open."
        return _reply(payload)
    finally:
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
                      assumptions: str = "") -> str:
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
        out = session.evaluate_wl(code, timeout=timeout)
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
        "evaluate_cells(from_=, to=) to run. Cells run from their stored boxes."
    ),
    "abort": (
        "abort() interrupts the running evaluation and KEEPS the kernel and every "
        "definition. It is the right response to a runaway computation.\n"
        "kernel(action='restart') destroys all state -- use it only for a wedged "
        "kernel, not a slow one.\n"
        "A timeout on evaluate() already aborts for you and keeps state; the reply "
        "says whether the kernel confirmed.\n"
        "Abort may not land while the kernel is inside an external process or a "
        "long library call; the reply says 'did not confirm' when that happens."
    ),
    "errors": (
        "evaluate() returns 'messages' (Part::partw and friends) and 'printed' "
        "alongside 'output'. A plausible-looking answer with a message attached is "
        "usually the message's fault -- read it.\n"
        "timed_out=true with kernel_state 'intact' means the evaluation was "
        "aborted, not the kernel: your earlier definitions are still there.\n"
        "'kernel died' means the link dropped; the next call builds a fresh kernel "
        "and all state is gone."
    ),
    "notebooks": (
        "A notebook is a .nb FILE ON DISK. Cells are evaluated from their original "
        "stored boxes, so nothing is lost retyping.\n"
        "Only Input/Code cells run; Text, Output and Print cells come back "
        "'skipped'. Roughly a quarter of a real notebook's cells run code, so judge "
        "a replay by 'executed', not by 'seen'.\n"
        "Large ranges return a summary; pass detail='full' for per-cell output.\n"
        "read_notebook_file() reads a .nb without opening a session."
    ),
    "performance": (
        "Round trip floor is ~0.3ms, so extra calls are cheap; huge results are "
        "not. Ask for Length/Short/Part rather than printing a large expression.\n"
        "LaunchKernels[] subkernels are tracked and closed with the kernel; "
        "status() lists them.\n"
        "render() drives a headless front end for typeset images -- rasterise a "
        "cell rather than dumping boxes when you want to SEE something."
    ),
}


@server.tool(
    description=("Usage notes for this server. topics: workflow | abort | errors | "
                 "notebooks | performance.")
)
def guide(topic: Literal["workflow", "abort", "errors", "notebooks",
                         "performance"] = "workflow") -> str:
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
             "verify_derivation", "read_notebook_file")
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
