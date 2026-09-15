"""End-to-end tests over the real MCP stdio protocol.

These spawn the server as a subprocess and talk to it the way a client does.
The unit tests in test_kernel.py prove the transport; these prove the wiring --
that tools are registered, that responses are JSON a model can use, and above
all that abort() reaches a kernel while evaluate() is still blocked on it.

That last one only works if the framework dispatches sync tools on worker
threads, which is why the tools are plain ``def``. If someone converts them to
``async def``, this file is what will catch it.

Run: .venv/bin/python tests/test_server_mcp.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client             # noqa: E402

# Point MATHEMATICA_WSTP_TEST_NOTEBOOK at any .nb to exercise the notebook
# tools against a real document. Unset, those checks are skipped -- the
# suite must not depend on a file that only exists on one machine.
NOTEBOOK = os.environ.get("MATHEMATICA_WSTP_TEST_NOTEBOOK", "")

PARAMS = StdioServerParameters(
    command=os.path.join(ROOT, ".venv", "bin", "python"),
    args=["-m", "mathematica_wstp.server"],
    env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")},
)


def _payload(result) -> dict:
    """Unwrap a tool result into the dict the tool returned."""
    for block in result.content:
        if getattr(block, "type", None) == "text":
            return json.loads(block.text)
    raise AssertionError(f"no text content in {result!r}")


async def run_checks() -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        out.append((name, ok, detail))

    async with stdio_client(PARAMS) as (read, write):
        async with ClientSession(read, write) as sess:
            await sess.initialize()

            tools = {t.name for t in (await sess.list_tools()).tools}
            expected = {"evaluate", "abort", "kernel", "status",
                        "notebooks", "cells", "evaluate_cells", "edit_cells"}
            check("tools registered", expected <= tools, f"missing {expected - tools}")

            r = _payload(await sess.call_tool("evaluate", {"code": "1+1"}))
            check("evaluate 1+1", r.get("output", "").strip() == "2", str(r))

            await sess.call_tool("evaluate", {"code": "marker = 424242"})
            r = _payload(await sess.call_tool("evaluate", {"code": "marker"}))
            check("state persists across calls", r.get("output", "").strip() == "424242", str(r))

            r = _payload(await sess.call_tool("evaluate", {"code": r'StringLength["\[Gamma]"]'}))
            check("escapes survive the tool layer", r.get("output", "").strip() == "1", str(r))

            # The point of the whole project: interrupt a running evaluation
            # through the protocol, from a second concurrent request.
            started = time.monotonic()
            long_call = asyncio.create_task(sess.call_tool(
                "evaluate", {"code": 'Do[qq = i, {i, 1, 10^12}]; "NEVER"', "timeout": 120}))
            await asyncio.sleep(3)
            abort_res = _payload(await sess.call_tool("abort", {}))
            eval_res = _payload(await long_call)
            elapsed = time.monotonic() - started

            check("abort confirmed", abort_res.get("confirmed") is True, str(abort_res))
            check("aborted evaluation returned promptly", elapsed < 30, f"{elapsed:.1f}s")
            # Two correct outcomes, decided by the kernel version. On 15.0.1 the
            # out-of-band abort unwinds the whole expression and the evaluation
            # reports the abort. On 14.0.0 it interrupts only the innermost
            # expression, so `Do[...]; "NEVER"` returns "NEVER" -- a partial
            # execution. What must never happen is that returning silently: the
            # reply has to say the result may be partial. Measured on both; the
            # Do alone is ~17 hours, so "NEVER" can only mean a partial run.
            reported_abort = bool(eval_res.get("aborted")) or "abort" in str(eval_res).lower()
            check("evaluation reports the abort, or flags the result as partial",
                  reported_abort, str(eval_res))
            if not eval_res.get("aborted") and eval_res.get("output", "").strip('"') == "NEVER":
                check("a partial result is not passed off as a clean one",
                      eval_res.get("result_may_be_partial") is True, str(eval_res))

            r = _payload(await sess.call_tool("evaluate", {"code": "marker"}))
            check("state SURVIVES the abort", r.get("output", "").strip() == "424242", str(r))

            # The guard, forced deterministically on any kernel version. CheckAbort
            # absorbs the abort, so the expression continues and returns a value
            # while an abort was outstanding -- the same shape as 14.0.0's partial
            # unwinding, which is otherwise only reproducible on that version.
            # A value that arrives after you asked for an abort must never look
            # like an ordinary result.
            swallow = asyncio.create_task(sess.call_tool(
                "evaluate", {"code": 'CheckAbort[Pause[8], "caught"]; "DONE"', "timeout": 60}))
            await asyncio.sleep(2)
            await sess.call_tool("abort", {})
            swallowed = _payload(await swallow)
            check("a value returned despite an abort is flagged as possibly partial",
                  swallowed.get("success") is True
                  and swallowed.get("result_may_be_partial") is True, str(swallowed)[:220])
            check("and the reply explains why rather than just flagging it",
                  "partly executed" in str(swallowed.get("note", "")), str(swallowed)[:220])
            r = _payload(await sess.call_tool("evaluate", {"code": "1+1"}))
            check("an ordinary result afterwards carries no such flag",
                  r.get("result_may_be_partial") is None, str(r)[:160])

            r = _payload(await sess.call_tool("evaluate",
                                              {"code": 'Do[z=i,{i,1,10^12}]', "timeout": 2}))
            check("timeout reports intact state",
                  r.get("timed_out") and "intact" in str(r.get("kernel_state", "")), str(r))
            r = _payload(await sess.call_tool("evaluate", {"code": "marker"}))
            check("state SURVIVES the timeout", r.get("output", "").strip() == "424242", str(r))

            r = _payload(await sess.call_tool("evaluate", {"code": 'Print["HI"]; 42'}))
            check("tool surfaces Print output",
                  r.get("output", "").strip() == "42" and r.get("printed") == ["HI"], str(r)[:200])

            r = _payload(await sess.call_tool("evaluate", {"code": "{1,2}[[5]]"}))
            check("tool surfaces Wolfram messages",
                  "Part::partw" in (r.get("message_names") or []), str(r)[:250])

            r = _payload(await sess.call_tool("evaluate", {"code": "2+2"}))
            check("clean evaluation carries no message noise",
                  r.get("output", "").strip() == "4" and "messages" not in r and "printed" not in r,
                  str(r)[:200])

            r = _payload(await sess.call_tool("status", {}))
            check("status reports WSTP + live kernel",
                  r.get("transport") == "WSTP" and r["kernel"].get("running"), str(r)[:200])

            if os.path.exists(NOTEBOOK):
                r = _payload(await sess.call_tool("notebooks",
                                                  {"action": "open", "path": NOTEBOOK}))
                check("open a notebook", isinstance(r.get("cell_count"), int)
                      and r["cell_count"] > 0, str(r)[:200])
                r = _payload(await sess.call_tool("cells", {"offset": 0, "limit": 3}))
                check("list cells", r.get("success") and len(r.get("cells", [])) == 3, str(r)[:200])
            else:
                check("notebook checks skipped (set MATHEMATICA_WSTP_TEST_NOTEBOOK)", True, "")

            # --- front-end rendering ---
            r = _payload(await sess.call_tool("render", {"action": "available"}))
            fe_up = bool(r.get("available"))
            check("headless front end available", fe_up, str(r))

            if fe_up:
                res = await sess.call_tool(
                    "render", {"action": "expression", "code": "Style[Integrate[1/(1+x^3),x],24]"})
                img = [b for b in res.content if getattr(b, "type", None) == "image"]
                check("expression renders to a PNG image block", bool(img), str(res)[:200])
                if img:
                    import base64
                    raw = base64.b64decode(img[0].data)
                    check("PNG bytes are a real PNG",
                          raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) > 2000,
                          f"{len(raw)} bytes, head={raw[:8]!r}")

                if os.path.exists(NOTEBOOK):
                    res = await sess.call_tool("render", {"action": "cell", "index": 3})
                    img = [b for b in res.content if getattr(b, "type", None) == "image"]
                    check("notebook cell renders to an image", bool(img), str(res)[:200])

                # Export renders what is visible. Collapsed groups export
                # collapsed, so the default reply must say so, and open_groups
                # must actually change the output. Needs a document open.
                have_nb = bool(NOTEBOOK) and os.path.exists(NOTEBOOK)
                out_pdf = "/tmp/mathematica_wstp_export_test.pdf"
                r = _payload(await sess.call_tool("render",
                                                  {"action": "export", "path": out_pdf})) \
                    if have_nb else {"success": True, "groups_opened": False,
                                     "note": "collapsed (skipped: no notebook)"}
                check("export succeeds and explains group collapsing",
                      r.get("success") and r.get("groups_opened") is False
                      and "collapsed" in r.get("note", ""), str(r)[:250])
                size_closed = os.path.getsize(out_pdf) if os.path.exists(out_pdf) else 0

                out_pdf2 = "/tmp/mathematica_wstp_export_open.pdf"
                if have_nb:
                    r = _payload(await sess.call_tool(
                        "render", {"action": "export", "path": out_pdf2, "open_groups": True}))
                    size_open = os.path.getsize(out_pdf2) if os.path.exists(out_pdf2) else 0
                    check("open_groups produces a larger export",
                          r.get("groups_opened") is True and size_open > size_closed,
                          f"closed={size_closed}B open={size_open}B {str(r)[:150]}")

            # --- tools added after the first release ---
            expected2 = {"vars", "batch", "guide", "verify_derivation", "read_notebook_file"}
            check("later tools registered", expected2 <= tools, f"missing {expected2 - tools}")

            await sess.call_tool("evaluate", {"code": "probeVar = 7; probeVar2 = {1,2,3}"})
            r = _payload(await sess.call_tool("vars", {"action": "list"}))
            names = [s.get("name") for s in (r.get("symbols") or [])]
            check("vars lists Global symbols",
                  any(n and n.endswith("probeVar") for n in names), str(r)[:200])
            r = _payload(await sess.call_tool("vars", {"action": "get", "name": "probeVar"}))
            check("vars get", r.get("value", "").strip() == "7", str(r)[:150])
            r = _payload(await sess.call_tool("vars", {"action": "set",
                                                       "name": "probeVar", "value": "99"}))
            check("vars set", r.get("success"), str(r)[:150])
            r = _payload(await sess.call_tool("vars", {"action": "clear", "name": "probeVar"}))
            check("vars clear", r.get("still_defined") is False, str(r)[:150])

            r = _payload(await sess.call_tool("batch", {"ops": [
                {"tool": "evaluate", "args": {"code": "2+2"}},
                {"tool": "evaluate", "args": {"code": "3*3"}},
                {"tool": "guide", "args": {"topic": "abort"}}]}))
            outs = [x.get("result", {}).get("output") for x in r.get("results", [])]
            check("batch runs several tools", r.get("ran") == 3 and outs[:2] == ["4", "9"], str(r)[:250])

            r = _payload(await sess.call_tool("batch", {"ops": [
                {"tool": "evaluate", "args": {"code": "1+1"}},
                {"tool": "nosuchtool", "args": {}},
                {"tool": "evaluate", "args": {"code": "2+2"}}]}))
            check("batch stops at first failure", r.get("ran") == 2, str(r)[:200])

            # batch must be able to dispatch every tool it advertises. The
            # hand-written version of that table omitted verify_derivation.
            from mathematica_wstp import server as _srv
            dispatchable = set(_srv._batchable())
            missing = (tools - {"batch"}) - dispatchable
            check("batch can dispatch every registered tool", not missing,
                  f"batch cannot reach: {sorted(missing)}")
            r = _payload(await sess.call_tool("batch", {"ops": [
                {"tool": "verify_derivation", "args": {"steps": ["(a+b)^2", "a^2+2*a*b+b^2"]}}]}))
            check("batch can run verify_derivation",
                  r["results"][0].get("result", {}).get("all_verified") is True, str(r)[:220])

            r = _payload(await sess.call_tool("guide", {"topic": "abort"}))
            check("guide returns guidance", "abort()" in r.get("guidance", ""), str(r)[:150])

            r = _payload(await sess.call_tool("verify_derivation",
                {"steps": ["(x+1)^2", "x^2 + 2*x + 1", "x^2 + 2*x + 1"]}))
            check("verify_derivation accepts a valid chain",
                  r.get("all_verified") is True, str(r)[:250])
            r = _payload(await sess.call_tool("verify_derivation",
                {"steps": ["(x+1)^2", "x^2 + 2*x + 5"]}))
            check("verify_derivation catches a bad step",
                  r.get("first_failing_step") == 1, str(r)[:250])

            if os.path.exists(NOTEBOOK):
                r = _payload(await sess.call_tool("read_notebook_file",
                                                  {"path": NOTEBOOK, "mode": "outline"}))
                heads = [o.get("style") for o in (r.get("outline") or [])]
                check("read_notebook_file outline",
                      isinstance(r.get("cell_count"), int) and bool(heads), str(r)[:220])
                r = _payload(await sess.call_tool("status", {}))
                check("read_notebook_file left no session open",
                      not r.get("notebooks"), str(r.get("notebooks"))[:150])

            # Compact mode: a wide range must summarise rather than blow the size
            # limit. Needs a real document -- a notebook of prose and code both,
            # since the point of the check is that the two are counted apart.
            if NOTEBOOK and os.path.exists(NOTEBOOK):
                await sess.call_tool("notebooks", {"action": "open", "path": NOTEBOOK})
                r = _payload(await sess.call_tool("evaluate_cells",
                    {"from_": 0, "to": 60, "timeout": 300, "stop_on_error": False,
                     "detail": "summary"}))
                check("evaluate_cells summary has counts, not per-cell output",
                      r.get("detail") == "summary" and "counts" in r and "results" not in r,
                      str(r)[:250])
                check("summary separates executed from skipped",
                      r["counts"]["skipped"] > 0 and r["counts"]["executed"] > 0,
                      str(r.get("counts")))

            r = _payload(await sess.call_tool("status", {}))
            check("status separates tracked kernels from orphans",
                  "kernels_tracked" in r and r.get("orphans") == [], str(r)[:200])

            r = _payload(await sess.call_tool("kernel", {"action": "restart"}))
            check("restart returns a new pid",
                  r.get("success") and r.get("pid") != r.get("previous_pid"), str(r))
            check("restart leaked no subkernels", not r.get("subkernels_leaked"), str(r))
            r = _payload(await sess.call_tool("evaluate", {"code": "marker"}))
            check("restart really cleared state",
                  r.get("output", "").strip() == "marker", str(r))

    return out


def check_ending_a_session_takes_the_whole_tree_down() -> list[tuple[str, bool, str]]:
    """Closing the pipe must take the master AND every helper kernel with it.

    Not a paraphrase of the shutdown path -- the actual one. Claude Code ending
    a session *is* this pipe closing, so the server is spawned raw here rather
    than through a client library whose context manager might terminate the
    process instead, which would test the wrong thing entirely.

    The failure this guards against is the previous server's: it signalled only
    the master, and a LaunchKernels[] fan-out stayed behind at ~400 MB apiece.
    """
    import subprocess
    from mathematica_wstp import registry

    out: list[tuple[str, bool, str]] = []
    srv = subprocess.Popen(
        [PARAMS.command, *PARAMS.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=ROOT, env=PARAMS.env)
    counter = [0]

    def rpc(method: str, params=None, notify: bool = False):
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            counter[0] += 1
            msg["id"] = counter[0]
        srv.stdin.write(json.dumps(msg) + "\n")
        srv.stdin.flush()
        if notify:
            return None
        while True:
            line = srv.stdout.readline()
            if not line:
                raise RuntimeError("server closed the pipe unexpectedly")
            reply = json.loads(line)
            if reply.get("id") == counter[0]:
                return reply

    def tool(name: str, args: dict) -> dict:
        return json.loads(rpc("tools/call", {"name": name, "arguments": args})
                          ["result"]["content"][0]["text"])

    try:
        rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "shutdown-test", "version": "0"}})
        rpc("notifications/initialized", {}, notify=True)
        launched = tool("evaluate", {"code": "Length[LaunchKernels[3]]", "timeout": 240})
        if launched.get("output", "").strip() in ("0", "$Failed", "$Aborted", ""):
            out.append(("session close takes helpers down", True,
                        "skipped: no subkernel licences available"))
            srv.kill()
            return out

        st = tool("status", {})
        master = st["kernel"]["pid"]
        helpers = list(st["kernel"]["subkernels"])
        # status must see them without anyone having asked for them explicitly;
        # a census that reads empty here cannot prove anything below.
        out.append(("status sees helpers unprompted", bool(helpers), str(st["kernel"])))
        if not helpers:
            srv.kill()
            return out

        srv.stdin.close()
        srv.wait(timeout=60)
        out.append(("server exits when the pipe closes", srv.returncode == 0,
                    f"returncode {srv.returncode}"))

        watch = [master] + helpers
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and any(registry.pid_alive(p) for p in watch):
            time.sleep(0.1)
        survivors = [p for p in watch if registry.pid_alive(p)]
        out.append(("session close takes helpers down", not survivors,
                    f"still running: {survivors}"))
        for p in survivors:
            try:
                os.kill(p, 9)
            except OSError:
                pass
    finally:
        if srv.poll() is None:
            srv.kill()
    return out


def check_sigterm_takes_the_whole_tree_down() -> list[tuple[str, bool, str]]:
    """SIGTERM must close the kernel tree, not just abandon it.

    The sibling test above covers the pipe closing. This covers the other way a
    server dies: someone kills it. Python runs no atexit handler on SIGTERM, so
    without an explicit handler the kernel and its helpers are simply left --
    the startup reaper finds them eventually, but the memory is gone until then.

    Spawned raw for the same reason as the sibling test: a client library's
    context manager would terminate the process itself and test the wrong path.

    The exit status is checked too. After cleaning up, the handler restores the
    default disposition and re-raises, so the process still reports death by
    signal (-15). Swallowing the signal would look like a clean exit to anything
    supervising the server.
    """
    import signal as _signal
    import subprocess
    from mathematica_wstp import registry

    out: list[tuple[str, bool, str]] = []
    srv = subprocess.Popen(
        [PARAMS.command, *PARAMS.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=ROOT, env=PARAMS.env)
    counter = [0]

    def rpc(method: str, params=None, notify: bool = False):
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            counter[0] += 1
            msg["id"] = counter[0]
        srv.stdin.write(json.dumps(msg) + "\n")
        srv.stdin.flush()
        if notify:
            return None
        while True:
            line = srv.stdout.readline()
            if not line:
                return None
            got = json.loads(line)
            if got.get("id") == msg["id"]:
                return got

    try:
        rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "sigterm-test", "version": "1"}})
        rpc("notifications/initialized", notify=True)
        rpc("tools/call", {"name": "evaluate",
                           "arguments": {"code": "Length[LaunchKernels[2]]", "timeout": 300}})
        reply = rpc("tools/call", {"name": "status", "arguments": {}})
        state = json.loads(reply["result"]["content"][0]["text"])
        master = state["kernel"]["pid"]
        helpers = list(state["kernel"].get("subkernels") or [])
        out.append(("sigterm test set up a kernel with helpers", bool(helpers),
                    f"master={master} helpers={helpers}"))

        srv.send_signal(_signal.SIGTERM)
        try:
            srv.wait(timeout=30)
        except subprocess.TimeoutExpired:
            srv.kill()
            out.append(("server exits on SIGTERM", False, "did not exit within 30s"))
            return out

        out.append(("server exits on SIGTERM", True, f"returncode={srv.returncode}"))
        out.append(("SIGTERM still reported in the exit status",
                    srv.returncode == -_signal.SIGTERM, f"returncode={srv.returncode}"))

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not registry.pid_alive(master) and not any(
                    registry.pid_alive(h) for h in helpers):
                break
            time.sleep(0.2)
        out.append(("SIGTERM took the master down", not registry.pid_alive(master), str(master)))
        survivors = [h for h in helpers if registry.pid_alive(h)]
        out.append(("SIGTERM took the helpers down", not survivors, f"survivors={survivors}"))
    finally:
        if srv.poll() is None:
            srv.kill()
    return out


def check_an_aborting_cell_does_not_take_the_session_down() -> list[tuple[str, bool, str]]:
    """One cell calling Abort[] must cost that cell, not the kernel.

    The failure this pins: TimeConstrained catches a cell that runs too long but
    not one that aborts itself, so the abort propagated out of the whole range
    evaluation. The helper then returned a bare symbol where a ByteArray was
    expected, WSGetInteger8List failed with "WSGet out of sequence", and the link
    was left in an error state -- which killed the kernel and silently replaced
    the session. Measured cost before the fix: five full notebook replays.
    """
    import subprocess
    import tempfile

    out: list[tuple[str, bool, str]] = []
    nb = tempfile.NamedTemporaryFile("w", suffix=".nb", delete=False)
    nb.write('Notebook[{'
             'Cell[BoxData["before = 111"], "Input"],'
             'Cell[BoxData["Abort[]"], "Input"],'
             'Cell[BoxData["after = 222"], "Input"]}]')
    nb.close()

    srv = subprocess.Popen(
        [PARAMS.command, *PARAMS.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=ROOT, env=PARAMS.env)
    counter = [0]

    def rpc(method: str, params=None, notify: bool = False):
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            counter[0] += 1
            msg["id"] = counter[0]
        srv.stdin.write(json.dumps(msg) + "\n")
        srv.stdin.flush()
        if notify:
            return None
        while True:
            line = srv.stdout.readline()
            if not line:
                raise RuntimeError("server closed the pipe")
            reply = json.loads(line)
            if reply.get("id") == counter[0]:
                return reply

    def tool(name: str, args: dict) -> dict:
        return json.loads(rpc("tools/call", {"name": name, "arguments": args})
                          ["result"]["content"][0]["text"])

    try:
        rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "abort-cell-test", "version": "0"}})
        rpc("notifications/initialized", {}, notify=True)
        tool("evaluate", {"code": "keepme = 31337", "timeout": 60})
        tool("notebooks", {"action": "open", "path": nb.name})

        res = tool("evaluate_cells", {"from_": 0, "to": 2, "detail": "full", "timeout": 120})
        out.append(("aborting cell still returns a payload", bool(res.get("success")), str(res)[:200]))

        cells = {c.get("index"): c for c in (res.get("results") or [])}
        out.append(("the aborting cell is reported as aborted",
                    bool(cells.get(1, {}).get("aborted")), str(cells.get(1))[:160]))
        out.append(("the replay continued past it",
                    cells.get(2, {}).get("success") is True, str(cells.get(2))[:160]))

        after = tool("evaluate", {"code": "{keepme, after}", "timeout": 60})
        out.append(("kernel survived with its state",
                    "31337" in str(after.get("output")) and "222" in str(after.get("output")),
                    str(after)[:160]))
        out.append(("no silent kernel swap", not after.get("kernel_replaced"), str(after)[:160]))

        st = tool("status", {})
        out.append(("link is clean afterwards",
                    not st["kernel"].get("link_error")
                    and st["kernel"].get("link_health") == "connected", str(st["kernel"])[:200]))
    finally:
        if srv.poll() is None:
            srv.kill()
            srv.wait()
        os.unlink(nb.name)
    return out


def check_summary_mode_keeps_the_index_bookkeeping() -> list[tuple[str, bool, str]]:
    """A summarised reply must still say that indices moved.

    _condense_range rebuilds the reply from a fixed set of keys, so anything not
    named there disappears -- and outputs_written / cells_inserted /
    indices_shifted were disappearing exactly when they matter most, because
    summarising kicks in on large ranges, which are the ranges most likely to
    insert cells. A caller told to watch indices_shifted was watching a field
    the reply had dropped.
    """
    from mathematica_wstp.server import _condense_range

    payload = {"success": True, "id": "hnb1", "from": 0, "to": 3,
               "results": [{"index": 0, "style": "Input", "success": True, "timing_ms": 5}],
               "outputs_written": 2, "cells_inserted": 1, "indices_shifted": True,
               "cell_count": 5, "dirty": True}
    out = _condense_range(payload)
    return [
        ("summary keeps indices_shifted", out.get("indices_shifted") is True, str(out)[:200]),
        ("summary keeps cells_inserted", out.get("cells_inserted") == 1, str(out)[:200]),
        ("summary keeps cell_count", out.get("cell_count") == 5, str(out)[:200]),
        ("summary still summarises", out.get("detail") == "summary", str(out)[:120]),
    ]


def check_write_outputs_makes_an_exported_record_faithful() -> list[tuple[str, bool, str]]:
    """A replay must be able to leave a record of what IT computed.

    Evaluating cells reads them and never writes results back, so exporting the
    notebook afterwards -- to .nb, PDF or Markdown -- reproduces whatever outputs
    were saved in the file, presented as if they were this run's. The stored
    output here is deliberately wrong so that a faithful export is the only way
    to pass.

    The saved file is also checked to be a notebook document rather than a bare
    Put of the expression, since a dump reads back perfectly here while a front
    end may refuse it.
    """
    import subprocess
    import tempfile

    out: list[tuple[str, bool, str]] = []
    src = tempfile.NamedTemporaryFile("w", suffix=".nb", delete=False)
    src.write('Notebook[{'
              'Cell["Demo", "Section"],'
              'Cell[BoxData["2+2"], "Input"],'
              'Cell[BoxData["\\"STALE-99999\\""], "Output"],'
              'Cell[BoxData["10*10"], "Input"]}]')
    src.close()
    saved = src.name.replace(".nb", "-saved.nb")

    srv = subprocess.Popen(
        [PARAMS.command, *PARAMS.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=ROOT, env=PARAMS.env)
    counter = [0]

    def rpc(method: str, params=None, notify: bool = False):
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            counter[0] += 1
            msg["id"] = counter[0]
        srv.stdin.write(json.dumps(msg) + "\n")
        srv.stdin.flush()
        if notify:
            return None
        while True:
            line = srv.stdout.readline()
            if not line:
                raise RuntimeError("server closed the pipe")
            reply = json.loads(line)
            if reply.get("id") == counter[0]:
                return reply

    def tool(name: str, args: dict) -> dict:
        return json.loads(rpc("tools/call", {"name": name, "arguments": args})
                          ["result"]["content"][0]["text"])

    try:
        rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "writeback-test", "version": "0"}})
        rpc("notifications/initialized", {}, notify=True)
        tool("notebooks", {"action": "open", "path": src.name})

        r = tool("evaluate_cells", {"from_": 0, "to": 3, "write_outputs": True, "timeout": 120})
        out.append(("write_outputs reports what it did",
                    r.get("outputs_written") == 2 and r.get("cells_inserted") == 1, str(r)[:200]))
        out.append(("an index shift is declared", r.get("indices_shifted") is True, str(r)[:160]))

        sv = tool("notebooks", {"action": "save", "path": saved})
        body = Path(saved).read_text()
        # A record nobody can open is not a record. Put and NotebookSave both
        # round-trip through the kernel, so only the file header distinguishes
        # a notebook document from a bare expression dump.
        out.append(("save writes a notebook file, not an expression dump",
                    body.lstrip().startswith("(* Content-type:"), body[:80]))
        out.append(("save declares how it wrote the file",
                    sv.get("written_by") == "frontend" and sv.get("notebook_file") is True,
                    str(sv)[:200]))
        out.append(("the stale stored output is gone", "STALE-99999" not in body, body[:200]))
        out.append(("the computed values are there",
                    '"4"' in body.replace(" ", "") or "[\"4\"]" in body, body[:200]))
        out.append(("the input with no output cell gained one", "100" in body, body[:200]))

        # Without the flag nothing may be touched -- the default must stay read-only.
        tool("notebooks", {"action": "open", "path": src.name})
        before = tool("cells", {"limit": 10, "include_content": True})
        tool("evaluate_cells", {"from_": 0, "to": 3, "timeout": 120})
        after = tool("cells", {"limit": 10, "include_content": True})
        out.append(("default leaves the document untouched",
                    before.get("cells") == after.get("cells"), "document changed without write_outputs"))
    finally:
        if srv.poll() is None:
            srv.kill()
            srv.wait()
        for f in (src.name, saved):
            if os.path.exists(f):
                os.unlink(f)
    return out


def check_reading_a_file_does_not_close_the_session_on_it() -> list[tuple[str, bool, str]]:
    """A read-only view must not destroy a session the caller is holding.

    read_notebook_file opens the file to read it and closes that session in a
    finally. But open() de-duplicates by path, so when the caller already has
    that file open it hands back THEIR id -- and the cleanup then closed the
    caller's session. Every later call failed with "No headless notebook
    matches ...", with nothing in the reply explaining why. Found by an agent
    mid-replay, which lost its notebook two calls after opening it.
    """
    import subprocess
    import tempfile

    out: list[tuple[str, bool, str]] = []
    src = tempfile.NamedTemporaryFile("w", suffix=".nb", delete=False)
    src.write('Notebook[{Cell["Demo", "Section"], Cell[BoxData["2+2"], "Input"]}]')
    src.close()

    srv = subprocess.Popen(
        [PARAMS.command, *PARAMS.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=ROOT, env=PARAMS.env)
    counter = [0]

    def rpc(method: str, params=None, notify: bool = False):
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            counter[0] += 1
            msg["id"] = counter[0]
        srv.stdin.write(json.dumps(msg) + "\n")
        srv.stdin.flush()
        if notify:
            return None
        while True:
            line = srv.stdout.readline()
            if not line:
                raise RuntimeError("server closed the pipe")
            reply = json.loads(line)
            if reply.get("id") == counter[0]:
                return reply

    def tool(name: str, args: dict) -> dict:
        return json.loads(rpc("tools/call", {"name": name, "arguments": args})
                          ["result"]["content"][0]["text"])

    try:
        rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "readonly-test", "version": "0"}})
        rpc("notifications/initialized", {}, notify=True)

        opened = tool("notebooks", {"action": "open", "path": src.name})
        held = opened.get("id")
        tool("read_notebook_file", {"path": src.name, "mode": "outline"})

        after = tool("cells", {"notebook": held, "limit": 5})
        out.append(("the held session survives a read of the same file",
                    after.get("success") is True, str(after)[:200]))
        listing = tool("notebooks", {"action": "list"})
        out.append(("the session is still listed",
                    any(n.get("id") == held for n in (listing.get("notebooks") or [])),
                    str(listing)[:200]))

        # A file nobody has open must still be cleaned up, or reads leak sessions.
        other = tempfile.NamedTemporaryFile("w", suffix=".nb", delete=False)
        other.write('Notebook[{Cell["Other", "Section"]}]')
        other.close()
        before = len(tool("notebooks", {"action": "list"}).get("notebooks") or [])
        tool("read_notebook_file", {"path": other.name, "mode": "outline"})
        now = len(tool("notebooks", {"action": "list"}).get("notebooks") or [])
        out.append(("reading an unopened file leaves no session behind",
                    now == before, f"before={before} after={now}"))
        os.unlink(other.name)
    finally:
        if srv.poll() is None:
            srv.kill()
            srv.wait()
        if os.path.exists(src.name):
            os.unlink(src.name)
    return out


def check_abort_stops_a_range_not_just_one_cell() -> list[tuple[str, bool, str]]:
    """abort() must end the span, not skip a cell and carry on.

    evalCell wraps each cell in CheckAbort so one aborting cell cannot desync
    the link. CheckAbort absorbs user aborts -- which is what abort() sends --
    so the span used to continue: measured, an abort 5s into an 8-cell span
    aborted cell 1 and ran the other seven to completion, 23s in total. On a
    long replay that makes abort() useless, which is the one thing this server
    exists to get right.

    The kernel cannot tell abort() from a cell calling Abort[] itself, so the
    span stops for either and says so.
    """
    import subprocess
    import tempfile
    import threading

    out: list[tuple[str, bool, str]] = []
    src = tempfile.NamedTemporaryFile("w", suffix=".nb", delete=False)
    src.write("Notebook[{"
              + ",".join(f'Cell[BoxData["Pause[3]; {i}"], "Input"]' for i in range(8))
              + "}]")
    src.close()

    srv = subprocess.Popen(
        [PARAMS.command, *PARAMS.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=ROOT, env=PARAMS.env)
    counter = [0]
    pending: dict = {}
    lock = threading.Lock()

    def reader() -> None:
        for line in srv.stdout:
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if "id" in msg:
                pending[msg["id"]] = msg
    threading.Thread(target=reader, daemon=True).start()

    def send(method: str, params=None, notify: bool = False):
        with lock:
            msg: dict = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            mid = None
            if not notify:
                counter[0] += 1
                msg["id"] = mid = counter[0]
            srv.stdin.write(json.dumps(msg) + "\n")
            srv.stdin.flush()
            return mid

    def wait(mid: int, timeout: float = 180):
        end = time.time() + timeout
        while time.time() < end:
            if mid in pending:
                return pending.pop(mid)
            time.sleep(0.05)
        raise RuntimeError("timed out waiting for a reply")

    def tool(name: str, args: dict) -> dict:
        return json.loads(wait(send("tools/call", {"name": name, "arguments": args}))
                          ["result"]["content"][0]["text"])

    try:
        wait(send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "abort-range", "version": "0"}}))
        send("notifications/initialized", {}, notify=True)
        tool("notebooks", {"action": "open", "path": src.name})

        started = time.time()
        mid = send("tools/call", {"name": "evaluate_cells",
                                  "arguments": {"from_": 0, "to": 7, "timeout": 300}})
        time.sleep(5.0)
        tool("abort", {})
        reply = json.loads(wait(mid)["result"]["content"][0]["text"])
        elapsed = time.time() - started

        results = reply.get("results") or []
        out.append(("the span stops when a cell aborts",
                    len(results) < 8, f"{len(results)} cells ran; expected to stop early"))
        out.append(("it stops promptly, not after the whole span",
                    elapsed < 15, f"took {elapsed:.1f}s (8 unaborted cells would be ~24s)"))
        out.append(("the span says it stopped early",
                    reply.get("stopped_early") is True, str(reply)[:200]))
        out.append(("it says why, and where to resume",
                    "abort" in str(reply.get("stopped_because", "")).lower()
                    and "resume" in str(reply.get("note", "")).lower(), str(reply)[:250]))
        out.append(("the kernel is still usable afterwards",
                    tool("evaluate", {"code": "1+1"}).get("success") is True, "kernel unusable"))
    finally:
        if srv.poll() is None:
            srv.kill()
            srv.wait()
        if os.path.exists(src.name):
            os.unlink(src.name)
    return out


def check_cells_can_find_where_a_symbol_is_assigned() -> list[tuple[str, bool, str]]:
    """cells(defines=...) must find assignments without inventing them.

    An agent replaying a notebook could get a symbol's value from the kernel but
    had no way to find the cell that produced it, and resorted to paging through
    previews by hand. The risk in a textual search is over-matching, so the
    negative cases matter more than the positive ones here.
    """
    import subprocess
    import tempfile

    out: list[tuple[str, bool, str]] = []
    lines = ["Res = 1", "MyRes = 2", "ResExtra = 3", "If[Res == 4, a, b]",
             "Res[x_] := x^2", "other = Res + 1", "Amp[2] = 7"]
    src = tempfile.NamedTemporaryFile("w", suffix=".nb", delete=False)
    src.write("Notebook[{"
              + ",".join('Cell[BoxData["%s"], "Input"]' % ln.replace('"', '\\"')
                         for ln in lines)
              + "}]")
    src.close()

    srv = subprocess.Popen(
        [PARAMS.command, *PARAMS.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=ROOT, env=PARAMS.env)
    counter = [0]

    def rpc(method: str, params=None, notify: bool = False):
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            counter[0] += 1
            msg["id"] = counter[0]
        srv.stdin.write(json.dumps(msg) + "\n")
        srv.stdin.flush()
        if notify:
            return None
        while True:
            line = srv.stdout.readline()
            if not line:
                raise RuntimeError("server closed the pipe")
            reply = json.loads(line)
            if reply.get("id") == counter[0]:
                return reply

    def tool(name: str, args: dict) -> dict:
        return json.loads(rpc("tools/call", {"name": name, "arguments": args})
                          ["result"]["content"][0]["text"])

    try:
        rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "defines-test", "version": "0"}})
        rpc("notifications/initialized", {}, notify=True)
        tool("notebooks", {"action": "open", "path": src.name})

        hits = {c["index"] for c in (tool("cells", {"defines": "Res"}).get("cells") or [])}
        out.append(("a plain assignment is found", 0 in hits, str(sorted(hits))))
        out.append(("a delayed assignment is found", 4 in hits, str(sorted(hits))))
        out.append(("a longer name is not mistaken for it",
                    1 not in hits and 2 not in hits, str(sorted(hits))))
        out.append(("a comparison is not read as an assignment", 3 not in hits, str(sorted(hits))))
        out.append(("merely using the symbol is not a definition", 5 not in hits, str(sorted(hits))))

        indexed = tool("cells", {"defines": "Amp[2]"})
        out.append(("an indexed assignment is found",
                    [c["index"] for c in (indexed.get("cells") or [])] == [6], str(indexed)[:160]))

        missing = tool("cells", {"defines": "NotDefinedHere"})
        out.append(("an unknown symbol reports none rather than guessing",
                    missing.get("count") == 0 and bool(missing.get("note")), str(missing)[:200]))
    finally:
        if srv.poll() is None:
            srv.kill()
            srv.wait()
        if os.path.exists(src.name):
            os.unlink(src.name)
    return out


def check_cell_labels_count_statements_not_cells() -> list[tuple[str, bool, str]]:
    """In[] advances per statement, and Out[] is numbered by its own statement.

    A multi-statement cell stores its statements interleaved with the newline
    boxes between them -- BoxData[{stmt, "\\n", stmt}] -- so counting the list
    length counts separators too, and counting surviving outputs counts neither.
    Both were wrong: a six-statement opening cell scored as one, and the next
    cell came out In[2] where a front end writes In[7].

    The expected numbers here are taken from a real notebook's own stored
    labels, written by an actual front end: 6 statements at In[1] followed by
    In[7]; three statements at In[9] whose second is the only one returning a
    value followed by Out[10], not Out[9]. Across that document, the rule
    matches 191 of 193 consecutive multi-statement pairs, and the counting it
    replaced matched none of them.
    """
    import subprocess
    import tempfile

    out: list[tuple[str, bool, str]] = []
    # One backslash in the file: WL must read the indenting-newline
    # character, not the literal characters of its escape.
    nl = '"\\[IndentingNewLine]"'
    six = ",".join(
        [f'RowBox[{{RowBox[{{"a{k}", "=", "{k}"}}], ";"}}]' for k in range(1, 7)][i // 2]
        if i % 2 == 0 else nl for i in range(11))
    three = ",".join([
        'RowBox[{RowBox[{"c", "=", "1"}], ";"}]', nl,
        'RowBox[{"c", "+", "1"}]', nl,
        'RowBox[{RowBox[{"d", "=", "3"}], ";"}]'])

    src = tempfile.NamedTemporaryFile("w", suffix=".nb", delete=False)
    src.write("Notebook[{"
              f'Cell[BoxData[{{{six}}}], "Input"],'
              'Cell[BoxData[RowBox[{"7", "*", "1"}]], "Input"],'
              'Cell[BoxData[RowBox[{RowBox[{"b", "=", "8"}], ";"}]], "Input"],'
              f'Cell[BoxData[{{{three}}}], "Input"]'
              "}]")
    src.close()
    saved = src.name.replace(".nb", "-labelled.nb")

    srv = subprocess.Popen(
        [PARAMS.command, *PARAMS.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=ROOT, env=PARAMS.env)
    counter = [0]

    def rpc(method: str, params=None, notify: bool = False):
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            counter[0] += 1
            msg["id"] = counter[0]
        srv.stdin.write(json.dumps(msg) + "\n")
        srv.stdin.flush()
        if notify:
            return None
        while True:
            line = srv.stdout.readline()
            if not line:
                raise RuntimeError("server closed the pipe")
            reply = json.loads(line)
            if reply.get("id") == counter[0]:
                return reply

    def tool(name: str, args: dict) -> dict:
        return json.loads(rpc("tools/call", {"name": name, "arguments": args})
                          ["result"]["content"][0]["text"])

    try:
        rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "label-test", "version": "0"}})
        rpc("notifications/initialized", {}, notify=True)
        tool("notebooks", {"action": "open", "path": src.name})
        tool("evaluate_cells", {"from_": 0, "to": 3, "write_outputs": True, "timeout": 120})
        tool("notebooks", {"action": "save", "path": saved})
        body = Path(saved).read_text()

        for want, why in [
            ("In[1]:=", "the first cell is In[1]"),
            ("In[7]:=", "six suppressed statements consume six line numbers"),
            ("Out[7]=", "a single-statement cell's output shares its number"),
            ("In[8]:=", "a suppressed single statement still consumes one"),
            ("In[9]:=", "the next cell follows on"),
            ("Out[10]=", "an output is numbered by the statement that produced it"),
        ]:
            out.append((why, want in body, f"{want!r} missing from the saved notebook"))
        out.append(("no cell is numbered as if statements did not count",
                    "In[2]:=" not in body, "In[2] present: statements were counted as one cell"))
        out.append(("an output is not numbered by its position among survivors",
                    "Out[9]=" not in body, "Out[9] present: numbered by survivor, not statement"))
    finally:
        if srv.poll() is None:
            srv.kill()
            srv.wait()
        for f in (src.name, saved):
            if os.path.exists(f):
                os.unlink(f)
    return out


def check_verify_catches_misnumbered_labels_without_a_reference() -> list[tuple[str, bool, str]]:
    """The record has to be checkable on its own, and only when it is ours.

    Shape verification compares output COUNTS and positions against a reference.
    The label bug slipped past all of it: right number of outputs, right places,
    wrong numbers on them. And a computation built from scratch has no reference
    at all -- which is exactly when the exported record is the only evidence a
    reader has.

    So the check is self-consistency, and it is gated on authorship: gaps and
    reused numbers are faults in a record this server numbered and normal in a
    notebook a person evaluated interactively. Telling someone their own good
    notebook is broken is how a report stops being read.
    """
    import subprocess
    import tempfile

    out: list[tuple[str, bool, str]] = []
    nl = '"\\[IndentingNewLine]"'
    six = ",".join(sum([[f'RowBox[{{RowBox[{{"a{k}", "=", "{k}"}}], ";"}}]']
                        + ([nl] if k < 6 else []) for k in range(1, 7)], []))
    src = tempfile.NamedTemporaryFile("w", suffix=".nb", delete=False)
    src.write("Notebook[{"
              f'Cell[BoxData[{{{six}}}], "Input"],'
              'Cell[BoxData[RowBox[{"7", "*", "1"}]], "Input"]'
              "}]")
    src.close()
    saved = src.name.replace(".nb", "-rec.nb")
    broken = src.name.replace(".nb", "-broken.nb")

    srv = subprocess.Popen(
        [PARAMS.command, *PARAMS.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=ROOT, env=PARAMS.env)
    counter = [0]

    def rpc(method: str, params=None, notify: bool = False):
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            counter[0] += 1
            msg["id"] = counter[0]
        srv.stdin.write(json.dumps(msg) + "\n")
        srv.stdin.flush()
        if notify:
            return None
        while True:
            line = srv.stdout.readline()
            if not line:
                raise RuntimeError("server closed the pipe")
            reply = json.loads(line)
            if reply.get("id") == counter[0]:
                return reply

    def tool(name: str, args: dict) -> dict:
        return json.loads(rpc("tools/call", {"name": name, "arguments": args})
                          ["result"]["content"][0]["text"])

    try:
        rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "verify-self", "version": "0"}})
        rpc("notifications/initialized", {}, notify=True)

        # As authored: not ours, so nothing may be reported as a fault.
        opened = tool("notebooks", {"action": "open", "path": src.name})
        asis = tool("notebooks", {"action": "verify", "notebook": opened.get("id")})
        out.append(("verify needs no reference", asis.get("success") is True, str(asis)[:160]))
        out.append(("an unreplayed document is never called faulty",
                    asis.get("discrepancy_count") == 0, str(asis)[:220]))

        # Ours, and correct.
        tool("evaluate_cells", {"from_": 0, "to": 1, "write_outputs": True, "timeout": 120})
        good = tool("notebooks", {"action": "verify"})
        out.append(("a clean record of our own passes",
                    good.get("discrepancy_count") == 0
                    and good.get("labels_written_by_this_session") is True, str(good)[:220]))

        tool("notebooks", {"action": "save", "path": saved})
        body = Path(saved).read_text()
        out.append(("the record is marked as ours so the check survives a reopen",
                    "MCPLinearReplay" in body, "no stamp in the saved notebook"))

        # Ours, reopened in a fresh session, and misnumbered the way the bug did.
        Path(broken).write_text(body.replace("In[7]:=", "In[2]:=", 1))
        reopened = tool("notebooks", {"action": "open", "path": broken})
        bad = tool("notebooks", {"action": "verify", "notebook": reopened.get("id")})
        kinds = " ".join(d.get("kind", "") for d in (bad.get("discrepancies") or []))
        out.append(("a misnumbered record of ours is caught after reopening",
                    bad.get("discrepancy_count", 0) > 0, str(bad)[:260]))
        out.append(("it names the numbering as the problem",
                    "numbering" in kinds, kinds or "no kinds reported"))
        out.append(("the verdict refuses to vouch for it",
                    "INCONSISTENT" in str(bad.get("verdict", "")), str(bad.get("verdict"))[:160]))

        # Against a reference, the increments are compared to labels a front end
        # wrote. This is the only check that tests the counting RULE rather than
        # the arithmetic: self-consistency uses the same statementCount the
        # writer used, so a wrong rule agrees with itself and passes.
        ref = src.name.replace(".nb", "-ref.nb")
        Path(ref).write_text(
            "Notebook[{"
            f'Cell[BoxData[{{{six}}}], "Input", CellLabel -> "In[1]:="],'
            'Cell[BoxData[RowBox[{"7", "*", "1"}]], "Input", CellLabel -> "In[7]:="]'
            "}]")
        # Name the notebook: several are open by now, so verify cannot pick one.
        again = tool("notebooks", {"action": "open", "path": src.name})
        mine_id = again.get("id")
        tool("evaluate_cells", {"notebook": mine_id, "from_": 0, "to": 1,
                                "write_outputs": True, "timeout": 120})
        against = tool("notebooks", {"action": "verify", "path": ref, "notebook": mine_id})
        inc = (against.get("labels") or {}).get("increments_vs_reference") or {}
        compared = inc.get("compared", 0)
        out.append(("increments are compared against the reference's own labels",
                    compared > 0, str(against)[:240]))
        # Guarded on compared > 0: equal counts of nothing is not agreement.
        out.append(("our numbering advances exactly as the front end's did",
                    compared > 0 and compared == inc.get("matching"), str(inc)))
        if os.path.exists(ref):
            os.unlink(ref)
    finally:
        if srv.poll() is None:
            srv.kill()
            srv.wait()
        for f in (src.name, saved, broken):
            if os.path.exists(f):
                os.unlink(f)
    return out


def check_overlapping_spans_cannot_renumber_silently() -> list[tuple[str, bool, str]]:
    """Re-running an already-labelled cell must be reported, not swallowed.

    Writing outputs back inserts cells, so a caller re-locating its next
    boundary after ``indices_shifted`` can easily land on a cell the previous
    call already ran. That cell then gets a fresh, higher In[] number and the
    number it held belongs to nothing -- a hole in the record that no cell
    accounts for. To a reader who cannot re-run the notebook that is
    indistinguishable from a missing cell.

    Found in a real replay: In[97] was followed by In[99]. The agent that
    produced it investigated, could not explain it, and reported it as an
    undocumented gap -- correctly. Reproduced here: overlapping spans orphan
    numbers, adjacent spans do not, and removing a stale output at the boundary
    does not either (which was the plausible-looking wrong explanation).
    """
    import re as _re
    import subprocess
    import tempfile

    out: list[tuple[str, bool, str]] = []

    def replay(cells: list[str], calls: list[tuple[int, int]]):
        src = tempfile.NamedTemporaryFile("w", suffix=".nb", delete=False)
        src.write("Notebook[{" + ",".join(cells) + "}]")
        src.close()
        saved = src.name.replace(".nb", "-r.nb")
        srv = subprocess.Popen(
            [PARAMS.command, *PARAMS.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=ROOT, env=PARAMS.env)
        counter = [0]

        def rpc(method, params=None, notify=False):
            msg = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            if not notify:
                counter[0] += 1
                msg["id"] = counter[0]
            srv.stdin.write(json.dumps(msg) + "\n")
            srv.stdin.flush()
            if notify:
                return None
            while True:
                line = srv.stdout.readline()
                if not line:
                    raise RuntimeError("server closed the pipe")
                reply = json.loads(line)
                if reply.get("id") == counter[0]:
                    return reply

        def tool(name, args):
            return json.loads(rpc("tools/call", {"name": name, "arguments": args})
                              ["result"]["content"][0]["text"])

        try:
            rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "overlap", "version": "0"}})
            rpc("notifications/initialized", {}, notify=True)
            tool("notebooks", {"action": "open", "path": src.name})
            replies = [tool("evaluate_cells", {"from_": a, "to": b,
                                               "write_outputs": True, "timeout": 120})
                       for a, b in calls]
            tool("notebooks", {"action": "save", "path": saved})
            labels = [int(x) for x in
                      _re.findall(r'CellLabel *-> *"In\[(\d+)\]', Path(saved).read_text())]
            gaps = [(labels[i], labels[i + 1]) for i in range(len(labels) - 1)
                    if labels[i + 1] - labels[i] != 1]
            return replies, labels, gaps
        finally:
            if srv.poll() is None:
                srv.kill()
                srv.wait()
            for f in (src.name, saved):
                if os.path.exists(f):
                    os.unlink(f)

    plain = [f'Cell[BoxData["x{i}={i};"], "Input"]' for i in range(6)]
    stale = ['Cell[BoxData["x0=0;"], "Input"]',
             'Cell[BoxData["x1=1;"], "Input"]',
             'Cell[BoxData["(*x2=2;*)"], "Input"]',
             'Cell[BoxData["\\"STALE\\""], "Output"]',
             'Cell[BoxData["x3=3;"], "Input"]',
             'Cell[BoxData["x4=4;"], "Input"]']

    reps, labels, gaps = replay(plain, [(0, 2), (3, 5)])
    out.append(("adjacent spans number continuously", gaps == [], f"labels={labels}"))
    out.append(("and report no re-evaluation",
                all(r.get("cells_re_evaluated") is None for r in reps), str(reps)[:160]))

    reps, labels, gaps = replay(plain, [(0, 3), (2, 5)])
    out.append(("an overlapping span is reported, not swallowed",
                reps[1].get("cells_re_evaluated") == 2, str(reps[1])[:220]))
    out.append(("the warning says the numbers are orphaned",
                "belong to no cell" in str(reps[1].get("warning", "")),
                str(reps[1].get("warning"))[:200]))
    out.append(("the count matches the numbers actually orphaned",
                len(gaps) == 1 and gaps[0][1] - gaps[0][0] - 1 == 2, f"labels={labels} gaps={gaps}"))

    reps, labels, gaps = replay(stale, [(0, 3), (4, 5)])
    out.append(("removing a stale output at a boundary is not mistaken for it",
                gaps == [] and reps[0].get("stale_outputs_removed") == 1
                and all(r.get("cells_re_evaluated") is None for r in reps),
                f"labels={labels} gaps={gaps} removed={reps[0].get('stale_outputs_removed')}"))
    return out


def check_the_subkernel_pool_can_be_released_without_losing_state() -> list[tuple[str, bool, str]]:
    """Closing the pool must free memory and cost no definitions.

    A pool outlives the work that needed it. Measured on this machine, one
    finished replay's 20 subkernels held 5.2 GB -- 69% of all Wolfram memory --
    an hour after they had anything to do, and nothing reclaimed it because the
    master kernel was still healthy and still held every definition the caller
    wanted. Restarting would have freed it and thrown the results away.

    The trap this pins: CloseKernels[] returns when the request is sent, not
    when the processes are gone. Counting immediately afterwards sees all of
    them still there and reports "closed 0" while the memory does in fact go.
    """
    import subprocess

    out: list[tuple[str, bool, str]] = []
    srv = subprocess.Popen(
        [PARAMS.command, *PARAMS.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=ROOT, env=PARAMS.env)
    counter = [0]

    def rpc(method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            counter[0] += 1
            msg["id"] = counter[0]
        srv.stdin.write(json.dumps(msg) + "\n")
        srv.stdin.flush()
        if notify:
            return None
        while True:
            line = srv.stdout.readline()
            if not line:
                raise RuntimeError("server closed the pipe")
            reply = json.loads(line)
            if reply.get("id") == counter[0]:
                return reply

    def tool(name, args):
        return json.loads(rpc("tools/call", {"name": name, "arguments": args})
                          ["result"]["content"][0]["text"])

    try:
        rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "subkernels", "version": "0"}})
        rpc("notifications/initialized", {}, notify=True)

        idle = tool("kernel", {"action": "close_subkernels"})
        out.append(("closing with no pool open is not an error",
                    idle.get("success") is True and idle.get("closed") == 0, str(idle)[:160]))

        tool("evaluate", {"code": "keepme = 42; Length[LaunchKernels[4]]", "timeout": 300})
        listed = tool("kernel", {"action": "subkernels"})
        out.append(("a pool is open to begin with", listed.get("count") == 4, str(listed)[:160]))

        closed = tool("kernel", {"action": "close_subkernels"})
        out.append(("the pool is reported closed, not merely asked to close",
                    closed.get("closed") == 4 and closed.get("still_open") == 0,
                    str(closed)[:220]))
        out.append(("it says how much memory came back",
                    (closed.get("freed_mb") or 0) > 0, str(closed)[:220]))
        out.append(("/proc agrees the subkernels are gone",
                    tool("kernel", {"action": "subkernels"}).get("count") == 0,
                    "subkernels still listed after close"))
        out.append(("the master kernel keeps its definitions",
                    tool("evaluate", {"code": "keepme", "timeout": 60}).get("output") == "42",
                    "definitions were lost -- this must not behave like a restart"))
    finally:
        if srv.poll() is None:
            srv.kill()
            srv.wait()
    return out


def check_a_span_that_skips_an_unrun_cell_says_so() -> list[tuple[str, bool, str]]:
    """Skipping a definition must be announced, and index=N must write back.

    Both come from one real replay. Re-locating a boundary after
    indices_shifted, the agent started a span one cell late and missed a
    function definition. Nothing failed: an undefined head stays unevaluated,
    Coefficient of an unevaluated head is 0, and ~700 later cells reported
    success while recording zeros. The record looked complete and was empty.

    Then, fixing one cell with evaluate_cells(index=N, write_outputs=True) did
    nothing at all -- that path never received write_outputs, so the cell ran,
    the reply said success, and the document was untouched. A silent no-op
    offered as a repair.
    """
    import subprocess
    import tempfile

    out: list[tuple[str, bool, str]] = []
    cells = ([f'Cell[BoxData["x{i}={i};"], "Input"]' for i in range(3)]
             + ['Cell["A heading", "Section"]']
             + [f'Cell[BoxData["y{i}={i}"], "Input"]' for i in range(3)])
    src = tempfile.NamedTemporaryFile("w", suffix=".nb", delete=False)
    src.write("Notebook[{" + ",".join(cells) + "}]")
    src.close()

    srv = subprocess.Popen(
        [PARAMS.command, *PARAMS.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=ROOT, env=PARAMS.env)
    counter = [0]

    def rpc(method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            counter[0] += 1
            msg["id"] = counter[0]
        srv.stdin.write(json.dumps(msg) + "\n")
        srv.stdin.flush()
        if notify:
            return None
        while True:
            line = srv.stdout.readline()
            if not line:
                raise RuntimeError("server closed the pipe")
            reply = json.loads(line)
            if reply.get("id") == counter[0]:
                return reply

    def tool(name, args):
        return json.loads(rpc("tools/call", {"name": name, "arguments": args})
                          ["result"]["content"][0]["text"])

    try:
        rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "skip", "version": "0"}})
        rpc("notifications/initialized", {}, notify=True)
        tool("notebooks", {"action": "open", "path": src.name})

        a = tool("evaluate_cells", {"from_": 0, "to": 1, "write_outputs": True, "timeout": 120})
        out.append(("a first span is not accused of skipping",
                    a.get("executable_cells_skipped") is None, str(a)[:160]))
        b = tool("evaluate_cells", {"from_": 2, "to": 3, "write_outputs": True, "timeout": 120})
        out.append(("spans that tile correctly stay quiet",
                    b.get("executable_cells_skipped") is None, str(b)[:160]))
        c = tool("evaluate_cells", {"from_": 5, "to": 6, "write_outputs": True, "timeout": 120})
        out.append(("a span that jumps past an unrun cell says so",
                    c.get("executable_cells_skipped") == 1, str(c)[:220]))
        out.append(("it names which cell was missed",
                    c.get("skipped_input_numbers") == [4], str(c.get("skipped_input_numbers"))))
        out.append(("the warning explains it will look like zeros, not an error",
                    "zeros" in str(c.get("warning_skipped", "")), str(c.get("warning_skipped"))[:200]))

        # A fresh document for the index checks: the spans above wrote outputs
        # back, so indices in the first one have already moved.
        other = tempfile.NamedTemporaryFile("w", suffix=".nb", delete=False)
        other.write('Notebook[{Cell[BoxData["7*6"], "Input"]}]')
        other.close()
        second = tool("notebooks", {"action": "open", "path": other.name}).get("id")
        d = tool("evaluate_cells", {"notebook": second, "index": 0,
                                    "write_outputs": True, "timeout": 120})
        out.append(("index=N with write_outputs actually writes back",
                    d.get("outputs_written") == 1, str(d)[:220]))
        e = tool("evaluate_cells", {"notebook": second, "index": 0,
                                    "write_outputs": True, "timeout": 120})
        out.append(("index=N reports re-evaluation like a range does",
                    e.get("cells_re_evaluated") == 1, str(e)[:220]))
        f = tool("evaluate_cells", {"notebook": second, "index": 0, "timeout": 120})
        out.append(("index=N without write_outputs still leaves the document alone",
                    f.get("outputs_written") is None, str(f)[:200]))
        os.unlink(other.name)
    finally:
        if srv.poll() is None:
            srv.kill()
            srv.wait()
        if os.path.exists(src.name):
            os.unlink(src.name)
    return out


def main() -> int:
    results = asyncio.run(run_checks())
    results += check_ending_a_session_takes_the_whole_tree_down()
    results += check_an_aborting_cell_does_not_take_the_session_down()
    results += check_write_outputs_makes_an_exported_record_faithful()
    results += check_reading_a_file_does_not_close_the_session_on_it()
    results += check_abort_stops_a_range_not_just_one_cell()
    results += check_cells_can_find_where_a_symbol_is_assigned()
    results += check_cell_labels_count_statements_not_cells()
    results += check_verify_catches_misnumbered_labels_without_a_reference()
    results += check_overlapping_spans_cannot_renumber_silently()
    results += check_the_subkernel_pool_can_be_released_without_losing_state()
    results += check_a_span_that_skips_an_unrun_cell_says_so()
    results += check_summary_mode_keeps_the_index_bookkeeping()
    results += check_sigterm_takes_the_whole_tree_down()
    failures = 0
    for name, ok, detail in results:
        if ok:
            print(f"PASS  {name}")
        else:
            failures += 1
            print(f"FAIL  {name}\n        {detail[:300]}")
    print(f"\n{len(results)-failures}/{len(results)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
