"""Which socket and which kernel the supervisor actions reach, checked without a kernel.

start honoured SUP_SOCK, while status, stop, use, lookup, running and abort
used the per-user default, so a supervisor started on SUP_SOCK could not be
found again. And the docs said evaluate() and vars() stay on this process's
kernel when a supervisor is selected; they go to the selected backend.

Run: .venv/bin/python tests/test_supervisor_socket.py
"""

from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from mathematica_wstp import server  # noqa: E402
from mathematica_wstp.evaluator import get_evaluator, set_evaluator  # noqa: E402
from mathematica_wstp.session import WLResult  # noqa: E402
from mathematica_wstp.supervisor import SupervisorUnavailable, connect, core, lifecycle  # noqa: E402
from mathematica_wstp.supervisor.backend import SupervisorEvaluator  # noqa: E402


def payload(result) -> dict:
    data = result.structured_content
    if not isinstance(data, dict):
        raise AssertionError(f"no structured content in {result!r}")
    return data


class with_sup_sock:
    """SUP_SOCK set to a path in a new directory, where nothing is listening."""

    def __enter__(self) -> str:
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = os.environ.get("SUP_SOCK")
        os.environ["SUP_SOCK"] = os.path.join(self.tmp.name, "sup.sock")
        return os.environ["SUP_SOCK"]

    def __exit__(self, *exc) -> None:
        if self.saved is None:
            os.environ.pop("SUP_SOCK", None)
        else:
            os.environ["SUP_SOCK"] = self.saved
        self.tmp.cleanup()


def test_every_action_uses_SUP_SOCK():
    with with_sup_sock() as sock:
        assert core.SupervisorConfig.from_env().sock == sock
        assert lifecycle.probe().socket_path == sock
        assert lifecycle.stop().socket_path == sock
        assert SupervisorEvaluator().socket_path == sock
        try:
            connect()
        except SupervisorUnavailable as exc:
            assert sock in str(exc), exc
        else:
            raise AssertionError("connect() found a supervisor that is not there")
        assert payload(server.supervisor(action="status"))["socket"] == sock
        assert payload(server.supervisor(action="stop"))["socket"] == sock
        for action, extra in (("use", {}), ("lookup", {"key": "k1"}),
                              ("running", {}), ("abort", {})):
            reply = payload(server.supervisor(action=action, **extra))
            assert reply["success"] is False and sock in reply["error"], (action, reply)


def test_an_explicit_socket_path_wins():
    with with_sup_sock() as sock:
        other = os.path.join(os.path.dirname(sock), "other.sock")
        assert lifecycle.probe(other).socket_path == other
        assert payload(server.supervisor(action="status", socket_path=other))["socket"] == other


def test_without_SUP_SOCK_the_per_user_default():
    saved = os.environ.pop("SUP_SOCK", None)
    try:
        assert core.configured_socket_path() == core.default_socket_path()
        assert lifecycle.probe().socket_path == core.default_socket_path()
    finally:
        if saved is not None:
            os.environ["SUP_SOCK"] = saved


class FakeBackend:
    """A selected backend that answers without a kernel and remembers what it got."""

    name = "fake"

    def __init__(self):
        self.codes: list[str] = []

    def evaluate_text(self, code: str, timeout: float = 60):
        self.codes.append(code)
        return WLResult(success=True, text="2")

    def evaluate_json(self, code: str, timeout: float = 60):
        self.codes.append(code)
        return {"success": True, "symbols": [], "count": 0}


def test_evaluate_and_vars_reach_the_selected_backend():
    fake = FakeBackend()
    previous = set_evaluator(fake)
    try:
        reply = payload(server.evaluate("1 + 1"))
        assert reply["success"] and reply["output"].strip() == "2", reply
        assert fake.codes == ["1 + 1"], fake.codes
        server.vars(action="list")
        assert len(fake.codes) == 2, fake.codes
        assert get_evaluator() is fake
    finally:
        set_evaluator(previous)


if __name__ == "__main__":
    import traceback

    tests = [obj for name, obj in list(globals().items())
             if name.startswith("test_") and callable(obj)]
    passed = failed = 0
    print("=== Supervisor socket and backend (no kernel) ===")
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
