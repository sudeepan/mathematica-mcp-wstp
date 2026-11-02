"""Progress messages while a tool runs, checked without a kernel.

A client may end a call that has been silent for a while, although the kernel
is still working. Every tool is therefore registered as an async twin that runs
the tool on a worker thread, as the SDK runs a sync tool, and sends a progress
message every few seconds until it returns.

Run: .venv/bin/python tests/test_progress.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from mcp.server.mcpserver.tools.base import Tool  # noqa: E402

from mathematica_wstp import server  # noqa: E402

INTERVAL = "MATHEMATICA_WSTP_PROGRESS_SECONDS"


class FakeContext:
    """Collects what a tool reports; with fail=True, reporting raises as a gone client would."""

    def __init__(self, fail: bool = False):
        self.reports: list[tuple[float, float | None, str | None]] = []
        self.fail = fail

    async def report_progress(self, progress: float, total: float | None = None,
                              message: str | None = None) -> None:
        if self.fail:
            raise RuntimeError("client went away")
        self.reports.append((progress, total, message))


def slow(seconds: float, value: str = "done") -> dict:
    time.sleep(seconds)
    return {"value": value, "thread": threading.current_thread().name}


def run(fn, ctx, **kwargs):
    return asyncio.run(server._with_progress(fn)(ctx=ctx, **kwargs))


def test_a_long_call_reports_progress_and_returns_its_result():
    ctx = FakeContext()
    result = run(slow, ctx, seconds=0.55, value="kept")
    assert result["value"] == "kept", result
    assert len(ctx.reports) >= 3, ctx.reports
    progress = [p for p, _, _ in ctx.reports]
    assert progress == sorted(set(progress)), progress
    assert all(total is None for _, total, _ in ctx.reports), ctx.reports
    assert all(m.startswith("slow still running after ") for _, _, m in ctx.reports), ctx.reports


def test_the_tool_runs_on_a_worker_thread():
    result = run(slow, FakeContext(), seconds=0)
    assert result["thread"] != threading.main_thread().name, result


def test_a_quick_call_sends_nothing():
    ctx = FakeContext()
    run(slow, ctx, seconds=0)
    assert ctx.reports == [], ctx.reports


def test_an_error_reaches_the_caller_unchanged():
    def broken() -> dict:
        raise ValueError("boom")
    try:
        run(broken, FakeContext())
    except ValueError as exc:
        assert str(exc) == "boom", exc
    else:
        raise AssertionError("the error was swallowed")


def test_a_failing_progress_message_does_not_fail_the_call():
    assert run(slow, FakeContext(fail=True), seconds=0.35)["value"] == "done"


def test_the_interval_setting():
    saved = os.environ.get(INTERVAL)
    try:
        for raw, want in (("0.1", 0.1), ("30", 30.0), ("", 15.0), ("soon", 15.0),
                          ("0", 15.0), ("-2", 15.0), ("nan", 15.0), ("inf", 15.0)):
            os.environ[INTERVAL] = raw
            assert server._progress_interval() == want, (raw, server._progress_interval())
    finally:
        if saved is None:
            os.environ.pop(INTERVAL, None)
        else:
            os.environ[INTERVAL] = saved


def test_every_tool_is_registered_with_progress_and_an_unchanged_schema():
    """The client sees the same tools, arguments and result schemas as before."""
    tools = server.server._tool_manager.list_tools()
    assert len(tools) >= 16, [t.name for t in tools]
    for tool in tools:
        plain = Tool.from_function(getattr(server, tool.name))
        assert not plain.is_async and plain.context_kwarg is None, tool.name
        assert tool.is_async and tool.context_kwarg == "ctx", tool.name
        assert tool.parameters == plain.parameters, tool.name
        assert tool.output_schema == plain.output_schema, tool.name


if __name__ == "__main__":
    import traceback

    os.environ[INTERVAL] = "0.1"
    tests = [obj for name, obj in list(globals().items())
             if name.startswith("test_") and callable(obj)]
    passed = failed = 0
    print("=== Progress messages (no kernel) ===")
    for fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
