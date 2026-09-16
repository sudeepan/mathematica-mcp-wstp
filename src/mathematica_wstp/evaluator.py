"""How this server asks a kernel to evaluate something, behind one interface.

Everything in the notebook layer reaches a kernel through a single call. That is
a good seam, and this module makes it an explicit one so the backend can change
without the notebook code changing with it -- the case that motivated it is a
supervisor process owning the kernel instead of this process owning it.

Two properties are deliberate.

**Control is scoped to a named execution.** ``submit_bytes`` returns a handle,
and aborting goes through the handle. There is no ``abort()`` on the evaluator
itself, because "abort whatever is running" is a race with anything else in
flight, and the point of moving to a supervisor is to stop expressing control
that way. The direct backend has only one evaluation at a time and could get
away with the sloppier form; the interface refuses to let it.

**Marshalling belongs to the backend.** A caller supplies an expression that
evaluates to a ``ByteArray`` and gets those exact bytes back. How they cross the
link is the backend's business: this one wraps the expression in ``Normal[...]``
because the link reads an integer-8 list and a bare ByteArray reply fails with
"WSGet out of sequence". That requirement used to live as an unexplained
``Normal[`` in the notebook layer's own code.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class BytesResult:
    """One evaluation's bytes, and enough identity to say where they came from."""

    data: bytes | None
    outcome: str                      # COMPLETED | ABORTED | FAILED | TIMED_OUT
    request_id: str
    backend: str
    token: str | None = None
    #: True only when the identity was issued by something outside this process
    #: and can be corroborated there. The direct backend's ids are local
    #: bookkeeping and say so.
    authenticated: bool = False
    detail: str = ""

    @property
    def success(self) -> bool:
        return self.outcome == "COMPLETED"

    @property
    def timed_out(self) -> bool:
        return self.outcome == "TIMED_OUT"


class Execution(Protocol):
    """A handle on one submitted evaluation."""

    request_id: str
    token: str | None
    authenticated: bool

    def wait(self, timeout: float = 3600) -> BytesResult: ...
    def abort(self) -> str: ...
    def status(self) -> str: ...


class Evaluator(Protocol):
    name: str

    def submit_bytes(self, code: str, timeout: float = 60,
                     idempotency_key: str | None = None,
                     correlation: dict[str, str] | None = None) -> Execution: ...


class _DirectExecution:
    """The one evaluation this process is running, behind a handle."""

    def __init__(self, request_id: str, code: str, timeout: float):
        self.request_id = request_id
        self.token = f"local/{request_id}"
        self.authenticated = False
        self._code, self._timeout = code, timeout
        self._result: BytesResult | None = None
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        from . import session
        try:
            reply = session.evaluate_wl_bytes(self._code, timeout=self._timeout)
            if reply.success:
                self._result = self._ok(reply.data)
            else:
                self._result = self._bad(
                    "TIMED_OUT" if reply.timed_out else
                    ("ABORTED" if reply.aborted else "FAILED"),
                    reply.error or "kernel evaluation failed")
        except Exception as exc:                      # noqa: BLE001 - reported, not swallowed
            self._result = self._bad("FAILED", f"{type(exc).__name__}: {exc}")
        finally:
            self._done.set()

    def _ok(self, data: bytes) -> BytesResult:
        return BytesResult(data=data, outcome="COMPLETED", request_id=self.request_id,
                           backend="direct", token=self.token)

    def _bad(self, outcome: str, detail: str) -> BytesResult:
        return BytesResult(data=None, outcome=outcome, request_id=self.request_id,
                           backend="direct", token=self.token, detail=detail)

    def wait(self, timeout: float = 3600) -> BytesResult:
        if not self._done.wait(timeout):
            return self._bad("TIMED_OUT", f"no reply within {timeout}s")
        assert self._result is not None
        return self._result

    def abort(self) -> str:
        """Interrupt this execution.

        There is one evaluation in this process, so the handle maps onto it --
        but only while the kernel is actually running it. A handle that has
        finished refuses rather than aborting whatever came next, and one whose
        expression has not reached the kernel yet refuses too: an abort that
        arrives at an idle kernel leaves an interrupt pending and wedges it.
        """
        state = self.status()
        if state == "TERMINAL":
            return f"REFUSED {self.request_id} is already finished"
        if state == "DISPATCHING":
            return f"REFUSED {self.request_id} has not reached the kernel yet"
        from . import session
        outcome = session.abort_current(wait=10)
        return f"ABORT_ISSUED {self.token} {outcome}"

    def status(self) -> str:
        """DISPATCHING, RUNNING or TERMINAL.

        RUNNING means the kernel has the expression, not merely that a thread
        exists to send it. The distinction is the difference between an abort
        that lands and one that wedges the kernel, so the handle reports the
        transport's answer rather than its own.
        """
        if self._done.is_set():
            return "TERMINAL"
        from . import session
        if not session.has_kernel():
            return "DISPATCHING"
        try:
            return "RUNNING" if session.get_kernel(start=False).evaluation_in_flight() else "DISPATCHING"
        except Exception:                             # noqa: BLE001 - never mask the state
            return "DISPATCHING"


class DirectSessionEvaluator:
    """Evaluate in this process's own kernel, through ``session``."""

    name = "direct"

    def __init__(self) -> None:
        self._ids = itertools.count(1)

    def submit_bytes(self, code: str, timeout: float = 60,
                     idempotency_key: str | None = None,
                     correlation: dict[str, str] | None = None) -> _DirectExecution:
        # Correlation and idempotency are accepted and ignored here on purpose:
        # this backend has no durable identity to deduplicate against, and
        # pretending otherwise would let a caller believe a repeat submission
        # was recognised when it was simply run again.
        return _DirectExecution(f"D{next(self._ids)}", f"Normal[{code}]", timeout)


_evaluator: Evaluator | None = None
_lock = threading.Lock()


def get_evaluator() -> Evaluator:
    global _evaluator
    with _lock:
        if _evaluator is None:
            _evaluator = DirectSessionEvaluator()
        return _evaluator


def set_evaluator(evaluator: Evaluator | None) -> Evaluator | None:
    """Swap the backend. Returns the previous one, so a caller can put it back."""
    global _evaluator
    with _lock:
        previous, _evaluator = _evaluator, evaluator
        return previous
