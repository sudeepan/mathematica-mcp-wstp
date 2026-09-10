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
            check("evaluation reports the abort",
                  eval_res.get("aborted") or "abort" in str(eval_res).lower(), str(eval_res))

            r = _payload(await sess.call_tool("evaluate", {"code": "marker"}))
            check("state SURVIVES the abort", r.get("output", "").strip() == "424242", str(r))

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


def main() -> int:
    results = asyncio.run(run_checks())
    results += check_ending_a_session_takes_the_whole_tree_down()
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
